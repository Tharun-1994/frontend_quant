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

    def get_prices(self, tickers, start_date, end_date=None,
                   interval='D', fields=None, align_to_nyse=True):
        """
        Returns {field_name: DataFrame(dates x tickers)} for the requested fields.
        """
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
