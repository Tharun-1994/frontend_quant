import io
import os
from http.client import HTTPException
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from numpy.testing.print_coercion_tables import print_new_cast_table
from sqlalchemy import exists, func

from sqlalchemy.orm import Session
import json
from plotly.utils import PlotlyJSONEncoder
from starlette.responses import FileResponse, StreamingResponse

from app.constants.PricePath import PricePath
from app.constants.static_config import UNIVERSES, FUNCTION_MAPPER, UNIVERSES_Codes
from app.database import get_db
from app.loader.GeneratePricesIndicators import GeneratePricesIndicators
from app.loader.PriceDataLoader import PriceDataLoader
from app.loader.Rule_Tree_JSON import dumps_tree, loads_tree, normalize_rules_tree, normalize_rule
from app.loader.TechnicalIndicators import INDICATOR_REGISTRY, IndicatorCalculator
from app.models import MarketRegime
from app.models.strategy_bucket import StrategyBucket
from app.schemas import StrategyRequest
import re
from typing import List, Dict, Any, Optional
import pandas as pd
import httpx

from app.schemas.strategy import MarketRegimeBase
from app.schemas.PerformanceMetrics import PerformanceMetrics
from app.services.BacktestService import backtest_service

router = APIRouter()

def build_expression(rules: List[Dict[str, Any]]) -> str:
    if not rules:
        return ""

    exprs = []
    for rule in rules:
        left = f"{rule['indicator'].lower()}_{rule['lookback']}"

        # Decide right-hand side
        if rule.get("value_type") == "indicator_price":
            right = f'{rule.get("value_indicator", "").lower()}_{rule.get("value_lookback", "")}'
        else:  # default numeric
            right = str(rule.get("value", 0.0))

        exprs.append(f"{left} {rule['operator']} {right}")

    # Interleave with connectors
    parts = [exprs[0]]
    for i, rule in enumerate(rules[:-1]):
        conn = rule.get("connector") or "&&"
        parts.append(f"{conn} {exprs[i + 1]}")

    return " ".join(parts)


def extract_labels(rules: List[Dict[str, Any]]) -> str:
    """
    Extract labels from rules. If rule has no label, use indicator as fallback.
    Returns JSON string for storage.
    """
    labels = []
    for r in rules:
        label = f"{r.get('indicator')}_{r.get('lookback')}_{r.get('label')}"
        labels.append(label)
    return json.dumps(labels)

def parse_expression(expr: str) -> List[Dict[str, Any]]:
    """
    Parse expression string back into list of rules.
    Supports numeric RHS and indicator RHS.
    Example: "sma_200 > 0.0 && sma_200 > close"
    """
    if not expr:
        return []

    # Split on connectors (&&, ||)
    tokens = re.split(r"\s+(&&|\|\|)\s+", expr)

    rules: List[Dict[str, Any]] = []
    connector = ""
    for token in tokens:
        if token in ("&&", "||"):
            connector = token
            continue

        # Match pattern: indicator_lookback operator value
        match = re.match(r"(\w+)_(\d+)\s*([<>!=]+)\s*(\w+|\d+\.?\d*)", token)
        if not match:
            continue

        indicator, lookback, operator, rhs = match.groups()

        # Decide type of RHS
        try:
            value = float(rhs)
            value_type = "value"
            value_indicator = ""
            value_lookback = 0
        except ValueError:
            value = 0
            value_type = "indicator_price"
            if '_' in rhs:

                value_side = rhs.split('_')
                value_indicator = value_side[0]
                value_lookback = value_side[1]
            else:
                value_indicator = rhs
                value_lookback = 0
        rules.append({
            "indicator": indicator,
            "lookback": int(lookback),
            "operator": operator,
            "value": value,
            "connector": connector,
            "value_type": value_type,
            "value_indicator": value_indicator,
            "value_lookback": value_lookback,
        })

        connector = ""  # reset
    return rules

