"""
universe_crsi.py — extend {universe}_CRSI_R{rsi}_U{updown}_X{roc}.csv with new bars.

PURPOSE
-------
Connors RSI is precomputed for tickers across selected universes (liquid500,
sp500, ...) and stored as a CSV per universe, indexed by date. Strategies
that use the `crsi` rule indicator read these files directly via
GeneratePricesIndicators.py.

Without this service the CRSI file would be static seed data — once written,
never refreshed. A live strategy needing today's CRSI would either miss
today's row entirely (KeyError) or read forward-filled stale CRSI.

Patch 93 closes that gap with a UNIVERSE-AGNOSTIC service. The shape
mirrors the legacy exe_update_CRSI() from Russell_3000_DATA_mark2.py:
  1. Read existing CRSI for the variant to find last_stored
  2. Load the last N days of daily_closes (warmup window)
  3. Compute CRSI on that window for ALL columns (active tickers,
     including any added by the most recent monthly rebalance)
  4. Stitch: keep existing rows older than the new-data first-valid
     date, take new rows from there forward
  5. Backup current CRSI file before write (loud-fail if backup misses)
  6. Write, prune old versions of THAT variant only

FILENAME CONVENTION (Patch 93)
------------------------------
Each combination of (universe_slug, RSI_length, UpDown_length, ROC_length)
gets its own file via universe_crsi_filename():

    {universe_slug}_CRSI_R{rsi}_U{updown}_X{roc}.csv

Default params (matching the legacy `create_crsi(closes, 3, 2, 100)`):
    liquid500_CRSI_R3_U2_X100.csv
    sp500_CRSI_R3_U2_X100.csv

Backups share the same naming so pruning is variant-scoped:
    _crsi_versions/liquid500_CRSI_R3_U2_X100_20260630_193512.csv

LEGACY MIGRATION
----------------
The legacy convention used universe-specific prefixes without parameter
suffix: Lq500CRSI.csv (liquid500), sp500CRSI.csv (sp500). On first run at
default params the service auto-migrates the legacy file to the new
parameter-aware filename, preserving any seeding work.

CRSI MATH
---------
CRSI = (RSI(prices, R) + RSI(streak, U) + ROC_RSI(prices, X)) / 3
Defaults match the legacy: R=3, U=2, X=100. Math implemented in
IndicatorCalculator.CRSI (TechnicalIndicators.py).

USAGE
-----
Called from two places, with the SAME signature for both:

  - universe_today_refresh (manual "Update today's prices" button):
        extend_universe_crsi(
            universe_slug='liquid500',
            base_path=PricePath.liquid500base_path,
            end_date=today,
        )

  - live_universe_pipeline (nightly Trigger Nightly):
        extend_universe_crsi(
            universe_slug='liquid500',
            base_path=PricePath.liquid500_live_base_path,
            end_date=today,
        )

To add CRSI maintenance for another universe (e.g., sp500), call the same
function with universe_slug='sp500' and the matching base_path. No
universe-specific code anywhere.
"""

from __future__ import annotations
import datetime as dt
import re
import shutil
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from app.loader.TechnicalIndicators import IndicatorCalculator


# ── Config — defaults match legacy exe_update_CRSI / create_crsi calls ────
# Patch 100: RSI default corrected 3 -> 2. The legacy RPT generator calls
# CRSI(prices, RSI_length=strategy_params['crsi_length'], UpDown_length=2,
# ROC_length=100) with crsi_length = 2 — i.e. R2/U2/X100, NOT R3. The old
# R3 default mislabelled the migrated legacy (R2) seed file and appended
# R3-computed rows onto it, producing the observed CRSI mismatches.
CRSI_RSI_LENGTH    = 2       # legacy: create_crsi(..., crsi_length=2, 2, 100)
CRSI_UPDOWN_LENGTH = 2       # legacy
CRSI_ROC_LENGTH    = 100     # legacy
CRSI_LOOKBACK_DAYS = 150     # legacy: data.daily_closes.iloc[-150:]

VERSIONS_FOLDER_NAME = '_crsi_versions'
MAX_VERSIONS_TO_KEEP = 30

