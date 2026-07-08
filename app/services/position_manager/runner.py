"""
runner.py — C2.7 (top-level orchestrator for Position Manager)

Wires Steps A → B → C → D for one strategy inside a single SQL transaction.
Logs the whole run to eod_run_log. On any error, rolls back ALL SQL writes
(no partial state) and re-raises — caller (orchestrator C5) catches and
continues with other strategies.

Sequence:
    A. fill_resolver.resolve_fills(...)
         + apply outcomes (UPDATE PENDING_FILL → LIVE | CANCELLED)
    B. live_seed_builder.build_live_holdings_seed(...)
         + payload_builder.build_execution_step_payload(...)
            + HTTP POST /api/execution/signals/last-bar    (Path B single-bar endpoint)
    C. exit_applier.apply_exits(response.proposedExits, ...)
    C.5 stop_updater.apply_stop_updates(response.stopUpdates, ...)
    D. proposed_inserter.insert_proposed_rows(
           response.proposedEntries, response.activeRegimeOnLastBar, ...)

Why one big transaction: any partial state is wrong. If fill resolution
succeeds but the engine call fails, the LIVE rows we just promoted would
sit alone without their corresponding proposed-orders update. Rollback
restores PENDING_FILL state for retry. Engine call failures are LOUD —
no silent partial commits.
"""

from __future__ import annotations
from datetime import date, datetime, timedelta
import traceback
from typing import Any, Optional

import pandas_market_calendars as mcal
import requests
from sqlalchemy.orm import Session

from app.models.strategy_bucket import StrategyBucket
from app.models.market_regime import MarketRegime
from app.models.tradelist import Tradelist
from app.models.eod_run_log import EodRunLog
from app.Settings import settings

from app.services.position_manager.fill_resolver import (
    resolve_fills,
    resolve_stop_tp_hits,          # Patch 109
    apply_stop_tp_hits,            # Patch 109
    FillOutcome,
    resolve_exit_fills,
    ExitFillOutcome,
    resolve_hypothetical_fills,
    HypotheticalFillOutcome,
)
from app.services.position_manager.live_seed_builder import (
    build_live_holdings_seed,
)
from app.services.position_manager.payload_builder import (
    build_execution_step_payload,
)
from app.services.position_manager.exit_applier import apply_exits
from app.services.position_manager.stop_updater import apply_stop_updates   # Patch 33
from app.services.live_equity_writer import write_live_equity_snapshot
from app.services.position_manager.proposed_inserter import (
    insert_proposed_rows,
)


# HTTP timeout for engine call. Backtest baseline ~9s per RT's benchmark.
# 5 min should be generous enough even for slow universes; longer would
# mask real engine hangs.
ENGINE_HTTP_TIMEOUT_SEC = 300