def sort_rules_by_connector(rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Sort rules so that ones having connectors ('AND' / 'OR' / '&&' / '||') come first.
    """
    if not rules:
        return []
    return sorted(
        rules,
        key=lambda r: 0 if r.get("connector") in ("AND", "OR", "&&", "||") else 1
    )

def parse_labels(labels_str: Optional[str], rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Merge parsed expression rules with stored labels.
    labels_str is a JSON array of strings like ["rsi_14_My RSI", "hv_100_HV Rule"]
    """
    if not rules:
        return []

    labels = []
    if labels_str:
        try:
            labels = json.loads(labels_str)
        except Exception:
            labels = []

    # Map from indicator_lookback → label
    label_map = {}
    for lbl in labels:
        parts = lbl.split("_", 2)  # indicator, lookback, label
        if len(parts) == 3:
            key = f"{parts[0]}_{parts[1]}"
            label_map[key] = parts[2]

    # Attach labels back into rules
    for r in rules:
        key = f"{r['indicator']}_{r['lookback']}"
        r["label"] = label_map.get(key, r["indicator"])

        if r['value_type'] == 'indicator_price':
            v_key = f"{r['value_indicator']}_{r['value_lookback']}"
            r["label"] = label_map.get(key, r["value_indicator"])

    return rules

def db_to_pydantic(db_obj: MarketRegime) -> MarketRegimeBase:
    m = MarketRegimeBase(
        id=db_obj.id,
        strategy_id=db_obj.strategy_id,
        regime_type=db_obj.regime_type,
        regime_ticker=db_obj.regime_ticker,
        market_trend_type=db_obj.market_trend_type,

        market_trend_rules=sort_rules_by_connector(parse_labels(db_obj.market_trend_rules_labels, parse_expression(db_obj.market_trend_rules))),
        volatility_rules=sort_rules_by_connector(parse_labels(db_obj.volatility_rules_labels, parse_expression(db_obj.volatility_rules))),
        # entry_rules=sort_rules_by_connector(parse_labels(db_obj.entry_rules_labels, parse_expression(db_obj.entry_rules))),
        # exit_rules=sort_rules_by_connector(parse_labels(db_obj.exit_rules_labels, parse_expression(db_obj.exit_rules))),

        entry_timing=db_obj.entry_timing,
        exit_timing=db_obj.exit_timing,
        stoploss_type=db_obj.stoploss_type,
        takeprofit_type=db_obj.takeprofit_type,
        takeprofit_dollar=db_obj.takeprofit_dollar,
        stoploss_dollar=db_obj.stoploss_dollar,
        stoploss_pct=db_obj.stoploss_pct,

        takeprofit_pct=db_obj.takeprofit_pct,
        stoploss_timing=db_obj.stoploss_timing,
        takeprofit_timing=db_obj.takeprofit_timing,
        atr_lookback_stp=db_obj.atr_lookback_stp,
        atr_lookback_tp=db_obj.atr_lookback_tp,
        ranking=db_obj.ranking,
        ranking_lookback=db_obj.ranking_lookback,
        ranking_order=db_obj.ranking_order,
        order_type=db_obj.order_type,
        limit_pct=db_obj.limit_pct,
        atr_limit_lookback=db_obj.atr_limit_lookback,
        universe=db_obj.universe,
        capital=db_obj.capital,
        slots=db_obj.slots,
        rebalance=db_obj.rebalance,
        created_at=db_obj.created_at,
        max_time=db_obj.max_time,
        banned_months=json.loads(db_obj.banned_months or "[]"),
        market_trend_rules_labels=db_obj.market_trend_rules_labels,
        volatility_rules_labels=db_obj.volatility_rules_labels,
        entry_rules_labels=db_obj.entry_rules_labels,
        exit_rules_labels=db_obj.exit_rules_labels,
        entry_rules_tree=loads_tree(db_obj.entry_rules_tree_json),
        exit_rules_tree=loads_tree(db_obj.exit_rules_tree_json),
        market_trend_rules_tree=loads_tree(db_obj.market_trend_rules_tree_json),
        volatility_rules_tree=loads_tree(db_obj.volatility_rules_tree_json),
        freeze_rules_tree=loads_tree(db_obj.freeze_rules_tree_json),
        resume_rules_tree=loads_tree(db_obj.resume_rules_tree_json),
        is_look_inside_bar=db_obj.is_look_inside_bar,
        sector_level=db_obj.sector_level,
        sector_limit=db_obj.sector_limit,
    )
    return m

@router.post("/api/save-strategy")
def save_strategy(strategy_data: StrategyRequest, db: Session = Depends(get_db)):
    strategy = None
    if hasattr(strategy_data, "id") and strategy_data.id:
        strategy = db.query(StrategyBucket).filter(StrategyBucket.id == strategy_data.id).first()

    if strategy:
        strategy.name = strategy_data.name
        strategy.start_date = strategy_data.start_date
        strategy.end_date = strategy_data.end_date
        strategy.rebalance = strategy_data.rebalance
        strategy.min_price = strategy_data.min_price
        strategy.min_quantity = strategy_data.min_quantity
        strategy.system_type = strategy_data.system_type
        strategy.market_regime_type = strategy_data.market_regime_type
        action = "updated"




    else:
        strategy = StrategyBucket(
            name=strategy_data.name,
            start_date=strategy_data.start_date,
            end_date=strategy_data.end_date,
            rebalance=strategy_data.rebalance,
            min_price=strategy_data.min_price,
            min_quantity=strategy_data.min_quantity,
            system_type=strategy_data.system_type,
            market_regime_type=strategy_data.market_regime_type,
        )

        db.add(strategy)
        action = "created"



    db.commit()
    db.refresh(strategy)

    PriceDataLoader.create_strategy_Folder(name = strategy_data.name)
    return {
        "strategy_id": strategy.id,
        "status": f"successfully {action}"
    }



@router.get("/api/check-username")
def check_username_taken(name: str, db: Session = Depends(get_db)):
    # This generates: SELECT count(*) FROM strategies_bucket WHERE name = '...'
    count = db.query(func.count(StrategyBucket.name)).filter(
        StrategyBucket.name == name
    ).scalar()

    return {"name": name, "taken": count > 0}





@router.get("/api/marketregime/{strategy_id}", response_model=List[MarketRegimeBase])
def get_market_regimes(strategy_id: int, db: Session = Depends(get_db)):

    db_objs = db.query(MarketRegime).filter(MarketRegime.strategy_id == strategy_id).all()

    return [db_to_pydantic(obj) for obj in db_objs]



@router.post("/api/save-marketregime")
def save_marketRegime(marketregime: MarketRegimeBase, db: Session = Depends(get_db)):
    if marketregime.id:
        # ✅ Update existing
        db_obj = db.query(MarketRegime).filter(MarketRegime.id == marketregime.id).first()
        if not db_obj:
            raise HTTPException(status_code=404, detail="MarketRegime not found")

        db_obj.regime_type = marketregime.regime_type
        db_obj.regime_ticker = marketregime.regime_ticker
        db_obj.market_trend_type = marketregime.market_trend_type

        db_obj.market_trend_rules = build_expression([r.dict() for r in (marketregime.market_trend_rules or [])])
        db_obj.market_trend_rules_labels = db_obj.market_trend_rules

        db_obj.volatility_rules = build_expression([r.dict() for r in (marketregime.volatility_rules or [])])
        db_obj.volatility_rules_labels = db_obj.volatility_rules

        db_obj.entry_rules = build_expression([r.dict() for r in (marketregime.entry_rules or [])])
        db_obj.entry_rules_labels = db_obj.entry_rules

        db_obj.exit_rules = build_expression([r.dict() for r in (marketregime.exit_rules or [])])
        db_obj.exit_rules_labels = db_obj.exit_rules

        db_obj.entry_timing = marketregime.entry_timing
        db_obj.exit_timing = marketregime.exit_timing

        db_obj.stoploss_type = marketregime.stoploss_type
        db_obj.takeprofit_type = marketregime.takeprofit_type
        db_obj.stoploss_pct = marketregime.stoploss_pct
        db_obj.takeprofit_pct = marketregime.takeprofit_pct
        db_obj.stoploss_timing = marketregime.stoploss_timing
        db_obj.takeprofit_timing = marketregime.takeprofit_timing
        db_obj.atr_lookback_stp = marketregime.atr_lookback_stp
        db_obj.atr_lookback_tp = marketregime.atr_lookback_tp
        db_obj.is_look_inside_bar = marketregime.is_look_inside_bar
        db_obj.ranking = marketregime.ranking
        db_obj.ranking_lookback = marketregime.ranking_lookback
        db_obj.ranking_order = marketregime.ranking_order

        db_obj.order_type = marketregime.order_type
        db_obj.limit_pct = marketregime.limit_pct
        db_obj.atr_limit_lookback = marketregime.atr_limit_lookback

        db_obj.universe = marketregime.universe
        db_obj.capital = marketregime.capital
        db_obj.slots = marketregime.slots
        db_obj.max_time = marketregime.max_time

        db_obj.banned_months = json.dumps(marketregime.banned_months or [])

        db.commit()
        db.refresh(db_obj)

        strategy = db.query(StrategyBucket).filter(StrategyBucket.id == marketregime.strategy_id).first()

        GeneratePricesIndicators.generate(marketRegime=marketregime,strategy=strategy)
        return db_obj

    else:
        # ✅ Insert new
        db_obj = MarketRegime(
            strategy_id=marketregime.strategy_id,
            regime_type=marketregime.regime_type,
            regime_ticker=marketregime.regime_ticker,
            market_trend_type=marketregime.market_trend_type,

            market_trend_rules=build_expression([r.dict() for r in (marketregime.market_trend_rules or [])]),
            market_trend_rules_labels=extract_labels([r.dict() for r in (marketregime.market_trend_rules or [])]),

            volatility_rules=build_expression([r.dict() for r in (marketregime.volatility_rules or [])]),
            volatility_rules_labels=extract_labels([r.dict() for r in (marketregime.volatility_rules or [])]),

            entry_rules=build_expression([r.dict() for r in (marketregime.entry_rules or [])]),
            entry_rules_labels=extract_labels([r.dict() for r in (marketregime.entry_rules or [])]),

            exit_rules=build_expression([r.dict() for r in (marketregime.exit_rules or [])]),
            exit_rules_labels=extract_labels([r.dict() for r in (marketregime.exit_rules or [])]),

            entry_timing=marketregime.entry_timing,
            exit_timing=marketregime.exit_timing,

            stoploss_type=marketregime.stoploss_type,
            takeprofit_type=marketregime.takeprofit_type,
            stoploss_pct=marketregime.stoploss_pct,
            takeprofit_pct=marketregime.takeprofit_pct,
            stoploss_timing=marketregime.stoploss_timing,
            takeprofit_timing=marketregime.takeprofit_timing,
            atr_lookback_stp=marketregime.atr_lookback_stp,
            atr_lookback_tp=marketregime.atr_lookback_tp,
            is_look_inside_bar=marketregime.is_look_inside_bar,
            ranking=marketregime.ranking,
            ranking_lookback=marketregime.ranking_lookback,
            ranking_order=marketregime.ranking_order,

            order_type=marketregime.order_type,
            limit_pct=marketregime.limit_pct,
            atr_limit_lookback=marketregime.atr_limit_lookback,

            universe=marketregime.universe,
            capital=marketregime.capital,
            slots=marketregime.slots,
            max_time = marketregime.max_time,
            banned_months=json.dumps(marketregime.banned_months or [])
        )
        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)

        strategy = db.query(StrategyBucket).filter(StrategyBucket.id == marketregime.strategy_id).first()

        GeneratePricesIndicators.generate(marketRegime=marketregime, strategy=strategy)
        return db_obj


