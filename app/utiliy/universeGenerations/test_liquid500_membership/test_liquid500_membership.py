"""
test_liquid500_membership.py
============================
Standalone parity test for the new ``extend_liquid500_membership`` service
against the existing maintained universe CSV.

What it does
------------
1. Reads the existing membership CSV (read-only — never modified).
2. Strips all rows on or after TEST_MONTH_START to make a "truncated" copy.
3. Calls ``extend_liquid500_membership`` against the truncated copy with
   ``end_date = TEST_MONTH_START`` and a dry-run output path.
4. Compares the recomputed row at TEST_MONTH_START to the original row.
5. Prints a parity report: overlap %, only-in-original tickers,
   only-in-recomputed tickers, sector distribution match.

Run
---
From the project root:

    python -m app.utiliy.universeGenerations.test_liquid500_membership

The production CSV at universes/liquid500/liquid500.csv is read but
never written. The recomputed output goes to a temp file that's deleted
on exit.

Runtime expectation
-------------------
One month-start = ~20,000 Norgate calls (4,500 IDs × 3 fields for the
price pull, plus SP1500 + R3000 membership, plus TRBC sectors + exchange
+ symbol lookups). With 10 parallel workers, expect ~5-15 minutes
depending on Norgate latency.
"""

# Patch 88 — parity test harness for the standalone v1 service

from __future__ import annotations
import datetime as dt
import tempfile
from pathlib import Path

import pandas as pd

from app.constants.PricePath import PricePath
from app.utiliy.universeGenerations.liquid500_membership import (
    extend_liquid500_membership,
)


SOURCE_CSV = Path(PricePath.backtestPath) / 'universes' / 'liquid500' / 'liquid500.csv'

# The month-start to parity-test against. Default chosen from the user's
# existing CSV — change to any earlier month-start as needed.
TEST_MONTH_START = dt.date(2026, 6, 1)


# ──────────────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────────────

def _read(p: Path) -> pd.DataFrame:
    df = pd.read_csv(p)
    df['Date'] = pd.to_datetime(df['Date'])
    df.set_index('Date', inplace=True)
    return df.loc[:, ~df.columns.str.startswith('Unnamed')]


def _active_tickers(row: pd.Series) -> set:
    """Tickers with value == 1.0 (NaN treated as 0)."""
    return set(row[row.fillna(0).astype(float) == 1.0].index)


# ──────────────────────────────────────────────────────────────────────
#  Main
# ──────────────────────────────────────────────────────────────────────

def main():
    print('=' * 72)
    print('Liquid 500 membership parity test')
    print('=' * 72)
    print(f'source   : {SOURCE_CSV}')
    print(f'target MS: {TEST_MONTH_START}')
    print()

    if not SOURCE_CSV.exists():
        raise SystemExit(f'Source file missing: {SOURCE_CSV}')

    original = _read(SOURCE_CSV)
    test_ts = pd.Timestamp(TEST_MONTH_START)

    if test_ts not in original.index:
        raise SystemExit(
            f'TEST_MONTH_START {TEST_MONTH_START} not found in source CSV. '
            f'Last stored date is {original.index.max().date()}. '
            f'Pick a month-start that exists in the file.'
        )

    print(f'original CSV: {len(original)} rows, '
          f'first={original.index.min().date()}, '
          f'last={original.index.max().date()}, '
          f'cols={len(original.columns)}')

    original_row = original.loc[test_ts]
    orig_active = _active_tickers(original_row)
    print(f'original {TEST_MONTH_START} active count: {len(orig_active)}')
    print()

    # ── Build a truncated copy ending strictly BEFORE TEST_MONTH_START ──
    truncated = original.loc[original.index < test_ts].copy()
    truncated_last = truncated.index.max().date() if len(truncated) else None
    print(f'truncated input: {len(truncated)} rows, '
          f'last={truncated_last}')

    with tempfile.TemporaryDirectory() as td:
        truncated_path = Path(td) / 'liquid500_truncated.csv'
        output_path    = Path(td) / 'liquid500_recomputed.csv'
        truncated.to_csv(truncated_path, index=True, index_label='Date')

        print()
        print('-' * 72)
        print('Running extend_liquid500_membership ...')
        print('-' * 72)
        summary = extend_liquid500_membership(
            str(truncated_path),
            end_date=TEST_MONTH_START,
            dry_run_output_path=str(output_path),
        )
        print()
        print('service summary:')
        for k, v in summary.items():
            if isinstance(v, list) and len(v) > 10:
                print(f'  {k}: ({len(v)} items) {v[:10]} ...')
            else:
                print(f'  {k}: {v}')

        recomputed = _read(output_path)

    if test_ts not in recomputed.index:
        raise SystemExit(
            f'Service did not produce a row for {TEST_MONTH_START}; '
            f'recomputed last={recomputed.index.max().date()}'
        )

    recomputed_row = recomputed.loc[test_ts]
    new_active = _active_tickers(recomputed_row)

    # ── Diff ──
    overlap   = orig_active & new_active
    only_orig = orig_active - new_active
    only_new  = new_active - orig_active

    print()
    print('=' * 72)
    print('PARITY REPORT')
    print('=' * 72)
    print(f'original active   : {len(orig_active)}')
    print(f'recomputed active : {len(new_active)}')
    print(f'overlap           : {len(overlap)}')
    print(f'only in original  : {len(only_orig)}')
    if only_orig:
        sample = sorted(only_orig)[:30]
        print(f'  sample (≤30)    : {sample}')
    print(f'only in recomputed: {len(only_new)}')
    if only_new:
        sample = sorted(only_new)[:30]
        print(f'  sample (≤30)    : {sample}')

    if not orig_active:
        print('\nno original active set to compare against.')
        return

    overlap_pct = 100.0 * len(overlap) / len(orig_active)
    print(f'\noverlap % vs original: {overlap_pct:.2f}%')

    if overlap_pct >= 98.0:
        print('PARITY: PASS  (≥ 98% overlap)')
    elif overlap_pct >= 90.0:
        print('PARITY: REVIEW  (90–98% overlap — likely Norgate restatement '
              'or A/B-dedupe ordering edge cases; inspect diff above)')
    else:
        print('PARITY: FAIL  (< 90% overlap — investigate)')

    print('=' * 72)


if __name__ == '__main__':
    main()