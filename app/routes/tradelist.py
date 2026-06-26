"""
tradelist.py — endpoints supporting the F2 (holdings + tradelist) and F3
(basket review) frontend pages.

Endpoints:
  GET  /api/tradelist/execution-enabled-strategies
       → list of execution_enabled strategies for the F2 dropdown.

  GET  /api/tradelist/strategy/{strategy_id}
       → all tradelist rows for one strategy (holdings + history).
       Optional query: ?status=LIVE,PENDING_FILL  ?limit=N  ?ledger=TRADED

  PATCH /api/tradelist/{row_id}/stop
       Body: { current_stop_price: float | null }
       → update the trader's stop override on a LIVE row.

  GET  /api/tradelist/basket/{trade_date}
       → all PROPOSED + SUBSTITUTE_POOL rows across execution_enabled
         strategies for a given intended_trade_date (F3).
"""

from __future__ import annotations
import logging
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import case   # Patch 41: SQL Server has no NULLS LAST
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.tradelist import Tradelist
from app.models.strategy_bucket import StrategyBucket
from app.models.market_regime import MarketRegime
from openpyxl import Workbook
from openpyxl.styles import Font
from fastapi.responses import StreamingResponse
import io

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tradelist", tags=["tradelist"])


# ── Response models ──────────────────────────────────────────────────────────


class ExecutionEnabledStrategy(BaseModel):
    id: int
    name: str
    system_type: str
    market_regime_type: str
    production_capital: Optional[float]


class TradelistRow(BaseModel):
    id: int
    strategy_id: int
    strategy_name: Optional[str]
    entered_regime_id: int
    ledger: str
    source_tag: str
    symbol: str
    direction: str
    status: str
    proposal_date: Optional[str]
    intended_trade_date: Optional[str]
    limit_price: Optional[float]
    intended_qty: Optional[int]
    intended_capital: Optional[float]
    initial_stop_price: Optional[float]
    initial_tp_price: Optional[float]
    current_stop_price: Optional[float]
    ranking_rank: Optional[int]
    ranking_value: Optional[float]
    entry_date: Optional[str]
    entry_price: Optional[float]
    entry_timing: Optional[str]
    filled_qty: Optional[int]
    avg_fill_price: Optional[float]
    fill_status: Optional[str]
    exit_date: Optional[str]
    exit_price: Optional[float]
    exit_reason: Optional[str]
    profit: Optional[float]


class StopPatchRequest(BaseModel):
    current_stop_price: Optional[float] = Field(
        None,
        description="The new stop level. Set null to clear (engine will then "
                    "fall back to entry × (1±stoploss_pct) recompute).",
    )


# ── Helpers ──────────────────────────────────────────────────────────────────


def _row_to_response(row: Tradelist, strategy_name: Optional[str]) -> TradelistRow:
    """Map a Tradelist ORM row to TradelistRow with stringified dates."""
    return TradelistRow(
        id=row.id,
        strategy_id=row.strategy_id,
        strategy_name=strategy_name,
        entered_regime_id=row.entered_regime_id,
        ledger=row.ledger,
        source_tag=row.source_tag,
        symbol=row.symbol,
        direction=row.direction,
        status=row.status,
        proposal_date=row.proposal_date.isoformat() if row.proposal_date else None,
        intended_trade_date=row.intended_trade_date.isoformat() if row.intended_trade_date else None,
        limit_price=float(row.limit_price) if row.limit_price is not None else None,
        intended_qty=row.intended_qty,
        intended_capital=float(row.intended_capital) if row.intended_capital is not None else None,
        initial_stop_price=float(row.initial_stop_price) if row.initial_stop_price is not None else None,
        initial_tp_price=float(row.initial_tp_price) if row.initial_tp_price is not None else None,
        current_stop_price=float(row.current_stop_price) if row.current_stop_price is not None else None,
        ranking_rank=row.ranking_rank,
        ranking_value=float(row.ranking_value) if row.ranking_value is not None else None,
        entry_date=row.entry_date.isoformat() if row.entry_date else None,
        entry_price=float(row.entry_price) if row.entry_price is not None else None,
        entry_timing=row.entry_timing,
        filled_qty=row.filled_qty,
        avg_fill_price=float(row.avg_fill_price) if row.avg_fill_price is not None else None,
        fill_status=row.fill_status,
        exit_date=row.exit_date.isoformat() if row.exit_date else None,
        exit_price=float(row.exit_price) if row.exit_price is not None else None,
        exit_reason=row.exit_reason,
        profit=float(row.profit) if row.profit is not None else None,
    )


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get(
    "/execution-enabled-strategies",
    response_model=list[ExecutionEnabledStrategy],
    summary="List execution_enabled strategies (for F2 dropdown)",
)
def list_execution_enabled_strategies(db: Session = Depends(get_db)):
    rows = (
        db.query(StrategyBucket)
        .filter(StrategyBucket.execution_enabled == True)
        .order_by(StrategyBucket.name.asc())
        .all()
    )
    return [
        ExecutionEnabledStrategy(
            id=s.id,
            name=s.system_code,
            system_type=s.system_type or "",
            market_regime_type=s.market_regime_type or "",
            production_capital=(
                float(s.production_capital) if s.production_capital is not None else None
            ),
        )
        for s in rows
    ]