@router.post("/api/save-marketregime-v2")
def save_marketRegime_v2(marketregime: MarketRegimeBase, db: Session = Depends(get_db)):
    # --- helper to compute legacy expression strings ---
    def expr_and_labels(rules):
        rules_dicts = [normalize_rule(r.dict()) for r in (rules or [])]
        expr = build_expression(rules_dicts)
        labels = extract_labels(rules_dicts)
        return expr, labels

    if marketregime.id:
        db_obj = db.query(MarketRegime).filter(MarketRegime.id == marketregime.id).first()
        if not db_obj:
            raise HTTPException(status_code=404, detail="MarketRegime not found")
    else:
        db_obj = MarketRegime(strategy_id=marketregime.strategy_id)
        db.add(db_obj)

    # --- normal fields (same as before) ---
    db_obj.regime_type = marketregime.regime_type
    db_obj.regime_ticker = marketregime.regime_ticker
    db_obj.market_trend_type = marketregime.market_trend_type

    db_obj.market_trend_rules = None
    db_obj.market_trend_rules_labels = None

    db_obj.entry_timing = marketregime.entry_timing
    db_obj.exit_timing = marketregime.exit_timing

    db_obj.stoploss_type = marketregime.stoploss_type
    db_obj.takeprofit_type = marketregime.takeprofit_type

    db_obj.takeprofit_dollar = marketregime.takeprofit_dollar
    db_obj.stoploss_dollar = marketregime.stoploss_dollar

    db_obj.stoploss_pct = marketregime.stoploss_pct
    db_obj.takeprofit_pct = marketregime.takeprofit_pct
    db_obj.stoploss_timing = marketregime.stoploss_timing
    db_obj.takeprofit_timing = marketregime.takeprofit_timing
    db_obj.atr_lookback_stp = marketregime.atr_lookback_stp
    db_obj.atr_lookback_tp = marketregime.atr_lookback_tp

    db_obj.ranking = marketregime.ranking
    db_obj.ranking_lookback = marketregime.ranking_lookback
    db_obj.ranking_order = marketregime.ranking_order

    db_obj.order_type = marketregime.order_type
    db_obj.limit_pct = marketregime.limit_pct
    db_obj.atr_limit_lookback = marketregime.atr_limit_lookback

    db_obj.universe = marketregime.universe
    db_obj.capital = marketregime.capital
    db_obj.slots = marketregime.slots
    db_obj.max_time = marketregime.max_time

    db_obj.banned_months = json.dumps(marketregime.banned_months or [])

    marketregime.market_trend_rules_tree = normalize_rules_tree(marketregime.market_trend_rules_tree)
    marketregime.entry_rules_tree = normalize_rules_tree(marketregime.entry_rules_tree)
    marketregime.exit_rules_tree = normalize_rules_tree(marketregime.exit_rules_tree)
    marketregime.freeze_rules_tree = normalize_rules_tree(marketregime.freeze_rules_tree)
    marketregime.resume_rules_tree = normalize_rules_tree(marketregime.resume_rules_tree)

    db_obj.market_trend_rules_tree_json = dumps_tree(marketregime.market_trend_rules_tree)
    db_obj.entry_rules_tree_json        = dumps_tree(marketregime.entry_rules_tree)
    db_obj.exit_rules_tree_json         = dumps_tree(marketregime.exit_rules_tree)
    db_obj.freeze_rules_tree_json = dumps_tree(marketregime.freeze_rules_tree)
    db_obj.resume_rules_tree_json = dumps_tree(marketregime.resume_rules_tree)
    db_obj.is_look_inside_bar = marketregime.is_look_inside_bar
    db_obj.sector_level = marketregime.sector_level
    db_obj.sector_limit = marketregime.sector_limit

    # ── ALWAYS commit + refresh first so we have the id ──
    db.commit()
    db.refresh(db_obj)

    # ── Generate indicator files (non-fatal — id is already saved) ──
    generate_warning = None
    try:
        strategy = db.query(StrategyBucket).filter(StrategyBucket.id == marketregime.strategy_id).first()
        GeneratePricesIndicators.generate(marketRegime=marketregime, strategy=strategy)
    except Exception as e:
        generate_warning = str(e)
        print(f"[WARNING] Indicator generation failed for regime {db_obj.id}: {e}")

    # Always return the saved object (with id) — frontend needs this to avoid duplicates
    result = {
        "id": db_obj.id,
        "strategy_id": db_obj.strategy_id,
        "regime_type": db_obj.regime_type,
    }
    if generate_warning:
        result["warning"] = f"Saved but indicator generation failed: {generate_warning}"

    return result


# @router.post("/api/save-marketregime-v2")
# def save_marketRegime_v2(marketregime: MarketRegimeBase, db: Session = Depends(get_db)):
#     # --- helper to compute legacy expression strings ---
#     def expr_and_labels(rules):
#         rules_dicts = [normalize_rule(r.dict()) for r in (rules or [])]  # ✅ changed
#         expr = build_expression(rules_dicts)
#         labels = extract_labels(rules_dicts)
#         return expr, labels
#
#     # mt_expr, mt_labels = expr_and_labels(marketregime.market_trend_rules)
#     # vol_expr, vol_labels = expr_and_labels(marketregime.volatility_rules)
#     # freeze_expr, freeze_labels = expr_and_labels(marketregime.freeze_rules_tree)
#     # resume_expr, resume_labels = expr_and_labels(marketregime.resume_rules_tree)
#
#     # en_expr, en_labels = expr_and_labels(marketregime.entry_rules)
#     # ex_expr, ex_labels = expr_and_labels(marketregime.exit_rules)
#
#     if marketregime.id:
#         db_obj = db.query(MarketRegime).filter(MarketRegime.id == marketregime.id).first()
#         if not db_obj:
#             raise HTTPException(status_code=404, detail="MarketRegime not found")
#     else:
#         db_obj = MarketRegime(strategy_id=marketregime.strategy_id)
#         db.add(db_obj)
#
#     # --- normal fields (same as before) ---
#     db_obj.regime_type = marketregime.regime_type
#     db_obj.regime_ticker = marketregime.regime_ticker
#     db_obj.market_trend_type = marketregime.market_trend_type
#
#     db_obj.market_trend_rules = None
#     db_obj.market_trend_rules_labels = None
#
#     # db_obj.resume_rules_tree_json = vol_expr
#     # db_obj.volatility_rules_labels = vol_labels
#     #
#     # db_obj.entry_rules = en_expr
#     # db_obj.entry_rules_labels = en_labels
#     #
#     # db_obj.exit_rules = ex_expr
#     # db_obj.exit_rules_labels = ex_labels
#
#     db_obj.entry_timing = marketregime.entry_timing
#     db_obj.exit_timing = marketregime.exit_timing
#
#     db_obj.stoploss_type = marketregime.stoploss_type
#     db_obj.takeprofit_type = marketregime.takeprofit_type
#
#     db_obj.takeprofit_dollar = marketregime.takeprofit_dollar
#     db_obj.stoploss_dollar = marketregime.stoploss_dollar
#
#     db_obj.stoploss_pct = marketregime.stoploss_pct
#     db_obj.takeprofit_pct = marketregime.takeprofit_pct
#     db_obj.stoploss_timing = marketregime.stoploss_timing
#     db_obj.takeprofit_timing = marketregime.takeprofit_timing
#     db_obj.atr_lookback_stp = marketregime.atr_lookback_stp
#     db_obj.atr_lookback_tp = marketregime.atr_lookback_tp
#
#     db_obj.ranking = marketregime.ranking
#     db_obj.ranking_lookback = marketregime.ranking_lookback
#     db_obj.ranking_order = marketregime.ranking_order
#
#     db_obj.order_type = marketregime.order_type
#     db_obj.limit_pct = marketregime.limit_pct
#     db_obj.atr_limit_lookback = marketregime.atr_limit_lookback
#
#     db_obj.universe = marketregime.universe
#     db_obj.capital = marketregime.capital
#     db_obj.slots = marketregime.slots
#     db_obj.max_time = marketregime.max_time
#
#     db_obj.banned_months = json.dumps(marketregime.banned_months or [])
#
#     marketregime.market_trend_rules_tree = normalize_rules_tree(marketregime.market_trend_rules_tree)
#
#     marketregime.entry_rules_tree = normalize_rules_tree(marketregime.entry_rules_tree)
#     marketregime.exit_rules_tree = normalize_rules_tree(marketregime.exit_rules_tree)
#
#     marketregime.freeze_rules_tree = normalize_rules_tree(marketregime.freeze_rules_tree)
#     marketregime.resume_rules_tree = normalize_rules_tree(marketregime.resume_rules_tree)
#
#     # ✅ NEW: save tree JSON into new columns
#     db_obj.market_trend_rules_tree_json = dumps_tree(marketregime.market_trend_rules_tree)
#     db_obj.entry_rules_tree_json        = dumps_tree(marketregime.entry_rules_tree)
#     db_obj.exit_rules_tree_json         = dumps_tree(marketregime.exit_rules_tree)
#
#     db_obj.freeze_rules_tree_json = dumps_tree(marketregime.freeze_rules_tree)
#     db_obj.resume_rules_tree_json = dumps_tree(marketregime.resume_rules_tree)
#     db_obj.is_look_inside_bar = marketregime.is_look_inside_bar
#
#     db.commit()
#     db.refresh(db_obj)
#
#     # ── Generate indicator files (non-fatal — id is already saved) ──
#     generate_warning = None
#     try:
#         strategy = db.query(StrategyBucket).filter(StrategyBucket.id == marketregime.strategy_id).first()
#         GeneratePricesIndicators.generate(marketRegime=marketregime, strategy=strategy)
#     except Exception as e:
#         generate_warning = str(e)
#         print(f"[WARNING] Indicator generation failed for regime {db_obj.id}: {e}")
#
#     # Always return the saved object (with id) — frontend needs this to avoid duplicates
#     result = {
#         "id": db_obj.id,
#         "strategy_id": db_obj.strategy_id,
#         "regime_type": db_obj.regime_type,
#     }
#     if generate_warning:
#         result["warning"] = f"Saved but indicator generation failed: {generate_warning}"
#
#     return result
#