def run_position_manager(
    db: Session,
    strategy_id: int,
    run_date: date,
    data_root: str,
) -> dict[str, Any]:
    """Run nightly PM for one strategy.

    Args:
        db: SQLAlchemy session.
        strategy_id: which strategy to process.
        run_date: data date — the bar that just closed (today in PM evening run).
        data_root: full absolute path to exec_data/{YYYYMMDD}/ folder.

    Returns:
        Summary dict with per-step counts plus eod_run_log id.

    Raises:
        On any sub-step exception. Transaction is rolled back, eod_run_log
        row is updated to FAILED, then re-raised to the caller.
    """
    # eod_run_log row created BEFORE work begins so crashes leave an audit trail.
    # status='RUNNING' until we explicitly set SUCCESS/FAILED.
    log_row = _create_eod_log_row(db, run_date, strategy_id)
    db.commit()   # commit the log row independently so it survives a rollback
                  # of the work transaction. log_row.id is now stable.

    # Patch 112: journal the pre-run state of every row this run may touch,
    # INSIDE the work transaction — journal and changes commit or roll back
    # together, so a FAILED run leaves no journal and nothing to revert.
    from app.services.position_manager.run_revert import (
        journal_pre_run_state, journal_created_rows,
    )
    journal_pre_run_state(db, log_row.id, strategy_id, run_date)

    summary: dict[str, Any] = {
        'eod_run_log_id': log_row.id,
        'strategy_id': strategy_id,
        'run_date': run_date.isoformat(),
        'fills_resolved': 0,
        'fills_cancelled': 0,
        'exits_applied': 0,
        'stop_updates_applied': 0,  # Patch 33
        'proposed_inserted': 0,
        'substitute_pool_inserted': 0,
        'proposed_deleted': 0,
        'active_regime_id': None,
        'exit_fills_resolved': 0,
        'stop_tp_hits_resolved': 0,   # Patch 109
        'hypothetical_fills_resolved': 0,
        # Patch 70: PORTFOLIO trip tracking
        'execution_disabled': False,
        'execution_disable_reason': None,
    }

    try:
        # Load strategy + first regime once. The first regime's universe is
        # the parquet folder PM reads (Phase 1 assumption: regimes share a
        # universe within a strategy). strategy.rebalance feeds the prefix.
        strategy = db.query(StrategyBucket).filter_by(id=strategy_id).first()
        if strategy is None:
            raise ValueError(f'Strategy id={strategy_id} not found')
        first_regime = (
            db.query(MarketRegime).filter_by(strategy_id=strategy_id)
            .order_by(MarketRegime.id.asc()).first()
        )
        if first_regime is None:
            raise ValueError(f'Strategy id={strategy_id} has no regimes')
        universe = first_regime.universe
        rebalance = strategy.rebalance

        print(f'[runner] === START strategy_id={strategy_id} ({strategy.name}) '
              f'run_date={run_date} universe={universe} rebalance={rebalance} ===')

        # ────────── Step A — fill resolution ──────────
        outcomes = resolve_fills(
            db,
            strategy_id=strategy_id,
            run_date=run_date,
            data_root=data_root,
            universe=universe,
            rebalance=rebalance,
        )
        _apply_fill_outcomes(db, outcomes, run_date)
        summary['fills_resolved']  = sum(1 for o in outcomes if o.filled)
        summary['fills_cancelled'] = sum(1 for o in outcomes if not o.filled)

        # ────────── Step A.5 — exit fill resolution ──────────
        # Reads EXIT_SUBMITTED rows whose exit_date == run_date,
        # looks up the exit open/close price, computes profit, flips EXITED.
        # Runs after entry fills so the parquets are already validated.
        exit_outcomes = resolve_exit_fills(
            db,
            strategy_id=strategy_id,
            run_date=run_date,
            data_root=data_root,
            universe=universe,
            rebalance=rebalance,
        )
        _apply_exit_fill_outcomes(db, exit_outcomes)
        summary['exit_fills_resolved'] = len(exit_outcomes)

        # ────────── Step A.6 — hypothetical fills for SYSTEM rows ──────────
        # Populates entry_price / exit_price / profit on SYSTEM-ledger rows
        # (original tickers that Vas elided or substituted). These are shadow
        # rows created by overlay_apply and carry the original symbol the engine
        # picked. Writing hypothetical P&L enables future substitution analysis:
        #   actual substitute profit  vs  hypothetical original profit.
        # Runs cross-strategy (universe-wide parquets) — non-fatal if it fails.
        try:
            hyp_outcomes = resolve_hypothetical_fills(
                db,
                run_date=run_date,
                data_root=data_root,
                universe=universe,
                rebalance=rebalance,
            )
            _apply_hypothetical_fill_outcomes(db, hyp_outcomes)
            summary['hypothetical_fills_resolved'] = len(hyp_outcomes)
        except Exception as e:
            print(f'[runner] WARNING: hypothetical fill resolution failed '
                  f'(non-fatal): {type(e).__name__}: {e}')
            summary['hypothetical_fills_resolved'] = 0

        # ────────── Step A.7 — stop / take-profit hit resolution (Patch 109) ──────────
        # The resting STP / TP LMT orders at IBKR are simulated against
        # run_date's bar (backtest-parity semantics: gap-open, else level
        # price; stop before TP). Runs BEFORE the engine call so a stopped-
        # out position is never seeded as a holding for today's decisions.
        # Loud-fail: a bad state here means live == broker is broken.
        hit_outcomes = resolve_stop_tp_hits(
            db,
            strategy_id=strategy_id,
            run_date=run_date,
            data_root=data_root,
            universe=universe,
            rebalance=rebalance,
        )
        apply_stop_tp_hits(db, hit_outcomes, run_date=run_date)
        summary['stop_tp_hits_resolved'] = len(hit_outcomes)

        # ────────── Step B — engine call ──────────
        live_holdings = build_live_holdings_seed(db, strategy_id=strategy_id)
        payload = build_execution_step_payload(
            db,
            strategy_id=strategy_id,
            run_date=run_date,
            live_holdings=live_holdings,
            data_root=data_root,
            execution_mode=True,   # live execution: use production_capital for sizing
        )
        engine_response = _call_engine(payload)

        # ────────── Step C — apply exits (Path B: proposedExits list) ──────────
        proposed_exits = engine_response.get('proposedExits') or []
        summary['exits_applied'] = apply_exits(
            db, proposed_exits=proposed_exits, run_date=run_date)

        # ────────── Step C.5 — apply stop updates (Path B: new step) ──────────
        # Engine emits per-LIVE-position stop value for tomorrow's broker
        # bracket. D3 trader overrides flow through here unchanged.
        stop_updates = engine_response.get('stopUpdates') or []
        summary['stop_updates_applied'] = apply_stop_updates(
            db, stop_updates=stop_updates, run_date=run_date)

        # ────────── Step C.7 Patch 70 — execution_enabled flip ──────────
        # Engine signals a PORTFOLIO stoploss trip via executionEnabledChange=False.
        # When present, flip strategy.execution_enabled=False so the next
        # nightly PM run is skipped (payload_builder.py:100 short-circuit).
        # proposedExits has already been processed above (LIVE → PENDING_EXIT);
        # broker_write will write SELL OPG rows tomorrow morning. After IBKR fills,
        # the strategy stays flat and halted until the user re-enables it from the UI.
        execution_change = engine_response.get('executionEnabledChange')
        if execution_change is False:
            disable_reason = (
                    engine_response.get('executionDisableReason')
                    or 'Portfolio stoploss tripped'
            )
            strategy.execution_enabled = False
            summary['execution_disabled'] = True
            summary['execution_disable_reason'] = disable_reason
            print(f'[runner] strategy_id={strategy_id} ({strategy.name}) — '
                  f'execution_enabled flipped FALSE: {disable_reason}')

        # ────────── Step D — insert PROPOSED + SUBSTITUTE_POOL ──────────
        # Engine emits full ranked list in `proposedEntries`. Middleware splits
        # into top free_slots (PROPOSED) + next substitute_pool_size (POOL).
        # Patch 70: when PORTFOLIO tripped, proposedEntries is empty (engine
        # cleared it). insert_proposed_rows is still called to clear stale
        # PROPOSED rows from yesterday — passing an empty list achieves that.
        proposed_entries = engine_response.get('proposedEntries') or []
        active_regime_label = engine_response.get('activeRegimeOnLastBar')
        intended_trade_date = _next_trading_day(run_date)

        d_result = insert_proposed_rows(
            db,
            strategy_id=strategy_id,
            intended_trade_date=intended_trade_date,
            proposed_orders=proposed_entries,  # parameter name kept for back-compat
            active_regime_label=active_regime_label,
            proposal_date=run_date,
        )
        summary['proposed_inserted']         = d_result['proposed_inserted']
        summary['substitute_pool_inserted']  = d_result['substitute_pool_inserted']
        summary['proposed_deleted']          = d_result['deleted']

        # Patch 112: journal the ids this run created (current PROPOSED/POOL
        # generation) so revert can delete exactly this run's output.
        journal_created_rows(db, log_row.id, strategy_id, run_date)
        summary['active_regime_id']          = d_result['active_regime_id']

        # ────────── Commit ──────────
        db.commit()

        # ────────── Step E — live equity snapshot ───────────────────────
        # Runs AFTER commit so a snapshot failure never rolls back PM work.
        # Writes one LiveEquitySnapshot row for today's close prices.
        try:
            equity_result = write_live_equity_snapshot(
                db=db,
                strategy_id=strategy_id,
                run_date=run_date,
                data_root=data_root,
                universe=universe,
                rebalance=rebalance,
            )
            db.commit()
            summary['equity_snapshot'] = equity_result
        except Exception as e:
            db.rollback()
            print(f'[runner] WARNING: live equity snapshot failed (non-fatal): '
                  f'{type(e).__name__}: {e}')
            summary['equity_snapshot'] = None

        # Update eod_run_log to SUCCESS in a separate mini-transaction
        rows_affected = (
                summary['fills_resolved'] + summary['fills_cancelled']
                + summary['exit_fills_resolved']
                + summary['exits_applied']
                + summary['stop_updates_applied']
                + summary['proposed_inserted'] + summary['substitute_pool_inserted']
        )
        _finalize_eod_log_row(db, log_row.id, status='SUCCESS',
                              rows_affected=rows_affected)

        print(f'[runner] === SUCCESS strategy_id={strategy_id} '
              f'fills={summary["fills_resolved"]}/{summary["fills_cancelled"]} '
              f'exit_fills={summary["exit_fills_resolved"]} '
              f'exits={summary["exits_applied"]} '
              f'stops={summary["stop_updates_applied"]} '
              f'proposed={summary["proposed_inserted"]}/{summary["substitute_pool_inserted"]} ===')

        return summary

    except Exception as e:
        db.rollback()
        err_msg = f'{type(e).__name__}: {e}\n{traceback.format_exc()}'
        try:
            _finalize_eod_log_row(db, log_row.id, status='FAILED',
                                  rows_affected=None, error_msg=err_msg)
        except Exception:
            # Don't mask the original exception if log-write fails.
            pass
        print(f'[runner] === FAILED strategy_id={strategy_id} — {type(e).__name__}: {e} ===')
        raise


