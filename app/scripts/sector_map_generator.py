"""
sector_map_generator.py

Patch 182: generate the TRBC sector map for a universe, directly from
Norgate. Writes one file per universe so every map can be generated and
eyeballed side by side before any of them goes near the engine.

WHY A SEPARATE SCRIPT
---------------------
The sector map is a static artifact: symbol -> TRBC level 1..5, no date
column. CsvDataStore.FIELD_TO_FILENAME does not list it, so
write_universe / read_universe and universe_today_refresh deliberately
leave it alone (universe_today_refresh.py docstring, "NOT TOUCHED BY THIS
SERVICE"). It is generated on demand, not nightly.

OUTPUT CONTRACT -- dictated by PriceDataLoader.load_sector_mapping():

    df = pd.read_csv(sector_path, sep='\t', header=None,
                     names=['symbol','level_1','level_2',
                            'level_3','level_4','level_5'])
    df.set_index('symbol', inplace=True)

  -> tab separated
  -> NO header row     (header=None means a header line becomes a data row
                        indexed 'Symbol' with level_1 == 'Level 1')
  -> exactly 6 columns (symbol, level_1..level_5)
  -> symbol first      (it becomes the index)

Unclassified cells are written as the literal token 'None', matching the
existing SP500 artifact. 'None' is in pandas' default NA list, so the
reader turns them back into NaN either way -- the token only exists so the
files diff cleanly against the SP500 one.

FILENAMES -- TEST OUTPUT IS NOT A PRODUCTION DROP-IN
----------------------------------------------------
Default output is  <out-dir>/<slug>_INDUSTRIES.csv  -- flat, one file per
universe, so --all can fill a single test folder without the universes
overwriting each other. NOTHING READS THESE. They are for inspection and
diffing only.

The reader opens exactly one name, 'SnP_500_INDUSTRIES.csv' (READER_FILENAME
below), hardcoded in load_sector_mapping() for BOTH the primary lookup
(<base_path>/) and the Patch-49 fallback (PricePath.sp500base_path/). To
promote a map to production it must be copied to
universes/<slug>/SnP_500_INDUSTRIES.csv -- under that name, in that
universe's own folder, the primary lookup hits and the sp500 fallback never
fires, so no reader change is needed.

Put it anywhere else, or under any other name, and the primary lookup MISSES
-> load_sector_mapping() silently falls back to SP500's map, and a Russell
3000 backtest ranks against SP500 sectors with no error and no log line.
Writing READER_FILENAME requires --force for exactly that reason.

USAGE
-----
    # every universe into the test folder, nothing production can see
    python -m app.utiliy.universeGenerations.sector_map_generator --all --dry-run
    python -m app.utiliy.universeGenerations.sector_map_generator --all

    # one universe
    python -m app.utiliy.universeGenerations.sector_map_generator --universe russell3000

    # promote to production (deliberate, guarded)
    python -m app.utiliy.universeGenerations.sector_map_generator \
        --universe russell3000 \
        --out-dir "C:\\Tharun\\Projects\\backtest_data\\universes\\russell3000" \
        --filename SnP_500_INDUSTRIES.csv --force
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import traceback
from pathlib import Path

import pandas as pd

# ── TRBC classification constants ─────────────────────────────────────────
# Mirrors liquid500_membership._classify_trbc_l1 (TRBC / 'Name') and the
# legacy AUTO_LIQ create_industries* functions, widened to all five levels.
SCHEME_NAME = 'TRBC'
RESULT_TYPE = 'Name'
ALL_LEVELS = (1, 2, 3, 4, 5)

# ── Output contract ───────────────────────────────────────────────────────
SEP = '\t'
WRITE_HEADER = False              # reader passes header=None
NA_TOKEN = 'None'

# The ONE name PriceDataLoader.load_sector_mapping() opens. Test output must
# never use it -- see FILENAMES in the module docstring.
READER_FILENAME = 'SnP_500_INDUSTRIES.csv'

TEST_ROOT = r'C:\Tharun\Projects\backtest_data\test'
UNIVERSES_ROOT = r'C:\Tharun\Projects\backtest_data\universes'

# Watchlist-driven universes. Keys match UNIVERSES_Codes / UniverseSpec
# slugs; values match UniverseSpec.universe, which UniverseProvider turns
# into '<universe> Current & Past'.
SLUG_TO_UNIVERSE = {
    'sp500':       'S&P 500',
    'sp1500':      'S&P Composite 1500',
    'russell3000': 'Russell 3000',
    'sp100':       'S&P 100',
    'nasdaq100':   'Nasdaq 100',
}

# Membership-driven universes: no Norgate watchlist exists, so the symbol
# list comes from the stored membership frame written by
# CsvDataStore.write_universe -- universes/<slug>/<slug>.csv, Date-indexed,
# columns are tickers (storage.py: 'DataFrame(dates x tickers)').
MEMBERSHIP_DRIVEN = ('liquid500',)

ALL_SLUGS = tuple(sorted(SLUG_TO_UNIVERSE)) + MEMBERSHIP_DRIVEN

# Norgate delisted-symbol convention: TICKER-YYYYMM (e.g. 'AABA-201910',
# 'AFS.A-200011'). Share classes use '.', so an anchored -YYYYMM is a safe
# delisted test.
_DELISTED_RE = re.compile(r'-\d{6}$')


def sector_filename(slug):
    """Test-output filename. Per-slug so --all can share one flat folder."""
    return '{}_INDUSTRIES.csv'.format(slug)


def _watchlist_symbols(nd, universe):
    """Every ticker ever in the universe, current + past.

    Same source UniverseProvider._build_from_index() uses, so the sector map
    keys line up with the membership columns by construction.
    """
    watchlist = '{} Current & Past'.format(universe)
    symbols = nd.watchlist_symbols(watchlist)
    if not symbols:
        raise RuntimeError(
            "Norgate watchlist {!r} returned no symbols. Check the watchlist "
            "exists and Norgate Data Updater has run.".format(watchlist)
        )
    return sorted(symbols)


def _membership_symbols(slug, universes_root):
    """Ever-members from the stored membership frame's column headers.

    nrows=0 reads the header line only -- liquid500.csv is ~54MB and the
    body is not needed to enumerate tickers.
    """
    mp = Path(universes_root) / slug / '{}.csv'.format(slug)
    if not mp.exists():
        raise FileNotFoundError(
            '{} has no Norgate watchlist, so its symbols come from the stored '
            'membership frame, and {} does not exist. Run universe_pipeline '
            'for {} first.'.format(slug, mp, slug)
        )
    cols = list(pd.read_csv(mp, index_col=0, nrows=0).columns)
    if not cols:
        raise RuntimeError('{} has no ticker columns.'.format(mp))
    return sorted(cols)


def resolve_symbols(nd, slug, universes_root=UNIVERSES_ROOT):
    if slug in SLUG_TO_UNIVERSE:
        universe = SLUG_TO_UNIVERSE[slug]
        print('[{}] Norgate watchlist {!r} Current & Past'.format(
            slug, universe))
        return _watchlist_symbols(nd, universe)
    if slug in MEMBERSHIP_DRIVEN:
        print('[{}] membership frame (no Norgate watchlist)'.format(slug))
        return _membership_symbols(slug, universes_root)
    raise KeyError(
        '{!r} is not a known universe. Known: {}'.format(
            slug, ', '.join(ALL_SLUGS))
    )


def classify(nd, symbols, levels=ALL_LEVELS, cache=None, tag='',
             progress_every=500):
    """symbol -> TRBC level_1..level_N. Fail-fast, collected.

    A Norgate return of None is DATA (the symbol genuinely has no
    classification at that level -- see the Real Estate / level_2 hole in
    coverage_report), not an error. An exception is a failure; failures are
    collected and raised together before anything touches disk, so one bad
    symbol neither kills a long run mid-way nor slips through silently.

    `cache` is an optional symbol -> {level: name} dict shared across
    universes in --all mode. Classification is a property of the ASSET, not
    the universe, and sp500 is a subset of russell3000 -- without it the
    overlap gets re-queried once per universe.
    """
    rows = []
    failures = []
    total = len(symbols)
    hits = 0

    for i, sym in enumerate(symbols, 1):
        cached = cache.get(sym) if cache is not None else None
        if cached is not None:
            hits += 1
            row = {'symbol': sym}
            row.update({'level_{}'.format(l): cached.get(l) for l in levels})
            rows.append(row)
            continue

        row = {'symbol': sym}
        resolved = {}
        for lvl in levels:
            try:
                c = nd.classification_at_level(sym, SCHEME_NAME,
                                               RESULT_TYPE, lvl)
            except Exception as exc:                  # noqa: BLE001
                failures.append((sym, lvl, '{}: {}'.format(
                    type(exc).__name__, exc)))
                c = None
            c = c if c else None
            resolved[lvl] = c
            row['level_{}'.format(lvl)] = c
        rows.append(row)
        if cache is not None:
            cache[sym] = resolved

        if progress_every and i % progress_every == 0:
            print('[{}] classified {}/{}'.format(tag, i, total))

    if failures:
        detail = '\n'.join('  {} level {}: {}'.format(s, l, e)
                           for s, l, e in failures[:20])
        raise RuntimeError(
            'classification_at_level raised for {} symbol/level pair(s); '
            'nothing written. First 20:\n{}'.format(len(failures), detail)
        )

    if cache is not None and hits:
        print('[{}] {}/{} symbols served from cache'.format(tag, hits, total))

    cols = ['symbol'] + ['level_{}'.format(l) for l in levels]
    return pd.DataFrame(rows, columns=cols).set_index('symbol')


def coverage_report(df, tag=''):
    """Per-level coverage, split listed vs delisted, printed before the write.

    On the SP500 artifact this reports 560 of 1187 delisted symbols (47%)
    unclassified at every level, and 46 symbols carrying level_1
    'Real Estate' with level_2 None but level_3/4/5 populated -- TRBC has no
    Business Sector under Real Estate. A level_2 sector cap therefore buckets
    every REIT together. The hierarchy has holes: level_1 present does NOT
    imply level_2 present.
    """
    delisted = df.index.to_series().str.contains(_DELISTED_RE)

    print('[{}] {} symbols: {} listed, {} delisted'.format(
        tag, len(df), int((~delisted).sum()), int(delisted.sum())))

    for col in df.columns:
        miss = df[col].isna()
        print('[{}]   {}: {:5d} classified, {:5d} None '
              '({:4d} listed / {:4d} delisted), {:4d} distinct'.format(
                  tag, col,
                  int((~miss).sum()), int(miss.sum()),
                  int((miss & ~delisted).sum()),
                  int((miss & delisted).sum()),
                  int(df[col].nunique())))

    # Hierarchy holes: a level is None while a DEEPER level is populated.
    cols = list(df.columns)
    for i, col in enumerate(cols[:-1]):
        deeper = cols[i + 1]
        holes = df[df[col].isna() & df[deeper].notna()]
        if len(holes):
            parents = sorted(df.loc[holes.index, cols[0]].dropna().unique())
            print('[{}]   HOLE: {} None but {} populated for {} symbol(s); '
                  'level_1 = {}'.format(
                      tag, col, deeper, len(holes), parents or ['None']))


def write_sector_map(df, out_dir, filename, force=False, tag=''):
    """Atomic write. --force required to write a reader-visible file."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / filename

    if filename == READER_FILENAME and not force:
        raise PermissionError(
            'Refusing to write {} without --force. That is the exact name '
            'PriceDataLoader.load_sector_mapping() opens, so this write is a '
            'live production change, not a test artifact. Re-run with --force '
            'if that is what you mean.'.format(dest)
        )

    tmp = out_dir / (filename + '.tmp')
    df.to_csv(tmp, sep=SEP, header=WRITE_HEADER, na_rep=NA_TOKEN)
    os.replace(tmp, dest)     # atomic on the same volume
    print('[{}] wrote {} rows x {} cols -> {}'.format(
        tag or 'sector_map', len(df), len(df.columns), dest))
    return dest


