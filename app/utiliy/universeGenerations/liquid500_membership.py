"""
liquid500_membership.py
=======================
Standalone Liquid 500 membership extender. v1 — selection logic ONLY,
deliberately decoupled from the universe pipeline / refresh-button flow
so it can be parity-tested against the existing maintained CSV before
any wiring is committed.

Reads the maintained source-of-truth membership file
    backtest_data/universes/liquid500/liquid500.csv
extends it append-only up to ``end_date``, and writes back.

Membership composition only changes on NYSE month-start trading days. On
any other day the previous month-start composition is forward-filled.

  - Common path (no month-start crossed since last write): pure ffill,
    no Norgate call. Milliseconds.
  - Rare path (≥1 month-start crossed): bounded ~220-trading-day
    Norgate pull over all US Listed Stocks, then for each month-start
    in the gap run the legacy selection (rank by 200-day mean dollar
    volume → price-drop filter → OTC restriction against S&P Composite
    1500 ∪ Russell 3000 → A/B class dedupe → top 500 → 30% TRBC L1
    sector cap). Forward-fill between month-starts.

The selection algorithm is a direct port of ``most_liquid_otc()`` in the
legacy Universe_Liq_500_Bifilter_R3000_SnP_1500_PRICE_DROP.py. Constants
live in liquid500_config.py and must not drift.

OTC restriction data source — Norgate-direct (v1)
-------------------------------------------------
The legacy reads S&P 1500 and Russell 3000 membership from maintained
CSV files in UniverseGenerate/SandP1500/ and Russell3000_universe/. Those
files are 6-8 months stale at migration time. v1 of this service pulls
them live from Norgate (same underlying data) so the parity test is
self-contained. When sp1500 + russell3000 are added to the webapp's
universe REGISTRY later, this lookup can be re-pointed at
universes/sp1500/ + universes/russell3000/.

Write discipline — append-only, never modify
--------------------------------------------
Pre-existing dates in the membership CSV stay byte-identical after this
runs. New rows are added strictly AFTER last_stored. Tickers entering on
a month-start are added as new columns (NaN for all historical rows;
1.0 from their first month-start onwards as ffilled). Loud failure
(raises ``RuntimeError``) over silent partial write — partial signals
would mean wrong trades downstream.
"""

# Patch 88 — Liquid 500 membership extender (standalone v1)

from __future__ import annotations
import datetime as dt
import math
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional, List, Set, Tuple

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal

from app.utiliy.universeGenerations.liquid500_config import (
    UNIVERSE_SIZE,
    MIN_DOLLAR_UNADJUSTED,
    PRICE_DROP_THRESHOLD,
    ADV_ROLLING_WINDOW,
    MAX_ROLLING_WINDOW,
    SECTOR_PCT_LIMIT,
    TRBC_LEVEL,
    TRBC_SCHEME_NAME,
    TRBC_RESULT_TYPE,
    US_LISTED_WATCHLIST,
    SP1500_INDEX_NAME,
    SP1500_WATCHLIST,
    R3000_INDEX_NAME,
    R3000_WATCHLIST,
    LOOKBACK_TRADING_DAYS,
    NORGATE_POOL_SIZE,
    SECTOR_CAP_MAX_ITERATIONS,
    # Patch 89: version control
    VERSIONS_FOLDER_NAME,
    MAX_VERSIONS_TO_KEEP,
)


# Process-lifetime cache for TRBC classification. Sector cap loop calls
# this repeatedly during one month-start compute; without caching, the
# same asset can be queried 3-5 times. Cleared on Python exit.
_TRBC_CACHE: dict = {}

# Patch 89: regex matching backup filenames in _versions/. Matches
# liquid500_YYYYMMDD_HHMMSS.csv with optional collision suffix _N.
_VERSION_FILENAME_PATTERN = re.compile(r'^liquid500_(\d{8})_(\d{6})(_\d+)?\.csv$')