# Pre-Patch 93 filenames — used only by the one-time migration helper.
# Add an entry whenever an existing CRSI file follows a legacy non-
# parameter-aware naming pattern. None means "no legacy file to look for".
LEGACY_CRSI_FILENAMES: dict = {
    'liquid500': 'Lq500CRSI.csv',
    'sp500':     'sp500CRSI.csv',
}


def universe_crsi_filename(universe_slug: str,
                           rsi_length: int    = CRSI_RSI_LENGTH,
                           updown_length: int = CRSI_UPDOWN_LENGTH,
                           roc_length: int    = CRSI_ROC_LENGTH) -> str:
    """Construct the parameter-aware CRSI filename for any universe.

    Pattern: {universe_slug}_CRSI_R{rsi}_U{updown}_X{roc}.csv

    Writer (manual button, live pipeline) and reader
    (GeneratePricesIndicators) both build paths through this helper so
    the convention stays in one place. Changing the pattern here changes
    it everywhere.
    """
    return f'{universe_slug}_CRSI_R{rsi_length}_U{updown_length}_X{roc_length}.csv'


def extend_universe_crsi(
    universe_slug: str,
    base_path: str | Path,
    end_date: dt.date,
    rsi_length: int = CRSI_RSI_LENGTH,
    updown_length: int = CRSI_UPDOWN_LENGTH,
    roc_length: int = CRSI_ROC_LENGTH,
    lookback_days: int = CRSI_LOOKBACK_DAYS,
) -> dict:
    """Extend the CRSI CSV under ``base_path`` for ``universe_slug`` and
    the given parameter set, computing CRSI from ``base_path/daily_closes.csv``.

    Returns a summary dict with universe / crsi_action / last_stored /
    last_written / rows_appended / new_tickers / backup_path /
    versions_pruned / num_tickers / params / filename / migrated_from.
    """
    base_path = Path(base_path)
    closes_csv_path = base_path / 'daily_closes.csv'
    crsi_filename = universe_crsi_filename(
        universe_slug, rsi_length, updown_length, roc_length,
    )
    crsi_csv_path = base_path / crsi_filename

    if not closes_csv_path.exists():
        raise FileNotFoundError(
            f'Cannot extend CRSI for {universe_slug}: missing daily_closes '
            f"at {closes_csv_path}. Click 'Update today's prices' (or run "
            f'Trigger Nightly Step 1) first so the closes file exists.'
        )

    # 0) Legacy migration. If this is the default-params variant AND a
    #    legacy un-suffixed file exists AND the new file does not, rename
    #    so prior seeding work is preserved.
    migrated_from: Optional[str] = None
    is_default_variant = (
        rsi_length == CRSI_RSI_LENGTH
        and updown_length == CRSI_UPDOWN_LENGTH
        and roc_length == CRSI_ROC_LENGTH
    )
    if not crsi_csv_path.exists() and is_default_variant:
        legacy_name = LEGACY_CRSI_FILENAMES.get(universe_slug)
        if legacy_name:
            legacy_path = base_path / legacy_name
            if legacy_path.exists():
                try:
                    shutil.move(str(legacy_path), str(crsi_csv_path))
                    migrated_from = str(legacy_path)
                    print(f'[universe_crsi:{universe_slug}] migrated legacy '
                          f'{legacy_path.name} -> {crsi_csv_path.name}')
                except Exception as e:
                    # Non-fatal — proceed as if no legacy was found.
                    print(f'[universe_crsi:{universe_slug}] legacy migration '
                          f'failed ({type(e).__name__}: {e}); continuing.')

    # 1) Read existing CRSI (None on first-ever run for this variant)
    existing: Optional[pd.DataFrame] = None
    if crsi_csv_path.exists():
        try:
            existing = pd.read_csv(
                crsi_csv_path, index_col='Date', parse_dates=True
            )
        except Exception as e:
            raise RuntimeError(
                f'Could not read existing CRSI file {crsi_csv_path}: {e}. '
                f'Delete or restore the file before retrying.'
            )

    last_stored: Optional[dt.date] = (
        existing.index.max().date()
        if existing is not None and not existing.empty
        else None
    )

    end_ts = pd.Timestamp(end_date)

    # 2) Already current? noop.
    if last_stored is not None and last_stored >= end_date:
        return _summary(
            universe_slug=universe_slug,
            crsi_df=existing,
            last_stored=last_stored,
            last_written=last_stored,
            action='NOOP',
            rows_appended=0,
            new_tickers=[],
            backup_path=None,
            versions_pruned=0,
            rsi_length=rsi_length,
            updown_length=updown_length,
            roc_length=roc_length,
            filename=crsi_csv_path.name,
            migrated_from=migrated_from,
        )

    # 3) Read closes — bound the end, take the warmup window
    closes = pd.read_csv(
        closes_csv_path, index_col=0, parse_dates=True,
    )
    closes = closes.loc[:end_ts]               # never compute past end_date
    if closes.empty:
        raise RuntimeError(
            f'daily_closes at {closes_csv_path} has no rows ≤ {end_date}.'
        )
    # Patch 100: window policy.
    #   First-ever write (existing is None) -> FULL closes history, so a
    #   newly generated variant has CRSI for the entire backtest range
    #   (a 150-bar window would leave all earlier history empty).
    #   Incremental extend -> warmup window, generalized so variants with
    #   large params (e.g. roc_length=200) always get enough bars.
    if existing is not None:
        eff_lookback = max(
            lookback_days,
            max(rsi_length, updown_length, roc_length) + 30,
        )
        closes = closes.iloc[-eff_lookback:]   # last N days for warmup

    # 4) Compute CRSI on the window for all active tickers
    print(f'[universe_crsi:{universe_slug}] computing CRSI on '
          f'{len(closes)} bars × {len(closes.columns)} tickers '
          f'(R={rsi_length}/U={updown_length}/X={roc_length}) ...')
    new_crsi = IndicatorCalculator.CRSI(
        closes,
        RSI_length=rsi_length,
        UpDown_length=updown_length,
        ROC_length=roc_length,
    )
    new_crsi = new_crsi.replace([np.inf, -np.inf], np.nan)

    # 5) Find first valid row in new_crsi (after warmup) — drop pure-NaN head
    valid_mask = ~new_crsi.isna().all(axis=1)
    if not valid_mask.any():
        # Window too small to produce any valid CRSI rows. Surface loudly.
        raise RuntimeError(
            f'No valid CRSI rows produced from {len(closes)}-day window for '
            f'{universe_slug}. Increase lookback_days (current '
            f'{lookback_days}; needs > max(R, U, X) = '
            f'{max(rsi_length, updown_length, roc_length)}).'
        )
    first_valid = new_crsi.index[valid_mask.values.argmax()]

    # 6) Stitch — old rows before first_valid, new rows from first_valid on
    if existing is not None:
        # Drop existing rows from first_valid onwards (they're being replaced)
        existing_kept = existing.loc[existing.index < first_valid]
        new_section = new_crsi.loc[first_valid:]
        # Column union: surface new tickers, preserve old ones
        all_cols = sorted(set(existing_kept.columns) | set(new_section.columns))
        existing_kept = existing_kept.reindex(columns=all_cols)
        new_section = new_section.reindex(columns=all_cols)
        merged = pd.concat([existing_kept, new_section], axis=0)
        new_ticker_set = sorted(
            set(new_section.columns) - set(existing.columns)
        )
    else:
        # First-ever write for this variant — take everything from
        # first_valid onwards
        merged = new_crsi.loc[first_valid:]
        new_ticker_set = sorted(list(merged.columns))

    merged.index.name = 'Date'
    merged = merged.sort_index()
    merged = merged[~merged.index.duplicated(keep='last')]

    # 7) Compute rows appended vs prior last_stored
    if last_stored is not None:
        rows_appended = int((merged.index > pd.Timestamp(last_stored)).sum())
    else:
        rows_appended = len(merged)

    # 8) Backup BEFORE write — loud-fail if it doesn't land
    backup_path = None
    if crsi_csv_path.exists():
        backup_path = _backup_csv(crsi_csv_path)

    crsi_csv_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(crsi_csv_path, index=True, index_label='Date')

    # 9) Prune old backups of THIS variant only (non-fatal)
    pruned = 0
    if backup_path is not None:
        pruned = _prune_old_versions(
            backup_path.parent, crsi_csv_path.stem, MAX_VERSIONS_TO_KEEP,
        )

    last_written: dt.date = merged.index.max().date()
    return _summary(
        universe_slug=universe_slug,
        crsi_df=merged,
        last_stored=last_stored,
        last_written=last_written,
        action='EXTENDED' if existing is not None else 'CREATED',
        rows_appended=rows_appended,
        new_tickers=new_ticker_set,
        backup_path=str(backup_path) if backup_path else None,
        versions_pruned=pruned,
        rsi_length=rsi_length,
        updown_length=updown_length,
        roc_length=roc_length,
        filename=crsi_csv_path.name,
        migrated_from=migrated_from,
    )


