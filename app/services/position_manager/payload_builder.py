"""
payload_builder.py — C2.2 (engine request construction for PM)

Builds the JSON body for the engine's /api/execution/step/single endpoint.
Composes:
  - StrategyRequest (Pydantic, same shape engine uses for /runbacktestv3)
    populated from the ORM
  - regimes via db_to_pydantic (same converter the /marketregime GET uses)
  - LiveHoldings list (already in engine-DTO shape — comes from C2.4)
  - dataRoot (Patch 14 — absolute path to date-stamped exec_data folder)

Date semantics in the payload (locked design):
  - run_date (function param) = the data date = the bar that just closed
    Tuesday night PM run: run_date = Tuesday (Norgate just posted Tuesday's EOD)
  - strategy.end_date = run_date — engine's day loop processes through this bar
  - ExecutionStepRequestDto.runDate = next_trading_day(run_date) = the trade date
    Tuesday night PM run: runDate = Wednesday (PROPOSED orders target this day)

The engine ignores runDate today (the comment on ExecutionStepRequestDto says
"informational, not consumed"). We set it semantically anyway for audit:
eod_run_log can round-trip the trade date by reading it back.
"""

from __future__ import annotations
from datetime import date, timedelta
from typing import Any, Optional
import pandas_market_calendars as mcal
from sqlalchemy.orm import Session

from app.models.strategy_bucket import StrategyBucket
from app.models.market_regime import MarketRegime
from app.schemas.strategy import StrategyRequest
from app.routes.backtest import db_to_pydantic


# ── Execution-mode start-date floor ──────────────────────────────────────────
#
# Backtests use strategy.start_date from the DB (often 2000-01-01 to cover
# the strategy's full validated window). Execution doesn't need that depth —
# it only needs enough lookback to (a) warm up indicators on the last bar
# and (b) validate any LIVE positions held today.
#
# 2023-01-01 gives ~3.5 years of runway, comfortably exceeding the longest
# lookback any current strategy uses (252-day YoY-style indicators). Cuts
# nightly engine compute by ~80% vs a 2000-start backtest.
#
# Caveat: any LIVE position with entry_date < this floor would fail the
# engine's day-loop validation (can't "have entered" before the loop starts).
# Phase 1 has zero LIVE positions so this is safe. If a LIVE position ever
# predates the floor, fold a dynamic lower bound in here:
#   start = min(EXECUTION_START_DATE, min(live.entry_date) - timedelta(days=60))
EXECUTION_START_DATE = date(2023, 1, 1)


