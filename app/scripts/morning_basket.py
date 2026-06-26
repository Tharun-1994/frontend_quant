"""morning_basket.py — D-day morning workflow.

Sequence:
  1. Discover substitution CSVs at <backtestPath>/substitution_input/{YYYYMMDD}/
     where YYYYMMDD = trade_date (today by default).
     Filename pattern: <strategy_name>.csv  (e.g., PullBack_X3_Sp500.csv)
  2. For each CSV found, call overlay_apply.apply_overlay() directly.
  3. Call broker_write.write_broker_basket() to emit M_Combined_{D}.xlsx.

Imports services directly (not via HTTP) — works without FastAPI running.

Failure handling:
  - Overlay failure on one strategy → LOG + CONTINUE with next CSV
  - Broker-write failure → ABORT (no XLSX produced)

Exit codes:
  0 — broker-write succeeded (all overlays + XLSX OK)
  1 — broker-write failed
  2 — one or more overlay-apply failed (broker-write still attempted)
  3 — unexpected error

Usage:
    python -m app.scripts.morning_basket
    python -m app.scripts.morning_basket --trade-date 2026-06-15
    python -m app.scripts.morning_basket --trade-date 2026-06-15 --strategy PullBack_X3_Sp500
"""

from __future__ import annotations
import argparse
import sys
import traceback
from datetime import date
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.strategy_bucket import StrategyBucket
from app.services.overlay_apply import apply_overlay
from app.services.broker_write import write_broker_basket
from app.constants.PricePath import PricePath


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    trade_date: date = args.trade_date

    print(f'[morning_basket] ============================================')
    print(f'[morning_basket] Morning basket START  trade_date={trade_date}')
    print(f'[morning_basket] ============================================')

    csv_dir = (
        Path(PricePath.backtestPath)
        / 'substitution_input'
        / trade_date.strftime('%Y%m%d')
    )

    # Guard: if XLSX already exists for today (Vas used SubstitutionPage),
    # skip the scheduled run entirely — no point regenerating the same file.
    output_dir = Path(PricePath.backtestPath) / 'broker_output' / trade_date.strftime('%Y%m%d')
    expected_xlsx = output_dir / f'M_Combined_{trade_date.strftime("%Y%m%d")}.xlsx'
    # Guard: skip only if XLSX exists AND is non-empty (size > 5KB).
    # An empty/corrupt XLSX (< 5KB) means a previous run failed mid-write —
    # we should regenerate in that case.
    if expected_xlsx.exists() and expected_xlsx.stat().st_size > 5000:
        print(f'[morning_basket] XLSX already exists ({expected_xlsx.stat().st_size} bytes) at {expected_xlsx}')
        print(f'[morning_basket] Skipping — basket already generated for today')
        print(f'[morning_basket] ============================================')
        return 0

    db = SessionLocal()
    n_overlay_failed = 0
    try:
        # Step 1: overlays
        n_overlay_failed = _apply_overlays_step(
            db, trade_date, csv_dir, args.strategy_filter
        )

        # Step 2: broker-write
        try:
            result = write_broker_basket(db=db, trade_date=trade_date)
            print(
                f'[morning_basket] broker-write SUCCESS — wrote '
                f'{result["orders_written"]} orders to {result["file_path"]} '
                f'(promoted {result["promoted_proposed"]} PROPOSED → PENDING_FILL)'
            )
        except Exception as e:
            print(f'[morning_basket] broker-write FAILED: '
                  f'{type(e).__name__}: {e}  (see eod_run_log for details)')
            return 1

        return 2 if n_overlay_failed > 0 else 0

    except Exception as e:
        print(f'[morning_basket] UNEXPECTED ERROR: {type(e).__name__}: {e}')
        traceback.print_exc()
        return 3
    finally:
        db.close()


def _apply_overlays_step(
    db: Session,
    trade_date: date,
    csv_dir: Path,
    strategy_filter: Optional[str],
) -> int:
    print(f'[morning_basket] === Step 1: overlay-apply ===')
    print(f'[morning_basket] looking for CSVs in {csv_dir}')

    if not csv_dir.exists():
        print(f'[morning_basket] no substitution_input folder for {trade_date} — '
              f'no overlays to apply (broker-write will auto-promote remaining '
              f'PROPOSED → PENDING_FILL)')
        return 0

    csv_files = sorted(csv_dir.glob('*.csv'))
    if not csv_files:
        print(f'[morning_basket] folder exists but no *.csv files — '
              f'no overlays to apply')
        return 0

    if strategy_filter:
        csv_files = [f for f in csv_files if f.stem == strategy_filter]
        if not csv_files:
            print(f'[morning_basket] --strategy {strategy_filter!r} '
                  f'but no matching CSV in {csv_dir}')
            return 0

    n_failed = 0
    for csv_path in csv_files:
        strategy_name = csv_path.stem
        print(f'[morning_basket] --- {strategy_name} ({csv_path.name}) ---')

        strategy = (
            db.query(StrategyBucket).filter_by(name=strategy_name).first()
        )
        if strategy is None:
            print(f'[morning_basket] strategy {strategy_name!r} not found in DB — '
                  f'skipping (rename CSV to match an existing strategy name)')
            n_failed += 1
            continue
        if not strategy.execution_enabled:
            print(f'[morning_basket] strategy {strategy_name!r} has '
                  f'execution_enabled=False — skipping')
            n_failed += 1
            continue

        try:
            csv_text = csv_path.read_text(encoding='utf-8-sig')
            result = apply_overlay(
                db=db,
                strategy_id=strategy.id,
                override_date=trade_date,
                csv_text=csv_text,
                uploaded_by='scheduler',
                csv_source_path=str(csv_path),
            )
            print(
                f'[morning_basket] {strategy_name} overlay SUCCESS: '
                f'elide={result["elided"]} sub={result["substituted"]} '
                f'adj={result["adjusted_capital"]} half={result["half_sized"]} '
                f'skipped={result["skipped_no_match"]} version={result["version"]}'
            )
        except Exception as e:
            n_failed += 1
            print(f'[morning_basket] {strategy_name} overlay FAILED: '
                  f'{type(e).__name__}: {e}  (see eod_run_log)')

    return n_failed


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog='morning_basket',
        description='D-day morning chain (overlay-apply + broker-write)',
    )
    parser.add_argument(
        '--trade-date',
        type=lambda s: date.fromisoformat(s),
        default=date.today(),
        help='Trade date YYYY-MM-DD (default: today)',
    )
    parser.add_argument(
        '--strategy',
        dest='strategy_filter',
        type=str,
        default=None,
        help='Process only one strategy by name (default: all CSVs in the folder)',
    )
    return parser.parse_args(argv)


if __name__ == '__main__':
    sys.exit(main())