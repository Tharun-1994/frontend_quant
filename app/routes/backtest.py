"""
backtest.py
===========
Backtest execution, file download, equity and performance data endpoints.

Endpoints
---------
POST /api/runbacktestv2                          — trigger a backtest run
GET  /api/{strategy_id}/equity                   — equity + drawdown chart JSON
GET  /api/{strategy_id}/performance              — performance metrics JSON
GET  /api/{strategy_id}/input-files              — list input parquet files
GET  /api/{strategy_id}/download-input/{filename} — download one input file as CSV
GET  /api/{strategy_id}/download/{file_type}     — download tradelist or equity as CSV
"""

import io
import json
import logging
import os
import re
from typing import List, Dict, Any, Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from plotly.utils import PlotlyJSONEncoder
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from app.database import get_db
from app.loader.Rule_Tree_JSON import dumps_tree, loads_tree, normalize_rules_tree
from app.models import MarketRegime
from app.models.strategy_bucket import StrategyBucket
from app.schemas import StrategyRequest
from app.schemas.strategy import MarketRegimeBase
from app.schemas.PerformanceMetrics import PerformanceMetrics
from app.services.BacktestService import backtest_service
from app.Settings import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["backtest"])

BASE_OUTPUT_DIR = settings.BACKTEST_DATA_PATH


# ── Rule expression helpers ───────────────────────────────────────────────────

def build_expression(rules: List[Dict[str, Any]]) -> str:
    if not rules:
        return ""

    exprs = []
    for rule in rules:
        left = f"{rule['indicator'].lower()}_{rule['lookback']}"
        if rule.get("value_type") == "indicator_price":
            right = (
                f"{rule.get('value_indicator', '').lower()}"
                f"_{rule.get('value_lookback', '')}"
            )
        else:
            right = str(rule.get("value", 0.0))
        exprs.append(f"{left} {rule['operator']} {right}")

    parts = [exprs[0]]
    for i, rule in enumerate(rules[:-1]):
        conn = rule.get("connector") or "&&"
        parts.append(f"{conn} {exprs[i + 1]}")

    return " ".join(parts)


def extract_labels(rules: List[Dict[str, Any]]) -> str:
    labels = [
        f"{r.get('indicator')}_{r.get('lookback')}_{r.get('label')}"
        for r in rules
    ]
    return json.dumps(labels)


def parse_expression(expr: str) -> List[Dict[str, Any]]:
    """Convert a stored expression string back into a list of rule dicts."""
    if not expr:
        return []

    tokens = re.split(r"\s+(&&|\|\|)\s+", expr)
    rules: List[Dict[str, Any]] = []
    connector = ""

    for token in tokens:
        if token in ("&&", "||"):
            connector = token
            continue

        match = re.match(r"(\w+)_(\d+)\s*([<>!=]+)\s*(\w+|\d+\.?\d*)", token)
        if not match:
            continue

        indicator, lookback, operator, rhs = match.groups()
        try:
            value          = float(rhs)
            value_type     = "value"
            value_indicator = ""
            value_lookback  = 0
        except ValueError:
            value           = 0
            value_type      = "indicator_price"
            if "_" in rhs:
                parts           = rhs.split("_")
                value_indicator = parts[0]
                value_lookback  = parts[1]
            else:
                value_indicator = rhs
                value_lookback  = 0

        rules.append({
            "indicator":      indicator,
            "lookback":       int(lookback),
            "operator":       operator,
            "value":          value,
            "connector":      connector,
            "value_type":     value_type,
            "value_indicator": value_indicator,
            "value_lookback":  value_lookback,
        })
        connector = ""

    return rules