# ──────────────────────────────────────────────────────────────────────
#  Patch 100 — variant sweep + full-history regenerate
# ──────────────────────────────────────────────────────────────────────

# Parses parameter-aware CRSI filenames back into (slug, R, U, X).
CRSI_VARIANT_RE = re.compile(
    r'^(?P<slug>.+)_CRSI_R(?P<r>\d+)_U(?P<u>\d+)_X(?P<x>\d+)\.csv$'
)


def sweep_universe_crsi_variants(
    universe_slug: str,
    base_path: str | Path,
    end_date: dt.date,
) -> list:
    """Patch 100: extend EVERY existing CRSI variant file for a universe.

    Globs {slug}_CRSI_R*_U*_X*.csv under base_path, parses each filename
    back into (R, U, X) via CRSI_VARIANT_RE, and runs the normal
    incremental extend on each. Universes with no variant files return []
    (no-op) — CREATION of a variant happens on demand in
    GeneratePricesIndicators (sync, full history) or via the regenerate
    endpoint, never here. Per-variant failures are captured in the
    returned summaries, not raised, so one bad variant can't block the
    others.
    """
    base = Path(base_path)
    if not base.exists():
        return []
    summaries = []
    for f in sorted(base.glob(f'{universe_slug}_CRSI_R*_U*_X*.csv')):
        m = CRSI_VARIANT_RE.match(f.name)
        if not m or m.group('slug') != universe_slug:
            continue
        try:
            summaries.append(extend_universe_crsi(
                universe_slug=universe_slug,
                base_path=base,
                end_date=end_date,
                rsi_length=int(m.group('r')),
                updown_length=int(m.group('u')),
                roc_length=int(m.group('x')),
            ))
        except Exception as e:
            summaries.append({
                'universe': universe_slug,
                'filename': f.name,
                'crsi_action': 'ERROR',
                'reason': f'{type(e).__name__}: {e}',
            })
    return summaries


