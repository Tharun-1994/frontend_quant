"""
ETF Price Data Loader (Optimized)
==================================
All lookups are O(1) dict-based. No pd.Timestamp conversions in hot path.
Minute bars stored as pre-extracted numpy arrays for vectorized SL/TP scanning.
"""

import pandas as pd
import numpy as np
import os
from datetime import date
from typing import Optional, Dict, Tuple


class ETFPriceData:

    def __init__(self, base_path: str, ticker: str = 'spy'):
        self.base_path = base_path
        self.ticker = ticker.lower()

        # Fast O(1) daily lookups: {date → float}
        self._daily_open: Dict[date, float] = {}
        self._daily_high: Dict[date, float] = {}
        self._daily_low: Dict[date, float] = {}
        self._daily_close: Dict[date, float] = {}

        # Minute bars as numpy arrays: {date → (opens, highs, lows, closes)}
        self._minute_arrays: Dict[date, Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}

        # Dates
        self.all_dates: list = []
        self.trading_dates: set = set()

        # Indicator dicts: {key → {date → float}}
        self._indicator_cache: Dict[str, Dict[date, float]] = {}

    # ------------------------------------------------------------------
    #  PUBLIC
    # ------------------------------------------------------------------
    def load(self):
        """Load all parquets and build fast lookup structures."""
        minute_data = self._load_minute_data()
        self._aggregate_daily(minute_data)
        self._build_minute_arrays(minute_data)
        self._load_dates()
        self._load_indicators()

    # -- O(1) daily lookups --------------------------------------------
    def get_daily_open(self, d: date) -> Optional[float]:
        return self._daily_open.get(d)

    def get_daily_high(self, d: date) -> Optional[float]:
        return self._daily_high.get(d)

    def get_daily_low(self, d: date) -> Optional[float]:
        return self._daily_low.get(d)

    def get_daily_close(self, d: date) -> Optional[float]:
        return self._daily_close.get(d)

    # -- Minute-bar arrays for vectorized scanning ----------------------
    def get_minute_arrays(self, d: date) -> Optional[Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
        """Returns (opens, highs, lows, closes) as numpy arrays for date d."""
        return self._minute_arrays.get(d)

    # -- O(1) indicator lookup -----------------------------------------
    def get_indicator_value(self, key: str, d: date) -> Optional[float]:
        cache = self._indicator_cache.get(key)
        if cache is None:
            return None
        return cache.get(d)

    # ------------------------------------------------------------------
    #  PRIVATE – loading
    # ------------------------------------------------------------------
    def _load_minute_data(self) -> pd.DataFrame:
        path = os.path.join(self.base_path, f'DAILY_{self.ticker}.parquet')
        df = pd.read_parquet(path)

        # Ensure DateTimeIndex
        if not isinstance(df.index, pd.DatetimeIndex):
            if 'Date' in df.columns and 'Time' in df.columns:
                df['datetime'] = pd.to_datetime(
                    df['Date'].astype(str) + ' ' + df['Time'].astype(str),
                    format='mixed', dayfirst=False
                )
                df.set_index('datetime', inplace=True)
                df.drop(['Date', 'Time'], axis=1, errors='ignore', inplace=True)
            elif 'Date' in df.columns:
                df.set_index(pd.to_datetime(df['Date']), inplace=True)
                df.drop(['Date'], axis=1, errors='ignore', inplace=True)

        # Standardize column names
        col_map = {}
        for c in df.columns:
            cl = c.lower()
            if 'open' in cl:   col_map[c] = 'Open'
            elif 'high' in cl: col_map[c] = 'High'
            elif 'low' in cl:  col_map[c] = 'Low'
            elif 'close' in cl:col_map[c] = 'Close'
        df.rename(columns=col_map, inplace=True)

        return df.sort_index()

    def _aggregate_daily(self, minute_data: pd.DataFrame):
        """Resample to daily OHLC and store as dicts."""
        daily = minute_data.resample('D').agg({
            'Open': 'first',
            'High': 'max',
            'Low': 'min',
            'Close': 'last',
        }).dropna(subset=['Open'])

        for ts, row in daily.iterrows():
            d = ts.date()
            self._daily_open[d] = float(row['Open'])
            self._daily_high[d] = float(row['High'])
            self._daily_low[d] = float(row['Low'])
            self._daily_close[d] = float(row['Close'])

    def _build_minute_arrays(self, minute_data: pd.DataFrame):
        """Pre-extract numpy arrays per date using searchsorted (fastest)."""
        idx = minute_data.index
        day_starts = idx.normalize()
        unique_days = day_starts.unique()

        boundaries = day_starts.searchsorted(unique_days, side='left')
        ends = np.append(boundaries[1:], len(idx))

        opens_all = minute_data['Open'].values
        highs_all = minute_data['High'].values
        lows_all = minute_data['Low'].values
        closes_all = minute_data['Close'].values

        for i in range(len(unique_days)):
            d = unique_days[i].date()
            s, e = boundaries[i], ends[i]
            self._minute_arrays[d] = (
                opens_all[s:e],
                highs_all[s:e],
                lows_all[s:e],
                closes_all[s:e],
            )

    def _load_dates(self):
        # all_dates
        path = os.path.join(self.base_path, 'all_dates.parquet')
        df = pd.read_parquet(path)
        col = df.iloc[:, 0]
        if not pd.api.types.is_datetime64_any_dtype(col):
            col = pd.to_datetime(col)
        self.all_dates = [d.date() if hasattr(d, 'date') else d for d in col]

        # trading_dates
        path = os.path.join(self.base_path, 'trading_dates.parquet')
        df = pd.read_parquet(path)
        col = df.iloc[:, 0]
        if not pd.api.types.is_datetime64_any_dtype(col):
            col = pd.to_datetime(col)
        self.trading_dates = set(
            d.date() if hasattr(d, 'date') else d for d in col
        )

    def _load_indicators(self):
        """Load each indicator parquet into a {date → float} dict."""
        skip = {
            f'DAILY_{self.ticker}.parquet',
            'all_dates.parquet',
            'trading_dates.parquet',
            f'{self.ticker}_universe.parquet',
        }
        ticker = self.ticker

        for fname in os.listdir(self.base_path):
            if not fname.endswith('.parquet') or fname in skip:
                continue

            key = fname.replace('.parquet', '')
            df = pd.read_parquet(os.path.join(self.base_path, fname))

            # Determine which column to use
            if len(df.columns) == 1:
                series = df.iloc[:, 0]
            elif ticker in df.columns:
                series = df[ticker]
            else:
                series = df.iloc[:, 0]

            # Build {date → float} dict, skipping NaN
            cache = {}
            for ts, val in series.items():
                if pd.notna(val):
                    d = ts.date() if hasattr(ts, 'date') else ts
                    cache[d] = float(val)

            self._indicator_cache[key] = cache