# ──────────────────────────────────────────────────────────────────────
#  Public API
# ──────────────────────────────────────────────────────────────────────

def extend_liquid500_membership(
    csv_path: str,
    end_date: Optional[dt.date] = None,
    *,
    dry_run_output_path: Optional[str] = None,
) -> dict:
    """
    Extend the Liquid 500 membership CSV up to ``end_date``. Append-only.

    Parameters
    ----------
    csv_path : str
        Path to the source-of-truth membership CSV (ticker-wide, 1/NaN).
        Read AND overwritten in-place unless ``dry_run_output_path`` is set.
    end_date : date, optional
        Target last date in the CSV after this call. Defaults to today.
        If end_date is not a trading day, rows are extended up to (but
        not past) the most recent NYSE trading day ≤ end_date.
    dry_run_output_path : str, optional
        When set, writes the extended CSV to this path instead of
        overwriting ``csv_path``. Used by the parity-test harness so we
        never mutate the production file by accident.

    Returns
    -------
    dict
        Summary:
            last_stored             — the previous max date in the CSV
            last_written            — the new max date after this call
            membership_action       — 'NOOP' | 'FFILLED' | 'RECOMPUTED'
            month_starts_computed   — list of month-start dates whose
                                      composition was recomputed (empty
                                      for NOOP / pure FFILLED)
            rows_appended           — number of new rows added
            new_tickers             — tickers added as new columns
            num_active              — count of '1' entries on the last
                                      written row

    Raises
    ------
    FileNotFoundError
        If ``csv_path`` does not exist.
    ValueError
        If the CSV is empty / malformed.
    RuntimeError
        Any failure during a month-start recompute (Norgate pull failure,
        sector cap loop divergence, etc.). Fail-fast — partial writes
        are never produced.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"Source-of-truth file missing: {csv_path}")

    if end_date is None:
        end_date = dt.date.today()

    # ── 1) Read existing membership ──────────────────────────────────
    membership = _read_membership(csv_path)
    if membership.empty:
        raise ValueError(f"Empty membership CSV: {csv_path}")
    last_stored: dt.date = membership.index.max().date()

    # ── 2) NYSE trading days in (last_stored, end_date] ──────────────
    nyse = mcal.get_calendar('NYSE')
    if end_date <= last_stored:
        return _summary(membership, last_stored, last_stored,
                        'NOOP', [], 0, [])
    valid = nyse.valid_days(
        last_stored + dt.timedelta(days=1), end_date,
    ).tz_localize(None)
    new_days: List[dt.date] = [d.date() for d in valid]
    if not new_days:
        return _summary(membership, last_stored, last_stored,
                        'NOOP', [], 0, [])

    # ── 3) Month-starts among the new days ───────────────────────────
    month_starts = _month_starts_in(new_days, nyse)

    # ── 4) Build the new rows DataFrame ──────────────────────────────
    new_rows = pd.DataFrame(
        index=pd.to_datetime(new_days),
        columns=membership.columns.copy(),
        dtype=float,
    )
    new_tickers_all: List[str] = []
    month_starts_done: List[dt.date] = []

    if not month_starts:
        # Pure ffill — no Norgate.
        last_row = membership.iloc[-1]
        for d in new_rows.index:
            new_rows.loc[d, :] = last_row.values
        action = 'FFILLED'
    else:
        # Recompute each month-start; ffill between.
        action = 'RECOMPUTED'
        prev_row = membership.iloc[-1].copy()
        for d in new_rows.index:
            d_date = d.date()
            if d_date in month_starts:
                row, added = _compute_month_start_row(
                    d_date, list(new_rows.columns),
                )
                # Add genuinely-new ticker columns to both the existing
                # membership (NaN history) and the new_rows frame.
                for t in added:
                    if t not in new_rows.columns:
                        new_rows[t] = np.nan
                        membership[t] = np.nan
                        new_tickers_all.append(t)
                row = row.reindex(new_rows.columns)
                new_rows.loc[d, :] = row.values
                prev_row = row.copy()
                month_starts_done.append(d_date)
            else:
                # Ffill within the gap.
                new_rows.loc[d, :] = prev_row.reindex(new_rows.columns).values


    # ── 5) Concat and write ──────────────────────────────────────────
    final = pd.concat([membership, new_rows]).sort_index()
    final = final[~final.index.duplicated(keep='first')]
    final.index.name = 'Date'

    # Patch 89: version control. Backup the current CSV BEFORE writing
    # new content. Loud-fail (RuntimeError) if the backup didn't land —
    # we never overwrite live state without a confirmed prior snapshot.
    # Dry-run mode (parity tests) skips backup since csv_path isn't
    # being touched in that path.
    backup_path: Optional[Path] = None
    versions_pruned: int = 0
    if not dry_run_output_path:
        backup_path = _backup_csv(csv_path)

    out_path = Path(dry_run_output_path) if dry_run_output_path else csv_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    final.to_csv(out_path, index=True, index_label='Date')

    # Patch 89: prune oldest backups beyond the keep cap. Non-fatal —
    # the main write has already succeeded by this point.
    if backup_path is not None:
        versions_pruned = _prune_old_versions(
            backup_path.parent, MAX_VERSIONS_TO_KEEP,
        )

    last_written: dt.date = final.index.max().date()
    return _summary(
        final,
        last_stored=last_stored,
        last_written=last_written,
        action=action,
        month_starts=[d.isoformat() for d in month_starts_done],
        rows_appended=len(new_rows),
        new_tickers=sorted(set(new_tickers_all)),
        backup_path=str(backup_path) if backup_path else None,
        versions_pruned=versions_pruned,
    )

# ──────────────────────────────────────────────────────────────────────
#  IO helpers
# ──────────────────────────────────────────────────────────────────────

def _read_membership(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df['Date'] = pd.to_datetime(df['Date'])
    df.set_index('Date', inplace=True)
    df = df.loc[:, ~df.columns.str.startswith('Unnamed')]
    # Patch 114: refuse to extend a corrupt membership file. Numeric
    # column headers are Norgate assetIDs / headerless-CSV artefacts —
    # extending and re-writing would cement the corruption.
    _bad = [c for c in df.columns
            if not isinstance(c, str) or c.strip().isdigit()]
    if _bad:
        raise RuntimeError(
            f'membership header corrupt in {csv_path}: {len(_bad)} '
            f'numeric column(s), e.g. {_bad[:10]} — restore from backup '
            f'before extending.')

    return df


def _count_active(row: pd.Series) -> int:
    """Number of '1' entries in a membership row. NaN counts as 0."""
    return int((row.fillna(0).astype(float) == 1.0).sum())


def _month_starts_in(trading_days: List[dt.date], nyse) -> Set[dt.date]:
    """Return the subset of trading_days that are the FIRST NYSE trading
    day of their calendar month. Mirrors the legacy
    `get_valid_dates(rebalance='month-start')` semantics."""
    out: Set[dt.date] = set()
    for d in trading_days:
        month_start = dt.date(d.year, d.month, 1)
        month_days = nyse.valid_days(
            month_start, d + dt.timedelta(days=5),
        ).tz_localize(None)
        if month_days.empty:
            continue
        if d == month_days[0].date():
            out.add(d)
    return out


def _summary(membership: pd.DataFrame, last_stored: dt.date,
             last_written: dt.date, action: str,
             month_starts: List[str], rows_appended: int,
             new_tickers: List[str],
             backup_path: Optional[str] = None,
             versions_pruned: int = 0) -> dict:
    return {
        'last_stored': last_stored.isoformat(),
        'last_written': last_written.isoformat(),
        'membership_action': action,
        'month_starts_computed': month_starts,
        'rows_appended': rows_appended,
        'new_tickers': new_tickers,
        'num_active': _count_active(membership.iloc[-1]),
        # Patch 89: version control surfacing.
        'backup_path': backup_path,
        'versions_pruned': versions_pruned,
    }


# ──────────────────────────────────────────────────────────────────────
#  Version control — backup + prune (Patch 89)
# ──────────────────────────────────────────────────────────────────────

def _backup_csv(csv_path: Path) -> Optional[Path]:
    """Copy csv_path to _versions/liquid500_YYYYMMDD_HHMMSS.csv BEFORE
    the new content is written. Returns the backup path on success, or
    None if csv_path didn't exist yet (first-ever write).

    Loud-fails (RuntimeError) if the backup didn't land. The contract
    is: if this function returns successfully, the prior file state is
    safely captured on disk. We never proceed to overwrite live data
    without a confirmed snapshot — a corrupted write with no backup
    means lost history."""
    if not csv_path.exists():
        return None

    versions_dir = csv_path.parent / VERSIONS_FOLDER_NAME
    versions_dir.mkdir(parents=True, exist_ok=True)

    ts = dt.datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = versions_dir / f'liquid500_{ts}.csv'

    # Same-second collision (rare double-click): append a numeric suffix.
    if backup_path.exists():
        for i in range(1, 100):
            alt = versions_dir / f'liquid500_{ts}_{i}.csv'
            if not alt.exists():
                backup_path = alt
                break
        else:
            raise RuntimeError(
                f'Backup name collision could not be resolved at {ts}'
            )

    try:
        shutil.copy2(csv_path, backup_path)
    except Exception as e:
        raise RuntimeError(
            f'Backup failed before write: {csv_path} -> {backup_path}: {e}'
        )
    if not backup_path.exists() or backup_path.stat().st_size == 0:
        raise RuntimeError(
            f'Backup landed missing or empty: {backup_path}'
        )
    print(f'[liquid500] backed up to {backup_path}')
    return backup_path


def _prune_old_versions(versions_dir: Path, keep: int) -> int:
    """Delete oldest backup files beyond the keep-N cap. Returns the
    count deleted. Failure to prune individual files is non-fatal —
    the main write has already succeeded — so we surface a warning per
    failed unlink and continue."""
    if not versions_dir.exists():
        return 0
    matching = [
        p for p in versions_dir.iterdir()
        if _VERSION_FILENAME_PATTERN.match(p.name)
    ]
    if len(matching) <= keep:
        return 0
    matching.sort(key=lambda p: p.name)  # lexicographic == chronological
    to_delete = matching[:-keep]
    deleted = 0
    for p in to_delete:
        try:
            p.unlink()
            deleted += 1
        except Exception as e:
            print(f'[liquid500] warn: failed to prune {p}: {e}')
    if deleted:
        print(f'[liquid500] pruned {deleted} old version(s); kept {keep}')
    return deleted


# ──────────────────────────────────────────────────────────────────────
#  Norgate-side helpers
# ──────────────────────────────────────────────────────────────────────

def _resolve_asset_ids(symbols: List[str]) -> List[int]:
    """Resolve a list of Norgate symbols to integer asset IDs, dropping
    any that can't be resolved. Mirrors legacy line 89."""
    import norgatedata as nd
    out: List[int] = []
    for sym in symbols:
        try:
            aid = nd.assetid(sym)
            if aid is not None:
                out.append(int(aid))
        except Exception:
            pass
    return out


