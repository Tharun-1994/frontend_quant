"""
proposed_inserter.py — C2.6 (Step D of Position Manager)

Path B (Patch 36): takes the single-bar endpoint's proposedEntries[]
(full ranked list) + activeRegimeOnLastBar and:
  1. Resolves which regime was active on the last bar.
  2. Computes free_slots = regime.slots - currently LIVE positions count
     (after Step C exits have been applied — caller's responsibility).
  3. Idempotently deletes any prior PROPOSED + SUBSTITUTE_POOL rows for
     (strategy, intended_trade_date) on the TRADED ledger.
  4. Inserts top `free_slots` candidates as PROPOSED, next
     `regime.substitute_pool_size` as SUBSTITUTE_POOL.

ProposedEntryDto field mapping → tradelist columns:
    symbol         → symbol
    direction      → direction
    orderType      → (not persisted; broker_write decides MKT vs STPMOC)
    limitPrice     → limit_price (null/0 for NORMAL; >0 for LIMIT/LIMIT_ATR)
    stopPrice      → initial_stop_price (null when stoploss disabled)
    rank           → ranking_rank
    score          → ranking_value
    quantity       → intended_qty
    capital        → intended_capital
    sector         → (not persisted; engine-side audit only)
    entryDate, entryTiming, entryReason → (not persisted; informational)

initial_tp_price stays NULL — the Path B response doesn't carry TP fields.
Strategies that use TP brackets will need a follow-up patch.

Empty proposed_entries is a valid input — engine may legitimately return
no candidates (no entry signals fire, all candidates already LIVE). C2.6
still runs the DELETE for idempotency then inserts 0 rows.
"""

from __future__ import annotations
from datetime import date
from decimal import Decimal
from typing import Any, Optional
from sqlalchemy.orm import Session

from app.models.tradelist import Tradelist
from app.models.market_regime import MarketRegime
from app.services.position_manager.regime_label import (
    compute_regime_label_from_tree,
)