def _apply_fill_outcomes(db: Session, outcomes: list[FillOutcome], run_date: date) -> None:
    """Translate fill outcomes into tradelist UPDATEs.

    Filled: status='LIVE', entry_* + fill_status populated.
    Cancelled: status='CANCELLED', entry_* stay NULL.
    """
    for o in outcomes:
        row = db.query(Tradelist).filter_by(id=o.row_id).first()
        if row is None:
            raise ValueError(
                f'FillOutcome references tradelist id={o.row_id} but row not '
                f'found. Concurrent deletion? Investigate.'
            )
        if row.status != 'PENDING_FILL':
            raise ValueError(
                f'tradelist id={o.row_id} expected status=PENDING_FILL but '
                f'got {row.status!r}. Race condition? Investigate.'
            )

        if o.filled:
            row.status        = 'LIVE'
            row.entry_date    = o.entry_date
            row.entry_price   = o.entry_price
            row.entry_timing  = o.entry_timing
            row.filled_qty    = row.intended_qty   # Phase 1: modeled full fill
            row.avg_fill_price = o.entry_price     # Phase 1: same as entry
            row.fill_status   = o.fill_status
        else:
            row.status        = 'CANCELLED'
            # entry_* columns stay NULL — order never filled

    db.flush()


def _apply_hypothetical_fill_outcomes(
    db: Session,
    outcomes: list[HypotheticalFillOutcome],
) -> None:
    """Apply hypothetical exit fill outcomes to SYSTEM-ledger rows.

    Writes exit_price, profit, profit_pct, day_count and flips status
    to 'EXITED' so the row is complete and queryable for analysis.
    Entry-only updates (Case 1 in resolve_hypothetical_fills) are applied
    directly to the ORM objects in resolve_hypothetical_fills — no separate
    apply function needed for those.
    """
    for o in outcomes:
        row = db.query(Tradelist).filter_by(id=o.row_id).first()
        if row is None:
            raise ValueError(
                f'HypotheticalFillOutcome references tradelist id={o.row_id} '
                f'but row not found.'
            )
        row.exit_price  = o.exit_price
        row.profit      = o.profit
        row.profit_pct  = o.profit_pct
        row.day_count   = o.day_count
        row.status      = 'EXITED'
        print(f'[runner] hypothetical exit id={o.row_id} {row.symbol}: '
              f'exit_price={o.exit_price:.4f} profit={o.profit:.2f} → EXITED')
    db.flush()


