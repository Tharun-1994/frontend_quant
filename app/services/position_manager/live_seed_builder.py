"""
live_seed_builder.py — C2.4

Builds the LIVE seedHoldings payload for the engine's
/api/execution/step/single call. Queries tradelist for one strategy's
LIVE rows on the TRADED ledger and maps each to the engine's
LiveHoldingsSeedDto shape.

Engine DTO contract (LiveHoldingsSeedDto.java, post-Patch 10):
    tradeId       String   — caller-supplied stable ID (we use str(tradelist.id))
    symbol        String
    direction     String   — "LONG" | "SHORT"
    entryDate     LocalDate — Jackson parses "YYYY-MM-DD" strings
    entryprice    float    — note: lowercase 'p' per Java field
    quantity      int
    capital       int
    entryTiming   String   — "open" | "intraday" | "close"
    entryReason   String   — audit string; backtest default is "Entries"
    pairId        Integer  — nullable; Phase 1 single-direction = null

D3: currentStopPrice IS sent when tradelist.current_stop_price is non-null.
Engine treats null = recompute (legacy path), non-null = use as the stop
level. Lets traders override the recomputed stop via the F2 UI; the engine
recomputes only when the override is null.
"""

from __future__ import annotations
from typing import Any
from sqlalchemy.orm import Session

from app.models.tradelist import Tradelist


def build_live_holdings_seed(db: Session, strategy_id: int) -> list[dict[str, Any]]:
    """Query LIVE rows on TRADED ledger for one strategy; return seedHoldings list.

    Args:
        db: SQLAlchemy session (caller owns transaction).
        strategy_id: which strategy's positions to seed. PM is per-strategy.

    Returns:
        List of dicts ready to drop into the engine's `liveHoldings` request
        field. Field names match LiveHoldingsSeedDto.java EXACTLY (note the
        lowercase 'p' in 'entryprice'). Empty list if no LIVE positions.

    Idempotency: pure SELECT, no writes. Safe to call multiple times.
    """
    live_rows = (
        db.query(Tradelist)
        .filter(
            Tradelist.strategy_id == strategy_id,
            Tradelist.ledger == 'TRADED',
            Tradelist.status == 'LIVE',
        )
        .order_by(Tradelist.entry_date.asc(), Tradelist.id.asc())
        .all()
    )

    seed = []
    for row in live_rows:
        seed.append(_map_row_to_seed_dto(row))
    return seed


def _map_row_to_seed_dto(row: Tradelist) -> dict[str, Any]:
    """Map one Tradelist row to a dict matching LiveHoldingsSeedDto shape.

    Defensive against NULL fill-time columns (entry_date, entry_price,
    filled_qty, entry_timing) — these should be populated when status='LIVE'
    but a row created mid-migration could lack them. We don't silently
    coerce to wrong values; raise so the bug surfaces.
    """
    # Defensive checks — LIVE rows must have these populated by Step A
    if row.entry_date is None:
        raise ValueError(
            f'Tradelist row id={row.id} has status=LIVE but entry_date is NULL — '
            f'inconsistent state, refusing to seed engine'
        )
    if row.entry_price is None:
        raise ValueError(
            f'Tradelist row id={row.id} has status=LIVE but entry_price is NULL'
        )
    if row.filled_qty is None:
        raise ValueError(
            f'Tradelist row id={row.id} has status=LIVE but filled_qty is NULL'
        )

    # Map. JSON field names match Java field names verbatim — Jackson uses
    # field names not getter conventions by default. The lowercase 'p' in
    # 'entryprice' is intentional, matching LiveHoldingsSeedDto:23.
    return {
        'tradeId':     str(row.id),
        'symbol':      row.symbol,
        'direction':   row.direction,
        'entryDate':   row.entry_date.isoformat(),       # "YYYY-MM-DD" string
        'entryprice':  float(row.entry_price),           # lowercase 'p'
        'quantity':    int(row.filled_qty),
        'capital':     int(row.intended_capital),
        'entryTiming': row.entry_timing or 'open',       # safe default
        'entryReason': 'Entries',                        # backtest default
        'pairId':      row.pair_id,                      # None for Phase 1
        # Patch 108: send the stop to the engine ONLY when the trader
        # explicitly overrode it (F2 UI sets stop_overridden). Pre-108 this
        # sent any non-null value — but stop_updater writes the nightly
        # recompute back into current_stop_price, so night 2 treated night
        # 1's computed value as a trader override and FROZE every stop at
        # its first value forever (all rows showed source=trader_override).
        # With the flag, non-overridden positions get a fresh ATR recompute
        # every night — the legacy daily behaviour.
        'currentStopPrice': (
            float(row.current_stop_price)
            if getattr(row, 'stop_overridden', False) and row.current_stop_price is not None
            else None
        ),
    }