def insert_proposed_rows(
    db: Session,
    strategy_id: int,
    intended_trade_date: date,
    proposed_orders: list[dict[str, Any]],
    active_regime_label: Optional[str],
    proposal_date: date,
) -> dict[str, int]:
    """Insert PROPOSED + SUBSTITUTE_POOL rows for one strategy.

    Args:
        db: SQLAlchemy session (caller owns transaction).
        strategy_id: which strategy these proposed orders belong to.
        intended_trade_date: the trade date for the new rows (typically
                             next_trading_day(run_date)).
        proposed_orders: engine's response.proposedEntries. List of dicts
                         matching the ProposedEntryDto JSON shape.
                         (Parameter name kept as proposed_orders for
                         backward compat with callers; content is now
                         ProposedEntryDto-shaped per Path B / Patch 36.)
        active_regime_label: engine's response.activeRegimeOnLastBar. The
                             string label of the regime that fired on the
                             last bar. None/empty for single-regime
                             strategies with empty market_trend_rules.
        proposal_date: the data date — the night PM created these rows.

    Returns:
        Dict with counts:
          {
            'deleted': N,                  # prior PROPOSED+POOL rows removed
            'proposed_inserted': N,        # new PROPOSED rows
            'substitute_pool_inserted': N, # new SUBSTITUTE_POOL rows
            'active_regime_id': int,       # the resolved regime
          }

    Raises:
        ValueError if active regime can't be resolved (multi-regime strategy
        whose computed labels don't match engine's activeRegimeOnLastBar).
    """
    # 1. Resolve the active regime
    active_regime = _resolve_active_regime(db, strategy_id, active_regime_label)

    # 2. Idempotency: remove prior PROPOSED + SUBSTITUTE_POOL rows for this
    #    (strategy, intended_trade_date) before inserting fresh. TRADED ledger
    #    only — SYSTEM ledger audit shadows are untouched.
    deleted = (
        db.query(Tradelist)
        .filter(
            Tradelist.strategy_id == strategy_id,
            Tradelist.ledger == 'TRADED',
            Tradelist.intended_trade_date == intended_trade_date,
            Tradelist.status.in_(['PROPOSED', 'SUBSTITUTE_POOL']),
        )
        .delete(synchronize_session=False)
    )
    if deleted:
        print(f'[proposed_inserter] deleted {deleted} prior PROPOSED/POOL row(s) '
              f'for strategy_id={strategy_id} intended_trade_date={intended_trade_date}')

    # Empty proposedOrders is valid — common pre-Patch 16 for NORMAL strategies
    if not proposed_orders:
        print(f'[proposed_inserter] empty proposed_orders, no new rows to insert')
        db.flush()
        return {
            'deleted': deleted,
            'proposed_inserted': 0,
            'substitute_pool_inserted': 0,
            'active_regime_id': active_regime.id,
        }

    # 3. Compute free_slots = regime.slots - currently-LIVE positions for this
    #    strategy on TRADED ledger. Caller must have applied Step C exits already.
    live_count = (
        db.query(Tradelist)
        .filter(
            Tradelist.strategy_id == strategy_id,
            Tradelist.ledger == 'TRADED',
            Tradelist.status == 'LIVE',
        )
        .count()
    )
    free_slots = max(0, int(active_regime.slots or 0) - live_count)
    pool_size = int(active_regime.substitute_pool_size or 0)
    print(f'[proposed_inserter] strategy_id={strategy_id} active_regime_id='
          f'{active_regime.id} slots={active_regime.slots} live={live_count} '
          f'free_slots={free_slots} substitute_pool_size={pool_size} '
          f'proposed_orders={len(proposed_orders)}')

    # 4. Split: top free_slots → PROPOSED, next pool_size → SUBSTITUTE_POOL
    proposed_slice = proposed_orders[:free_slots]
    pool_slice = proposed_orders[free_slots:free_slots + pool_size]

    # 5. Insert
    proposed_inserted = 0
    for order in proposed_slice:
        row = _build_tradelist_row(
            order, strategy_id, active_regime.id,
            intended_trade_date, proposal_date, status='PROPOSED',
        )
        db.add(row)
        proposed_inserted += 1

    pool_inserted = 0
    for order in pool_slice:
        row = _build_tradelist_row(
            order, strategy_id, active_regime.id,
            intended_trade_date, proposal_date, status='SUBSTITUTE_POOL',
        )
        db.add(row)
        pool_inserted += 1

    db.flush()   # caller commits

    print(f'[proposed_inserter] inserted {proposed_inserted} PROPOSED + '
          f'{pool_inserted} SUBSTITUTE_POOL row(s)')

    return {
        'deleted': deleted,
        'proposed_inserted': proposed_inserted,
        'substitute_pool_inserted': pool_inserted,
        'active_regime_id': active_regime.id,
    }


def _resolve_active_regime(
    db: Session,
    strategy_id: int,
    active_regime_label: Optional[str],
) -> MarketRegime:
    """Find the regime that was active on the last bar.

    Single-regime strategy: ignore the label, use the only regime.
    Multi-regime strategy: compute label per regime via C2.1, match the engine's.

    Raises ValueError if no match found in the multi-regime case.
    """
    regimes = (
        db.query(MarketRegime)
        .filter_by(strategy_id=strategy_id)
        .order_by(MarketRegime.id.asc())
        .all()
    )
    if not regimes:
        raise ValueError(f'Strategy id={strategy_id} has no regimes')

    if len(regimes) == 1:
        return regimes[0]

    # Multi-regime — match by computed label
    engine_label = (active_regime_label or '').strip()
    for regime in regimes:
        import json
        tree_json = regime.market_trend_rules_tree_json
        tree = json.loads(tree_json) if tree_json else None
        regime_label = compute_regime_label_from_tree(tree)
        if regime_label == engine_label:
            return regime

    raise ValueError(
        f'Strategy id={strategy_id} has {len(regimes)} regimes but none match '
        f'engine activeRegimeOnLastBar={active_regime_label!r}. Label '
        f'computation may have drifted from engine. Regime IDs: '
        f'{[r.id for r in regimes]}'
    )