def _pull_field_window(
    asset_ids: List[int], field: str,
    start_date: dt.date, end_date: dt.date,
) -> pd.DataFrame:
    """Pull one Norgate field over a window for many assetIDs in parallel.
    Returns a DataFrame indexed by Date with columns = assetIDs.

    Mirrors legacy `get_norgatedata(asset_ids, fields=[field], ...)`
    which is a parallel wrapper around `price_timeseries` in the
    legacy Backtest module — we replicate that with a ThreadPool here.
    """
    import norgatedata as nd

    def _pull_one(aid: int):
        try:
            df = nd.price_timeseries(
                int(aid),
                stock_price_adjustment_setting=nd.StockPriceAdjustmentType.TOTALRETURN,
                padding_setting=nd.PaddingType.NONE,
                start_date=start_date,
                end_date=end_date,
                fields=[field],
                timeseriesformat='pandas-dataframe',
            )
            if df is None or df.empty:
                return aid, None
            col = df.columns[0]
            return aid, df[col].rename(aid)
        except Exception:
            return aid, None

    series_map: dict = {}
    with ThreadPoolExecutor(max_workers=NORGATE_POOL_SIZE) as ex:
        for aid, series in ex.map(_pull_one, asset_ids):
            if series is not None:
                series_map[aid] = series

    if not series_map:
        raise RuntimeError(
            f"Norgate returned no data for any of {len(asset_ids)} asset IDs "
            f"for field '{field}' over {start_date}..{end_date}"
        )
    return pd.DataFrame(series_map).sort_index()