def build_execution_step_payload(
    db: Session,
    strategy_id: int,
    run_date: date,
    live_holdings: list[dict[str, Any]],
    data_root: str,
    test_start_date: Optional[date] = None,   # None = use EXECUTION_START_DATE
    execution_mode: bool = True,              # False = test mode: use regime.capital (backtest scale)
    allow_disabled: bool = False,             # Patch 147: combined-exec members are scouts (see combined/execute.py)
) -> dict[str, Any]:
    """Build the JSON-serializable dict for POST /api/execution/step/single.

    Args:
        db: SQLAlchemy session (read-only).
        strategy_id: which strategy to build the payload for. PM is per-strategy.
        run_date: data date (the bar that just closed). Becomes strategy.end_date
                  in the engine payload. PM resolves intended_trade_date == run_date
                  rows for fill checks.
        live_holdings: from live_seed_builder.build_live_holdings_seed(). Already
                       in engine LiveHoldingsSeedDto shape, ready to drop in.
        data_root: full absolute path to exec_data/{YYYYMMDD}/ folder.
                   Engine appends /{universe}/Filename.parquet to it.

    Returns:
        Dict matching engine's ExecutionStepRequestDto JSON shape. Serializable
        via json.dumps or requests.post(json=...).

    Raises:
        ValueError if strategy_id not found, has no regimes, or has unexpected
        system_type for Phase 1 (LONGSHORT rejected — pair execution is Phase 2).
    """
    # 1. Load strategy ORM
    strategy_orm = db.query(StrategyBucket).filter_by(id=strategy_id).first()
    if strategy_orm is None:
        raise ValueError(f'No StrategyBucket with id={strategy_id}')

    # Phase 1 contract: engine rejects LONGSHORT at the controller anyway,
    # but failing early in middleware gives a cleaner error message.
    if (strategy_orm.system_type or '').upper() == 'LONGSHORT':
        raise ValueError(
            f'Strategy id={strategy_id} has system_type=LONGSHORT. '
            f'Pair execution is Phase 2 — use the dedicated pair endpoint '
            f'when it lands.'
        )

    # Execution_enabled MUST be true at runtime — admin-controlled kill switch.
    # Patch 147: combined-execution members are the ONE sanctioned exception —
    # they generate candidates but never emit orders (the combined re-sizes
    # and is the sole emitter), so combined/execute.py passes
    # allow_disabled=True. Every other caller keeps the hard fail.
    if not strategy_orm.execution_enabled and not allow_disabled:
        raise ValueError(
            f'Strategy id={strategy_id} ({strategy_orm.name}) has '
            f'execution_enabled=False. PM should not have been invoked for it.'
        )

    # 2. Load regimes, convert to Pydantic via the same path /marketregime GET uses
    regime_orms = (
        db.query(MarketRegime)
        .filter_by(strategy_id=strategy_id)
        .order_by(MarketRegime.id.asc())
        .all()
    )
    if not regime_orms:
        raise ValueError(
            f'Strategy id={strategy_id} ({strategy_orm.name}) has no regimes. '
            f'PM cannot compute signals without at least one regime.'
        )
    regime_schemas = [db_to_pydantic(r) for r in regime_orms]

    # 3. Boundary-override (execution mode only): replace each regime's capital
    #    with production_capital so the engine's sd.getStartingCapital() returns
    #    the live execution figure.
    #    test mode (execution_mode=False): regime.capital stays as backtest capital
    #    so new entry quantities match the backtest tradelist for verification.
    if execution_mode:
        for regime_schema in regime_schemas:
            regime_schema.capital = _resolve_execution_capital(regime_schema, strategy_orm)

    # 3. Build StrategyRequest (same Pydantic that /runbacktestv3 consumes)
    #    start_date is the execution floor (NOT strategy.start_date — see
    #    the EXECUTION_START_DATE comment above). end_date is overridden
    #    to run_date — engine's day loop ends there, skip-last-bar guard
    #    captures D's proposedOrders for the trade date.
    strategy_req = StrategyRequest(
        id=strategy_orm.id,
        name=strategy_orm.name,
        rebalance=strategy_orm.rebalance,
        start_date=_resolve_start_date(test_start_date, live_holdings).isoformat(),
        end_date=run_date.isoformat(),
        min_quantity=int(strategy_orm.min_quantity or 0),
        min_price=float(strategy_orm.min_price or 0),
        system_type=strategy_orm.system_type,
        market_regime_type=strategy_orm.market_regime_type,
        production_capital=(
            float(strategy_orm.production_capital)
            if strategy_orm.production_capital is not None else None
        ),
        execution_enabled=bool(strategy_orm.execution_enabled),
        regimes=regime_schemas,
    )

    # 4. Compute the trade date (D = next NYSE trading day after data date)
    trade_date = _next_trading_day(run_date)

    # 5. Compose ExecutionStepRequestDto outer envelope
    payload = {
        'strategy':     strategy_req.to_dict(),  # jsonable_encoder handles dates/decimals
        'runDate':      trade_date.isoformat(),
        'liveHoldings': live_holdings,
        'dataRoot':     data_root,
    }

    print(
        f'[payload_builder] strategy_id={strategy_id} ({strategy_orm.name}) '
        f'run_date={run_date} trade_date={trade_date} '
        f'regimes={len(regime_schemas)} live_holdings={len(live_holdings)} '
        f'dataRoot={data_root!r}'
    )

    return payload


