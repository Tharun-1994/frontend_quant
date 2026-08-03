"""
exit_applier.py — C2.5 (Step C of Position Manager)

Path B (Patch 34): processes the single-bar endpoint's `proposedExits` list.
Engine emits one entry per LIVE position whose exit rule fired on the last
bar. No exit_price (broker hasn't filled yet — that's tomorrow morning's
broker_write job).

Pure write step — runner.py (C2.7) calls this inside the strategy's
transaction. No engine call here, no parquet reads.

State transition: LIVE → PENDING_EXIT
  - exit_date populated with intended trade date (= engine's `exitDate`)
  - exit_reason populated from engine's signal text
  - exit_price stays NULL (broker fills tomorrow)
  - status flips PENDING_EXIT → EXITED later, when morning broker_write
    confirms the fill (TODO follow-up patch: broker_write must learn to
    read PENDING_EXIT rows and submit MOO/MOC orders).

Engine response field consumed (Path B shape):
    proposedExits: [
        {
          "tradeId":    str,         # echoes str(tradelist.id)
          "symbol":     str,
          "exitReason": str,         # free-form rule text (engine evolution may vary)
          "exitDate":   "YYYY-MM-DD",
          "exitTiming": str          # "open" | "close" (echoed from regime)
        }, ...
    ]

Phase 1 reality: with zero LIVE positions, proposedExits is always [] and
this function returns 0. The code below is forward-compatible for when
positions accumulate.
"""

from __future__ import annotations
from datetime import date, datetime
from typing import Any
from sqlalchemy.orm import Session

from app.models.tradelist import Tradelist


# Path B: engine emits free-form exit reason text (e.g. "exit rule fired
# on 2026-06-12"). No fixed taxonomy — exit_applier just records whatever
# the engine sent.


def apply_exits(
    db: Session,
    proposed_exits: list[dict[str, Any]],
    run_date: date,
) -> int:
    """Apply engine-emitted exit signals to tradelist. Pure write step.

    Args:
        db: SQLAlchemy session (caller owns transaction; we issue UPDATEs
            but don't commit).
        proposed_exits: engine response's `proposedExits` list. Each item:
            {tradeId, symbol, exitReason, exitDate, exitTiming}.
        run_date: data date — recorded for audit, no longer the validation
            anchor (engine writes exitDate=next_trading_day).

    Returns:
        Count of rows transitioned LIVE → PENDING_EXIT.

    Raises:
        ValueError if engine emitted an exit for a tradeId that isn't a
        LIVE row in our DB (engine state diverged from SQL — must investigate).
    """
    if not proposed_exits:
        print('[exit_applier] empty proposedExits, no exits to apply')
        return 0

    print(f'[exit_applier] {len(proposed_exits)} exit signal(s) to mark '
          f'as PENDING_EXIT (run_date={run_date})')

    n_updated = 0
    for exit_signal in proposed_exits:
        n_updated += _apply_one_exit(db, exit_signal, run_date)

    print(f'[exit_applier] marked {n_updated} row(s) PENDING_EXIT')
    return n_updated


def _apply_one_exit(
    db: Session,
    exit_signal: dict[str, Any],
    run_date: date,
) -> int:
    """Mark a single LIVE row as PENDING_EXIT. Returns 1 on success."""

    trade_id_str = exit_signal.get('tradeId')
    if trade_id_str is None:
        raise ValueError(f'proposedExits item missing tradeId: {exit_signal!r}')

    # Convert the string tradeId back to int for the DB lookup
    try:
        row_id = int(trade_id_str)
    except (TypeError, ValueError):
        raise ValueError(
            f'proposedExits contained non-integer tradeId={trade_id_str!r}. '
            f'PM only sends str(tradelist.id); engine should echo verbatim.'
        )

    row = db.query(Tradelist).filter(Tradelist.id == row_id).first()
    if row is None:
        raise ValueError(
            f'proposedExits references tradeId={row_id} but no tradelist row '
            f'exists. Engine state diverged from SQL.'
        )

    # Sanity: row must currently be LIVE (engine only exits LIVE positions)
    if row.status != 'LIVE':
        raise ValueError(
            f'Engine returned exit for tradeId={row_id} but tradelist row '
            f'has status={row.status!r}, not LIVE. Race condition or '
            f'stale engine state.'
        )

    # Parse fields from engine signal. exit_date is the INTENDED execution
    # date (engine sets it = request.runDate = next_trading_day).
    exit_date = _parse_date(
        exit_signal['exitDate'], f'tradeId={row_id} exitDate')
    exit_reason = exit_signal.get('exitReason')

    # Apply the UPDATE — LIVE → PENDING_EXIT. exit_price/profit/day_count
    # stay NULL until broker_write submits the order and the morning
    # workflow records the actual fill.
    row.status = 'PENDING_EXIT'
    row.exit_date = exit_date
    row.exit_reason = exit_reason

    db.flush()

    print(f'[exit_applier]   tradeId={row_id} {row.symbol} {row.direction}: '
          f'LIVE → PENDING_EXIT (reason={exit_reason!r}, intended_date={exit_date})')

    return 1


def _parse_date(value: Any, context: str) -> date:
    """Parse engine's date field. Engine emits 'YYYY-MM-DD' strings via
    Jackson's default LocalDate serializer.
    """
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value)   # 'YYYY-MM-DD'
    raise ValueError(f'{context}: expected ISO date string, got {type(value).__name__}={value!r}')


# ---------------------------------------------------------------------------
# Manual smoke (only runs when this file is executed directly, not imported)
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    from app.database import SessionLocal

    # Synthetic tradeLogger payload — mimics what engine would return for
    # the LIVE AAPL row (id=1). Tests one exit + one no-op entry.
    fake_response_tradelogger = {
        '6': {
            'tradeId': '6',
            'symbol': 'AAPL',
            'direction': 'LONG',
            'entryDate': '2026-06-11',
            'entryPrice': 144.50,
            'quantity': 692,
            'exitDate': '2026-06-12',
            'exitPrice': 137.28,
            'exitReason': 'stop_loss',
            'exitTiming': 'intraday',
            'profit': -5009.84,
            'profitPercentage': -0.0501,
        },
        '999999': {
            'tradeId': '999999',
            'symbol': 'PHANTOM',
            'direction': 'LONG',
            'exitDate': None,   # no-op entry, no action
        },
    }

    db = SessionLocal()
    try:
        # NOTE: this WILL transition AAPL id=1 from LIVE → EXITED in your DB.
        # Comment out if you want to keep id=1 LIVE for further testing.
        n = apply_exits(db, fake_response_tradelogger, run_date=date(2026, 6, 12))
        print(f'Applied {n} exit(s)')
        db.commit()   # explicit commit for the smoke test — runner.py owns
                      # the transaction in production
    except Exception as e:
        db.rollback()
        print(f'Error: {e}')
        raise
    finally:
        db.close()