# @router.post("/api/save-strategys")
# def save_strategys(strategy_data: StrategyRequest, db: Session = Depends(get_db)):
#     # 🔹 Check if strategy exists (update case)
#     strategy = None
#     if hasattr(strategy_data, "id") and strategy_data.id:
#         strategy = db.query(Strategy).filter(Strategy.id == strategy_data.id).first()
#
#     if strategy:
#         # 🔹 Update existing strategy
#         strategy.name = strategy_data.strategy_name
#         strategy.rebalance = strategy_data.rebalance
#         strategy.universe = strategy_data.universe
#         strategy.slots = strategy_data.slots
#         strategy.capital = strategy_data.capital
#         strategy.start_date = strategy_data.start_date
#         strategy.end_date = strategy_data.end_date
#         strategy.stoploss_pct = strategy_data.stoploss_pct
#         strategy.takeprofit_pct = strategy_data.takeprofit_pct
#         strategy.entry_rules = build_expression([rule.dict() for rule in strategy_data.entry_rules])
#         strategy.exit_rules = build_expression([rule.dict() for rule in strategy_data.exit_rules])
#         strategy.ranking = strategy_data.ranking
#         strategy.stoploss_timing = strategy_data.stoploss_timing
#         strategy.takeprofit_timing = strategy_data.takeprofit_timing
#         strategy.entry_timing = strategy_data.entry_timing
#         strategy.exit_timing = strategy_data.exit_timing
#         strategy.ranking_lookback = strategy_data.ranking_lookback
#         strategy.ranking_order = strategy_data.ranking_order
#         strategy.min_quantity = strategy_data.min_quantity
#         strategy.min_price = strategy_data.min_price
#         strategy.system_type = strategy_data.system_type
#         strategy.stoploss_type = strategy_data.stoploss_type
#         strategy.takeprofit_type = strategy_data.takeprofit_type
#         strategy.order_type = strategy_data.order_type
#         strategy.limit_pct = strategy_data.limit_pct
#         strategy.atr_limit_lookback = strategy_data.atr_limit_lookback
#         strategy.atr_lookback_stp = strategy_data.atr_lookback_stp
#         strategy.atr_lookback_tp = strategy_data.atr_lookback_tp
#
#         action = "updated"
#
#     else:
#         # 🔹 Create new strategy
#         strategy = Strategy(
#             name=strategy_data.strategy_name,
#             rebalance=strategy_data.rebalance,
#             universe=strategy_data.universe,
#             slots=strategy_data.slots,
#             capital=strategy_data.capital,
#             start_date=strategy_data.start_date,
#             end_date=strategy_data.end_date,
#             stoploss_pct=strategy_data.stoploss_pct,
#             takeprofit_pct=strategy_data.takeprofit_pct,
#             entry_rules=build_expression([rule.dict() for rule in strategy_data.entry_rules]),
#             exit_rules=build_expression([rule.dict() for rule in strategy_data.exit_rules]),
#             ranking=strategy_data.ranking,
#             stoploss_timing=strategy_data.stoploss_timing,
#             takeprofit_timing=strategy_data.takeprofit_timing,
#             entry_timing=strategy_data.entry_timing,
#             exit_timing=strategy_data.exit_timing,
#             ranking_lookback=strategy_data.ranking_lookback,
#             ranking_order=strategy_data.ranking_order,
#             min_quantity=strategy_data.min_quantity,
#             min_price=strategy_data.min_price,
#             system_type=strategy_data.system_type,
#             stoploss_type=strategy_data.stoploss_type,
#             takeprofit_type=strategy_data.takeprofit_type,
#             order_type = strategy_data.order_type,
#             limit_pct = strategy_data.limit_pct,
#             atr_limit_lookback = strategy_data.atr_limit_lookback,
#             atr_lookback_stp = strategy_data.atr_lookback_stp,
#             atr_lookback_tp = strategy_data.atr_lookback_tp
#             )
#         db.add(strategy)
#         action = "created"
#
#
#
#     for univ in UNIVERSES.keys():
#         if strategy_data.universe == UNIVERSES[univ]:
#
#             if univ == 'sp500':
#                 loader = PriceDataLoader(PricePath.sp500base_path)
#             elif univ == 'liquid500':
#                 loader = PriceDataLoader(PricePath.liquid500base_path)
#             else:
#                 loader = PriceDataLoader(PricePath.russell3000base_path)
#
#             price_data = loader.load_all(rebalance=strategy_data.rebalance,universe=univ)
#             date_to_active_tickers = price_data[f'{univ}_universe'].apply(lambda row: row[row == 1].index.tolist(), axis=1)
#             df_out = date_to_active_tickers.to_frame(name="active_tickers")
#             price_data[f'{univ}_universe'] = df_out["active_tickers"].apply(lambda x: ",".join(x)).to_frame()
#
#             price_data.update(loader.load_spy_close(rebalance=strategy_data.rebalance))
#
#             indictor_Set = set()
#
#             # This is For LIMIT ATR PRODUCTION
#             if (strategy_data.order_type and strategy_data.order_type == 'LIMIT_ATR'
#                     and  strategy_data.atr_limit_lookback and strategy_data.atr_limit_lookback > 0) :
#                 result = call_indicator(FUNCTION_MAPPER["atr"],
#                                         Highs=price_data[f'{strategy_data.rebalance}_highs'],
#                                         Lows=price_data[f'{strategy_data.rebalance}_lows'],
#                                         Closes=price_data[f'{strategy_data.rebalance}_closes'], length=strategy_data.atr_limit_lookback)
#                 indictor_Set.add(f'{FUNCTION_MAPPER["atr"]}_{strategy_data.atr_limit_lookback}')
#                 price_data[f'{FUNCTION_MAPPER["atr"]}_{strategy_data.atr_limit_lookback}'] = result
#
#             # This is For LIMIT ATR PRODUCTION For Stoploss
#             if (strategy_data.stoploss_type and strategy_data.stoploss_type == 'ATR_BASED'
#                     and strategy_data.atr_lookback_stp and strategy_data.atr_lookback_stp > 0):
#                 result = call_indicator(FUNCTION_MAPPER["atr"],
#                                         Highs=price_data[f'{strategy_data.rebalance}_highs'],
#                                         Lows=price_data[f'{strategy_data.rebalance}_lows'],
#                                         Closes=price_data[f'{strategy_data.rebalance}_closes'],
#                                         length=strategy_data.atr_lookback_stp)
#                 indictor_Set.add(f'{FUNCTION_MAPPER["atr"]}_{strategy_data.atr_lookback_stp}')
#                 price_data[f'{FUNCTION_MAPPER["atr"]}_{strategy_data.atr_lookback_stp}'] = result
#
#             # This is For LIMIT ATR PRODUCTION For Take Profit
#             if (strategy_data.takeprofit_type and strategy_data.takeprofit_type == 'ATR_BASED'
#                     and strategy_data.atr_lookback_tp and strategy_data.atr_lookback_tp > 0):
#                 result = call_indicator(FUNCTION_MAPPER["atr"],
#                                         Highs=price_data[f'{strategy_data.rebalance}_highs'],
#                                         Lows=price_data[f'{strategy_data.rebalance}_lows'],
#                                         Closes=price_data[f'{strategy_data.rebalance}_closes'],
#                                         length=strategy_data.atr_lookback_tp)
#                 indictor_Set.add(f'{FUNCTION_MAPPER["atr"]}_{strategy_data.atr_lookback_tp}')
#                 price_data[f'{FUNCTION_MAPPER["atr"]}_{strategy_data.atr_lookback_tp}'] = result
#
#
#
#             # Entry Rule  Generation
#             for rule in strategy_data.entry_rules:
#
#                 if rule.indicator == 'rsi':
#                     result = call_indicator(FUNCTION_MAPPER[rule.indicator], prices=price_data[f'{strategy_data.rebalance}_closes'], n=rule.lookback)
#                     indictor_Set.add(f'{rule.indicator}_{rule.lookback}')
#                     price_data[f'{rule.indicator}_{rule.lookback}'] = result
#
#                 elif rule.indicator == 'adx':
#                     result = call_indicator(FUNCTION_MAPPER[rule.indicator], Highs=price_data[f'{strategy_data.rebalance}_highs'],
#                                             Lows=price_data[f'{strategy_data.rebalance}_lows'],Closes=price_data[f'{strategy_data.rebalance}_closes'], length=rule.lookback)
#                     indictor_Set.add(f'{rule.indicator}_{rule.lookback}')
#                     price_data[f'{rule.indicator}_{rule.lookback}'] = result
#
#                 elif rule.indicator == 'atr':
#                     result = call_indicator(FUNCTION_MAPPER[rule.indicator], Highs=price_data[f'{strategy_data.rebalance}_highs'],
#                                             Lows=price_data[f'{strategy_data.rebalance}_lows'],Closes=price_data[f'{strategy_data.rebalance}_closes'], length=rule.lookback)
#                     indictor_Set.add(f'{rule.indicator}_{rule.lookback}')
#                     price_data[f'{rule.indicator}_{rule.lookback}'] = result
#
#                 elif rule.indicator == 'hv':
#
#                     result = call_indicator(FUNCTION_MAPPER[rule.indicator],
#                                             prices=price_data[f'{strategy_data.rebalance}_closes'],
#                                             n=rule.lookback)
#
#                     price_data[f'{rule.indicator}_{rule.lookback}'] = result
#                     indictor_Set.add(f'{rule.indicator}_{rule.lookback}')
#
#
#                 elif rule.indicator == 'sma':
#
#                     result = call_indicator(FUNCTION_MAPPER[rule.indicator],
#                                             prices=price_data[f'{strategy_data.rebalance}_closes'],
#                                             lookback=rule.lookback)
#
#                     price_data[f'{rule.indicator}_{rule.lookback}'] = result
#                     indictor_Set.add(f'{rule.indicator}_{rule.lookback}')
#
#                 elif rule.indicator == 'crsi' and univ == univ == 'liquid500':
#
#                     crsi_liq = pd.read_csv(f'{PricePath.liquid500base_path}/Lq500CRSI.csv',
#                                             index_col=['Date'], parse_dates=True)
#
#                     price_data[f'{rule.indicator}_{rule.lookback}'] = crsi_liq
#                     indictor_Set.add(f'{rule.indicator}_{rule.lookback}')
#
#                 elif rule.indicator == 'relative_momentum':
#
#                     # Indicator on stock closes
#                     stock_indicator = call_indicator(
#                         FUNCTION_MAPPER[rule.indicator],
#                         df=price_data[f"{strategy_data.rebalance}_closes"],
#                         lookback=rule.lookback,
#                     )
#
#                     # Indicator on SPY closes (broadcasted to all stock columns)
#                     spy_indicator = call_indicator(
#                         FUNCTION_MAPPER[rule.indicator],
#                         df=price_data[f"{strategy_data.rebalance}_closes_spy"],
#                         lookback=rule.lookback,
#                     )
#
#                     # Divide stock indicator by SPY indicator (aligning index)
#                     relative_momentum = stock_indicator.div(spy_indicator, axis=0)
#                     price_data[f'{rule.indicator}_{rule.lookback}'] = relative_momentum
#                     indictor_Set.add(f'{rule.indicator}_{rule.lookback}')
#
#
#             # Exit Rule  Generation
#             for rule in strategy_data.exit_rules:
#                 if rule.indicator == 'rsi':
#                     if f'{rule.indicator}_{rule.lookback}' not in indictor_Set:
#                         result = call_indicator(FUNCTION_MAPPER[rule.indicator], prices=price_data[f'{strategy_data.rebalance}_closes'], n=rule.lookback)
#                         price_data[f'{rule.indicator}_{rule.lookback}'] = result
#
#                 elif rule.indicator == 'adx':
#                     if f'{rule.indicator}_{rule.lookback}' not in indictor_Set:
#                         result = call_indicator(FUNCTION_MAPPER[rule.indicator], Highs=price_data[f'{strategy_data.rebalance}_highs'],
#                                                 Lows=price_data[f'{strategy_data.rebalance}_lows'],Closes=price_data[f'{strategy_data.rebalance}_closes'], length=rule.lookback)
#                         indictor_Set.add(f'{rule.indicator}_{rule.lookback}')
#                         price_data[f'{rule.indicator}_{rule.lookback}'] = result
#
#
#                 elif rule.indicator == 'atr':
#                     if f'{rule.indicator}_{rule.lookback}' not in indictor_Set:
#                         result = call_indicator(FUNCTION_MAPPER[rule.indicator], Highs=price_data[f'{strategy_data.rebalance}_highs'],
#                                                 Lows=price_data[f'{strategy_data.rebalance}_lows'],Closes=price_data[f'{strategy_data.rebalance}_closes'], length=rule.lookback)
#                         indictor_Set.add(f'{rule.indicator}_{rule.lookback}')
#                         price_data[f'{rule.indicator}_{rule.lookback}'] = result
#
#
#                 elif rule.indicator == 'hv':
#                     if f'{rule.indicator}_{rule.lookback}' not in indictor_Set:
#                         result = call_indicator(FUNCTION_MAPPER[rule.indicator],
#                                                 prices=price_data[f'{strategy_data.rebalance}_closes'],
#                                                 n=rule.lookback)
#
#                         price_data[f'{rule.indicator}_{rule.lookback}'] = result
#                         indictor_Set.add(f'{rule.indicator}_{rule.lookback}')
#
#                 elif rule.indicator == 'sma':
#                     if f'{rule.indicator}_{rule.lookback}' not in indictor_Set:
#                         result = call_indicator(FUNCTION_MAPPER[rule.indicator],
#                                                 prices=price_data[f'{strategy_data.rebalance}_closes'],
#                                                 lookback=rule.lookback)
#
#                         price_data[f'{rule.indicator}_{rule.lookback}'] = result
#                         indictor_Set.add(f'{rule.indicator}_{rule.lookback}')
#
#                 elif rule.indicator == 'crsi' and univ == 'liquid500':
#                     if f'{rule.indicator}_{rule.lookback}' not in indictor_Set:
#                         crsi_liq = pd.read_csv(f'{PricePath.liquid500base_path}/Lq500CRSI.csv',
#                                                index_col=['Date'], parse_dates=True)
#
#                         price_data[f'{rule.indicator}_{rule.lookback}'] = crsi_liq
#                         indictor_Set.add(f'{rule.indicator}_{rule.lookback}')
#
#                 elif rule.indicator == 'relative_momentum':
#                     if f'{rule.indicator}_{rule.lookback}' not in indictor_Set:
#                         # Indicator on stock closes
#                         stock_indicator = call_indicator(
#                             FUNCTION_MAPPER[rule.indicator],
#                             df=price_data[f"{strategy_data.rebalance}_closes"],
#                             lookback=rule.lookback,
#                         )
#
#                         # Indicator on SPY closes (broadcasted to all stock columns)
#                         spy_indicator = call_indicator(
#                             FUNCTION_MAPPER[rule.indicator],
#                             df=price_data[f"{strategy_data.rebalance}_closes_spy"],
#                             lookback=rule.lookback,
#                         )
#
#                         # Divide stock indicator by SPY indicator (aligning index)
#                         relative_momentum = stock_indicator.div(spy_indicator, axis=0)
#                         price_data[f'{rule.indicator}_{rule.lookback}'] = relative_momentum
#                         indictor_Set.add(f'{rule.indicator}_{rule.lookback}')
#
#
#             # Ranking Indicator Generation
#             if (strategy_data.ranking and strategy_data.ranking_lookback > 0):
#
#                 if strategy_data.ranking == 'hv':
#                     if f'{strategy_data.ranking}_{strategy_data.ranking_lookback}' not in indictor_Set:
#                         result = call_indicator(FUNCTION_MAPPER[strategy_data.ranking],
#                                                 prices=price_data[f'{strategy_data.rebalance}_closes'], n=strategy_data.ranking_lookback)
#
#                         price_data[f'{strategy_data.ranking}_{strategy_data.ranking_lookback}'] = result
#
#                         indictor_Set.add(f'{strategy_data.ranking}_{strategy_data.ranking_lookback}')
#                         price_data[f'{strategy_data.ranking}_{strategy_data.ranking_lookback}'] = result
#
#                 elif strategy_data.ranking == 'atr':
#                     if f'{strategy_data.ranking}_{strategy_data.ranking_lookback}' not in indictor_Set:
#                         result = call_indicator(FUNCTION_MAPPER[strategy_data.ranking],
#                                                 Highs=price_data[f'{strategy_data.rebalance}_highs'],
#                                                 Lows=price_data[f'{strategy_data.rebalance}_lows'],
#                                                 Closes=price_data[f'{strategy_data.rebalance}_closes'],
#                                                 length=strategy_data.ranking_lookback)
#
#                         indictor_Set.add(f'{strategy_data.ranking}_{strategy_data.ranking_lookback}')
#                         price_data[f'{strategy_data.ranking}_{strategy_data.ranking_lookback}'] = result
#
#                 elif strategy_data.ranking == 'adx':
#                     if f'{strategy_data.ranking}_{strategy_data.ranking_lookback}' not in indictor_Set:
#                         result = call_indicator(FUNCTION_MAPPER[strategy_data.ranking],
#                                                 Highs=price_data[f'{strategy_data.rebalance}_highs'],
#                                                 Lows=price_data[f'{strategy_data.rebalance}_lows'],
#                                                 Closes=price_data[f'{strategy_data.rebalance}_closes'],
#                                                 length=strategy_data.ranking_lookback)
#
#                         indictor_Set.add(f'{strategy_data.ranking}_{strategy_data.ranking_lookback}')
#                         price_data[f'{strategy_data.ranking}_{strategy_data.ranking_lookback}'] = result
#
#                 elif strategy_data.ranking == 'sma':
#                     if f'{strategy_data.ranking}_{strategy_data.ranking_lookback}' not in indictor_Set:
#                         result = call_indicator(FUNCTION_MAPPER[strategy_data.ranking],
#                                                 prices=price_data[f'{strategy_data.rebalance}_closes'],
#                                                 lookback=strategy_data.ranking_lookback)
#
#                         indictor_Set.add(f'{strategy_data.ranking}_{strategy_data.ranking_lookback}')
#                         price_data[f'{strategy_data.ranking}_{strategy_data.ranking_lookback}'] = result
#
#                 elif strategy_data.ranking == 'rsi':
#
#                     if f'{strategy_data.ranking}_{strategy_data.ranking_lookback}' not in indictor_Set:
#
#                         result = call_indicator(FUNCTION_MAPPER[strategy_data.ranking],
#                                                 prices=price_data[f'{strategy_data.rebalance}_closes'], n=strategy_data.ranking_lookback)
#                         indictor_Set.add(f'{strategy_data.ranking}_{strategy_data.ranking_lookback}')
#                         price_data[f'{strategy_data.ranking}_{strategy_data.ranking_lookback}'] = result
#
#                 elif strategy_data.ranking == 'crsi' and univ == 'liquid500':
#
#                     if f'{strategy_data.ranking}_{strategy_data.ranking_lookback}' not in indictor_Set:
#                         crsi_liq = pd.read_csv(f'{PricePath.liquid500base_path}/Lq500CRSI.csv',
#                                                index_col=['Date'], parse_dates=True)
#
#                         price_data[f'{strategy_data.ranking}_{strategy_data.ranking_lookback}'] = crsi_liq
#                         indictor_Set.add(f'{strategy_data.ranking}_{strategy_data.ranking_lookback}')
#
#                 elif strategy_data.ranking == 'relative_momentum':
#
#                     rm1 = IndicatorCalculator.ROC(price_data[f"{strategy_data.rebalance}_closes"],
#                                                   strategy_data.ranking_lookback)
#                     rm2 = IndicatorCalculator.ROC(price_data[f"{strategy_data.rebalance}_closes_spy"],
#                                                   strategy_data.ranking_lookback)
#
#                     # take SPY ROC as Series
#                     rm2_series = rm2.iloc[:, 0]
#
#                     # row-wise divide, no column mismatch
#                     relative_momentum = rm1.div(rm2_series, axis=0)
#                     price_data[f'{strategy_data.ranking}_{strategy_data.ranking_lookback}'] = relative_momentum
#                     indictor_Set.add(f'{strategy_data.ranking}_{strategy_data.ranking_lookback}')
#
#             # All Dates Generation
#             all_dates = price_data[f'{strategy_data.rebalance}_closes'].index
#             all_dates_df = pd.DataFrame(data=all_dates, columns=['Date'])
#             price_data[f'all_dates'] = all_dates_df
#
#
#             # Max look back now it takes the default.
#             trading_dates = loader.get_trading_dates( start_trading=strategy_data.start_date, end_trading=strategy_data.end_date,
#                                      use_data=True, daily_closes=price_data[f'{strategy_data.rebalance}_closes']
#                                                      ,all_dates= all_dates,rebalance=strategy_data.rebalance)
#             trading_days_df = pd.DataFrame(data=trading_dates, columns=['Date'])
#             price_data[f'trading_dates'] = trading_days_df
#
#
#             loader.uploadCommonPath(price_data=price_data)
#     db.commit()
#     db.refresh(strategy)
#
#     return {
#         "strategy_id": strategy.id,
#         "status": f"successfully {action}"
#     }