def parse_labels(
    labels_str: Optional[str], rules: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    if not rules:
        return []

    label_map: Dict[str, str] = {}
    if labels_str:
        try:
            for lbl in json.loads(labels_str):
                parts = lbl.split("_", 2)
                if len(parts) == 3:
                    label_map[f"{parts[0]}_{parts[1]}"] = parts[2]
        except Exception:
            pass

    for r in rules:
        key     = f"{r['indicator']}_{r['lookback']}"
        r["label"] = label_map.get(key, r["indicator"])

    return rules


def sort_rules_by_connector(rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        rules,
        key=lambda r: 0 if r.get("connector") in ("AND", "OR", "&&", "||") else 1,
    )

def _load_safety_nets(db_obj):
    """Read safety_nets_json off a marketregime row and return List[SafetyNetItem].

    Returns None when the column is NULL/empty — the engine treats this as
    'no safety nets configured'. Falls back gracefully on malformed JSON
    so a single bad row doesn't crash all backtests.
    """
    blob = getattr(db_obj, "safety_nets_json", None)
    if not blob:
        return None
    try:
        return json.loads(blob)
    except Exception as e:
        print(f"[WARN] safety_nets_json parse failed for regime "
              f"{getattr(db_obj, 'id', '?')}: {e}")
        return None

def db_to_pydantic(db_obj: MarketRegime) -> MarketRegimeBase:
    return MarketRegimeBase(
        id=db_obj.id,
        strategy_id=db_obj.strategy_id,
        regime_type=db_obj.regime_type,
        regime_ticker=db_obj.regime_ticker,
        market_trend_type=db_obj.market_trend_type,
        market_trend_rules=sort_rules_by_connector(
            parse_labels(
                db_obj.market_trend_rules_labels,
                parse_expression(db_obj.market_trend_rules),
            )
        ),
        volatility_rules=sort_rules_by_connector(
            parse_labels(
                db_obj.volatility_rules_labels,
                parse_expression(db_obj.volatility_rules),
            )
        ),
        entry_timing=db_obj.entry_timing,
        exit_timing=db_obj.exit_timing,
        freeze_timing=db_obj.freeze_timing or "open",
        resume_timing=db_obj.resume_timing or "open",
        safety_net_type=db_obj.safety_net_type or "none",
        safety_nets=_load_safety_nets(db_obj),
        stoploss_type=db_obj.stoploss_type,
        takeprofit_type=db_obj.takeprofit_type,
        takeprofit_dollar=db_obj.takeprofit_dollar,
        stoploss_dollar=db_obj.stoploss_dollar,
        stoploss_pct=db_obj.stoploss_pct,
        takeprofit_pct=db_obj.takeprofit_pct,
        stoploss_timing=db_obj.stoploss_timing,
        takeprofit_timing=db_obj.takeprofit_timing,
        portfolio_stoploss_anchor=db_obj.portfolio_stoploss_anchor,  # Patch 72f
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
        production_capital=(
            float(db_obj.production_capital)
            if db_obj.production_capital is not None else None
        ),  # Patch 49
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
        close_positions_on_regime_exit=db_obj.close_positions_on_regime_exit,
        sector_level=db_obj.sector_level,
        sector_limit=db_obj.sector_limit,
        gap_filter_pct=db_obj.gap_filter_pct,
        max_duplicates=db_obj.max_duplicates,
        max_duplicate_sets=db_obj.max_duplicate_sets,
        tdom_filters=json.loads(db_obj.tdom_filters_json or "[]"),
        vol_filter=(
            json.loads(db_obj.vol_filter_json) if db_obj.vol_filter_json else None
        ),
        # LRA Patch 12: LONGSHORT fields — None for existing strategies
        ticker_classification=(
            json.loads(db_obj.ticker_classification)
            if db_obj.ticker_classification else None
        ),
        pairing_entry_rules=(
            json.loads(db_obj.pairing_entry_rules)
            if db_obj.pairing_entry_rules else None
        ),
        pairing_exit_rules=(
            json.loads(db_obj.pairing_exit_rules)
            if db_obj.pairing_exit_rules else None
        ),
        sizing_policy=(
            json.loads(db_obj.sizing_policy)
            if db_obj.sizing_policy else None
        ),
        pair_exit_policy=(
            json.loads(db_obj.pair_exit_policy)
            if db_obj.pair_exit_policy else None
        ),
        # LRA Patch 34: per-leg entry rule trees
        entry_rules_tree_long=(
            json.loads(db_obj.entry_rules_tree_long)
            if db_obj.entry_rules_tree_long else None
        ),
        entry_rules_tree_short=(
            json.loads(db_obj.entry_rules_tree_short)
            if db_obj.entry_rules_tree_short else None
        ),
    )


# ── Backtest execution ────────────────────────────────────────────────────────

@router.post("/runbacktestv2")
async def run_backtest(strategy_data: StrategyRequest):
    """Trigger a backtest. Routes to TradeStation or Java engine based on regime type."""
    try:
        if strategy_data.market_regime_type == "Individual ETFs - Simple":
            await backtest_service.run_tradestation_backtest(strategy_data=strategy_data)
        else:
            await backtest_service.run_java_backtest(strategy_data=strategy_data)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("run_backtest failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── Equity & performance data ─────────────────────────────────────────────────

# --- Patch 10: benchmark overlay support (index compare on equity chart) ---
def _benchmark_index_folder() -> str:
    """Shared folder of daily_closes_{key}.csv produced by generate_index_prices.py."""
    from app.constants.PricePath import PricePath
    return PricePath.index_path


def list_available_benchmarks() -> List[Dict[str, str]]:
    """Discover index benchmarks on disk: daily_closes_{key}.csv -> {key, label}."""
    import glob
    folder = _benchmark_index_folder()
    out: List[Dict[str, str]] = []
    prefix, suffix = "daily_closes_", ".csv"
    for path in sorted(glob.glob(os.path.join(folder, prefix + "*" + suffix))):
        key = os.path.basename(path)[len(prefix):-len(suffix)]
        if key and key != "spy_close_0":
            out.append({"key": key, "label": key.upper()})
    return out


def _load_benchmark_closes(key: str) -> Optional[pd.Series]:
    """Load one index's daily closes as a date-indexed float Series, or None."""
    path = os.path.join(_benchmark_index_folder(), f"daily_closes_{key}.csv")
    if not os.path.exists(path):
        return None
    s = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
    col = key if key in s.columns else s.columns[0]
    return pd.to_numeric(s[col], errors="coerce").dropna().sort_index()


@router.get("/benchmarks")
def get_benchmarks():
    """Index benchmarks available for the equity-compare overlay."""
    return list_available_benchmarks()
# --- end Patch 10 ---


@router.get("/{strategy_id}/equity")
def get_equity(strategy_id: int, benchmark: Optional[str] = Query(None), db: Session = Depends(get_db)):
    """Return Plotly-compatible equity + drawdown + utility chart JSON."""
    strategy = db.query(StrategyBucket).filter(
        StrategyBucket.id == strategy_id
    ).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    file_path = f"{BASE_OUTPUT_DIR}/{strategy.name}/output/Equity.json"
    try:
        df = pd.read_json(file_path).T
    except Exception:
        raise HTTPException(status_code=404, detail="Equity file not found")

    first_regime = next((r for r in strategy.regimes if r.capital is not None), None)
    capital_offset = float(first_regime.capital if first_regime else 100_000)

    df["equityValue"]   = df["equityValue"] - capital_offset
    df["dailyDrawdown"] = df["dailyDrawdown"] * -1
    df.index.name       = "date"

    x_dates = df.index.strftime("%Y-%m-%d").tolist()

    data = [
        {
            "x": x_dates, "y": df["equityValue"].tolist(),
            "type": "scatter", "mode": "lines", "name": "Equity",
            "line": {"color": "green", "width": 2},
            "hovertemplate": "Equity: %{y:,.0f}<br>Date: %{x|%Y-%m-%d}<extra></extra>",
            "xaxis": "x", "yaxis": "y",
        },
        {
            "x": x_dates, "y": df["dailyDrawdown"].tolist(),
            "type": "scatter", "mode": "lines", "fill": "tozeroy",
            "fillcolor": "rgba(220,38,38,0.6)", "name": "Drawdown",
            "line": {"color": "rgba(220,38,38,1)", "width": 1},
            "hovertemplate": "Drawdown: %{y:,.0f}<br>Date: %{x|%Y-%m-%d}<extra></extra>",
            "xaxis": "x2", "yaxis": "y2",
        },
        {
            "x": x_dates, "y": df["dayEndUtilityValue"].tolist(),
            "type": "scatter", "mode": "lines", "fill": "tozeroy",
            "fillcolor": "rgba(16,185,129,0.5)", "name": "Utility Value",
            "line": {"color": "rgba(16,185,129,1)", "width": 2},
            "hovertemplate": "Utility Value: %{y:,.0f}<br>Date: %{x|%Y-%m-%d}<extra></extra>",
            "xaxis": "x3", "yaxis": "y3",
        },
        {
            "x": x_dates, "y": df["dayEndUtility"].tolist(),
            "type": "scatter", "mode": "lines", "fill": "tozeroy",
            "fillcolor": "rgba(16,185,129,0.5)", "name": "Utility Slots",
            "hovertemplate": "Utility Slots: %{y}<br>Date: %{x|%Y-%m-%d}<extra></extra>",
            "xaxis": "x3", "yaxis": "y3",
        },
    ]

    # --- Patch 11: optional benchmark overlay (rebased to starting capital) ---
    if benchmark:
        bench = _load_benchmark_closes(benchmark)
        if bench is not None and not bench.empty:
            aligned = bench.reindex(df.index, method="ffill").bfill()
            base = aligned.iloc[0]
            if base and base > 0:
                bench_pnl = (aligned / base - 1.0) * capital_offset
                data.append({
                    "x": x_dates, "y": bench_pnl.round(2).tolist(),
                    "type": "scatter", "mode": "lines",
                    "name": f"{benchmark.upper()} (buy & hold)",
                    "line": {"color": "#2563eb", "width": 2, "dash": "dot"},
                    "hovertemplate": f"{benchmark.upper()}: %{{y:,.0f}}<br>Date: %{{x|%Y-%m-%d}}<extra></extra>",
                    "xaxis": "x", "yaxis": "y",
                })
    # --- end Patch 11 ---

    layout = {
        "title": f"Equity Curve — strategy {strategy_id}",
        "height": 800,
        "plot_bgcolor": "white",
        "paper_bgcolor": "white",
        "font": {"family": "Inter, sans-serif", "size": 12, "color": "#333"},
        "hovermode": "x unified",
        "legend": {"orientation": "h", "y": -0.25},
        "xaxis":  {"domain": [0, 1], "anchor": "y",  "showticklabels": False},
        "yaxis":  {"domain": [0.40, 1], "title": "Equity"},
        "xaxis2": {"domain": [0, 1], "anchor": "y2", "matches": "x", "showticklabels": False},
        "yaxis2": {"domain": [0.20, 0.39], "title": "Drawdown"},
        "xaxis3": {"domain": [0, 1], "anchor": "y3", "matches": "x", "title": "Date"},
        "yaxis3": {"domain": [0, 0.19], "title": "Utility"},
    }

    return json.loads(json.dumps({"data": data, "layout": layout}, cls=PlotlyJSONEncoder))

    # ── Patch 50: utility distribution (slots + capital deployed, % of days) ───────
@router.get("/{strategy_id}/utility-distribution")
def get_utility_distribution(strategy_id: int, db: Session = Depends(get_db)):
    """Distribution of end-of-day utility slots and the matching capital
    deployed, as a % of all trading days. Reads the same Equity.json the
    equity chart uses (dayEndUtility / dayEndUtilityValue) — no engine change.
    The £-per-slot unit is derived from the data, so it generalises to any
    slot count."""
    strategy = db.query(StrategyBucket).filter(
        StrategyBucket.id == strategy_id
    ).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    file_path = f"{BASE_OUTPUT_DIR}/{strategy.name}/output/Equity.json"
    try:
        df = pd.read_json(file_path).T
    except Exception:
        raise HTTPException(status_code=404, detail="Equity file not found")

    if "dayEndUtility" not in df.columns:
        raise HTTPException(
            status_code=404,
            detail="Equity.json has no dayEndUtility column",
        )

    util = pd.to_numeric(df["dayEndUtility"], errors="coerce").dropna()
    util = util.round().astype(int)
    total = int(len(util))
    if total == 0:
        raise HTTPException(status_code=404, detail="No utility data available")

    max_slot = int(util.max())
    levels = list(range(0, max_slot + 1))
    counts = util.value_counts().reindex(levels, fill_value=0)
    pct = (counts / total * 100.0).round(1)

    # £-per-slot unit from the data (value / slots is constant where slots > 0).
    nz = util > 0
    unit = 0.0
    if "dayEndUtilityValue" in df.columns and bool(nz.any()):
        val = pd.to_numeric(df["dayEndUtilityValue"], errors="coerce").reindex(util.index)
        ratio = (val[nz] / util[nz]).median()
        unit = float(ratio) if pd.notna(ratio) else 0.0

    slots_x = [f"{k} slots" for k in levels]
    cap_x = [f"\u00A3{round(k * unit):,.0f}" for k in levels]
    y_vals = pct.tolist()
    labels = [f"{v:.1f}%" for v in y_vals]

    data = [
        {
            "x": slots_x, "y": y_vals, "type": "bar",
            "marker": {"color": "#FFA15A"}, "name": "Utility slots",
            "text": labels, "textposition": "outside", "cliponaxis": False,
            "hovertemplate": "%{x}<br>%{y:.1f}% of days<extra></extra>",
            "xaxis": "x", "yaxis": "y",
        },
        {
            "x": cap_x, "y": y_vals, "type": "bar",
            "marker": {"color": "#19D3F3"}, "name": "Capital value",
            "text": labels, "textposition": "outside", "cliponaxis": False,
            "hovertemplate": "%{x}<br>%{y:.1f}% of days<extra></extra>",
            "xaxis": "x2", "yaxis": "y2",
        },
    ]

    y_max = (max(y_vals) if y_vals else 0) * 1.15 + 1

    layout = {
        "showlegend": False,
        "height": 420,
        "margin": {"t": 50, "b": 70, "l": 55, "r": 20},
        "plot_bgcolor": "white",
        "paper_bgcolor": "white",
        "font": {"family": "Inter, sans-serif", "size": 12, "color": "#333"},
        "annotations": [
            {"text": "Utility slots distribution", "showarrow": False,
             "x": 0.21, "y": 1.12, "xref": "paper", "yref": "paper",
             "font": {"size": 14, "color": "#111827"}},
            {"text": "Capital value distribution", "showarrow": False,
             "x": 0.79, "y": 1.12, "xref": "paper", "yref": "paper",
             "font": {"size": 14, "color": "#111827"}},
        ],
        "xaxis": {"domain": [0, 0.45], "tickangle": 0},
        "xaxis2": {"domain": [0.55, 1], "tickangle": -35},
        "yaxis": {"title": "% of days", "range": [0, y_max],
                  "ticksuffix": "%", "gridcolor": "rgba(0,0,0,0.06)"},
        "yaxis2": {"range": [0, y_max], "ticksuffix": "%",
                   "gridcolor": "rgba(0,0,0,0.06)"},
    }

    cap_series = (
        pd.to_numeric(df["dayEndUtilityValue"], errors="coerce").reindex(util.index)
        if "dayEndUtilityValue" in df.columns else None
    )

    def _avg_block(slots_s):
        avg_slots = float(slots_s.mean())
        cap = (float(cap_series.reindex(slots_s.index).mean())
               if cap_series is not None else avg_slots * unit)
        return {
            "avg_slots": round(avg_slots, 2),
            "util_pct": round(avg_slots / max_slot * 100.0, 1) if max_slot else 0.0,
            "avg_capital": round(cap),
            "days": int(len(slots_s)),
        }

    yr_ser = pd.Series(pd.to_datetime(util.index, errors="coerce").year, index=util.index)
    averages = {
        "max_slots": max_slot,
        "overall": _avg_block(util),
        "by_year": [
            {"year": int(yr), **_avg_block(util[yr_ser == yr])}
            for yr in sorted(set(int(y) for y in yr_ser.dropna()))
        ],
    }
    # ── end Patch 51 ───────────────────────────────────────────────────────────────

    return json.loads(json.dumps(
        {"data": data, "layout": layout, "averages": averages},
        cls=PlotlyJSONEncoder,
    ))

    # ── end Patch 50 ───────────────────────────────────────────────────────────────

@router.get("/{strategy_id}/performance", response_model=PerformanceMetrics)
def get_performance(strategy_id: int, db: Session = Depends(get_db)):
    """Return structured performance metrics for a strategy."""
    strategy = db.query(StrategyBucket).filter(
        StrategyBucket.id == strategy_id
    ).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    base = f"{BASE_OUTPUT_DIR}/{strategy.name}/output"

    try:
        equity_df   = pd.read_json(f"{base}/Equity.json").T
        equity_df.index.name = "date"
        equity_df["dailyDrawdown"] = equity_df["dailyDrawdown"] * -1

        trade_df    = pd.read_json(f"{base}/TradeList.json").T
        trade_df.index.name = "id"
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Output files not found: {e}")

    return PerformanceMetrics.calculate_performance(equity_df, trade_df, 100_000)


# ── File download endpoints ───────────────────────────────────────────────────

@router.get("/{strategy_id}/input-files")
def list_input_files(
    strategy_id: int,
    system_name: str = Query(...),
    db: Session = Depends(get_db),
):
    """List all parquet input files for a strategy."""
    strategy = db.query(StrategyBucket).filter(
        StrategyBucket.id == strategy_id
    ).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    input_dir = f"{BASE_OUTPUT_DIR}/{system_name}/input/{strategy.regimes[0].universe}"
    if not os.path.exists(input_dir):
        raise HTTPException(
            status_code=404, detail=f"Input directory not found: {input_dir}"
        )

    files = []
    for fname in sorted(os.listdir(input_dir)):
        if not fname.endswith(".parquet"):
            continue
        name       = fname.replace(".parquet", "")
        size_bytes = os.path.getsize(os.path.join(input_dir, fname))

        if name.startswith("DAILY_") or name.startswith("MINUTE_"):
            category = "prices"
        elif name in ("all_dates", "trading_dates"):
            category = "dates"
        elif "_universe" in name:
            category = "universe"
        else:
            category = "indicator"

        files.append({
            "filename": fname,
            "name":     name,
            "category": category,
            "size_kb":  round(size_bytes / 1024, 1),
        })

    return files


@router.get("/{strategy_id}/download-input/{filename}")
def download_input_file(
    strategy_id: int,
    filename: str,
    system_name: str = Query(...),
    db: Session = Depends(get_db),
):
    """Download a single input parquet file as CSV."""
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    strategy = db.query(StrategyBucket).filter(
        StrategyBucket.id == strategy_id
    ).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    universe = strategy.regimes[0].universe if strategy.regimes else "sp500"
    file_path = os.path.join(
        f"{BASE_OUTPUT_DIR}/{system_name}/input/{universe}", filename
    )
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")

    try:
        buf = io.StringIO()
        pd.read_parquet(file_path).to_csv(buf, index=True)
        buf.seek(0)
        csv_name = filename.replace(".parquet", ".csv")
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{system_name}_{csv_name}"'
            },
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{strategy_id}/download/{file_type}")
def download_csv(
    strategy_id: int,
    file_type: str,
    system_name: str = Query(...),
    db: Session = Depends(get_db),
):
    """Download tradelist or equity output as CSV."""
    file_map = {"tradelist": "Tradelist.json", "equity": "Equity.json"}
    if file_type not in file_map:
        raise HTTPException(
            status_code=400, detail=f"Invalid file_type '{file_type}'."
        )

    file_path = f"{BASE_OUTPUT_DIR}/{system_name}/output/{file_map[file_type]}"
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}")

    try:
        buf = io.StringIO()
        pd.read_json(file_path).T.to_csv(buf, index=True)
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{system_name}_{file_type}.csv"'
                )
            },
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))