def regenerate_universe_crsi(
    universe_slug: str,
    base_path: str | Path,
    end_date: dt.date,
    rsi_length: int = CRSI_RSI_LENGTH,
    updown_length: int = CRSI_UPDOWN_LENGTH,
    roc_length: int = CRSI_ROC_LENGTH,
) -> dict:
    """Patch 100: FULL-HISTORY regenerate of one CRSI variant.

    Use after Norgate restatements (TOTALRETURN rescale) or whenever a
    stored variant is suspected of stitch contamination: backs up the
    existing file to _crsi_versions/, removes it, then calls
    extend_universe_crsi — which, with no existing file, computes CRSI
    over the ENTIRE daily_closes history and writes a clean variant.
    Loud-fails if the backup doesn't land; the original is never deleted
    without a confirmed on-disk backup.
    """
    base = Path(base_path)
    crsi_path = base / universe_crsi_filename(
        universe_slug, rsi_length, updown_length, roc_length,
    )
    if crsi_path.exists():
        _backup_csv(crsi_path)          # loud-fail inside if backup misses
        crsi_path.unlink()
        print(f'[universe_crsi:{universe_slug}] regenerate: removed '
              f'{crsi_path.name} (backup taken) — full-history recompute')
    summary = extend_universe_crsi(
        universe_slug=universe_slug,
        base_path=base,
        end_date=end_date,
        rsi_length=rsi_length,
        updown_length=updown_length,
        roc_length=roc_length,
    )
    summary['crsi_action'] = 'REGENERATED'
    return summary