# @router.post("/api/run-insample")
# async def run_insample_backtest(strategy_data: StrategyRequest):
#     try:
#
#         strategy = Strategy(
#             name=strategy_data.strategy_name,
#             rebalance=strategy_data.rebalance,
#             universe=strategy_data.universe,
#             slots=strategy_data.slots,
#             capital=strategy_data.capital,
#             start_date=strategy_data.start_date,
#             end_date=strategy_data.end_date,
#             stoploss_pct=strategy_data.stoploss_pct,
#             takeprofit_pct=strategy_data.takeprofit_pct,
#             entry_rules=build_expression([rule.dict() for rule in strategy_data.entry_rules]),
#             exit_rules=build_expression([rule.dict() for rule in strategy_data.exit_rules]),
#             ranking=strategy_data.ranking,
#             stoploss_timing=strategy_data.stoploss_timing,
#             takeprofit_timing=strategy_data.takeprofit_timing,
#             entry_timing=strategy_data.entry_timing,
#             exit_timing=strategy_data.exit_timing,
#             ranking_lookback=strategy_data.ranking_lookback,
#             ranking_order=strategy_data.ranking_order,
#             min_quantity=strategy_data.min_quantity,
#             min_price=strategy_data.min_price,
#             system_type=strategy_data.system_type,
#             stoploss_type=strategy_data.stoploss_type,
#             takeprofit_type=strategy_data.takeprofit_type,
#             order_type = strategy_data.order_type,
#             limit_pct = strategy_data.limit_pct,
#             atr_limit_lookback = strategy_data.atr_limit_lookback,
#             atr_lookback_stp=strategy_data.atr_lookback_stp,
#             atr_lookback_tp = strategy_data.atr_lookback_tp,
#         )
#         print(strategy)
#         strategy_dict = strategy.to_dict()
#         print("Strategy object as dictionary:")
#         print(strategy_dict)
#         try:
#             async with httpx.AsyncClient() as client:
#                 response = await client.post(
#                     "http://localhost:8080/api/backtest",  # Your external service endpoint
#                     json=strategy.to_dict()
#                 )
#             if response.status_code == 200:
#                 print(response.json())
#                 result = {"message": "Backtest completed", "equity_curve_path": "outputs/curve.png"}
#                 return result
#             return {"error": f"External API failed: {response.status_code}"}
#         except Exception as e:
#             print(e)
#             return {"error": f"Failed to call external API: {str(e)}"}
#     except Exception as e:
#         print(e)
#         raise HTTPException(status_code=500, detail=str(e))
#

