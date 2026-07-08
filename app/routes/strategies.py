"""
strategies.py
=============
All strategy and market-regime CRUD endpoints.

Endpoints
---------
GET    /api/strategies                      — list all strategies
GET    /api/get-strategy/{id}               — fetch one strategy (full)
POST   /api/save-strategy                   — create or update a strategy bucket
DELETE /api/strategies/{strategy_id}        — delete strategy + its regimes
GET    /api/check-username                  — check if a strategy name is taken

GET    /api/marketregime/{strategy_id}      — list all regimes for a strategy
POST   /api/save-marketregime-v2            — create or update a market regime (current)
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.loader.GeneratePricesIndicators import GeneratePricesIndicators
from app.loader.PriceDataLoader import PriceDataLoader
from app.loader.Rule_Tree_JSON import dumps_tree, normalize_rules_tree
from app.models import MarketRegime, StrategyBucket
from app.schemas import StrategyRequest
from app.schemas.strategy import MarketRegimeBase
from app.schemas.StrategyResponse import StrategyResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["strategies"])


# ── Strategy CRUD ─────────────────────────────────────────────────────────────

@router.get("/strategies", response_model=List[StrategyResponse])
def list_strategies(db: Session = Depends(get_db)):
    """Return all strategy buckets."""
    return db.query(StrategyBucket).all()


@router.get("/get-strategy/{strategy_id}", response_model=StrategyRequest)
def get_strategy(strategy_id: int, db: Session = Depends(get_db)):
    """Fetch one strategy by ID."""
    strategy = db.query(StrategyBucket).filter(StrategyBucket.id == strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    return StrategyRequest(
        id=strategy.id,
        name=strategy.name,
        rebalance=strategy.rebalance,
        start_date=strategy.start_date.isoformat() if strategy.start_date else None,
        end_date=strategy.end_date.isoformat() if strategy.end_date else None,
        min_quantity=strategy.min_quantity,
        min_price=strategy.min_price,
        system_type=strategy.system_type,
        market_regime_type=strategy.market_regime_type,
        # F5: round-trip the live-execution config so the editor doesn't reset
        # these fields to defaults on reload (which would clobber them on the
        # next save).
        production_capital=(
            float(strategy.production_capital)
            if strategy.production_capital is not None else None
        ),
        execution_enabled=bool(strategy.execution_enabled),
        system_code=strategy.system_code,
    )


@router.post("/save-strategy")
def save_strategy(strategy_data: StrategyRequest, db: Session = Depends(get_db)):
    """Create or update a strategy bucket.

    Patch 55: production_capital no longer written here — it has moved to the
    regime level (Patches 56-57). The column on strategies_bucket is preserved
    for backward compat but is read-only from this route.
    execution_enabled=TRUE now requires every regime to have
    production_capital > 0 (validated below).
    """
    strategy = None
    if getattr(strategy_data, "id", None):
        strategy = db.query(StrategyBucket).filter(
            StrategyBucket.id == strategy_data.id
        ).first()

    # Patch 89: enable-then-fund. The "every regime must be funded" gate that
    # used to BLOCK here (Patch 55) has MOVED to the nightly orchestrator
    # (eod_orchestrator._run_position_managers_step): a live strategy whose
    # regimes aren't all funded is now SKIPPED + logged at run time, instead of
    # the form refusing to save. So execution_enabled=TRUE saves immediately and
    # the strategy simply won't trade until every regime has production_capital>0.
    # Backstop remains: payload_builder._resolve_execution_capital raises if PM
    # is invoked (e.g. a manual replay) on a regime with NULL production_capital.

    if strategy:
        strategy.name = strategy_data.name
        strategy.start_date = strategy_data.start_date
        strategy.end_date = strategy_data.end_date
        strategy.rebalance = strategy_data.rebalance
        strategy.min_price = strategy_data.min_price
        strategy.min_quantity = strategy_data.min_quantity
        strategy.system_type = strategy_data.system_type
        strategy.market_regime_type = strategy_data.market_regime_type
        # Patch 55: production_capital write removed — see docstring.
        strategy.execution_enabled = bool(strategy_data.execution_enabled)
        strategy.system_code = (strategy_data.system_code or '').strip().upper() or None
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
            # Patch 55: production_capital not set here — see docstring.
            execution_enabled=bool(strategy_data.execution_enabled),
            system_code=(strategy_data.system_code or '').strip().upper() or None,
        )
        db.add(strategy)
        action = "created"

    db.commit()
    db.refresh(strategy)

    PriceDataLoader.create_strategy_Folder(name=strategy_data.name)

    return {"strategy_id": strategy.id, "status": f"successfully {action}"}


@router.delete("/strategies/{strategy_id}")
def delete_strategy(strategy_id: int, db: Session = Depends(get_db)):
    """
    Delete a strategy and all its market regimes.
    Regimes are deleted first to avoid FK constraint errors.
    """
    strategy = db.query(StrategyBucket).filter(
        StrategyBucket.id == strategy_id
    ).first()
    if not strategy:
        raise HTTPException(
            status_code=404, detail=f"Strategy {strategy_id} not found"
        )

    strategy_name = strategy.name
    try:
        regimes_deleted = (
            db.query(MarketRegime)
            .filter(MarketRegime.strategy_id == strategy_id)
            .delete(synchronize_session=False)
        )
        db.delete(strategy)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete strategy {strategy_id}: {e}",
        )

    return {
        "success": True,
        "id": strategy_id,
        "name": strategy_name,
        "regimes_deleted": regimes_deleted,
        "message": f"Strategy '{strategy_name}' deleted",
    }


@router.get("/check-username")
def check_username(name: str, db: Session = Depends(get_db)):
    """Check whether a strategy name is already taken."""
    count = db.query(func.count(StrategyBucket.name)).filter(
        StrategyBucket.name == name
    ).scalar()
    return {"name": name, "taken": count > 0}


# ── Market Regime CRUD ────────────────────────────────────────────────────────

@router.get("/marketregime/{strategy_id}", response_model=List[MarketRegimeBase])
def get_market_regimes(strategy_id: int, db: Session = Depends(get_db)):
    """List all market regimes for a strategy."""
    from app.routes.backtest import db_to_pydantic  # avoid circular at module level
    db_objs = db.query(MarketRegime).filter(
        MarketRegime.strategy_id == strategy_id
    ).all()
    return [db_to_pydantic(obj) for obj in db_objs]


@router.post("/save-marketregime-v2")
def save_market_regime(marketregime: MarketRegimeBase, db: Session = Depends(get_db)):
    """Create or update a market regime (current v2 implementation)."""
    if marketregime.id:
        db_obj = db.query(MarketRegime).filter(
            MarketRegime.id == marketregime.id
        ).first()
        if not db_obj:
            raise HTTPException(status_code=404, detail="MarketRegime not found")
    else:
        db_obj = MarketRegime(strategy_id=marketregime.strategy_id)
        db.add(db_obj)

    # Scalar fields
    db_obj.regime_type                   = marketregime.regime_type
    db_obj.regime_ticker                 = marketregime.regime_ticker
    db_obj.market_trend_type             = marketregime.market_trend_type
    db_obj.market_trend_rules            = None
    db_obj.market_trend_rules_labels     = None
    db_obj.entry_timing                  = marketregime.entry_timing
    db_obj.exit_timing                   = marketregime.exit_timing
    db_obj.freeze_timing = marketregime.freeze_timing or "open"
    db_obj.resume_timing = marketregime.resume_timing or "open"
    db_obj.safety_net_type = marketregime.safety_net_type or "none"
    if marketregime.safety_nets is None:
        db_obj.safety_nets_json = None
    else:
        # Pydantic v1: .dict() / v2: .model_dump()
        items = [
            (sn.dict() if hasattr(sn, "dict") else sn.model_dump())
            for sn in marketregime.safety_nets
        ]
        db_obj.safety_nets_json = json.dumps(items)

    # Patch 66 (re-indent fix, AGAIN): validate stoploss_type × regime_type mapping
    # before writing. ETF regimes accept only DOLLAR_BASED; non-ETF accept
    # NORMAL/ATR/PORTFOLIO. Runs for EVERY regime save, not only the
    # safety_nets-configured ones. If this comment ever lies about its position
    # in the file again, audit the indent immediately.
    if marketregime.stoploss_type:
        from app.constants.static_config import (
            allowed_stoploss_types_for_regime,
            PORTFOLIO_STOPLOSS_ANCHOR,
            PORTFOLIO_STOPLOSS_ANCHOR_DEFAULT,
        )
        allowed = allowed_stoploss_types_for_regime(marketregime.regime_type)
        if marketregime.stoploss_type not in allowed:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"stoploss_type={marketregime.stoploss_type!r} not allowed "
                    f"for regime_type={marketregime.regime_type!r}. "
                    f"Allowed types: {allowed}"
                ),
            )
        # Patch 66: PORTFOLIO is EOD-only. Force the timing field regardless of input.
        if marketregime.stoploss_type == "PORTFOLIO":
            marketregime.stoploss_timing = "EOD"
            # Patch 72e: anchor validation. Default to PEAK if unset; reject
            # unknown values explicitly to avoid silent fallback at the engine.
            valid_anchors = set(PORTFOLIO_STOPLOSS_ANCHOR.values())
            if not marketregime.portfolio_stoploss_anchor:
                marketregime.portfolio_stoploss_anchor = PORTFOLIO_STOPLOSS_ANCHOR_DEFAULT
            elif marketregime.portfolio_stoploss_anchor not in valid_anchors:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"portfolio_stoploss_anchor={marketregime.portfolio_stoploss_anchor!r} "
                        f"not in allowed values {sorted(valid_anchors)}"
                    ),
                )
        else:
            # Patch 72e: anchor is only meaningful for PORTFOLIO. Clear it
            # for other stoploss types so the DB row stays clean.
            marketregime.portfolio_stoploss_anchor = None

    db_obj.stoploss_type = marketregime.stoploss_type
    db_obj.takeprofit_type = marketregime.takeprofit_type
    db_obj.takeprofit_dollar = marketregime.takeprofit_dollar
    db_obj.stoploss_dollar = marketregime.stoploss_dollar
    db_obj.stoploss_pct = marketregime.stoploss_pct
    db_obj.stoploss_max_pct = marketregime.stoploss_max_pct  # Patch 99
    db_obj.takeprofit_pct = marketregime.takeprofit_pct
    db_obj.stoploss_timing = marketregime.stoploss_timing
    # Patch 72e: anchor column write. Validated/defaulted above.
    db_obj.portfolio_stoploss_anchor = marketregime.portfolio_stoploss_anchor
    db_obj.takeprofit_timing = marketregime.takeprofit_timing
    db_obj.atr_lookback_stp              = marketregime.atr_lookback_stp
    db_obj.atr_lookback_tp               = marketregime.atr_lookback_tp
    db_obj.ranking                       = marketregime.ranking
    db_obj.ranking_lookback              = marketregime.ranking_lookback
    db_obj.ranking_order                 = marketregime.ranking_order
    db_obj.order_type                    = marketregime.order_type
    db_obj.limit_pct                     = marketregime.limit_pct
    db_obj.atr_limit_lookback            = marketregime.atr_limit_lookback
    db_obj.universe = marketregime.universe
    db_obj.capital = marketregime.capital
    db_obj.slots = marketregime.slots
    db_obj.max_time = marketregime.max_time
    db_obj.is_look_inside_bar = marketregime.is_look_inside_bar
    db_obj.close_positions_on_regime_exit = marketregime.close_positions_on_regime_exit
    db_obj.sector_level = marketregime.sector_level
    db_obj.sector_limit = marketregime.sector_limit
    db_obj.gap_filter_pct = marketregime.gap_filter_pct
    db_obj.max_duplicates = marketregime.max_duplicates
    db_obj.max_duplicate_sets = marketregime.max_duplicate_sets
    db_obj.substitute_pool_size = marketregime.substitute_pool_size

    # Patch 56: per-regime production_capital write. Audit row appended after
    # commit+refresh (see _patch56_production_capital_audit below).
    db_obj.production_capital = marketregime.production_capital

    # JSON-serialised fields
    db_obj.banned_months      = json.dumps(marketregime.banned_months or [])
    db_obj.tdom_filters_json  = json.dumps(
        [f.dict() for f in (marketregime.tdom_filters or [])]
    )
    db_obj.vol_filter_json    = (
        json.dumps(marketregime.vol_filter.dict()) if marketregime.vol_filter else None
    )

    # Rule trees — normalise then serialise
    marketregime.market_trend_rules_tree = normalize_rules_tree(
        marketregime.market_trend_rules_tree
    )
    marketregime.entry_rules_tree  = normalize_rules_tree(marketregime.entry_rules_tree)
    marketregime.exit_rules_tree   = normalize_rules_tree(marketregime.exit_rules_tree)
    marketregime.freeze_rules_tree = normalize_rules_tree(marketregime.freeze_rules_tree)
    marketregime.resume_rules_tree = normalize_rules_tree(marketregime.resume_rules_tree)

    db_obj.market_trend_rules_tree_json = dumps_tree(marketregime.market_trend_rules_tree)
    db_obj.entry_rules_tree_json        = dumps_tree(marketregime.entry_rules_tree)
    db_obj.exit_rules_tree_json         = dumps_tree(marketregime.exit_rules_tree)
    db_obj.freeze_rules_tree_json       = dumps_tree(marketregime.freeze_rules_tree)
    db_obj.resume_rules_tree_json       = dumps_tree(marketregime.resume_rules_tree)

    # LRA Patch 12: persist LONGSHORT fields as JSON text (None when absent)
    db_obj.ticker_classification = (
        json.dumps(marketregime.ticker_classification)
        if marketregime.ticker_classification else None
    )
    db_obj.pairing_entry_rules = (
        json.dumps(marketregime.pairing_entry_rules)
        if marketregime.pairing_entry_rules else None
    )
    db_obj.pairing_exit_rules = (
        json.dumps(marketregime.pairing_exit_rules)
        if marketregime.pairing_exit_rules else None
    )
    db_obj.sizing_policy = (
        json.dumps(marketregime.sizing_policy)
        if marketregime.sizing_policy else None
    )
    db_obj.pair_exit_policy = (
        json.dumps(marketregime.pair_exit_policy)
        if marketregime.pair_exit_policy else None
    )

    # LRA Patch 34: persist per-leg entry rule trees as JSON text
    db_obj.entry_rules_tree_long = (
        json.dumps(marketregime.entry_rules_tree_long)
        if marketregime.entry_rules_tree_long else None
    )
    db_obj.entry_rules_tree_short = (
        json.dumps(marketregime.entry_rules_tree_short)
        if marketregime.entry_rules_tree_short else None
    )

    db.commit()
    db.refresh(db_obj)

    # Patch 56: audit row write — must follow refresh so db_obj.id is populated
    # for the regime_id FK on newly created regimes.
    _patch56_production_capital_audit(db, db_obj, marketregime)
    db.commit()

    # Indicator file generation — non-fatal; id is already saved
    generate_warning = None
    try:
        strategy = db.query(StrategyBucket).filter(
            StrategyBucket.id == marketregime.strategy_id
        ).first()
        GeneratePricesIndicators.generate(marketRegime=marketregime, strategy=strategy)
    except Exception as e:
        generate_warning = str(e)
        logger.warning(
            f"Indicator generation failed for regime {db_obj.id}: {e}"
        )

    result = {
        "id": db_obj.id,
        "strategy_id": db_obj.strategy_id,
        "regime_type": db_obj.regime_type,
    }
    if generate_warning:
        result["warning"] = f"Saved but indicator generation failed: {generate_warning}"

    return result

# Patch 56: regime-level production_capital audit. Runs AFTER db.refresh so
# db_obj.id is available for FK on new regimes. The column write itself
# happens in the main save block (one of the db_obj.* assignments above).
# This function only appends an audit row when the value transitioned.
def _patch56_production_capital_audit(db: Session, db_obj, marketregime) -> None:
    from app.models.strategy_production_capital_history import (
        StrategyProductionCapitalHistory,
    )
    new_cap = getattr(marketregime, "production_capital", None)
    new_f = float(new_cap) if new_cap is not None else None
    # Read the pre-update value: we already wrote db_obj.production_capital
    # above, so we have to detect "did this differ from prior state" via
    # the schema input + a separate query. Simpler: query the latest history
    # row for this regime and compare.
    prior = (
        db.query(StrategyProductionCapitalHistory)
        .filter(StrategyProductionCapitalHistory.regime_id == db_obj.id)
        .order_by(StrategyProductionCapitalHistory.id.desc())
        .first()
    )
    prior_capital = float(prior.new_capital) if prior is not None else None
    if prior_capital == new_f:
        return  # no transition
    history = StrategyProductionCapitalHistory(
        strategy_id=db_obj.strategy_id,
        regime_id=db_obj.id,
        old_capital=prior_capital,
        new_capital=(new_f if new_f is not None else 0.0),
        changed_by=None,
        reason=None,
    )
    db.add(history)
# Patch 56: regime-level production_capital writer + history audit.
# Called inline from save_market_regime. Detects no-op vs change, appends a
# row to strategy_production_capital_history with regime_id populated.
def _patch56_production_capital_update(db: Session, db_obj, marketregime) -> None:
    from app.models.strategy_production_capital_history import (
        StrategyProductionCapitalHistory,
    )
    new_cap = getattr(marketregime, "production_capital", None)
    old_cap = db_obj.production_capital
    # Normalise comparison: NULL == NULL is a no-op; numeric equality is no-op.
    old_f = float(old_cap) if old_cap is not None else None
    new_f = float(new_cap) if new_cap is not None else None
    if old_f == new_f:
        return  # no change, no audit row, no write
    db_obj.production_capital = new_cap
    # Don't append audit row if going from "unset to still unset" — already
    # short-circuited above. Audit row only for real value transitions.
    if new_f is None:
        # Going from set to NULL: still audit (it's a deliberate clear).
        new_capital_value = 0.0
    else:
        new_capital_value = new_f
    history = StrategyProductionCapitalHistory(
        strategy_id=db_obj.strategy_id,
        regime_id=db_obj.id,
        old_capital=old_f,
        new_capital=new_capital_value,
        changed_by=None,
        reason=None,
    )
    db.add(history)