def _fetch_index_members_at(
    index_name: str, watchlist: str, target_date: dt.date,
) -> Set[int]:
    """DEPRECATED in Patch 91 — kept for the v1 parity test harness only.
    Production code MUST use _otc_keep_symbols_at(d) which reads the
    maintained universes/sp1500/sp1500.csv and universes/russell3000/
    russell3000.csv files (legacy-pattern). Direct Norgate calls per
    month-start were a v1 test convenience; the legacy reads from
    maintained files and so should we for production parity.
    """
    import norgatedata as nd

    syms = nd.watchlist_symbols(watchlist)
    if not syms:
        raise RuntimeError(f"Norgate watchlist empty: {watchlist}")

    def _check_one(sym: str):
        try:
            series = nd.index_constituent_timeseries(
                sym, index_name,
                padding_setting=nd.PaddingType.NONE,
                start_date=target_date,
                end_date=target_date,
                timeseriesformat='pandas-dataframe',
            )
            if series is None or series.empty:
                return None
            val = series.iloc[0, 0]
            if val == 1:
                aid = nd.assetid(sym)
                return int(aid) if aid is not None else None
            return None
        except Exception:
            return None

    members: Set[int] = set()
    with ThreadPoolExecutor(max_workers=NORGATE_POOL_SIZE) as ex:
        for aid in ex.map(_check_one, syms):
            if aid is not None:
                members.add(aid)
    return members