BASE_OUTPUT_DIR = r"C:\Tharun\Projects\backtest_data"

@router.get("/api/{strategy_id}/input-files")
def list_input_files(
    strategy_id: int,
    system_name: str = Query(...),
    db: Session = Depends(get_db),
):
    """List all parquet files in the strategy's input/spy directory."""

    strategy = db.query(StrategyBucket).filter(StrategyBucket.id == strategy_id).first()
    input_dir = f"{BASE_OUTPUT_DIR}/{system_name}/input/{strategy.regimes[0].universe}"
    if not os.path.exists(input_dir):
        raise HTTPException(status_code=404, detail=f"Input directory not found: {input_dir}")

    files = []
    for fname in sorted(os.listdir(input_dir)):
        if fname.endswith('.parquet'):
            fpath = os.path.join(input_dir, fname)
            size_bytes = os.path.getsize(fpath)
            name = fname.replace('.parquet', '')

            if name.startswith('DAILY_') or name.startswith('MINUTE_'):
                category = 'prices'
            elif name in ('all_dates', 'trading_dates'):
                category = 'dates'
            elif '_universe' in name:
                category = 'universe'
            else:
                category = 'indicator'

            files.append({
                'filename': fname,
                'name': name,
                'category': category,
                'size_kb': round(size_bytes / 1024, 1),
            })

    return files


