"""
universe_today_refresh.py
=========================
Append-only daily extension of the STATIC backtest universes
(backtest_data/universes/) and the shared index series
(backtest_data/universes/index/) up to the latest *posted* Norgate session.

This is the cheap "bring the backtest up to today" button. It does NOT do a
full re-pull (that is universe_pipeline.py / generate_index_prices.py). It only
adds bars for trading days that are not already in the files.

WHY APPEND-ONLY — NEVER RE-PULL  (the load-bearing design rule)
---------------------------------------------------------------
Backtest entries must reproduce the already-validated tradelist on days that
are signed off. The PullBack indicators (RSI(2), IBS, consec_down, SMA150,
HV20) all compute off TOTALRETURN-adjusted closes. If we re-pulled history,
Norgate hands back its *restated* adjusted series — a recent ex-dividend /
split retro-rescales the whole column — which shifts the indicator values, and
therefore the entries, on days that are already validated. That is exactly the
parity we must not break. So:

    Existing rows are NEVER touched. Only dates strictly after the last stored
    bar (and not already present) are appended.

RESIDUAL SEAM (detected, not silently written through)
-------------------------------------------------------
A strict append cannot *remove* a restatement seam either: if a name goes
ex-div on an appended day, its new bar sits on a freshly re-anchored adjusted
scale versus the frozen history -> a one-name discontinuity on *new*
(unvalidated) days only — it can never corrupt the historical entries that a
re-pull would. To make that loud rather than silent, each universe/index pull
includes ONE overlap bar (the last date already stored). If the freshly pulled
value for that overlap date differs from what is on disk, the column was
restated since the last update; the affected tickers are reported back so a
later backtest-vs-live mismatch on that name is recognisable as a corporate
action, not an execution bug. (Reported, never auto-corrected — append-only.)

SCOPE — sources of truth (mapped in code, auto-synced)
------------------------------------------------------
  - universes    : REGISTRY     (app.utiliy.universeGenerations.universe_registry)
  - index series : INDEX_REGISTRY (app.utiliy.generate_index_prices)
Uncommenting a spec / index key there makes this button pick it up with no code
change here. A universe/index with no base on disk is SKIPPED — this button
extends an existing base, it does not create one.

CONCURRENCY
-----------
Norgate is pulled SEQUENTIALLY, in-process. The pipeline's PriceProvider /
UniverseProvider fan out with multiprocessing, which is unsafe to spawn inside
the uvicorn worker on Windows (the detached-process pattern in eod.py exists
precisely to keep that fan-out OUT of the web process). This service is meant
to be called synchronously from a request handler, so it never forks. The pull
window is only a handful of days, so even ~1000 current+past tickers complete
in tens of seconds.

NOT TOUCHED BY THIS SERVICE
---------------------------
SnP_500_INDUSTRIES.csv (sector map — static, no date column) and
sp500CRSI.csv (precomputed CRSI — needs full history to recompute) are not in
the CsvDataStore field map, so read/write_universe leave them alone. PullBack
uses neither. A CRSI strategy would still need its CRSI file extended
separately — out of scope here.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal

from app.utiliy.universeGenerations.storage import (
    CsvDataStore, DataStore, UniverseDataset,
)
from app.utiliy.universeGenerations.universe_registry import REGISTRY, UniverseSpec
from app.utiliy.generate_index_prices import INDEX_REGISTRY, INDEX_FOLDER
from app.constants.PricePath import PricePath
from app.services.exec_data_refresh import resolve_data_date

# Relative tolerance for the overlap-bar restatement check. A stored vs freshly
# pulled adjusted close differing by more than this on the SAME date means the
# column was retro-restated by a corporate action since the last update.
RESTATEMENT_REL_TOL = 0.001  # 0.1 %

# Default Norgate fields when a spec doesn't override (mirrors
# PriceProvider.DEFAULT_FIELDS so appended columns match the pipeline's).
DEFAULT_FIELDS = ['Open', 'High', 'Low', 'Close',
                  'Volume', 'Turnover', 'Unadjusted Close']

# Folder A — the static backtest universes this service extends.
UNIVERSES_ROOT = PricePath.backtestPath + r'\universes'


# ──────────────────────────────────────────────────────────────────────────
#  Calendar helpers
# ──────────────────────────────────────────────────────────────────────────
def _nyse_days_after(last_stored: dt.date, end_date: dt.date) -> list:
    """NYSE trading days strictly after `last_stored`, up to and including
    `end_date`. Empty if the file is already current (end <= last_stored).
    Holidays/weekends drop out automatically via the NYSE calendar, so gaps
    of any length are filled with exactly the missing sessions — no holes."""
    if end_date <= last_stored:
        return []
    nyse = mcal.get_calendar('NYSE')
    valid = nyse.valid_days(start_date=last_stored, end_date=end_date).tz_localize(None)
    return [d.date() for d in valid if d.date() > last_stored]


# ──────────────────────────────────────────────────────────────────────────
#  Sequential Norgate pulls (no multiprocessing — see module docstring)
# ──────────────────────────────────────────────────────────────────────────
def _fetch_prices_window(tickers, start_date, end_date, fields,
                         padding='NONE', price_adjust='TOTALRETURN') -> dict:
    """Pull OHLCV(+Turnover/Unadjusted) for `tickers` over [start, end],
    in-process. Returns {field_name: DataFrame(dates x tickers)} — same shape
    and column layout PriceProvider.get_prices produces, so appended rows line
    up with the existing CSVs. Tickers that return nothing (delisted before the
    window, unsupported field) are skipped with a printed note."""
    import norgatedata  # lazy: importing this module shouldn't require Norgate

    adjust = getattr(norgatedata.StockPriceAdjustmentType, price_adjust.upper())
    pad = getattr(norgatedata.PaddingType, padding.upper())

    wide = pd.DataFrame()
    for ticker in tickers:
        try:
            prices = norgatedata.price_timeseries(
                ticker,
                stock_price_adjustment_setting=adjust,
                padding_setting=pad,
                start_date=start_date,
                end_date=end_date,
                timeseriesformat='pandas-dataframe',
                interval='D',
                fields=fields)
            if prices is None or prices.empty:
                continue
            prices.columns = pd.MultiIndex.from_product([[ticker], prices.columns])
            wide = pd.concat([prices, wide], axis=1)
        except Exception as e:
            print(f'[universe_today] price pull skipped: {ticker} '
                  f'-> {type(e).__name__}: {e}')

    # NYSE alignment, identical to PriceProvider.get_prices.
    nyse = mcal.get_calendar('NYSE')
    valid_dates = nyse.valid_days(start_date=start_date, end_date=end_date).tz_localize(None)

    per_field = {}
    for field in fields:
        if wide.empty:
            per_field[field] = pd.DataFrame()
            continue
        df = wide.loc[:, wide.columns.get_level_values(1) == field]
        df.columns = [col[0] for col in df.columns]
        df = df.loc[df.index.intersection(valid_dates)]
        per_field[field] = df.sort_index()
    return per_field


def _fetch_membership_window(universe_key, start_date, end_date,
                             padding='NONE', liquid_500_csv=None):
    """Resolve (membership_df, tickers) for `universe_key` over [start, end],
    in-process. Mirrors UniverseProvider's three sources:
      list/tuple  -> tickers = the list, membership = None (always 'in')
      'Liquid_500'-> read maintained CSV, sliced to the window
      index name  -> '<name> Current & Past' watchlist + index_constituent_timeseries
    """
    import norgatedata  # lazy

    # 1) explicit ticker list -> no membership timeseries
    if isinstance(universe_key, (list, tuple, np.ndarray)):
        return None, list(universe_key)

    # 2) maintained Liquid 500 membership CSV
    if universe_key == 'Liquid_500':
        if liquid_500_csv is None:
            raise ValueError("universe='Liquid_500' requires liquid_500_csv.")
        membership = pd.read_csv(liquid_500_csv)
        membership['Date'] = pd.to_datetime(membership['Date'])
        membership.set_index('Date', inplace=True)
        membership = membership.loc[:, ~membership.columns.str.startswith('Unnamed')]
        membership = membership.loc[start_date:end_date]
        return membership, list(membership.columns)

    # 3) Norgate index / watchlist
    pad = getattr(norgatedata.PaddingType, padding.upper())
    watchlist = f'{universe_key} Current & Past'
    universe_tickers = norgatedata.watchlist_symbols(watchlist)

    df = pd.DataFrame()
    for ticker in universe_tickers:
        try:
            series = norgatedata.index_constituent_timeseries(
                ticker, universe_key,
                padding_setting=pad,
                start_date=start_date,
                end_date=end_date,
                timeseriesformat='pandas-dataframe')
            if series is None or series.empty:
                continue
            series.columns = [ticker]
            df = pd.concat([series, df], axis=1)
        except Exception:
            print(f'[universe_today] membership pull skipped: {ticker}')

    if not df.empty:
        df = df.loc[start_date:end_date].dropna(axis=1, how='all')
    return df, list(df.columns)


def _fetch_index_window(symbol, adjustment, padding, start_date, end_date):
    """Full OHLCV pull for a single index symbol over [start, end], in-process.
    Mirrors generate_index_prices.fetch_index (no `fields=` -> default set)."""
    import norgatedata as nd  # lazy
    df = nd.price_timeseries(
        symbol,
        stock_price_adjustment_setting=adjustment,
        padding_setting=padding,
        start_date=start_date,
        end_date=end_date,
        timeseriesformat='pandas-dataframe')
    if df is None or df.empty:
        return pd.DataFrame()
    df.index.name = 'Date'
    df.index = pd.to_datetime(df.index)
    return df.sort_index()


# ──────────────────────────────────────────────────────────────────────────
#  Restatement detection
# ──────────────────────────────────────────────────────────────────────────
def _detect_restated_columns(stored_close, fresh_close, overlap_ts,
                             price_adjust) -> list:
    """Tickers whose stored adjusted close on `overlap_ts` differs from a
    fresh pull by more than RESTATEMENT_REL_TOL. Empty for non-TOTALRETURN
    series (VIX etc. are never restated) or when the overlap bar is missing."""
    if str(price_adjust).upper() != 'TOTALRETURN':
        return []
    if stored_close is None or fresh_close is None:
        return []
    if overlap_ts not in stored_close.index or overlap_ts not in fresh_close.index:
        return []
    a = stored_close.loc[overlap_ts]
    b = fresh_close.loc[overlap_ts]
    common = a.index.intersection(b.index) if hasattr(a, 'index') else []
    if len(common) == 0:
        return []
    a = pd.to_numeric(a[common], errors='coerce')
    b = pd.to_numeric(b[common], errors='coerce')
    denom = a.abs().replace(0, np.nan)
    rel = (a - b).abs() / denom
    return sorted(rel[rel > RESTATEMENT_REL_TOL].dropna().index.tolist())


# ──────────────────────────────────────────────────────────────────────────
#  Per-universe append
# ──────────────────────────────────────────────────────────────────────────
def append_universe_today(store: DataStore, spec: UniverseSpec,
                          end_date: dt.date) -> dict:
    """Append trading days (last_stored, end_date] to one universe's CSVs.
    Append-only: existing rows are never modified. Returns a result dict."""
    slug = spec.slug
    if not store.has_universe(slug):
        return {'slug': slug, 'status': 'SKIPPED',
                'reason': 'no base on disk — run universe_pipeline first'}

    existing = store.read_universe(slug)
    close = existing.fields.get('Close')
    if close is None or close.empty:
        return {'slug': slug, 'status': 'SKIPPED', 'reason': 'empty Close series'}

    last_stored = close.index.max().date()
    append_days = _nyse_days_after(last_stored, end_date)
    if not append_days:
        return {'slug': slug, 'status': 'CURRENT',
                'last_stored': last_stored.isoformat(), 'appended': []}

    fields = spec.fields or DEFAULT_FIELDS
    overlap_ts = pd.Timestamp(last_stored)  # included in the pull, never appended

    # Resolve members + prices for [last_stored .. end_date] (overlap + new).
    membership_win, tickers = _fetch_membership_window(
        spec.universe, last_stored, end_date,
        padding=spec.padding, liquid_500_csv=spec.liquid_500_csv)
    fields_win = _fetch_prices_window(
        tickers, last_stored, end_date, fields,
        padding=spec.padding, price_adjust=spec.price_adjust)

    win_close = fields_win.get('Close')
    if win_close is None or win_close.empty:
        return {'slug': slug, 'status': 'ERROR',
                'reason': f'Norgate returned no Close for the window '
                          f'{last_stored}..{end_date}'}

    # Overlap-bar restatement check (TOTALRETURN only).
    restated = _detect_restated_columns(close, win_close, overlap_ts, spec.price_adjust)

    # Build appended frames: existing rows + window rows strictly after overlap.
    # concat(axis=0) unions columns: a newly-added member appears as a new
    # column, NaN for every historical row (only tradable from its first bar);
    # a name absent from the window pull stays NaN on the new rows.
    new_fields = {}
    for fname, df in existing.fields.items():
        w = fields_win.get(fname)
        if w is None or w.empty:
            new_fields[fname] = df
            continue
        add = w[w.index > overlap_ts]
        merged = pd.concat([df, add])
        merged = merged[~merged.index.duplicated(keep='first')].sort_index()
        new_fields[fname] = merged

    # Membership rows (index universes only; list universes carry membership=None).
    new_membership = existing.membership
    if existing.membership is not None and membership_win is not None and not membership_win.empty:
        add_m = membership_win[membership_win.index > overlap_ts]
        new_membership = pd.concat([existing.membership, add_m])
        new_membership = new_membership[~new_membership.index.duplicated(keep='first')].sort_index()

    appended = sorted(
        d.date().isoformat()
        for d in new_fields['Close'].index if d > overlap_ts)

    # Refresh manifest stamps (write_universe re-stamps written_at itself).
    manifest = dict(existing.manifest)
    manifest['last_data_date'] = new_fields['Close'].index.max()
    manifest['num_tickers'] = int(new_fields['Close'].shape[1])
    manifest['end_date'] = end_date

    store.write_universe(UniverseDataset(slug, new_fields, new_membership, manifest))

    return {
        'slug': slug,
        'status': 'APPENDED',
        'last_stored': last_stored.isoformat(),
        'appended': appended,
        'num_tickers': int(new_fields['Close'].shape[1]),
        'restated': restated,
    }


# ──────────────────────────────────────────────────────────────────────────
#  Per-index append
# ──────────────────────────────────────────────────────────────────────────
def append_index_today(folder: Path, key: str, cfg: dict,
                       end_date: dt.date) -> dict:
    """Append trading days (last_stored, end_date] to one index series'
    daily_closes_{key}.csv and daily_ohlcv_{key}.csv. Append-only."""
    folder = Path(folder)
    closes_path = folder / f'daily_closes_{key}.csv'
    ohlcv_path = folder / f'daily_ohlcv_{key}.csv'
    if not closes_path.exists():
        return {'key': key, 'status': 'SKIPPED',
                'reason': 'no base on disk — run generate_index_prices first'}

    existing_closes = pd.read_csv(closes_path, index_col=['Date'], parse_dates=True)
    if existing_closes.empty:
        return {'key': key, 'status': 'SKIPPED', 'reason': 'empty closes file'}

    last_stored = existing_closes.index.max().date()
    append_days = _nyse_days_after(last_stored, end_date)
    if not append_days:
        return {'key': key, 'status': 'CURRENT',
                'last_stored': last_stored.isoformat(), 'appended': []}

    overlap_ts = pd.Timestamp(last_stored)
    df = _fetch_index_window(
        cfg['symbol'], cfg['adjustment'], cfg['padding'], last_stored, end_date)
    if df.empty or 'Close' not in df.columns:
        return {'key': key, 'status': 'ERROR',
                'reason': f'Norgate returned no data for {cfg["symbol"]} '
                          f'over {last_stored}..{end_date}'}

    # Restatement check vs the single stored column (named after the key).
    # cfg['adjustment'] is a Norgate enum member; .name is stable across
    # Python versions (IntEnum.__str__ returns the int on 3.11+, so str() is not).
    restated = []
    _adj_name = getattr(cfg['adjustment'], 'name', str(cfg['adjustment'])).upper()
    if _adj_name == 'TOTALRETURN':
        if overlap_ts in existing_closes.index and overlap_ts in df.index:
            stored_v = pd.to_numeric(existing_closes.loc[overlap_ts].iloc[0], errors='coerce')
            fresh_v = pd.to_numeric(df.loc[overlap_ts, 'Close'], errors='coerce')
            if pd.notna(stored_v) and stored_v != 0:
                if abs(stored_v - fresh_v) / abs(stored_v) > RESTATEMENT_REL_TOL:
                    restated = [key]

    add = df[df.index > overlap_ts]
    if add.empty:
        return {'key': key, 'status': 'CURRENT',
                'last_stored': last_stored.isoformat(), 'appended': []}

    # 1) closes file — single column named after the key (load_spy_close format).
    add_close = add[['Close']].rename(columns={'Close': key})
    new_closes = pd.concat([existing_closes, add_close])
    new_closes = new_closes[~new_closes.index.duplicated(keep='first')].sort_index()
    new_closes.index.name = 'Date'
    new_closes.to_csv(closes_path, index=True, index_label='Date')

    # 2) ohlcv file — same columns generate_index_prices.save_index writes.
    if ohlcv_path.exists():
        existing_ohlcv = pd.read_csv(ohlcv_path, index_col=['Date'], parse_dates=True)
        ohlcv_cols = [c for c in ['Open', 'High', 'Low', 'Close',
                                  'Volume', 'Unadjusted Close', 'Turnover']
                      if c in df.columns]
        add_ohlcv = add[ohlcv_cols]
        new_ohlcv = pd.concat([existing_ohlcv, add_ohlcv])
        new_ohlcv = new_ohlcv[~new_ohlcv.index.duplicated(keep='first')].sort_index()
        new_ohlcv.index.name = 'Date'
        new_ohlcv.to_csv(ohlcv_path, index=True, index_label='Date')

    appended = sorted(d.date().isoformat() for d in add.index)
    return {'key': key, 'status': 'APPENDED',
            'last_stored': last_stored.isoformat(),
            'appended': appended, 'restated': restated}


# ──────────────────────────────────────────────────────────────────────────
#  Orchestrator
# ──────────────────────────────────────────────────────────────────────────
def refresh_all_today(end_date: Optional[dt.date] = None,
                      only_universes: Optional[set] = None,
                      only_index: Optional[set] = None,
                      universes_root: Optional[str] = None,
                      index_folder: Optional[str] = None) -> dict:
    """Extend every active universe (REGISTRY) and index series (INDEX_REGISTRY)
    to the latest posted Norgate session.

    Args:
        end_date: target data date. Defaults to today, rolled back to the prior
            trading day if before the Norgate post hour (same guard as the
            nightly pipeline) so a half / unposted session is never written.
        only_universes / only_index: optional slug/key filters for targeted runs.
        universes_root / index_folder: path overrides (tests).

    Returns a summary dict (per-universe + per-index results, restatement
    flags, and has_errors). Each item is wrapped in try/except so one failing
    universe reports ERROR without aborting the rest — failures are surfaced,
    never swallowed.
    """
    requested = end_date or dt.date.today()
    resolved = resolve_data_date(end_date)  # None -> today, with post-hour rollback
    today_excluded = resolved < requested

    store = CsvDataStore(universes_root or UNIVERSES_ROOT)
    idx_folder = Path(index_folder or INDEX_FOLDER)

    universe_results = []
    for spec in REGISTRY:
        if only_universes is not None and spec.slug not in only_universes:
            continue
        try:
            universe_results.append(append_universe_today(store, spec, resolved))
        except Exception as e:
            universe_results.append({'slug': spec.slug, 'status': 'ERROR',
                                     'reason': f'{type(e).__name__}: {e}'})

    index_results = []
    for key, cfg in INDEX_REGISTRY.items():
        if only_index is not None and key not in only_index:
            continue
        try:
            index_results.append(append_index_today(idx_folder, key, cfg, resolved))
        except Exception as e:
            index_results.append({'key': key, 'status': 'ERROR',
                                  'reason': f'{type(e).__name__}: {e}'})

    has_errors = any(r.get('status') == 'ERROR'
                     for r in universe_results + index_results)
    restated_any = sorted({
        t for r in universe_results + index_results
        for t in (r.get('restated') or [])
    })

    return {
        'requested_end': requested.isoformat(),
        'resolved_data_date': resolved.isoformat(),
        'today_excluded': today_excluded,
        'universes': universe_results,
        'index': index_results,
        'restated_any': restated_any,
        'has_errors': has_errors,
    }


if __name__ == '__main__':
    import sys
    from pathlib import Path as _P
    _ROOT = _P(__file__).resolve().parents[2]   # .../frontend_quant
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

    import json
    # Targeted dry run: just sp500 + spy index, latest posted session.
    # result = refresh_all_today(only_universes={'sp500'}, only_index={'spy'})
    result = refresh_all_today()
    print(json.dumps(result, indent=2, default=str))