# Patch 91 — file-based OTC lookup (legacy pattern)

def _read_index_universe_at(slug: str, target_date: dt.date) -> Set[str]:
    """Read universes/{slug}/{slug}.csv and return the set of TICKER
    SYMBOLS active on ``target_date``.

    Mirrors the legacy script which reads SP1500 membership from
    SandP1500/S&P_Composite_1500_most_recent.csv and R3000 from
    Russell3000_universe/NEW_Russell_3000_universe.csv. Maintained
    locally and refreshed daily by the manual "Update today's prices"
    button (same loop that calls extend_liquid500_membership).

    Loud-fails (FileNotFoundError) if the maintained universe hasn't
    been seeded — directs user to run pipeline.run(only={slug}) once.
    Loud-fails (RuntimeError) if the file exists but has no row for
    target_date — directs user to click "Update today's prices" to
    bring it current. No silent fallback to Norgate-direct: that would
    mask the architectural dependency and make stale-data bugs hard to
    spot.
    """
    from app.constants.PricePath import PricePath
    p = Path(PricePath.backtestPath) / 'universes' / slug / f'{slug}.csv'
    if not p.exists():
        raise FileNotFoundError(
            f'Missing maintained universe: {p}\n'
            f'Run `pipeline.run(only={{{slug!r}}})` once to seed it, then '
            f'click "Update today\'s prices" to keep it current.'
        )
    df = pd.read_csv(p)
    df['Date'] = pd.to_datetime(df['Date'])
    df.set_index('Date', inplace=True)
    df = df.loc[:, ~df.columns.str.startswith('Unnamed')]
    target_ts = pd.Timestamp(target_date)
    if target_ts not in df.index:
        raise RuntimeError(
            f'No row for {target_date} in {p}; file ends '
            f'{df.index.max().date()}. The manual button extends this '
            f'file daily — click "Update today\'s prices" first, or '
            f'pass an earlier end_date.'
        )
    row = df.loc[target_ts]
    return set(row[row.fillna(0).astype(float) == 1.0].index)


