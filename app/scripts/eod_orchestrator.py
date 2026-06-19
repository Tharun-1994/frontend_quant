"""eod_orchestrator.py — nightly EOD pipeline entry point.

Runs the post-universe-pipeline portion of the nightly chain:
  1. exec_data_refresh — recompute exec_data parquets (universe-scope step)
  2. Per execution_enabled strategy: run_position_manager (per-strategy step)

PREREQUISITE: universe_pipeline.py must have already refreshed Folder A.
Run that separately (or via its own Task Scheduler entry) BEFORE this script.

Failure handling (per Gap 6 decision):
  - exec_data_refresh failure → ABORT (PM cannot run without parquets)
  - Per-strategy PM failure → LOG + CONTINUE with next strategy

eod_run_log writes:
  - One row for exec_data_refresh (universe-scope, strategy_id=NULL)
  - One row per strategy for execution_step (written by PM runner itself)

Exit codes:
  0 — all strategies succeeded
  1 — exec_data_refresh failed
  2 — one or more PM runs failed (rest continued)
  3 — unexpected error

Usage:
    python -m app.scripts.eod_orchestrator
    python -m app.scripts.eod_orchestrator --run-date 2026-06-08
    python -m app.scripts.eod_orchestrator --strategy 27
    python -m app.scripts.eod_orchestrator --skip-exec-data --strategy 27
"""

from __future__ import annotations
import argparse
import logging
import sys
import traceback
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.eod_run_log import EodRunLog
from app.models.strategy_bucket import StrategyBucket
from app.services.exec_data_refresh import run_exec_data_refresh, resolve_data_date
from app.services.position_manager import run_position_manager
from app.constants.PricePath import PricePath


logger = logging.getLogger(__name__)


def main(argv: Optional[list[str]] = None) -> int:
    """Returns exit code. argv passed for testability; defaults to sys.argv[1:]."""
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )

    # Resolve ONCE up front. Both exec_data_refresh and PM must agree on
    # the data date — otherwise PM sends today's data_root to the engine
    # while exec_data parquets land in yesterday's folder, and the engine
    # 500s on the missing folder. Single source of truth.
    requested: date = args.run_date
    run_date: date = resolve_data_date(requested)
    print(f'[orchestrator] ============================================')
    print(f'[orchestrator] EOD orchestrator START')
    print(f'[orchestrator]   requested run_date = {requested}')
    print(f'[orchestrator]   resolved  run_date = {run_date}')
    print(f'[orchestrator] ============================================')

    db = SessionLocal()
    try:
        # ── Step 1: exec_data_refresh ────────────────────────────────────
        if args.skip_exec_data:
            print('[orchestrator] --skip-exec-data set, skipping Step 1')
        else:
            success = _run_exec_data_refresh_step(db, run_date)
            if not success:
                print('[orchestrator] exec_data_refresh FAILED — aborting')
                return 1

        # ── Step 2: PM per strategy ──────────────────────────────────────
        n_failed = _run_position_managers_step(db, run_date, args.strategy_id)

        if n_failed > 0:
            return 2
        return 0
    except Exception as e:
        # Catch-all so unexpected errors don't leave stale RUNNING rows.
        print(f'[orchestrator] UNEXPECTED ERROR: {type(e).__name__}: {e}')
        traceback.print_exc()
        return 3
    finally:
        db.close()


# ── Step 1: exec_data_refresh ─────────────────────────────────────────────────


def _run_exec_data_refresh_step(db: Session, run_date: date) -> bool:
    """Run exec_data_refresh and report status. The service writes its own
    eod_run_log row (RUNNING → SUCCESS/FAILED) so the orchestrator only
    needs to surface the outcome.
    """
    print(f'[orchestrator] === Step 1: exec_data_refresh ===')
    try:
        result = run_exec_data_refresh(db, run_date=run_date)
        print(f'[orchestrator] Step 1 SUCCESS: refreshed '
              f'{len(result) if result else 0} universe(s) — {result}')
        return True
    except Exception as e:
        # Service already wrote the FAILED row with traceback. We just
        # surface a one-line failure marker on the console.
        print(f'[orchestrator] Step 1 FAILED: {type(e).__name__}: {e}')
        return False


# ── Step 2: PM per strategy ───────────────────────────────────────────────────


def _run_position_managers_step(
    db: Session,
    run_date: date,
    strategy_id_filter: Optional[int],
) -> int:
    """Run PM for each execution_enabled strategy. Returns count of failures.

    Each strategy gets its own SQL transaction (managed by run_position_manager).
    A failure on one strategy does NOT abort the rest. Log row per strategy
    is written by the runner itself (step='execution_step', strategy_id
    populated).
    """
    print(f'[orchestrator] === Step 2: PM per strategy ===')

    query = db.query(StrategyBucket).filter(
        StrategyBucket.execution_enabled == True
    )
    if strategy_id_filter is not None:
        query = query.filter(StrategyBucket.id == strategy_id_filter)
    strategies = query.order_by(StrategyBucket.id.asc()).all()

    if not strategies:
        print('[orchestrator] no execution_enabled strategies (after filter) — nothing to do')
        return 0

    # data_root same for all strategies — universe-shared exec_data folder
    data_root = str(
        Path(PricePath.backtestPath)
        / 'exec_data'
        / run_date.strftime('%Y%m%d')
    )

    n_success = 0
    n_failed  = 0

    for strategy in strategies:
        print(f'[orchestrator] --- strategy_id={strategy.id} ({strategy.name}) ---')
        try:
            result = run_position_manager(
                db=db,
                strategy_id=strategy.id,
                run_date=run_date,
                data_root=data_root,
            )
            n_success += 1
            print(
                f'[orchestrator] strategy_id={strategy.id} SUCCESS: '
                f'fills={result["fills_resolved"]}/{result["fills_cancelled"]} '
                f'exits={result["exits_applied"]} '
                f'proposed={result["proposed_inserted"]}/'
                f'{result["substitute_pool_inserted"]} '
                f'eod_run_log_id={result["eod_run_log_id"]}'
            )
        except Exception as e:
            n_failed += 1
            # Runner already rolled back its SQL writes + persisted FAILED row
            # with full traceback. We just acknowledge here and move on.
            print(
                f'[orchestrator] strategy_id={strategy.id} FAILED: '
                f'{type(e).__name__}: {e}  (see eod_run_log for details)'
            )

    print(f'[orchestrator] ============================================')
    print(f'[orchestrator] Step 2 SUMMARY: {n_success} succeeded, {n_failed} failed '
          f'(of {len(strategies)} total)')
    print(f'[orchestrator] ============================================')
    return n_failed


# ── CLI ───────────────────────────────────────────────────────────────────────


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog='eod_orchestrator',
        description='Nightly EOD pipeline (post-universe-pipeline)',
    )
    parser.add_argument(
        '--run-date',
        type=lambda s: date.fromisoformat(s),
        default=date.today(),
        help='Data date YYYY-MM-DD (default: today)',
    )
    parser.add_argument(
        '--strategy',
        dest='strategy_id',
        type=int,
        default=None,
        help='Run for one strategy id (default: all execution_enabled)',
    )
    parser.add_argument(
        '--skip-exec-data',
        action='store_true',
        help='Skip Step 1 (exec_data_refresh) — assumes already done',
    )
    return parser.parse_args(argv)


if __name__ == '__main__':
    sys.exit(main())