def _build_tradelist_row(
    order: dict[str, Any],
    strategy_id: int,
    regime_id: int,
    intended_trade_date: date,
    proposal_date: date,
    status: str,
) -> Tradelist:
    """Map one ProposedEntryDto dict to a Tradelist row (Path B / Patch 36)."""

    # Required field — symbol (was: ticker on the LimitOrder DTO)
    ticker = order.get('symbol')
    if not ticker:
        raise ValueError(f'ProposedEntry missing symbol: {order!r}')

    limit_price = order.get('limitPrice', 0.0)
    if limit_price is None:
        limit_price = 0.0

    # ProposedEntryDto fields. quantity/capital/stopPrice/score are the
    # renamed equivalents of the old LimitOrder rich fields (intendedQty,
    # intendedCapital, initialStopPrice, rankingValue).
    direction        = order.get('direction')
    rank             = order.get('rank')
    intended_qty     = order.get('quantity')
    intended_capital = order.get('capital')
    initial_stop     = order.get('stopPrice')
    initial_tp       = None                      # Not in Path B response shape
    ranking_value    = order.get('score')

    if direction is None:
        raise ValueError(
            f'ProposedEntry for {ticker} missing direction field. '
            f'Engine must populate this — bug.'
        )
    if intended_qty is None:
        raise ValueError(
            f'ProposedEntry for {ticker} missing quantity field. '
            f'Engine must populate this — bug.'
        )
    if intended_capital is None:
        raise ValueError(
            f'ProposedEntry for {ticker} missing capital field. '
            f'Engine must populate this — bug.'
        )

    return Tradelist(
        strategy_id=strategy_id,
        entered_regime_id=regime_id,
        substitute_link_id=None,           # Phase 1: no substitutions until Phase D
        pair_id=None,                      # Phase 1: single-direction
        ledger='TRADED',                   # everything PM creates goes to TRADED ledger
        source_tag='SYSTEM',               # system-generated (vs trader substitutions)
        symbol=ticker,
        direction=direction,
        status=status,
        proposal_date=proposal_date,
        intended_trade_date=intended_trade_date,
        limit_price=Decimal(str(limit_price)),
        intended_qty=int(intended_qty),
        intended_capital=Decimal(str(intended_capital)),
        initial_stop_price=Decimal(str(initial_stop)) if initial_stop is not None else None,
        initial_tp_price=Decimal(str(initial_tp)) if initial_tp is not None else None,
        ranking_rank=int(rank) if rank is not None else None,
        ranking_value=Decimal(str(ranking_value)) if ranking_value is not None else None,
        # Fill/exit columns left NULL; populated later in their lifecycle
    )


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    from app.database import SessionLocal

    # Synthetic engine response — mimics what /api/execution/step/single returns
    # post-Patch 15. Two LimitOrder entries with rich fields populated.
    fake_proposed_orders = [
        {
            'ticker': 'AAPL', 'limitPrice': 144.50,
            'direction': 'LONG', 'rank': 1,
            'intendedQty': 692, 'intendedCapital': 100000.0,
            'referenceClose': 150.52, 'initialStopPrice': 143.00,
            'initialTpPrice': None, 'rankingValue': 22.5,
        },
        {
            'ticker': 'MSFT', 'limitPrice': 388.50,
            'direction': 'LONG', 'rank': 2,
            'intendedQty': 257, 'intendedCapital': 100000.0,
            'referenceClose': 405.00, 'initialStopPrice': 384.75,
            'initialTpPrice': None, 'rankingValue': 24.1,
        },
    ]

    db = SessionLocal()
    try:
        result = insert_proposed_rows(
            db,
            strategy_id=27,
            intended_trade_date=date(2026, 6, 9),    # next trading day after 6/8
            proposed_orders=fake_proposed_orders,
            active_regime_label='',                  # single-regime strategy
            proposal_date=date(2026, 6, 8),          # data date
        )
        print('Result:', result)
        db.commit()
    except Exception as e:
        db.rollback()
        print(f'Error: {type(e).__name__}: {e}')
        raise
    finally:
        db.close()