def generate(slug, out_dir=TEST_ROOT, filename=None, levels=ALL_LEVELS,
             universes_root=UNIVERSES_ROOT, dry_run=False, force=False,
             cache=None):
    import norgatedata as nd    # lazy: importing this module shouldn't need Norgate

    symbols = resolve_symbols(nd, slug, universes_root=universes_root)
    print('[{}] {} symbols; classifying TRBC levels {}'.format(
        slug, len(symbols), list(levels)))

    df = classify(nd, symbols, levels=levels, cache=cache, tag=slug)
    coverage_report(df, tag=slug)

    if dry_run:
        print('[{}] --dry-run: nothing written'.format(slug))
        return df

    write_sector_map(df, out_dir, filename or sector_filename(slug),
                     force=force, tag=slug)
    return df


def generate_all(slugs=ALL_SLUGS, out_dir=TEST_ROOT, levels=ALL_LEVELS,
                 universes_root=UNIVERSES_ROOT, dry_run=False, force=False):
    """Per-universe isolation: one universe failing must not abort the rest.

    Same rule the nightly chain follows. Failures are reported together at
    the end and set a non-zero exit.
    """
    cache = {}
    failed = []
    for slug in slugs:
        print('\n' + '=' * 62)
        try:
            generate(slug, out_dir=out_dir, levels=levels,
                     universes_root=universes_root, dry_run=dry_run,
                     force=force, cache=cache)
        except Exception as exc:                      # noqa: BLE001
            failed.append((slug, '{}: {}'.format(type(exc).__name__, exc)))
            print('[{}] FAILED -- continuing with the rest'.format(slug))
            traceback.print_exc()

    print('\n' + '=' * 62)
    print('[sector_map] {}/{} universes ok, {} distinct symbols classified'
          .format(len(slugs) - len(failed), len(slugs), len(cache)))
    if failed:
        for slug, err in failed:
            print('[sector_map] FAILED {}: {}'.format(slug, err))
    return failed


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Generate universe TRBC sector maps from Norgate.')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--universe', help='universe slug. Known: {}'.format(
        ', '.join(ALL_SLUGS)))
    g.add_argument('--all', action='store_true',
                   help='every known universe into --out-dir')
    ap.add_argument('--out-dir', default=TEST_ROOT,
                    help='output folder (default: {})'.format(TEST_ROOT))
    ap.add_argument('--filename',
                    help='override the output filename (single --universe '
                         'only; default <slug>_INDUSTRIES.csv)')
    ap.add_argument('--universes-root', default=UNIVERSES_ROOT,
                    help='Folder A universes root, for membership-driven '
                         'universes (default: {})'.format(UNIVERSES_ROOT))
    ap.add_argument('--dry-run', action='store_true',
                    help='classify and report coverage, write nothing')
    ap.add_argument('--force', action='store_true',
                    help='allow writing {}'.format(READER_FILENAME))
    args = ap.parse_args(argv)

    if not args.all and not args.universe:
        ap.error('pass --universe <slug> or --all')
    if args.all and args.filename:
        ap.error('--filename is single-universe only; --all derives '
                 '<slug>_INDUSTRIES.csv per universe')

    if args.all:
        failed = generate_all(out_dir=args.out_dir,
                              universes_root=args.universes_root,
                              dry_run=args.dry_run, force=args.force)
        return 1 if failed else 0

    generate(args.universe, out_dir=args.out_dir, filename=args.filename,
             universes_root=args.universes_root, dry_run=args.dry_run,
             force=args.force)
    return 0


if __name__ == '__main__':
    sys.exit(main())