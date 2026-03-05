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
    def load(self, load_minute: bool = True):
        """Load all parquets and build fast lookup structures.

        Daily OHLC always comes from DAILY_{ticker}.parquet (matches
        TradeStation's daily bar prices for entry/exit).

        When load_minute=True, MINUTE_{ticker}.parquet is additionally
        loaded to provide intrabar arrays for SL/TP scanning.

        Indicators are loaded from their own parquet files (already
        computed from the correct source by GeneratePricesIndicators).
        """
        # 1. Daily OHLC — always from the DAILY file (entry/exit prices)
        self._load_daily_only()

        # 2. Minute arrays — for intrabar SL/TP scanning
        if load_minute:
            self._load_minute_arrays()

        # 3. Dates and indicators
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
    def _load_daily_only(self):
        """Load DAILY_{ticker}.parquet directly into daily dicts.
        These are the authoritative entry/exit prices."""
        path = os.path.join(self.base_path, f'DAILY_{self.ticker}.parquet')
        if not os.path.exists(path):
            raise FileNotFoundError(f'{path} not found')

        df = pd.read_parquet(path)
        if not isinstance(df.index, pd.DatetimeIndex):
            if 'Date' in df.columns:
                df.set_index(pd.to_datetime(df['Date']), inplace=True)
                df.drop(['Date'], axis=1, errors='ignore', inplace=True)

        col_map = {}
        for c in df.columns:
            cl = c.lower()
            if 'open' in cl:   col_map[c] = 'Open'
            elif 'high' in cl: col_map[c] = 'High'
            elif 'low' in cl:  col_map[c] = 'Low'
            elif 'close' in cl:col_map[c] = 'Close'
        df.rename(columns=col_map, inplace=True)

        for ts, row in df.iterrows():
            d = ts.date()
            self._daily_open[d] = float(row['Open'])
            self._daily_high[d] = float(row['High'])
            self._daily_low[d] = float(row['Low'])
            self._daily_close[d] = float(row['Close'])

        print(f'[ETFPriceData] Loaded {len(self._daily_open)} daily bars from DAILY_{self.ticker}.parquet')

    def _load_minute_arrays(self):
        """Load MINUTE_{ticker}.parquet and build intrabar numpy arrays only.
        Does NOT overwrite daily OHLC — those come from _load_daily_only()."""
        minute_data = self._load_minute_data()
        self._build_minute_arrays(minute_data)
        print(f'[ETFPriceData] Loaded {len(self._minute_arrays)} days of minute bars from MINUTE_{self.ticker}.parquet')

    def _load_minute_data(self) -> pd.DataFrame:
        """Load MINUTE_{ticker}.parquet for intrabar scanning.
        Falls back to DAILY_{ticker}.parquet if minute file unavailable."""
        minute_path = os.path.join(self.base_path, f'MINUTE_{self.ticker}.parquet')
        daily_path = os.path.join(self.base_path, f'DAILY_{self.ticker}.parquet')

        if os.path.exists(minute_path):
            path = minute_path
        elif os.path.exists(daily_path):
            print(f'[ETFPriceData] WARNING: MINUTE_{self.ticker}.parquet not found, falling back to DAILY')
            path = daily_path
        else:
            raise FileNotFoundError(f'Neither {minute_path} nor {daily_path} found')

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

    # NOTE: _aggregate_daily removed — daily OHLC must always come from
    # DAILY_{ticker}.parquet to match TradeStation entry/exit prices.
    # Indicators are computed from MINUTE-aggregated data separately
    # by GeneratePricesIndicators.

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
        # all_dates — MUST be unique calendar days (parquet may have per-minute rows)
        path = os.path.join(self.base_path, 'all_dates.parquet')
        df = pd.read_parquet(path)
        col = df.iloc[:, 0]
        if not pd.api.types.is_datetime64_any_dtype(col):
            col = pd.to_datetime(col)

        # Deduplicate while preserving chronological order
        seen = set()
        unique = []
        for d in col:
            day = d.date() if hasattr(d, 'date') else d
            if day not in seen:
                seen.add(day)
                unique.append(day)
        self.all_dates = unique

        # trading_dates (set = already unique)
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
            f'MINUTE_{self.ticker}.parquet',
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