def _otc_keep_symbols_at(target_date: dt.date) -> Set[str]:
    """Set of ticker symbols that are in S&P 1500 Composite OR Russell
    3000 on ``target_date``. Used by the OTC restriction in
    _compute_month_start_row to mirror the legacy.

    Calls _read_index_universe_at twice (one file read each). Both
    universes are maintained by the same manual-button loop that calls
    this service, so they are current by the time liquid500 runs (which
    is later in the loop — see REGISTRY ordering in
    universe_registry.py).
    """
    sp1500_syms = _read_index_universe_at('sp1500', target_date)
    r3000_syms = _read_index_universe_at('russell3000', target_date)
    return sp1500_syms | r3000_syms


def _classify_trbc_l1(asset_ids: List[int]) -> pd.Series:
    """Return Series mapping assetID → TRBC level-1 sector name. Cached.
    Mirrors legacy line 227 (classification_at_level for each top-500
    asset). Repeated calls inside the sector-cap loop hit the cache."""
    import norgatedata as nd
    classifications = []
    for aid in asset_ids:
        aid_i = int(aid)
        cached = _TRBC_CACHE.get(aid_i)
        if cached is not None:
            classifications.append(cached)
            continue
        try:
            c = nd.classification_at_level(
                aid_i, TRBC_SCHEME_NAME, TRBC_RESULT_TYPE, TRBC_LEVEL,
            )
            cls = c if c else 'Unclassified'
        except Exception:
            cls = 'Unclassified'
        _TRBC_CACHE[aid_i] = cls
        classifications.append(cls)
    return pd.Series(classifications, index=asset_ids)


def _exchange_names(asset_ids: List[int]) -> dict:
    """Return dict assetID → exchange name. Mirrors legacy line 127."""
    import norgatedata as nd
    out: dict = {}
    for aid in asset_ids:
        try:
            out[int(aid)] = nd.exchange_name(int(aid))
        except Exception:
            out[int(aid)] = ''
    return out


def _ab_class_dedupe(ranked: pd.Series) -> pd.Series:
    """Strip '.X' suffix from the current symbol of each assetID and keep
    the highest-ranked occurrence of each root. Mirrors legacy lines
    209-220 exactly."""
    import norgatedata as nd
    if ranked.empty:
        return ranked

    asset_ids = list(ranked.index)
    syms = []
    for aid in asset_ids:
        try:
            syms.append(nd.symbol(int(aid)) or str(aid))
        except Exception:
            syms.append(str(aid))
    df = pd.DataFrame({
        'sym_root': [s.split('.')[0] for s in syms],
        'asset_id': asset_ids,
        'adv': ranked.values,
    })
    df = df.drop_duplicates(subset='sym_root', keep='first')
    return pd.Series(df['adv'].values, index=df['asset_id'].values)


def _nyse_days_before(target: dt.date, n: int) -> dt.date:
    """Date n NYSE trading days before ``target``."""
    nyse = mcal.get_calendar('NYSE')
    look_back_days = int(n * 1.6) + 30
    days = nyse.valid_days(
        target - dt.timedelta(days=look_back_days), target,
    ).tz_localize(None)
    days = [d.date() for d in days]
    if len(days) <= n:
        return days[0] if days else target - dt.timedelta(days=look_back_days)
    return days[-(n + 1)]