@router.get("/api/{strategy_id}/download-input/{filename}")
def download_input_file(
    strategy_id: int,
    filename: str,
    system_name: str = Query(...),
    db: Session = Depends(get_db),
):
    """Download a single input parquet file converted to CSV."""
    if '/' in filename or '\\' in filename or '..' in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    input_dir = f"{BASE_OUTPUT_DIR}/{system_name}/input/spy"
    file_path = os.path.join(input_dir, filename)

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")

    try:
        df = pd.read_parquet(file_path)

        buffer = io.StringIO()
        df.to_csv(buffer, index=True)
        buffer.seek(0)

        csv_name = filename.replace('.parquet', '.csv')
        return StreamingResponse(
            iter([buffer.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{system_name}_{csv_name}"'},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/{strategy_id}/download/{file_type}")
def download_csv(
    strategy_id: int,
    file_type: str,
    system_name: str = Query(...),
    db: Session = Depends(get_db),
):
    if file_type not in ("tradelist", "equity"):
        raise HTTPException(status_code=400, detail=f"Invalid file_type '{file_type}'.")

    # Map file_type to actual JSON filename
    file_names = {
        "tradelist": "Tradelist.json",
        "equity": "Equity.json",
    }


    file_path = f"{BASE_OUTPUT_DIR}/{system_name}/output/{file_names[file_type]}"

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}")

    try:
        df = pd.read_json(file_path).T  # same as your equity endpoint
        buffer = io.StringIO()
        df.to_csv(buffer, index=True)
        buffer.seek(0)

        filename = f"{system_name}_{file_type}.csv"
        return StreamingResponse(
            iter([buffer.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/{strategy_id}/equity")
def get_equity(strategy_id: str, db: Session = Depends(get_db)):
    data =[]
    layout = {}
    strategy = None
    if strategy_id:
        strategy = db.query(StrategyBucket).filter(StrategyBucket.id == strategy_id).first()

        # file_path = r"C:\Tharun\Projects\backtest_data\outputs\Equity.json"
        commonPath = r'C:\Tharun\Projects\backtest_data'

        file_path = f'{commonPath}/{strategy.name}/output/Equity.json'


        try:
            df = pd.read_json(file_path).T
        except Exception:
            raise HTTPException(status_code=404, detail="Equity file not found")

        if 'spy' in strategy.market_regime_type.lower():
            df["equityValue"] = df["equityValue"] - 37500
        else:
            df["equityValue"] = df["equityValue"] - 100000

        df["dailyDrawdown"] = -1 * df["dailyDrawdown"]
        df.index.name = "date"

        # ✅ Extract values
        x_dates = df.index.strftime("%Y-%m-%d").tolist()

        data = [
            # (1) Equity
            {
                "x": x_dates,
                "y": df["equityValue"].tolist(),
                "type": "scatter",
                "mode": "lines",
                "name": "Equity",
                "line": {"color": "green", "width": 2},
                "hovertemplate": "Equity: %{y:,.0f}<br>Date: %{x|%Y-%m-%d}<extra></extra>",
                "xaxis": "x",
                "yaxis": "y",
            },
            # (2) Drawdown
            {
                "x": x_dates,
                "y": df["dailyDrawdown"].tolist(),
                "type": "scatter",
                "mode": "lines",
                "fill": "tozeroy",
                "fillcolor": "rgba(220,38,38,0.6)",
                "name": "Drawdown",
                "line": {"color": "rgba(220,38,38,1)", "width": 1},
                "hovertemplate": "Drawdown: %{y:,.0f}<br>Date: %{x|%Y-%m-%d}<extra></extra>",
                "xaxis": "x2",
                "yaxis": "y2",
            },
            # (3a) Utility Value — area chart
            {
                "x": x_dates,
                "y": df["dayEndUtilityValue"].tolist(),
                "type": "scatter",
                "mode": "lines",
                "fill": "tozeroy",
                "fillcolor": "rgba(16,185,129,0.5)",
                "name": "Utility Value",
                "line": {"color": "rgba(16,185,129,1)", "width": 2},
                "hovertemplate": "Utility Value: %{y:,.0f}<br>Date: %{x|%Y-%m-%d}<extra></extra>",
                "xaxis": "x3",
                "yaxis": "y3",
            },
            # (3b) Utility Slots — scatter dots
            {
                "x": x_dates,
                "y": df["dayEndUtility"].tolist(),
                "type": "scatter",
                "mode": "lines",
                "fill": "tozeroy",
                "fillcolor": "rgba(16,185,129,0.5)",
                "name": "Utility Slots",
                "hovertemplate": "Utility Slots: %{y}<br>Date: %{x|%Y-%m-%d}<extra></extra>",
                "xaxis": "x3",
                "yaxis": "y3",
            },
        ]

        layout = {
            "title": f"Equity Curve - {strategy_id}",
            "height": 800,
            "plot_bgcolor": "white",
            "paper_bgcolor": "white",
            "font": {"family": "Inter, sans-serif", "size": 12, "color": "#333"},
            "hovermode": "x unified",
            "legend": {"orientation": "h", "y": -0.25},

            # Subplot configs
            "xaxis": {"domain": [0, 1], "anchor": "y", "showticklabels": False},
            "yaxis": {"domain": [0.40, 1], "title": "Equity"},
            "xaxis2": {"domain": [0, 1], "anchor": "y2", "matches": "x", "showticklabels": False},
            "yaxis2": {"domain": [0.20, 0.39], "title": "Drawdown"},
            "xaxis3": {"domain": [0, 1], "anchor": "y3", "matches": "x", "title": "Date"},
            "yaxis3": {"domain": [0, 0.19], "title": "Utility"},
        }

    return json.loads(json.dumps({"data": data, "layout": layout}, cls=PlotlyJSONEncoder))

# @router.get("/api/{strategy_id}/equity")
# def get_equity(strategy_id: str):
#     file_path = r"C:\Tharun\Projects\backtest_data\outputs\Equity.json"
#
#     try:
#         df = pd.read_json(file_path).T
#     except Exception:
#         raise HTTPException(status_code=404, detail="Equity file not found")
#
#     df["equityValue"] = df["equityValue"] - 100000
#
#     df['dailyDrawdown'] = -1 * df['dailyDrawdown']
#     df.index.name = "date"
#
#     data = [
#         # Equity (subplot 1 - keep line)
#         {
#             "x": df.index.strftime("%Y-%m-%d").tolist(),
#             "y": df["equityValue"].tolist(),
#             "type": "scatter",
#             "mode": "lines",
#             "name": "Equity",
#             "line": {"color": "green", "width": 2},  # yellow
#             "hovertemplate": "Equity: %{y:,.0f}<br>Date: %{x|%Y-%m-%d}<extra></extra>",
#             "xaxis": "x",
#             "yaxis": "y",
#         },
#         # Drawdown (subplot 2 - area fill to zero)
#         {
#             "x": df.index.strftime("%Y-%m-%d").tolist(),
#             "y": df["dailyDrawdown"].tolist(),
#             "type": "scatter",
#             "mode": "lines",
#             "fill": "tozeroy",  # ✅ fill to zero
#             "fillcolor": "rgba(220,38,38,0.6)",  # ✅ semi-transparent red
#             "name": "Drawdown",
#             "line": {"color": "rgba(220,38,38,1)", "width": 1},
#             "hovertemplate": "Drawdown: %{y:,.0f}<br>Date: %{x|%Y-%m-%d}<extra></extra>",
#             "xaxis": "x2",
#             "yaxis": "y2",
#         },
#         # Utility (subplot 3 - area fill to zero)
#         {
#             "x": df.index.strftime("%Y-%m-%d").tolist(),
#             "y": df["dayEndUtilityValue"].tolist(),
#             "type": "scatter",
#             "mode": "lines",
#             "fill": "tozeroy",  # ✅ fill to zero
#             "fillcolor": "rgba(16,185,129,0.6)",  # ✅ semi-transparent green
#             "name": "Utility",
#             "line": {"color": "rgba(16,185,129,1)", "width": 1},
#             "hovertemplate": "Utility: %{y:,.2f}<br>Date: %{x|%Y-%m-%d}<extra></extra>",
#             "xaxis": "x3",
#             "yaxis": "y3",
#         },
#     ]
#
#     layout = {
#         "title": f"Equity Curve - {strategy_id}",
#         "height": 800,
#         "plot_bgcolor": "white",
#         "paper_bgcolor": "white",
#         "font": {"family": "Inter, sans-serif", "size": 12, "color": "#333"},
#         "hovermode": "x unified",
#         "legend": {"orientation": "h", "y": -0.2},
#
#         # First subplot: Equity (60%)
#         "xaxis": {
#             "domain": [0, 1], "anchor": "y",
#             "showgrid": True,
#             "showticklabels": False,  # ❌ hide ticks
#             "title": ""  # ❌ no title
#         },
#         "yaxis": {
#             "domain": [0.40, 1], "anchor": "x",
#             "title": "Equity"
#         },
#
#         # Second subplot: Drawdown (20%) — matches xaxis
#         "xaxis2": {
#             "domain": [0, 1], "anchor": "y2",
#             "showgrid": True,
#             "showticklabels": False,  # ❌ hide ticks
#             "title": "",
#             "matches": "x"  # ✅ sync zoom/pan
#         },
#         "yaxis2": {
#             "domain": [0.20, 0.39], "anchor": "x2",
#             "title": "Drawdown"
#         },
#
#         # Third subplot: Utility (20%) — matches xaxis
#         "xaxis3": {
#             "domain": [0, 1], "anchor": "y3",
#             "showgrid": True,
#             "showticklabels": True,  # ✅ only bottom shows ticks
#             "title": "Date",
#             "matches": "x"
#         },
#         "yaxis3": {
#             "domain": [0, 0.19], "anchor": "x3",
#             "title": "Utility"
#         },
#     }
#
#     return json.loads(json.dumps({"data": data, "layout": layout}, cls=PlotlyJSONEncoder))

@router.get("/api/{strategy_id}/performance", response_model=PerformanceMetrics)
def get_performence(strategy_id: str, db: Session = Depends(get_db)):

    strategy = db.query(StrategyBucket).filter(StrategyBucket.id == strategy_id).first()

    commonPath = r'C:\Tharun\Projects\backtest_data'

    equity_path = f'{commonPath}/{strategy.name}/output/Equity.json'
    equity_df = pd.read_json(equity_path).T
    equity_df.index.name = 'date'
    equity_df['dailyDrawdown'] = -1 * equity_df['dailyDrawdown']

    tradelist_path = f'{commonPath}/{strategy.name}/output/TradeList.json'
    trade_df = pd.read_json(tradelist_path).T
    trade_df.index.name = 'id'


    return PerformanceMetrics.calculate_performance(equity_df, trade_df, 100000)


@router.post("/api/runbacktestv2")
async def run_backtest(strategy_data: StrategyRequest):
    try:
        strategy_dict = strategy_data.to_dict()
        print("Strategy object as dictionary:", strategy_dict)

        # Determine which endpoint to use
        if strategy_data.market_regime_type == 'Individual ETFs - Simple':
            await backtest_service.run_tradestation_backtest(strategy_data=strategy_data)
        else:
            await backtest_service.run_java_backtest(strategy_data= strategy_data)


    except HTTPException as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        print("Error in run_backtest:", e)
        raise HTTPException(status_code=500, detail=str(e))

# def parse_expression(expr: str) -> List[Dict[str, Any]]:
#     """
#     Reverse of build_expression: takes a string like
#     'rsi_2 < 30.0 && adx_10 > 30.0'
#     and returns a list of rule dicts.
#     """
#     if not expr:
#         return []
#
#     # Split by connectors (&&, ||, AND, OR)
#     tokens = re.split(r'\s+(?:&&|\|\||AND|OR)\s+', expr)
#
#     # Extract connectors (keep order)
#     connectors = re.findall(r'(?:&&|\|\||AND|OR)', expr)
#
#     rules = []
#     for i, token in enumerate(tokens):
#         m = re.match(r'([a-zA-Z_]+)_(\d+)\s*([<>=!]+)\s*([\d.]+)', token.strip())
#         if not m:
#             continue
#         indicator, lookback, operator, value = m.groups()
#         rules.append({
#             "indicator": indicator,   # match your indicators dict
#             "lookback": int(lookback),
#             "operator": operator,
#             "value": float(value),
#             "connector": connectors[i] if i < len(connectors) else ""
#         })
#
#     return rules

@router.get("/api/get-strategy/{id}", response_model=StrategyRequest)
def get_strategy(id: int, db: Session = Depends(get_db)):
    strategy = db.query(StrategyBucket).filter(StrategyBucket.id == id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    return StrategyRequest(
        id=strategy.id,
        name=strategy.name,  # ⚡ maps correctly
        rebalance=strategy.rebalance,
        start_date=strategy.start_date.isoformat() if strategy.start_date else None,
        end_date=strategy.end_date.isoformat() if strategy.end_date else None,
        min_quantity=strategy.min_quantity,
        min_price=strategy.min_price,
        system_type=strategy.system_type,
        market_regime_type=strategy.market_regime_type,
    )

# @router.get("/api/get-strategsy/{id}", response_model=StrategyRequest)
# def get_strategy(id: int, db: Session = Depends(get_db)):
#     strategy = db.query(Strategy).filter(Strategy.id == id).first()
#     if not strategy:
#         raise HTTPException(status_code=404, detail="Strategy not found")
#
#     return StrategyRequest(
#         id=strategy.id,
#         strategy_name=strategy.name,
#         rebalance=strategy.rebalance,
#         universe=strategy.universe,
#         slots=strategy.slots,
#         capital=strategy.capital,
#         start_date=str(strategy.start_date) if strategy.start_date else "",
#         end_date=str(strategy.end_date) if strategy.end_date else "",
#         stoploss_pct=strategy.stoploss_pct or 0.0,
#         takeprofit_pct=strategy.takeprofit_pct or 0.0,
#         stoploss_timing=strategy.stoploss_timing or "",
#         takeprofit_timing=strategy.takeprofit_timing or "",
#         entry_timing=strategy.entry_timing or "",
#         exit_timing=strategy.exit_timing or "",
#         ranking=strategy.ranking or "",
#         ranking_lookback=strategy.ranking_lookback or 0,
#         ranking_order=strategy.ranking_order or "",
#         min_quantity=strategy.min_quantity or 0,
#         min_price=strategy.min_price or 0.0,
#         system_type=strategy.system_type or "",
#         stoploss_type=strategy.stoploss_type or "",
#         takeprofit_type=strategy.takeprofit_type or "",
#         entry_rules=parse_expression(strategy.entry_rules),
#         exit_rules=parse_expression(strategy.exit_rules),
#         order_type = strategy.order_type or "",
#         limit_pct = strategy.limit_pct or 0.0,
#         atr_limit_lookback=strategy.atr_limit_lookback or 0,
#         atr_lookback_tp=strategy.atr_lookback_tp or 0,
#         atr_lookback_stp=strategy.atr_lookback_stp or 0
#     )