def _apply_exit_fill_outcomes(
    db: Session,
    outcomes: list[ExitFillOutcome],
) -> None:
    """Apply exit fill outcomes — flip EXIT_SUBMITTED → EXITED with P&L.

    Populates: exit_price, profit, profit_pct, day_count.
    exit_date and exit_reason were already set by exit_applier when the
    row was marked PENDING_EXIT. We just close the loop with the actual
    price and computed P&L.
    """
    for o in outcomes:
        row = db.query(Tradelist).filter_by(id=o.row_id).first()
        if row is None:
            raise ValueError(
                f'ExitFillOutcome references tradelist id={o.row_id} but row '
                f'not found. Concurrent deletion?'
            )
        if row.status != 'EXIT_SUBMITTED':
            raise ValueError(
                f'tradelist id={o.row_id} expected EXIT_SUBMITTED but got '
                f'{row.status!r}. Race condition?'
            )
        row.status     = 'EXITED'
        row.exit_price = o.exit_price
        row.profit     = o.profit
        row.profit_pct = o.profit_pct
        row.day_count  = o.day_count

        print(f'[runner] exit_fill id={o.row_id} {row.symbol}: '
              f'EXIT_SUBMITTED → EXITED '
              f'exit_price={o.exit_price:.4f} profit={o.profit:.2f}')

    db.flush()