@router.get(
    "/strategy/{strategy_id}",
    response_model=list[TradelistRow],
    summary="Tradelist rows for one strategy (F2)",
)
def get_tradelist_for_strategy(
    strategy_id: int,
    status: Optional[str] = Query(
        None,
        description="Comma-separated status filter, e.g. 'LIVE,PENDING_FILL'. "
                    "Default: all statuses.",
    ),
    ledger: str = Query("TRADED", description="TRADED | SYSTEM | ALL"),
    limit: int = Query(500, ge=1, le=5000),
    db: Session = Depends(get_db),
):
    strategy = db.query(StrategyBucket).filter_by(id=strategy_id).first()
    if strategy is None:
        raise HTTPException(404, detail=f"Strategy id={strategy_id} not found")

    q = db.query(Tradelist).filter(Tradelist.strategy_id == strategy_id)
    if ledger != "ALL":
        q = q.filter(Tradelist.ledger == ledger)
    if status:
        status_list = [s.strip().upper() for s in status.split(",") if s.strip()]
        q = q.filter(Tradelist.status.in_(status_list))

    # Recent first — entry_date for LIVE/EXITED, intended_trade_date for PROPOSED/PENDING
    rows = q.order_by(Tradelist.id.desc()).limit(limit).all()
    return [_row_to_response(r, strategy.name) for r in rows]


@router.patch("/{row_id}/stop", response_model=TradelistRow, summary="Set / clear trader stop override on a LIVE row (F2)",)
def patch_current_stop_price( row_id: int, request: StopPatchRequest = Body(...),db: Session = Depends(get_db),):
    row = db.query(Tradelist).filter_by(id=row_id).first()
    if row is None:
        raise HTTPException(404, detail=f"Tradelist row id={row_id} not found")

    # Phase 1 contract: only LIVE rows on TRADED ledger can have their stop edited.
    # PROPOSED/PENDING_FILL stops are governed by the proposed-orders capture path.
    if row.ledger != "TRADED":
        raise HTTPException(
            409,
            detail=f"Tradelist row id={row_id} is on ledger={row.ledger!r}; "
                   f"trader stop overrides apply to TRADED ledger only.",
        )
    if row.status != "LIVE":
        raise HTTPException(
            409,
            detail=f"Tradelist row id={row_id} has status={row.status!r}; "
                   f"trader stop overrides apply to LIVE rows only.",
        )

    new_value = request.current_stop_price
    if new_value is not None and new_value <= 0:
        raise HTTPException(
            400, detail=f"current_stop_price must be positive (got {new_value})"
        )

    row.current_stop_price = (
        Decimal(str(new_value)) if new_value is not None else None
    )
    db.commit()
    db.refresh(row)

    strategy = db.query(StrategyBucket).filter_by(id=row.strategy_id).first()
    logger.info(
        f"[tradelist] row_id={row_id} strategy={strategy.name if strategy else '?'} "
        f"symbol={row.symbol} current_stop_price set to {new_value}"
    )
    return _row_to_response(row, strategy.name if strategy else None)


