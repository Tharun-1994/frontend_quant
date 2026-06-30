"""
equity_view.py
==============
Strategy-name-based equity chart and performance endpoints.
(Separate from the ID-based endpoints in backtest.py.)

Endpoints
---------
GET /api/strategy/{strategy_name}/equity       — Plotly chart JSON
GET /api/strategy/{strategy_name}/performance  — performance metrics JSON
"""

import json
import logging
import os

import pandas as pd
import plotly.graph_objects as go
from _plotly_utils.utils import PlotlyJSONEncoder
from fastapi import APIRouter, HTTPException
from plotly.subplots import make_subplots
from starlette.responses import JSONResponse
from sqlalchemy.orm import Session as _Session
from fastapi import Depends as _Depends
from app.database import get_db as _get_db
from app.models.live_equity_snapshot import LiveEquitySnapshot as _LiveEquitySnapshot

from app.constants.PricePath import PricePath
from app.schemas.PerformanceMetrics import PerformanceMetrics

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/strategy", tags=["equity_view"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_strategy_data(strategy_name: str):
    """Load tradelist and equity DataFrames from JSON output files."""
    output_path = PricePath.getCommonOutputPath()
    trades_path = os.path.join(output_path, "TradeList.json")
    equity_path = os.path.join(output_path, "Equity.json")

    if not os.path.exists(trades_path) or not os.path.exists(equity_path):
        raise FileNotFoundError(
            f"Missing output files for strategy '{strategy_name}'"
        )

    trades_df = pd.read_json(trades_path).T
    trades_df["entryDate"] = pd.to_datetime(trades_df["entryDate"])
    trades_df["exitDate"]  = pd.to_datetime(trades_df["exitDate"])

    equity_df = pd.read_json(equity_path).T
    equity_df.index.name    = "date"
    equity_df               = equity_df.reset_index()
    equity_df["date"]       = pd.to_datetime(equity_df["date"])
    equity_df["dailyDrawdown"] = equity_df["dailyDrawdown"] * -1

    return trades_df, equity_df


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/{strategy_name}/equity", summary="Equity + drawdown chart as Plotly JSON")
def get_equity_chart(strategy_name: str):
    equity_path = os.path.join(PricePath.getCommonOutputPath(), "Equity.json")
    if not os.path.exists(equity_path):
        raise HTTPException(status_code=404, detail="No equity data found")

    equity_df = pd.read_json(equity_path).T
    equity_df.index.name       = "date"
    equity_df                  = equity_df.reset_index()
    equity_df["date"]          = pd.to_datetime(equity_df["date"])
    equity_df["equityValue"]   = equity_df["equityValue"] - 37_500

    fig = make_subplots(
        rows=2, cols=1,
        row_heights=[0.8, 0.2],
        specs=[[{"secondary_y": True}], [{"secondary_y": False}]],
        shared_xaxes=True,
        subplot_titles=["Equity", "Drawdown"],
        vertical_spacing=0.05,
    )

    fig.add_trace(
        go.Scatter(
            x=equity_df["date"],
            y=equity_df["equityValue"],
            name="Equity",
            mode="lines",
            line=dict(color="#0F766E", width=2),
            hovertemplate="Equity: %{y:,.0f}<br>Date: %{x|%Y-%m-%d}<extra></extra>",
        ),
        row=1, col=1,
    )

    fig.add_trace(
        go.Scatter(
            x=equity_df["date"],
            y=equity_df["dailyDrawdown"],
            name="Drawdown",
            mode="lines",
            fill="tozeroy",
            line=dict(color="rgba(220,38,38,0.8)", width=1.5),
            hovertemplate="Drawdown: %{y:,.0f}<br>Date: %{x|%Y-%m-%d}<extra></extra>",
        ),
        row=2, col=1,
    )

    fig.update_layout(
        height=520,
        margin=dict(t=40, b=30, l=50, r=30),
        plot_bgcolor="white",
        paper_bgcolor="white",
        font=dict(family="Inter, sans-serif", size=12, color="#333"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified",
    )
    fig.update_xaxes(showgrid=True, gridwidth=0.5, gridcolor="rgba(0,0,0,0.05)")
    fig.update_yaxes(showgrid=True, gridwidth=0.5, gridcolor="rgba(0,0,0,0.05)")

    return JSONResponse(
        content=json.loads(json.dumps(fig, cls=PlotlyJSONEncoder))
    )


@router.get("/{strategy_name}/performance", response_model=PerformanceMetrics,
            summary="Performance metrics JSON")
def get_performance(strategy_name: str):
    try:
        trades_df, equity_df = _load_strategy_data(strategy_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("get_performance failed for %s", strategy_name)
        raise HTTPException(status_code=500, detail=str(e))

    return PerformanceMetrics.calculate_performance(trades_df, equity_df, 100_000)


@router.get("/{strategy_id}/live-equity")
def get_live_equity(strategy_id: int, db: _Session = _Depends(_get_db)):
    """Return all LiveEquitySnapshot rows for a strategy ordered by date asc.

    Used by EquityTab to stitch live equity onto the backtest equity curve.
    Returns a list of dicts with date, equity, unrealised_pnl, drawdown,
    open_position_count so the frontend can render a continuous line.
    """
    rows = (
        db.query(_LiveEquitySnapshot)
        .filter_by(strategy_id=strategy_id)
        .order_by(_LiveEquitySnapshot.snapshot_date.asc())
        .all()
    )
    return [
        {
            'date':               r.snapshot_date.isoformat(),
            'equity':             float(r.equity)          if r.equity          is not None else None,
            'unrealised_pnl':     float(r.unrealised_pnl)  if r.unrealised_pnl  is not None else None,
            'unrealised_pct':     float(r.unrealised_pct)  if r.unrealised_pct  is not None else None,
            'drawdown':           float(r.drawdown)         if r.drawdown         is not None else None,
            'drawdown_pct':       float(r.drawdown_pct)     if r.drawdown_pct     is not None else None,
            'open_position_count': r.open_position_count,
            'production_capital': float(r.production_capital) if r.production_capital is not None else None,
        }
        for r in rows
    ]# ── Live Performance endpoints ────────────────────────────────────────────────
# Append these to equity_view.py

from typing import Optional as _Optional
from collections import defaultdict as _defaultdict
import pandas as _pd
from app.loader import strategy_stat_functions as _ssf
from app.models.tradelist import Tradelist as _Tradelist
from app.models.strategy_bucket import StrategyBucket as _StrategyBucket
from app.models.substitution_override import SubstitutionOverride as _SubOvr


@router.get("/{strategy_id}/live-performance")
def get_live_performance(strategy_id: int, db: _Session = _Depends(_get_db)):
    """Full live performance data for a strategy.

    Returns:
      equity_series    — daily from live_equity_snapshot, offset by production_capital
                         (same convention as backtest: shows P&L relative to baseline)
      closed_trades    — EXITED TRADED rows with P&L
      monthly_returns  — [{year, months:[Jan..Dec], total}] like PerformanceTab
      monthly_trades   — [{year, months:[Jan..Dec], total}] trade counts
      yearly_returns   — [{year, pnl_pct, trades, win_rate}]
      metrics          — stat card values
    """
    # ── 1. Equity snapshots ───────────────────────────────────────────────────
    snaps = (
        db.query(_LiveEquitySnapshot)
        .filter_by(strategy_id=strategy_id)
        .order_by(_LiveEquitySnapshot.snapshot_date.asc())
        .all()
    )

    prod_cap = float(snaps[0].production_capital) if snaps and snaps[0].production_capital else 0.0

    equity_series = [
        {
            'date':               s.snapshot_date.isoformat(),
            # offset: P&L above/below baseline (matches backtest convention)
            'equity_offset':      round(float(s.equity) - prod_cap, 2)        if s.equity            is not None else None,
            'equity':             float(s.equity)                              if s.equity            is not None else None,
            'unrealised_pnl':     float(s.unrealised_pnl)                     if s.unrealised_pnl    is not None else None,
            'unrealised_pct':     float(s.unrealised_pct)                     if s.unrealised_pct    is not None else None,
            # drawdown offset (high watermark minus current equity, negative)
            'drawdown_offset':    round(float(s.equity) - float(s.max_equity), 2)
                                  if s.equity is not None and s.max_equity is not None else None,
            'drawdown_pct':       float(s.drawdown_pct)                       if s.drawdown_pct      is not None else None,
            'max_equity_offset':  round(float(s.max_equity) - prod_cap, 2)    if s.max_equity        is not None else None,
            'deployed_capital':   float(s.deployed_capital)                   if s.deployed_capital  is not None else None,
            'unused_capital':     float(s.unused_capital)                     if s.unused_capital    is not None else None,
            'open_position_count': s.open_position_count,
        }
        for s in snaps
    ]

    # ── 2. Closed trades (EXITED, TRADED ledger) ──────────────────────────────
    closed = (
        db.query(_Tradelist)
        .filter(
            _Tradelist.strategy_id == strategy_id,
            _Tradelist.ledger == 'TRADED',
            _Tradelist.status == 'EXITED',
        )
        .order_by(_Tradelist.exit_date.asc())
        .all()
    )

    closed_trades = [
        {
            'id':          t.id,
            'symbol':      t.symbol,
            'direction':   t.direction,
            'entry_date':  t.entry_date.isoformat()  if t.entry_date  else None,
            'exit_date':   t.exit_date.isoformat()   if t.exit_date   else None,
            'entry_price': float(t.entry_price)      if t.entry_price  is not None else None,
            'exit_price':  float(t.exit_price)       if t.exit_price   is not None else None,
            'filled_qty':  t.filled_qty,
            'profit':      float(t.profit)           if t.profit       is not None else None,
            'profit_pct':  float(t.profit_pct)       if t.profit_pct   is not None else None,
            'day_count':   t.day_count,
            'exit_reason': t.exit_reason,
        }
        for t in closed
    ]

    # ── 3. Monthly / Yearly returns via strategy_stat_functions ───────────────
    # Build a daily equity Series from live_equity_snapshot (offset by prod_cap)
    monthly_returns_out = []
    monthly_trades_out  = []
    yearly_returns_out  = []

    if snaps and closed:
        eq_index  = _pd.to_datetime([s.snapshot_date for s in snaps])
        eq_values = _pd.Series(
            [float(s.equity) - prod_cap if s.equity else 0.0 for s in snaps],
            index=eq_index,
            name='equity',
        )

        # monthly_returns uses the same function as the backtest PerformanceTab
        monthly_df = _ssf.monthly_returns(eq_values, prod_cap, False)

        eq_periods = {(int(ts.year), int(ts.month)) for ts in eq_index}

        for year, row in monthly_df.iterrows():
            yr = int(year)
            months = [
                float(row.get(m)) if (yr, m) in eq_periods and row.get(m) is not None else None
                for m in range(1, 13)
            ]
            total = float(row.get('Total')) if row.get('Total') is not None else None
            monthly_returns_out.append({'year': yr, 'months': months, 'total': total})

        # Monthly trade counts
        # Patch 86: to_datetime(list) yields a DatetimeIndex (no .dt accessor);
        # wrap in a Series so .dt.year/.dt.month + groupby().size() work.
        close_dates = _pd.Series(_pd.to_datetime([t.exit_date for t in closed if t.exit_date]))
        mt_series = close_dates.groupby([close_dates.dt.year, close_dates.dt.month]).size()
        mt_lookup   = {(int(y), int(m)): int(c) for (y, m), c in mt_series.items()}

        for row_data in monthly_returns_out:
            yr = row_data['year']
            counts = [
                mt_lookup.get((yr, m), 0) if (yr, m) in eq_periods else None
                for m in range(1, 13)
            ]
            total_trades = sum(c for c in counts if c is not None)
            monthly_trades_out.append({'year': yr, 'months': counts, 'total': total_trades})

        # Yearly returns from monthly totals
        profits_by_year: dict = _defaultdict(list)
        for t in closed:
            if t.exit_date and t.profit is not None:
                profits_by_year[t.exit_date.year].append(float(t.profit))

        for row_data in monthly_returns_out:
            yr       = row_data['year']
            yr_profs = profits_by_year.get(yr, [])
            wins     = [p for p in yr_profs if p > 0]
            yearly_returns_out.append({
                'year':      yr,
                'pnl':       round(sum(yr_profs), 2),
                'pnl_pct':   row_data['total'],
                'trades':    len(yr_profs),
                'win_rate':  round(len(wins) / len(yr_profs) * 100, 1) if yr_profs else 0.0,
            })

    # ── 4. Summary metrics ─────────────────────────────────────────────────────
    profits   = [float(t.profit) for t in closed if t.profit is not None]
    wins      = [p for p in profits if p > 0]
    losses    = [p for p in profits if p < 0]
    hold_days = [t.day_count for t in closed if t.day_count]
    latest    = snaps[-1] if snaps else None

    return {
        'equity_series':    equity_series,
        'closed_trades':    closed_trades,
        'monthly_returns':  monthly_returns_out,
        'monthly_trades':   monthly_trades_out,
        'yearly_returns':   yearly_returns_out,
        'metrics': {
            'production_capital':    prod_cap,
            'total_return_pct':      round(float(latest.unrealised_pct) * 100, 2) if latest and latest.unrealised_pct else 0.0,
            'current_equity':        float(latest.equity) if latest and latest.equity else prod_cap,
            'max_drawdown_pct':      round(min((float(s.drawdown_pct) for s in snaps if s.drawdown_pct), default=0.0) * 100, 2),
            'current_drawdown_pct':  round(float(latest.drawdown_pct) * 100, 2) if latest and latest.drawdown_pct else 0.0,
            'win_rate_pct':          round(len(wins) / len(profits) * 100, 1) if profits else 0.0,
            'avg_hold_days':         round(sum(hold_days) / len(hold_days), 1) if hold_days else 0.0,
            'total_closed_pnl':      round(sum(profits), 2),
            'profit_factor':         round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else None,
            'total_trades':          len(profits),
        },
    }


@router.get("/{strategy_id}/substitution-scorecard")
def get_substitution_scorecard(strategy_id: int, db: _Session = _Depends(_get_db)):
    """Substitution scorecard — system hypothetical vs actual traded P&L.

    Pairs each SYSTEM-ledger row (engine's original pick) with the
    TRADED row actually executed. Completed trades show final P&L;
    open positions show current unrealised. Includes cumulative totals.
    """
    system_rows = (
        db.query(_Tradelist)
        .filter(
            _Tradelist.strategy_id == strategy_id,
            _Tradelist.ledger == 'SYSTEM',
        )
        .order_by(_Tradelist.intended_trade_date.desc())
        .all()
    )

    scorecard = []
    total_system_pnl = 0.0
    total_actual_pnl = 0.0

    for sys_row in system_rows:
        traded_row = None
        if sys_row.source_tag in ('SUBSTITUTE', 'ADJUSTED'):
            traded_row = (
                db.query(_Tradelist)
                .filter(
                    _Tradelist.substitute_link_id == sys_row.id,
                    _Tradelist.ledger == 'TRADED',
                )
                .first()
            )

        ovr = (
            db.query(_SubOvr)
            .filter(
                _SubOvr.strategy_id == strategy_id,
                _SubOvr.original_symbol == sys_row.symbol,
                _SubOvr.override_date == sys_row.intended_trade_date,
            )
            .order_by(_SubOvr.id.desc())
            .first()
        )

        sys_pnl    = float(sys_row.profit)    if sys_row.profit    is not None else None
        actual_pnl = float(traded_row.profit) if traded_row and traded_row.profit is not None else (
            0.0 if sys_row.source_tag in ('ELIDE', 'ELIDED') else None
        )

        diff = round((actual_pnl or 0.0) - (sys_pnl or 0.0), 2)

        if sys_pnl is not None:    total_system_pnl += sys_pnl
        if actual_pnl is not None: total_actual_pnl += actual_pnl

        scorecard.append({
            'date':              sys_row.intended_trade_date.isoformat() if sys_row.intended_trade_date else None,
            'action':            sys_row.source_tag,
            'original_symbol':   sys_row.symbol,
            'substitute_symbol': traded_row.symbol if traded_row else None,
            'status':            'open' if sys_row.status not in ('EXITED', 'CANCELLED', 'ELIDED') else 'closed',

            'system_pnl':        sys_pnl,
            'system_pnl_pct':    float(sys_row.profit_pct) if sys_row.profit_pct is not None else None,

            'actual_pnl':        actual_pnl,
            'actual_pnl_pct':    float(traded_row.profit_pct) if traded_row and traded_row.profit_pct is not None else None,
            'actual_qty':        traded_row.filled_qty if traded_row else None,

            'difference_pnl':    diff,
            'reason_for_action': ovr.reason_for_action if ovr else None,
        })

    total_diff = round(total_actual_pnl - total_system_pnl, 2)

    return {
        'rows': scorecard,
        'summary': {
            'total_system_pnl':  round(total_system_pnl, 2),
            'total_actual_pnl':  round(total_actual_pnl, 2),
            'total_difference':  total_diff,
            'decisions':         len(scorecard),
            'better_count':      sum(1 for r in scorecard if r['difference_pnl'] > 0),
            'worse_count':       sum(1 for r in scorecard if r['difference_pnl'] < 0),
        },
    }

# ── Patch 78: Recalculate live equity. Rebuilds live_equity_snapshot from the
# tradelist (mark-to-market: production_capital + realized + unrealized) on
# demand, triggered by the page's "Recalculate equity" button. The display
# endpoints (/live-performance, /live-equity) keep reading the table unchanged.
from fastapi import HTTPException as _HTTPException
from app.services.equity_recompute import recalc_and_store as _recalc_and_store


@router.post("/{strategy_id}/recalc-equity",
             summary="Rebuild live_equity_snapshot from the tradelist (Patch 78)")
def post_recalc_equity(strategy_id: int, db: _Session = _Depends(_get_db)):
    """Recompute every trading day's equity + drawdown from the tradelist and
    upsert them into live_equity_snapshot. Returns a summary for the UI."""
    try:
        summary = _recalc_and_store(db, strategy_id, adjusted=True)
        db.commit()
        return summary
    except Exception as e:
        db.rollback()
        raise _HTTPException(
            status_code=500,
            detail=f"recalc failed: {type(e).__name__}: {e}",
        )