def _call_engine(payload: dict[str, Any]) -> dict[str, Any]:
    """POST to /api/execution/signals/last-bar and return parsed JSON response.

    Raises on non-2xx, timeout, or JSON parse failure. The runner catches
    and rolls back the SQL transaction.
    """
    # Settings property is uppercase by convention (see app/Settings.py:58-60
    # and app/routes/backtest.py:42 for BACKTEST_DATA_PATH usage).
    url = f'{settings.BACKTEST_JAVA_URL}/api/execution/signals/last-bar'
    print(f'[runner] POST {url} (timeout={ENGINE_HTTP_TIMEOUT_SEC}s)')

    response = requests.post(
        url,
        json=payload,
        timeout=ENGINE_HTTP_TIMEOUT_SEC,
    )

    if response.status_code != 200:
        # Surface engine's error body in our exception — usually a clear
        # message like "LONGSHORT not supported" or a stack trace snippet.
        body_snippet = response.text[:500]
        raise RuntimeError(
            f'Engine returned HTTP {response.status_code}: {body_snippet}'
        )

    try:
        return response.json()
    except Exception as e:
        raise RuntimeError(
            f'Engine returned 200 but body is not valid JSON: '
            f'{response.text[:500]}'
        ) from e


def _create_eod_log_row(
    db: Session,
    run_date: date,
    strategy_id: int,
) -> EodRunLog:
    """Insert a RUNNING row at the start of the PM run. Returned for later
    SUCCESS/FAILED update.
    """
    row = EodRunLog(
        run_date=run_date,
        step='execution_step',
        strategy_id=strategy_id,
        status='RUNNING',
    )
    db.add(row)
    db.flush()
    return row


def _finalize_eod_log_row(
    db: Session,
    log_id: int,
    status: str,
    rows_affected: Optional[int] = None,
    error_msg: Optional[str] = None,
) -> None:
    """Update an existing eod_run_log row at end of run."""
    row = db.query(EodRunLog).filter_by(id=log_id).first()
    if row is None:
        return
    row.status        = status
    row.finished_at   = datetime.utcnow()
    row.rows_affected = rows_affected
    row.error_msg     = error_msg
    db.commit()


def _next_trading_day(ref: date) -> date:
    """First NYSE trading day strictly after `ref`. Mirrors payload_builder."""
    nyse = mcal.get_calendar('NYSE')
    valid = nyse.valid_days(ref, ref + timedelta(days=10)).tz_localize(None)
    forward = [d.date() for d in valid if d.date() > ref]
    return forward[0] if forward else ref + timedelta(days=1)


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        result = run_position_manager(
            db,
            strategy_id=27,
            run_date=date(2026, 6, 8),
            data_root=r'C:\Tharun\Projects\backtest_data\exec_data\20260608',
        )
        print('\n=== RESULT ===')
        for k, v in result.items():
            print(f'  {k}: {v}')
    finally:
        db.close()