@router.get("/basket/{trade_date}",response_model=list[TradelistRow],summary="PROPOSED + SUBSTITUTE_POOL rows for a trade_date (F3)",)
def get_basket_for_date(trade_date: date, db: Session = Depends(get_db),):
    rows = (
        db.query(Tradelist)
        .filter(
            Tradelist.intended_trade_date == trade_date,
            Tradelist.ledger == "TRADED",
            Tradelist.status.in_(["PROPOSED", "SUBSTITUTE_POOL"]),
        )
        .order_by(
            Tradelist.strategy_id.asc(),
            # Patch 41: portable NULLS LAST — SQL Server doesn't accept the
            # 'NULLS LAST' keyword. CASE returns 0 for non-null, 1 for null,
            # so ASC sort puts non-null first.
            case((Tradelist.ranking_rank.is_(None), 1), else_=0).asc(),
            Tradelist.ranking_rank.asc(),
        )
        .all()
    )

    # Bulk-resolve strategy names
    strategy_ids = {r.strategy_id for r in rows}
    name_map = {
        s.id: s.name
        for s in db.query(StrategyBucket).filter(StrategyBucket.id.in_(strategy_ids)).all()
    } if strategy_ids else {}

    return [_row_to_response(r, name_map.get(r.strategy_id)) for r in rows]



@router.get(
    "/basket-csv/{trade_date}",
    summary="Download combined IBKR basket CSV (M_Combined_YYYYMMDD.csv)",
    response_class=Response,
)
def download_combined_basket_csv(
    trade_date: date,
    db: Session = Depends(get_db),
):
    """Returns 18-column IBKR Basket Trader CSV for trade_date.

    Includes every PROPOSED row on TRADED ledger where the parent strategy
    has execution_enabled=True. SUBSTITUTE_POOL rows are excluded (those are
    backups, not the actual basket sent to IBKR).

    Filename: M_Combined_YYYYMMDD.csv

    Empty basket (no PROPOSED rows for this date) returns headers-only CSV
    with HTTP 200 — frontend can still download an empty template.
    """
    from app.services.position_manager.broker_basket_builder import (
        build_combined_basket,
        basket_to_csv_string,
    )

    basket = build_combined_basket(db, trade_date)
    csv_text = basket_to_csv_string(basket)

    filename = f"M_Combined_{trade_date.strftime('%Y%m%d')}.csv"
    logger.info(
        "[basket-csv] trade_date=%s rows=%d filename=%s",
        trade_date, len(basket), filename,
    )

    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )

@router.get(
    "/basket-csv-sub/{trade_date}",
    summary="Download substitute pool reference CSV (M_Combined_YYYYMMDD_SUB.csv)",
    response_class=Response,
)
def download_substitute_basket_csv(
    trade_date: date,
    db: Session = Depends(get_db),
):
    """Returns SUBSTITUTE_POOL rows in 19-column IBKR basket format for trade_date.

    Same layout as the main basket. OrderId/ParentOrderId/OCAGroup blank —
    reference-only for Vas to identify substitute_symbol values when filling
    the morning substitution CSV. Not loaded into IBKR directly.

    Filename: M_Combined_YYYYMMDD_SUB.csv
    """
    from app.services.position_manager.broker_basket_builder import (
        build_substitute_basket,
        substitute_to_csv_string,
    )

    basket = build_substitute_basket(db, trade_date)
    csv_text = substitute_to_csv_string(basket)

    filename = f"M_Combined_{trade_date.strftime('%Y%m%d')}_SUB.csv"
    logger.info(
        "[basket-csv-sub] trade_date=%s rows=%d filename=%s",
        trade_date, len(basket), filename,
    )

    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )

@router.get(
    "/latest-basket-date",
    summary="Most recent intended_trade_date with any PROPOSED row",
)
def latest_basket_date(db: Session = Depends(get_db)):
    """Returns the most recent trade_date that has PROPOSED basket rows.

    Used by F2 'All Systems' view so it always shows the freshest basket
    regardless of wall-clock timing. Pre-22:00 (Norgate hasn't updated yet)
    the latest is yesterday's run targeting today's trade date; post-22:30
    (tonight's nightly fired) the latest is tonight's run targeting
    tomorrow's trade date.

    Returns {"trade_date": "YYYY-MM-DD"} or {"trade_date": null} when no
    PROPOSED rows exist (Phase 0, before any nightly has run).
    """
    latest = (
        db.query(Tradelist.intended_trade_date)
        .filter(
            Tradelist.ledger == "TRADED",
            Tradelist.status.in_(["PROPOSED", "PENDING_FILL"]),
        )
        .order_by(Tradelist.intended_trade_date.desc())
        .limit(1)
        .scalar()
    )
    return {"trade_date": latest.isoformat() if latest else None}

