"""
price_provider.py

Pulls Norgate OHLCV (+ Turnover, Unadjusted Close) for a set of tickers over a
date range. Knows nothing about universes or file output -- you hand it tickers,
it hands you one DataFrame per field.

Preserves the legacy multiprocessing pull and the optional NYSE-calendar
alignment that PriceData used.
"""

from __future__ import annotations

import multiprocessing as mp

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal
import norgatedata


class PriceProvider:

    DEFAULT_FIELDS = ['Open', 'High', 'Low', 'Close',
                      'Volume', 'Turnover', 'Unadjusted Close']

    def __init__(self, num_of_cpus=6, padding='NONE', price_adjust='TOTALRETURN'):
        self.num_of_cpus = num_of_cpus
        self.padding = padding.upper()
        self.price_adjust = price_adjust.upper()

    @staticmethod
    def _validate_tickers(tickers):
        # Patch 114: a numeric 'ticker' is an assetID leak from a corrupt
        # membership header. norgatedata would PRICE it silently and the
        # integer column would propagate to live CSVs and exec parquets.
        bad = [t for t in tickers
               if not isinstance(t, str) or str(t).strip().isdigit()]
        if bad:
            raise RuntimeError(
                f'{len(bad)} non-ticker (numeric) symbol(s) in the pull '
                f'list, e.g. {bad[:10]} — membership header is corrupt; '
                f'restore the universe CSV from backup.')

    def get_prices(self, tickers, start_date, end_date=None,
                   interval='D', fields=None, align_to_nyse=True):
        """
        Returns {field_name: DataFrame(dates x tickers)} for the requested fields.
        """
        self._validate_tickers(tickers)   # Patch 114

        fields = fields or self.DEFAULT_FIELDS
        wide = self._pull_multiprocess(tickers, start_date, end_date, interval, fields)

        valid_dates = None
        if align_to_nyse:
            nyse = mcal.get_calendar('NYSE')
            valid_dates = nyse.valid_days(
                start_date=start_date, end_date=end_date).tz_localize(None)

        per_field = {}
        for field in fields:
            df = wide.loc[:, wide.columns.get_level_values(1) == field]
            df.columns = [col[0] for col in df.columns]
            if align_to_nyse:
                # intersection (not .loc[valid_dates]) so a missing trading day
                # can't raise -- it just drops out.
                df = df.loc[df.index.intersection(valid_dates)]
            per_field[field] = df.sort_index()

        # Patch 105: freshness logging — THE line that answers "where does
        # the last date come from". norgatedata.price_timeseries returns AT
        # MOST what the LOCAL Norgate DB (NDU) has ingested at pull time; it
        # silently truncates when today's close hasn't landed yet. Log the
        # achieved ceiling against the requested end so a stale-NDU night is
        # visible here, at the source, not three steps later.
        closes = per_field.get('Close')
        if closes is not None and not closes.empty:
            achieved = closes.index.max()
            print(f'[price_provider] Norgate pull: {len(tickers)} tickers, '
                  f'requested end={end_date}, '
                  f'ACHIEVED LAST DATE={achieved:%Y-%m-%d} '
                  f'(= local Norgate DB ceiling at pull time)')
            # Per-ticker tail distribution: shows whether ALL tickers stop at
            # the ceiling (DB-wide staleness) or only some lag (per-symbol).
            _lasts = closes.apply(lambda s: s.last_valid_index())
            _dist = _lasts.value_counts().sort_index(ascending=False).head(3)
            for _d, _n in _dist.items():
                print(f'[price_provider]   {_n} ticker(s) last valid on '
                      f'{_d:%Y-%m-%d}')
            if end_date is not None and achieved.date() < end_date:
                print(f'[price_provider] *** STALE SOURCE WARNING: requested '
                      f'closes up to {end_date} but the local Norgate DB '
                      f'only holds {achieved:%Y-%m-%d}. NDU has NOT ingested '
                      f'the {end_date} close yet — tonight\'s fill '
                      f'resolution for {end_date} WILL fail. Check Norgate '
                      f'Data Updater schedule. ***')
        return per_field

    def _pull_multiprocess(self, tickers, start_date, end_date, interval, fields):
        ticker_groups = np.array_split(list(tickers), self.num_of_cpus)
        manager = mp.Manager()
        collected = manager.list()
        processes = []
        for group in ticker_groups:
            p = mp.Process(
                target=self._fetch_group,
                args=(list(group), start_date, end_date, interval, fields,
                      self.price_adjust, self.padding, collected))
            processes.append(p)
            p.start()
        for p in processes:
            p.join()

        wide = pd.DataFrame()
        for part in collected:
            wide = pd.concat([part, wide], axis=1)
        return wide

    @staticmethod
    def _fetch_group(tickers, start_date, end_date, interval, fields,
                     price_adjust, padding, collected):
        adjust = getattr(norgatedata.StockPriceAdjustmentType, price_adjust)
        pad = getattr(norgatedata.PaddingType, padding)
        part = pd.DataFrame()
        for ticker in tickers:
            try:
                # print(f'[price_provider] {ticker} {start_date} -> {end_date}')
                prices = norgatedata.price_timeseries(
                    ticker,
                    stock_price_adjustment_setting=adjust,
                    padding_setting=pad,
                    start_date=start_date,
                    end_date=end_date,
                    timeseriesformat='pandas-dataframe',
                    interval=interval,
                    fields=fields)
                prices.columns = pd.MultiIndex.from_product([[ticker], prices.columns])
                # print(prices)
                part = pd.concat([prices, part], axis=1)
            except Exception as e:
                # LRA Patch 15-pre: surface the actual exception type and message
                # so forex / unsupported-field failures aren't silently swallowed.
                print(f'[price_provider] price pull failed: {ticker} '
                      f'-> {type(e).__name__}: {e}')
        collected.append(part)