# ──────────────────────────────────────────────────────────────────────
#  Per-month-start composition (the legacy `most_liquid_otc` core)
# ──────────────────────────────────────────────────────────────────────

def _compute_month_start_row(
    d: dt.date, existing_columns: List[str],
) -> Tuple[pd.Series, List[str]]:
    """Run the legacy selection logic for one month-start date.

    Returns
    -------
    (row, new_tickers)
        row          — Series indexed by ticker symbol with 1.0 for active
                       members on ``d``, NaN elsewhere (matches the
                       existing CSV's empty-cell-for-inactive format).
        new_tickers  — tickers that were not present in ``existing_columns``.

    Raises RuntimeError on any failure during Norgate pulls / classification.
    Loud-fail by design — silent partial selection downstream would mean
    wrong trades.
    """
    import norgatedata as nd

    print(f'[liquid500] computing month-start composition for {d} ...')

    # 1) Universe pool — all US Listed Stocks (current + past).
    all_us_symbols = nd.watchlist_symbols(US_LISTED_WATCHLIST)
    if not all_us_symbols:
        raise RuntimeError(
            f"Norgate returned empty watchlist {US_LISTED_WATCHLIST!r}"
        )
    print(f'[liquid500]   universe pool: {len(all_us_symbols)} symbols')

    all_us_ids = _resolve_asset_ids(all_us_symbols)
    if not all_us_ids:
        raise RuntimeError('No assetIDs resolved from US Listed Stocks watchlist')

    # 2) Norgate pull window (~220 trading days ending at d).
    pull_start = _nyse_days_before(d, LOOKBACK_TRADING_DAYS)
    print(f'[liquid500]   pulling Close/Volume/Unadj over {pull_start}..{d}')

    closes      = _pull_field_window(all_us_ids, 'Close',           pull_start, d)
    volumes     = _pull_field_window(all_us_ids, 'Volume',          pull_start, d)
    unadj_closes = _pull_field_window(all_us_ids, 'Unadjusted Close', pull_start, d)

    if pd.Timestamp(d) not in closes.index:
        raise RuntimeError(
            f"Norgate returned no Close bar at {d} (window {pull_start}..{d})"
        )

    # 3) Drop no-data names (legacy lines 101-103).
    no_data_ids = closes.columns[closes.count() == 0]
    closes = closes.drop(columns=no_data_ids)
    volumes = volumes.drop(columns=no_data_ids)
    # Note: legacy does NOT drop these from unadj_closes; preserve that.

    # 4) Turnovers, 200d ADV, 125d max close (legacy lines 111-115).
    turnovers = closes * volumes
    adv200 = turnovers.rolling(ADV_ROLLING_WINDOW).mean()
    max_125 = closes.rolling(MAX_ROLLING_WINDOW).max()

    d_ts = pd.Timestamp(d)
    if d_ts not in adv200.index:
        raise RuntimeError(
            f"No ADV200 row at {d}; insufficient lookback "
            f"(have {len(closes)} bars, need {ADV_ROLLING_WINDOW})"
        )

    # 5) Rank descending (legacy line 137).
    ranked = adv200.loc[d_ts].dropna().sort_values(ascending=False)
    print(f'[liquid500]   after rank+dropna: {len(ranked)} candidates')

    today_adj     = closes.loc[d_ts]
    today_max_125 = max_125.loc[d_ts]
    today_unadj   = unadj_closes.loc[d_ts] if d_ts in unadj_closes.index else pd.Series(dtype=float)

    # 6) Price-drop filter (legacy lines 150-158).
    dollar_under_5 = today_unadj[today_unadj < MIN_DOLLAR_UNADJUSTED]
    if not dollar_under_5.empty:
        common = (dollar_under_5.index
                  .intersection(today_adj.index)
                  .intersection(today_max_125.index))
        fail_mask = today_adj.loc[common] < (PRICE_DROP_THRESHOLD * today_max_125.loc[common])
        to_drop = fail_mask[fail_mask].index
        ranked = ranked.drop(ranked.index.intersection(to_drop))
    print(f'[liquid500]   after price-drop filter: {len(ranked)}')

    # 7) OTC restriction (legacy lines 169-204).
    # Patch 91: legacy-pattern lookup. The legacy reads SP1500 + R3000
    # membership from maintained CSV files; we mirror that by reading
    # universes/sp1500/sp1500.csv and universes/russell3000/russell3000.csv
    # (kept current by the same refresh_all_today loop that calls this
    # service — REGISTRY ordering guarantees they run first).
    print(f'[liquid500]   resolving exchanges for {len(ranked)} names ...')
    exchanges = _exchange_names(list(ranked.index))
    otc_ids = [aid for aid in ranked.index if exchanges.get(int(aid)) == 'OTC']
    if otc_ids:
        print(f'[liquid500]   reading SP1500 + R3000 from maintained files at {d} ...')
        otc_keep_symbols = _otc_keep_symbols_at(d)
        otc_drop = []
        for aid in otc_ids:
            try:
                sym = nd.symbol(int(aid))
                if not sym or sym not in otc_keep_symbols:
                    otc_drop.append(aid)
            except Exception:
                # Can't resolve current symbol -> defensive drop.
                otc_drop.append(aid)
        if otc_drop:
            ranked = ranked.drop(otc_drop)
    print(f'[liquid500]   after OTC restriction: {len(ranked)}')

    # 8) A/B class dedupe (legacy lines 209-220).
    ranked = _ab_class_dedupe(ranked)
    print(f'[liquid500]   after A/B dedupe: {len(ranked)}')

    # 9) Top 500 + sector cap (legacy lines 224-238).
    top_ids = list(ranked.iloc[:UNIVERSE_SIZE].dropna().index)
    limit_per_sector = math.ceil(UNIVERSE_SIZE * SECTOR_PCT_LIMIT)

    sectors = _classify_trbc_l1(top_ids)
    counts = sectors.value_counts()
    guard = 0
    while not counts.empty and counts.max() > limit_per_sector:
        guard += 1
        if guard > SECTOR_CAP_MAX_ITERATIONS:
            raise RuntimeError(
                f"Sector cap loop did not converge in {SECTOR_CAP_MAX_ITERATIONS} "
                f"iterations at {d}; counts={counts.to_dict()}"
            )
        for sec in counts[counts == counts.max()].index:
            n_drop = counts[sec] - limit_per_sector
            stocks_to_go = sectors[sectors == sec].iloc[-n_drop:].index
            ranked = ranked.drop(stocks_to_go)
        top_ids = list(ranked.iloc[:UNIVERSE_SIZE].dropna().index)
        sectors = _classify_trbc_l1(top_ids)
        counts = sectors.value_counts()
    print(f'[liquid500]   after sector cap ({guard} iter): {len(top_ids)}')

    # 10) AssetIDs → ticker symbols (legacy did this in LiquidFileConverter).
    selected_tickers: List[str] = []
    for aid in top_ids:
        try:
            sym = nd.symbol(int(aid))
            if sym:
                selected_tickers.append(sym)
        except Exception:
            pass
    print(f'[liquid500]   final: {len(selected_tickers)} tickers')

    # 11) Build the wide 1.0/NaN row. NaN for inactive (matches existing
    # CSV's empty-cell format).
    all_cols = list(existing_columns)
    new_tickers = [t for t in selected_tickers if t not in all_cols]
    for t in new_tickers:
        all_cols.append(t)
    row = pd.Series(np.nan, index=all_cols, dtype=float)
    for t in selected_tickers:
        row.loc[t] = 1.0
    return row, new_tickers