@router.get(
    "/basket-xlsx/{trade_date}",
    summary="Download M_Combined XLSX from disk (authoritative file for IBKR)",
    response_class=Response,
)
def download_combined_basket_xlsx(
    trade_date: date,
    db: Session = Depends(get_db),
):
    """Serves the M_Combined_{YYYYMMDD}.xlsx written by broker_write.
    This is the authoritative file — includes STP stop rows, exits and entries.
    Returns 404 if broker_write has not been run for this date yet.
    """
    from fastapi.responses import FileResponse
    from app.constants.PricePath import PricePath

    file_path = (
        Path(PricePath.backtestPath)
        / 'broker_output'
        / trade_date.strftime('%Y%m%d')
        / f'M_Combined_{trade_date.strftime("%Y%m%d")}.xlsx'
    )
    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f'M_Combined XLSX not found for {trade_date}. '
                   f'Run morning basket first (trigger from EOD Run History page).'
        )
    filename = f'M_Combined_{trade_date.strftime("%Y%m%d")}.xlsx'
    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )


# ── latest-basket-date ────────────────────────────────────────────────────────

# ── holdings-xlsx ─────────────────────────────────────────────────────────────

@router.get(
    "/holdings-xlsx",
    summary="Download all LIVE positions across execution-enabled strategies as XLSX",
)
def download_holdings_xlsx(db: Session = Depends(get_db)):
    """Returns M_Holdings_{today}.xlsx with all LIVE rows across all
    execution-enabled strategies.

    Columns: ticker, quantity, price (entry_price), trade_status,
             strategy (system_code or name), entry_date

    Matches legacy M_holdings_format_1_{YYYYMMDD}.xlsx format.
    """
    from datetime import date as _date
    today = _date.today()

    live_rows = (
        db.query(Tradelist, StrategyBucket)
        .join(StrategyBucket, Tradelist.strategy_id == StrategyBucket.id)
        .filter(
            Tradelist.ledger == 'TRADED',
            Tradelist.status == 'LIVE',
            StrategyBucket.execution_enabled == True,
        )
        .order_by(StrategyBucket.name, Tradelist.entry_date)
        .all()
    )

    wb = Workbook()
    ws = wb.active
    ws.title = 'Holdings'

    headers = ['ticker', 'quantity', 'price', 'trade_status', 'strategy', 'entry_date']
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for tl, strat in live_rows:
        # Use system_code as strategy label if set, else name
        strategy_label = strat.system_code or strat.name
        direction      = (tl.direction or 'LONG').lower()
        ws.append([
            tl.symbol,
            int(tl.filled_qty or tl.intended_qty or 0),
            float(tl.entry_price) if tl.entry_price else '',
            direction,
            strategy_label,
            tl.entry_date.isoformat() if tl.entry_date else '',
        ])

    # Auto-size columns
    for col in ws.columns:
        max_len = max((len(str(c.value or '')) for c in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = max(12, min(30, max_len + 2))

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = f'M_Holdings_{today.strftime("%Y%m%d")}.xlsx'
    return StreamingResponse(
        buf,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )

@router.get(
    "/all-live-holdings",
    response_model=list[TradelistRow],
    summary="All LIVE rows across every execution-enabled strategy",
)
def get_all_live_holdings(db: Session = Depends(get_db)):
    """Returns all LIVE tradelist rows across all execution-enabled strategies.
    Used by the All Systems view in HoldingsAndTradesPage to show combined holdings."""
    live_rows = (
        db.query(Tradelist, StrategyBucket)
        .join(StrategyBucket, Tradelist.strategy_id == StrategyBucket.id)
        .filter(
            Tradelist.ledger == 'TRADED',
            Tradelist.status == 'LIVE',
            StrategyBucket.execution_enabled == True,
        )
        .order_by(StrategyBucket.name, Tradelist.entry_date)
        .all()
    )
    return [_row_to_response(tl, strat.name) for tl, strat in live_rows]