# ──────────────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────────────

def _summary(universe_slug: str, crsi_df: Optional[pd.DataFrame],
             last_stored: Optional[dt.date], last_written: Optional[dt.date],
             action: str, rows_appended: int, new_tickers: list,
             backup_path: Optional[str], versions_pruned: int,
             rsi_length: int, updown_length: int, roc_length: int,
             filename: str, migrated_from: Optional[str]) -> dict:
    return {
        'universe':          universe_slug,
        'last_stored':       last_stored.isoformat() if last_stored else None,
        'last_written':      last_written.isoformat() if last_written else None,
        'crsi_action':       action,
        'rows_appended':     rows_appended,
        'new_tickers':       new_tickers,
        'num_tickers':       int(crsi_df.shape[1]) if crsi_df is not None else 0,
        'backup_path':       backup_path,
        'versions_pruned':   versions_pruned,
        'params': {
            'rsi_length':    rsi_length,
            'updown_length': updown_length,
            'roc_length':    roc_length,
        },
        'filename':          filename,
        'migrated_from':     migrated_from,
    }


def _backup_csv(csv_path: Path) -> Optional[Path]:
    """Snapshot the existing CRSI file to _crsi_versions/ BEFORE the new
    content is written. Backup filename embeds the source stem so
    different parameter variants and universes never share backups:

        liquid500_CRSI_R3_U2_X100_20260630_193512.csv

    Loud-fails if the copy didn't land — we do not overwrite an existing
    CRSI file without a confirmed prior-state backup on disk.
    """
    if not csv_path.exists():
        return None

    versions_dir = csv_path.parent / VERSIONS_FOLDER_NAME
    versions_dir.mkdir(parents=True, exist_ok=True)

    stem = csv_path.stem  # e.g. 'liquid500_CRSI_R3_U2_X100'
    ts = dt.datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = versions_dir / f'{stem}_{ts}.csv'

    # Same-second collision (rare double-click): append a numeric suffix.
    if backup_path.exists():
        for i in range(1, 100):
            alt = versions_dir / f'{stem}_{ts}_{i}.csv'
            if not alt.exists():
                backup_path = alt
                break
        else:
            raise RuntimeError(
                f'CRSI backup name collision could not be resolved at {ts}'
            )

    try:
        shutil.copy2(csv_path, backup_path)
    except Exception as e:
        raise RuntimeError(
            f'CRSI backup failed before write: {csv_path} -> {backup_path}: {e}'
        )
    if not backup_path.exists() or backup_path.stat().st_size == 0:
        raise RuntimeError(
            f'CRSI backup landed missing or empty: {backup_path}'
        )
    print(f'[universe_crsi] backed up to {backup_path}')
    return backup_path


def _prune_old_versions(versions_dir: Path, stem_prefix: str,
                        keep: int) -> int:
    """Delete oldest backup files for the SPECIFIC variant/universe
    beyond the keep-N cap. Other variants or other universes in the same
    _crsi_versions/ folder are untouched. Non-fatal per-file failures
    are warned and continued.
    """
    if not versions_dir.exists():
        return 0
    # Match: {stem_prefix}_YYYYMMDD_HHMMSS(.N)?.csv
    pattern = re.compile(
        rf'^{re.escape(stem_prefix)}_\d{{8}}_\d{{6}}(_\d+)?\.csv$'
    )
    matching = [
        p for p in versions_dir.iterdir() if pattern.match(p.name)
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
            print(f'[universe_crsi] warn: failed to prune {p}: {e}')
    if deleted:
        print(f'[universe_crsi] pruned {deleted} old version(s) of '
              f'{stem_prefix}; kept {keep}')
    return deleted