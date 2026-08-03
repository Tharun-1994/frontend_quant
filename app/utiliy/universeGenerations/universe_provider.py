"""
universe_provider.py

Resolves *which tickers belong to a universe* over a date range -- and nothing
else. Price fetching lives in price_provider.py; file IO lives in
daily_data_generator.py.

Three universe sources are supported (all already present in the legacy
PriceData class, just untangled here):

  1. An explicit list of tickers        -> every ticker is always "in".
  2. The pre-built Liquid 500 membership -> read from a maintained CSV.
  3. A Norgate index / watchlist name    -> built from index_constituent_timeseries.
"""

from __future__ import annotations

import multiprocessing as mp

import numpy as np
import pandas as pd
import norgatedata


class UniverseProvider:

    LIQUID_500_KEY = 'Liquid_500'

    def __init__(self, universe, start_date, end_date=None,
                 num_of_cpus=6, padding='NONE', liquid_500_csv=None):
        self.universe = universe
        self.start_date = start_date
        self.end_date = end_date
        self.num_of_cpus = num_of_cpus
        self.padding = padding.upper()
        self.liquid_500_csv = liquid_500_csv

        self._membership = None   # DataFrame (dates x tickers) or None for a raw list
        self._tickers = None      # list[str]
        self._resolve()

    # ---- public API -------------------------------------------------------
    @property
    def membership(self):
        """Daily in/out membership (dates x tickers). None for a raw ticker list."""
        return self._membership

    @property
    def tickers(self):
        """Every ticker that prices must be pulled for in this universe."""
        return self._tickers

    # ---- resolution -------------------------------------------------------
    def _resolve(self):
        if isinstance(self.universe, (list, tuple, np.ndarray)):
            self._tickers = list(self.universe)
            self._membership = None
        elif self.universe == self.LIQUID_500_KEY:
            self._membership = self._load_liquid_500()
            self._tickers = list(self._membership.columns)
        else:
            self._membership = self._build_from_index()
            self._tickers = list(self._membership.columns)

    def _load_liquid_500(self):
        if self.liquid_500_csv is None:
            raise ValueError(
                "universe='Liquid_500' requires liquid_500_csv to point at the "
                "maintained membership file.")
        membership = pd.read_csv(self.liquid_500_csv)
        membership['Date'] = pd.to_datetime(membership['Date'])
        membership.set_index('Date', inplace=True)
        # Drop any stray index columns ('Unnamed: 0' etc.) so they never leak
        # into self.tickers and get fed to Norgate as a phantom symbol.
        membership = membership.loc[:, ~membership.columns.str.startswith('Unnamed')]

        # Patch 114: HARD header validation. A membership column that is
        # numeric is a Norgate assetID (or a headerless-CSV artefact), NOT
        # a ticker — and norgatedata will happily PRICE an assetID, so the
        # corruption flows silently into live CSVs and exec parquets as
        # integer-named columns (observed: 205 integer columns; symbol
        # lookups like 'BKR' then fail three layers later). Fail HERE,
        # naming the file and the offending columns.
        _bad = [c for c in membership.columns
                if not isinstance(c, str) or c.strip().isdigit()]
        if _bad:
            raise RuntimeError(
                f'liquid500 membership header corrupt: {len(_bad)} numeric/'
                f'non-ticker column(s), e.g. {_bad[:10]} in '
                f'{self.liquid_500_csv}. These are Norgate assetIDs, not '
                f'tickers — restore the CSV from its backup and re-run the '
                f'membership extension.')
        # Patch 90: do NOT slice ROWS by start_date for Liquid_500. The CSV
        # is the source of truth — slicing on read means it gets written
        # back truncated by store.write_universe(), silently losing
        # pre-start_date history. PriceProvider receives tickers and
        # date range as separate args (see daily_data_generator.generate),
        # so price-fetch scope is unaffected by preserving full history.
        # Bound the END only — never write rows beyond the run's end_date.
        membership = membership.loc[:self.end_date]

        # Patch 105: window the TICKER SELECTION (columns) to symbols that
        # are actually members inside [start_date, end_date]. Previously
        # tickers = every column in the CSV's full history, so the nightly
        # pulled Norgate prices for symbols that left the universe years
        # before the live window — wasted pull time and all-NaN columns.
        # Rows are untouched (Patch 90 rule); only the active set shrinks.
        # Truthiness is explicit: any non-null, non-False cell counts as
        # membership, so bool/0-1/string CSVs all behave identically.
        full_cols = list(membership.columns)
        win = membership.loc[self.start_date:self.end_date]
        active_mask = win.notna() & ~win.isin(
            [False, 'False', 'FALSE', 'false', 0, '0', 0.0]
        )
        active_cols = [c for c in full_cols if bool(active_mask[c].any())]
        membership = membership.loc[:, active_cols]

        print(f'[universe_provider] Liquid_500 membership source: '
              f'{self.liquid_500_csv}')
        print(f'[universe_provider]   CSV rows: '
              f'{membership.index.min():%Y-%m-%d} .. '
              f'{membership.index.max():%Y-%m-%d} '
              f'(LAST MEMBERSHIP DATE = {membership.index.max():%Y-%m-%d}; '
              f'end-bounded at {self.end_date})')
        print(f'[universe_provider]   ticker window '
              f'[{self.start_date} .. {self.end_date}]: '
              f'{len(active_cols)} active of {len(full_cols)} '
              f'all-history columns '
              f'({len(full_cols) - len(active_cols)} dropped as never-member '
              f'in window)')
        return membership

    def _build_from_index(self):
        watchlist = f'{self.universe} Current & Past'
        universe_tickers = norgatedata.watchlist_symbols(watchlist)
        ticker_groups = np.array_split(universe_tickers, self.num_of_cpus)
        padding = getattr(norgatedata.PaddingType, self.padding)

        manager = mp.Manager()
        collected = manager.list()
        processes = []
        for group in ticker_groups:
            p = mp.Process(
                target=self._fetch_membership_group,
                args=(list(group), self.universe, self.start_date,
                      self.end_date, padding, collected))
            processes.append(p)
            p.start()
        for p in processes:
            p.join()

        membership = pd.concat(list(collected), axis=1)
        return membership.loc[self.start_date:self.end_date].dropna(axis=1, how='all')

    @staticmethod
    def _fetch_membership_group(tickers, universe, start_date, end_date,
                                padding, collected):
        df = pd.DataFrame()
        for ticker in tickers:
            try:
                series = norgatedata.index_constituent_timeseries(
                    ticker, universe,
                    padding_setting=padding,
                    start_date=start_date,
                    end_date=end_date,
                    timeseriesformat='pandas-dataframe')
                series.columns = [ticker]
                df = pd.concat([series, df], axis=1)
            except Exception:
                print(f'[universe_provider] membership pull failed: {ticker}')
        collected.append(df)