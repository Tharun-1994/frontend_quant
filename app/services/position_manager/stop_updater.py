"""
stop_updater.py — Step C.5 of Position Manager (Path B, Patch 35).

Applies the single-bar endpoint's `stopUpdates` list. Each item carries a
new `current_stop_price` value for a LIVE position; this service writes it
to the tradelist row so tomorrow's broker_write submits the right bracket.

D3 trader-override semantics flow through unchanged:
  - If the trader edited current_stop_price via the F2 UI yesterday, the
    engine receives that value in seedHoldings (live_seed_builder reads
    tradelist.current_stop_price and sends it as LiveHoldings.currentStopPrice)
  - Engine echoes it back in stopUpdates with source='trader_override'
  - This service writes it back to current_stop_price (idempotent — same value)
  - If trader edits again tonight after PM runs, next nightly picks it up

When trader never overrode, engine computes the stop from the regime's
stoploss_pct and emits source='pct_recompute'. Same write path.

Pure write step — runner.py (C2.7) calls this inside the strategy's
transaction. No engine call, no parquet reads.

Phase 1 reality: with zero LIVE positions, stopUpdates is always []. Code
below is forward-compatible for when positions accumulate.

Engine response field consumed:
    stopUpdates: [
        {
          "tradeId":     str,      # echoes str(tradelist.id)
          "symbol":      str,
          "newStopPrice": float or null,   # null = no stop bracket needed
          "source":      str       # "trader_override" | "pct_recompute"
        }, ...
    ]
"""

from __future__ import annotations
from datetime import date
from decimal import Decimal
from typing import Any
from sqlalchemy.orm import Session

from app.models.tradelist import Tradelist


def apply_stop_updates(
    db: Session,
    stop_updates: list[dict[str, Any]],
    run_date: date,
) -> int:
    """Write engine-emitted stop values to LIVE / PENDING_EXIT rows.

    Args:
        db: SQLAlchemy session (caller owns transaction).
        stop_updates: engine response's `stopUpdates` list.
        run_date: data date (recorded for audit, not validated).

    Returns:
        Count of rows updated.

    Raises:
        ValueError on any tradeId mismatch (engine state diverged from SQL).
    """
    if not stop_updates:
        print('[stop_updater] empty stopUpdates, nothing to write')
        return 0

    print(f'[stop_updater] {len(stop_updates)} stop value(s) to apply '
          f'(run_date={run_date})')

    n_updated = 0
    for upd in stop_updates:
        n_updated += _apply_one_stop_update(db, upd)

    print(f'[stop_updater] wrote {n_updated} stop value(s)')
    return n_updated


def _apply_one_stop_update(db: Session, upd: dict[str, Any]) -> int:
    """Apply a single stop update. Returns 1 on success."""

    trade_id_str = upd.get('tradeId')
    if trade_id_str is None:
        raise ValueError(f'stopUpdates item missing tradeId: {upd!r}')

    try:
        row_id = int(trade_id_str)
    except (TypeError, ValueError):
        raise ValueError(
            f'stopUpdates contained non-integer tradeId={trade_id_str!r}. '
            f'PM only sends str(tradelist.id); engine should echo verbatim.'
        )

    row = db.query(Tradelist).filter(Tradelist.id == row_id).first()
    if row is None:
        raise ValueError(
            f'stopUpdates references tradeId={row_id} but no tradelist row '
            f'exists. Engine state diverged from SQL.'
        )

    # Engine only emits stopUpdates for LIVE positions that aren't exiting.
    # PENDING_EXIT is tolerable too (race: PM marked it PENDING_EXIT just
    # now in Step C, and engine had emitted a stop value before knowing).
    if row.status not in ('LIVE', 'PENDING_EXIT'):
        print(f'[stop_updater]   skipping tradeId={row_id}: status='
              f'{row.status!r} (not LIVE/PENDING_EXIT)')
        return 0

    new_stop = upd.get('newStopPrice')
    # Patch 108: engine also emits newTpPrice (daily TP maintenance).
    # .get() tolerates an older engine that doesn't send the key.
    new_tp = upd.get('newTpPrice')
    source = upd.get('source', 'unknown')

    if new_stop is None:
        # Engine explicitly emitted null → no broker stop bracket needed
        # for this position. Clear the field if it was set.
        row.current_stop_price = None
    else:
        row.current_stop_price = Decimal(str(new_stop))

    # Patch 108: TP maintenance value — same null-clears semantics.
    if new_tp is None:
        row.current_tp_price = None
    else:
        row.current_tp_price = Decimal(str(new_tp))

    db.flush()

    print(f'[stop_updater]   tradeId={row_id} {row.symbol}: '
          f'current_stop_price={new_stop} current_tp_price={new_tp} '
          f'(source={source})')

    return 1