def _resolve_execution_capital(regime_schema, strategy_orm) -> float:
    """Patch 54: regime-only resolution. Raises if missing — no silent fallback.

    Per RT's "atomic moves over phased fallbacks" preference, the previous
    fallback chain (regime → strategy → regime.capital) is removed. With
    production_capital now edited per-regime in the UI (Patches 57+59), the
    only correct source is regime.production_capital.

    Backtest calls never reach payload_builder so this is execution-only.
    The engine's sd.getStartingCapital() reads regime.capital from the payload;
    overriding it here means the engine automatically uses production capital
    for perSlotCapital without any engine changes.
    """
    if getattr(regime_schema, 'production_capital', None) is not None:
        return float(regime_schema.production_capital)
    raise ValueError(
        f'Regime id={getattr(regime_schema, "id", "?")} (strategy '
        f'{strategy_orm.name}) has no production_capital. Execution requires '
        f'this set on every regime. Either set production_capital on the regime, '
        f'or flip strategy.execution_enabled=False.'
    )


def _resolve_start_date(
    test_start_date: Optional[date],
    live_holdings: list[dict[str, Any]],
) -> date:
    """Resolve engine start date, ensuring it predates all live positions.

    Uses test_start_date if supplied (test mode).
    Otherwise uses EXECUTION_START_DATE but lowers it dynamically if any
    live position entry_date is within 60 days of the floor — so the engine
    day loop always starts before the earliest held position.
    """
    if test_start_date is not None:
        return test_start_date
    if not live_holdings:
        return EXECUTION_START_DATE
    entry_dates = []
    for h in live_holdings:
        ed = h.get('entryDate')
        if ed:
            try:
                entry_dates.append(date.fromisoformat(ed))
            except (ValueError, TypeError):
                pass
    if not entry_dates:
        return EXECUTION_START_DATE
    earliest = min(entry_dates)
    dynamic_floor = earliest - timedelta(days=60)
    return min(EXECUTION_START_DATE, dynamic_floor)


def _next_trading_day(ref: date) -> date:
    """First NYSE trading day strictly after `ref`. Mirrors
    universe_pipeline._previous_trading_day style — same NYSE calendar.
    """
    nyse = mcal.get_calendar('NYSE')
    valid = nyse.valid_days(ref, ref + timedelta(days=10)).tz_localize(None)
    forward = [d.date() for d in valid if d.date() > ref]
    return forward[0] if forward else ref + timedelta(days=1)


# ---------------------------------------------------------------------------
# Smoke test (run directly to verify payload structure)
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import json
    from app.database import SessionLocal
    from app.services.position_manager.live_seed_builder import build_live_holdings_seed

    db = SessionLocal()
    try:
        # Build a payload exactly the way the runner will, for PullBack_X3_Sp500
        live = build_live_holdings_seed(db, strategy_id=27)
        payload = build_execution_step_payload(
            db,
            strategy_id=27,
            run_date=date(2026, 6, 8),                           # data date
            live_holdings=live,
            data_root=r'C:\Tharun\Projects\backtest_data\exec_data\20260608',
        )

        # Quick shape check — top-level keys + strategy field set
        print('\n--- payload top-level keys ---')
        for k, v in payload.items():
            if isinstance(v, dict):
                print(f'  {k}: dict with {len(v)} keys (first few: {list(v.keys())[:5]})')
            elif isinstance(v, list):
                print(f'  {k}: list of {len(v)} items')
            else:
                print(f'  {k}: {v!r}')

        print(f'\n--- strategy.regimes count: {len(payload["strategy"]["regimes"])}')
        if payload['strategy']['regimes']:
            r0 = payload['strategy']['regimes'][0]
            print(f'  first regime fields: {list(r0.keys())[:10]} ...')

        # Print the full payload for visual inspection (truncate if huge)
        s = json.dumps(payload, indent=2, default=str)
        if len(s) > 4000:
            print(f'\n--- payload JSON (first 4000 chars of {len(s)}) ---')
            print(s[:4000])
            print('... [truncated]')
        else:
            print(f'\n--- payload JSON ---')
            print(s)
    finally:
        db.close()