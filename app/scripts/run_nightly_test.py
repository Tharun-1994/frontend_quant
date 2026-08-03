"""
run_nightly_test.py — Test nightly entry point. No DB writes for PM step.

Runs:
  1. exec_data_refresh for the given run_date (regenerates parquets)
  2. run_test_position_manager per strategy (engine call + CSV write, no DB)

Usage (spawned by /api/eod/trigger-nightly-test):
  python -m app.scripts.run_nightly_test --run-date 2009-07-05
  python -m app.scripts.run_nightly_test --run-date 2009-07-05 --strategy 27
"""
from __future__ import annotations
import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Optional
import datetime as _dt
from app.database import SessionLocal
from app.models.strategy_bucket import StrategyBucket
from app.services.exec_data_refresh import run_exec_data_refresh
from app.services.position_manager.test_runner import run_test_position_manager
from app.constants.PricePath import PricePath


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    run_date: date = args.run_date
    strategy_id_filter: Optional[int] = args.strategy_id

    print(f'[test_nightly] ============================================')
    print(f'[test_nightly] TEST nightly START  run_date={run_date}')
    print(f'[test_nightly]   strategy_filter={strategy_id_filter or "all"}')
    print(f'[test_nightly] No DB writes for PM step.')
    print(f'[test_nightly] ============================================')

    db = SessionLocal()
    try:
        # Step 1: exec_data_refresh — always regenerate for the test date.
        # resolve_data_date is bypassed here intentionally: the user explicitly
        # chose a historical date, we must not roll it back to yesterday.
        print(f'[test_nightly] === Step 1: exec_data_refresh for {run_date} ===')
        try:
            test_start_date = run_date - _dt.timedelta(days=650)
            run_exec_data_refresh(
                db,
                run_date=run_date,
                write_eod_log=False,
                start_date=test_start_date,
            )
            print(f'[test_nightly] Step 1 SUCCESS')
        except Exception as e:
            print(f'[test_nightly] Step 1 FAILED: {type(e).__name__}: {e}')
            return 1

        # Step 2: engine call + CSV write per strategy. No DB writes.
        data_root = str(
            Path(PricePath.backtestPath)
            / 'exec_data'
            / run_date.strftime('%Y%m%d')
        )
        print(f'[test_nightly] === Step 2: test PM per strategy ===')
        print(f'[test_nightly]   data_root={data_root}')

        query = db.query(StrategyBucket).filter(
            StrategyBucket.execution_enabled == True
        )
        if strategy_id_filter is not None:
            query = query.filter(StrategyBucket.id == strategy_id_filter)
        strategies = query.order_by(StrategyBucket.id.asc()).all()

        if not strategies:
            print('[test_nightly] no execution_enabled strategies — nothing to do')
            return 0

        n_ok = 0
        n_fail = 0
        for strategy in strategies:
            print(f'[test_nightly] --- strategy_id={strategy.id} ({strategy.name}) ---')
            try:
                result = run_test_position_manager(
                    db=db,
                    strategy_id=strategy.id,
                    run_date=run_date,
                    data_root=data_root,
                )
                n_ok += 1
                print(
                    f'[test_nightly] SUCCESS  entries={result["entries_count"]} '
                    f'exits={result["exits_count"]}  csv={result["csv_path"]}'
                )
            except Exception as e:
                n_fail += 1
                print(f'[test_nightly] FAILED: {type(e).__name__}: {e}')

        print(f'[test_nightly] ============================================')
        print(f'[test_nightly] Step 2 SUMMARY: {n_ok} ok, {n_fail} failed')
        print(f'[test_nightly] ============================================')
        return 0 if n_fail == 0 else 2

    finally:
        db.close()


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog='run_nightly_test')
    parser.add_argument(
        '--run-date',
        type=lambda s: date.fromisoformat(s),
        required=True,
        help='Historical close date YYYY-MM-DD',
    )
    parser.add_argument(
        '--strategy',
        dest='strategy_id',
        type=int,
        default=None,
        help='Run for one strategy id only (default: all execution_enabled)',
    )
    return parser.parse_args(argv)


if __name__ == '__main__':
    sys.exit(main())