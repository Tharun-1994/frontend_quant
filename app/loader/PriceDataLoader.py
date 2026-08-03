from pathlib import Path
from typing import Tuple

from app.constants.PricePath import PricePath
import pandas as pd
import pandas_market_calendars as mcal

from app.constants.static_config import UNIVERSES_Codes


class PriceDataLoader:
    offset = 0
    def __init__(self, base_path):
        self.base_path = base_path
        self.offset = 0

    def load_all(self, rebalance='', universe=''):
        universe = universe.lower()
        if universe != 'spy':
            return {
                f'{rebalance}_closes': pd.read_csv(PricePath.close(self.base_path), index_col=['Date'], parse_dates=True),
                f'{rebalance}_opens': pd.read_csv(PricePath.opens(self.base_path), index_col=['Date'], parse_dates=True),
                f'{rebalance}_highs': pd.read_csv(PricePath.highs(self.base_path), index_col=['Date'], parse_dates=True),
                f'{rebalance}_lows': pd.read_csv(PricePath.lows(self.base_path), index_col=['Date'], parse_dates=True),
                f'{universe}_universe': pd.read_csv(PricePath.universe(f'{self.base_path}/{universe}'), index_col=['Date'], parse_dates=True),
                f'{rebalance}_unadjusted_closes': pd.read_csv(PricePath.unadjustedCloses(self.base_path), index_col=['Date'], parse_dates=True),
                f'{rebalance}_volumes': pd.read_csv(PricePath.volumes(self.base_path), index_col=['Date'], parse_dates=True),
                f'{rebalance}_turnovers': pd.read_csv(PricePath.turnovers(self.base_path), index_col=['Date'], parse_dates=True),
            }
        else:
            spy_dict = {}

            # ── Daily OHLC (one row per day) ──────────────────────────
            daily_df = self._load_spy_daily(PricePath.spy_daily_prices(self.base_path))
            spy_dict[f'DAILY_{universe}'] = daily_df

            # ── Minute OHLC (one row per minute bar) ──────────────────
            minute_df = self._load_spy_minute(PricePath.spy_minute_prices(self.base_path))
            spy_dict[f'MINUTE_{universe}'] = minute_df

            # ── Universe placeholder ──────────────────────────────────
            spy_dict[f'{universe}_universe'] = pd.DataFrame(
                index=daily_df.index, data={'universe': 'SPY'})

            return spy_dict

    # ------------------------------------------------------------------
    #  SPY file loaders
    # ------------------------------------------------------------------
    @staticmethod
    def _load_spy_daily(path: str) -> pd.DataFrame:
        """
        Load SPY daily file.
        Format: Date,Time,Open,High,Low,Close,Vol,OI
        Example: 01/03/2000,21:00,148.25,148.25,143.88,145.44,8164300,0

        Returns DataFrame with DateTimeIndex and OHLC columns.
        """
        df = pd.read_csv(path, index_col=['Date'], parse_dates=True)
        df.drop(['Time'], axis=1, errors='ignore', inplace=True)
        return df.sort_index()

    @staticmethod
    def _load_spy_minute(path: str) -> pd.DataFrame:
        """
        Load SPY minute file.
        Format: Date,Time,Open,High,Low,Close,Up,Down
        Example: 01/03/2000,09:32,148.25,148.25,148.25,148.25,177300,0

        Returns DataFrame with DateTimeIndex (date+time) and OHLC columns.
        Parquet will preserve the DateTimeIndex so ETFPriceData needs no parsing.
        """
        df = pd.read_csv(path, parse_dates=[['Date', 'Time']], index_col=0)
        df.index.name = 'Date'
        return df.sort_index()

    # ------------------------------------------------------------------
    #  Existing methods (unchanged)
    # ------------------------------------------------------------------
    def load_spy_close(self, rebalance=''):
        return {
            f'{rebalance}_closes_spy': pd.read_csv(PricePath.spy_closes(PricePath.index_path),
                                                          index_col=['Date'], parse_dates=True)
        }

    def load_ticker_close(self, ticker: str, rebalance: str = '') -> dict:
        """
        Load close prices for any index ticker from the shared index folder.

        Reads:  {index_path}/daily_closes_{ticker}.csv
        Returns: { '{rebalance}_closes_{ticker}': DataFrame }

        This is the generic counterpart to load_spy_close.
        Any ticker added to generate_index_prices.INDEX_REGISTRY is automatically
        available here — no code changes needed when new indexes are added.

        Parameters
        ----------
        ticker : str
            Lowercase ticker key, e.g. 'vix', 'spxmcsum', 'gld'.
            Must match a key produced by generate_index_prices.py.
        rebalance : str
            Rebalance prefix, e.g. 'DAILY'.

        Raises
        ------
        FileNotFoundError
            If daily_closes_{ticker}.csv does not exist in the index folder.
            Run generate_index_prices.py to create it.
        """
        import os
        ticker = ticker.lower()
        csv_path = os.path.join(PricePath.index_path, f'daily_closes_{ticker}.csv')
        if not os.path.exists(csv_path):
            raise FileNotFoundError(
                f"Index close file not found: {csv_path}\n"
                f"Run generate_index_prices.py --only {ticker} to generate it."
            )
        df = pd.read_csv(csv_path, index_col=['Date'], parse_dates=True)
        return {f'{rebalance}_closes_{ticker}': df}

    def load_sector_mapping(self):
        """Load sector/industry classification from SnP_500_INDUSTRIES.csv (tab-separated, no header).

        Patch 49: when self.base_path is the LIVE folder
        (backtest_data/live_universes/sp500/) the CSV typically isn't there —
        it's a hand-maintained static artifact that lives in
        backtest_data/universes/sp500/. Fall back to that static path so
        production runs find it without duplicating the file. Backtest mode
        passes the static path as base_path directly, so the first lookup
        succeeds and the fallback is never hit.
        """
        import os
        sector_path = os.path.join(self.base_path, 'SnP_500_INDUSTRIES.csv')
        if not os.path.exists(sector_path):
            # Patch 49 fallback — static backtest folder is the single source of truth
            from app.constants.PricePath import PricePath
            fallback_path = os.path.join(PricePath.sp500base_path, 'SnP_500_INDUSTRIES.csv')
            if os.path.exists(fallback_path):
                print(f"[load_sector_mapping] live path missed, using fallback: {fallback_path}")
                sector_path = fallback_path
            else:
                print(f"[WARNING] Sector file not found in live OR static: "
                      f"{sector_path} / {fallback_path}")
                return {}
        df = pd.read_csv(sector_path, sep='\t', header=None,
                         names=['symbol', 'level_1', 'level_2', 'level_3', 'level_4', 'level_5'])
        df.set_index('symbol', inplace=True)
        return {'sector_mapping': df}

    def uploadCommonPath(self, price_data={}, universe="", strategy_name="",
                         production=False, run_date=None, excluded_tickers=None,
                         already_written=None):   # Patch 174: per-universe write-once
        """Write computed parquets.

        C1 Patch 2: when production=True, writes to the universe-shared,
        date-stamped exec_data folder (PricePath.getExecDataInputPath).
        Otherwise writes to the legacy per-strategy backtest_data folder
        (PricePath.getBacktestInputPath). Backtest behaviour is unchanged
        when production is omitted — all existing callers pass nothing
        for the new args and take the legacy branch.

        Args:
            price_data: dict of {field_name: DataFrame} to write.
            universe: universe slug (e.g. "sp500"). Required.
            strategy_name: strategy name (ignored when production=True
                because exec_data is universe-shared).
            production: when True, write to exec_data path. Default False
                preserves backtest behaviour.
            run_date: the data date (the day Norgate posted EOD data).
                Used to name the date-stamped exec_data folder. Defaults
                to today when None. Ignored when production=False.
        """
        # C1 Patch 2: resolve output directory once, branch on production flag.
        if production:
            out_dir = PricePath.getExecDataInputPath(universe=universe, run_date=run_date)
        else:
            out_dir = PricePath.getBacktestInputPath(universe=universe, strategy_name=strategy_name)

        _p174_written = 0
        _p174_skipped = 0
        for k in price_data.keys():
            # Patch 174: internal markers never hit disk; keys another pair
            # already wrote this run are skipped (content-identical by the
            # shared-cache construction -- rewriting base wides 4x per
            # refresh was pure parquet IO).
            if k.startswith('_p174'):
                continue
            if already_written is not None and k in already_written:
                _p174_skipped += 1
                continue
            df = price_data[k]
            # Apply ticker exclusions — drop columns for excluded tickers.
            # Covers both backtest (production=False) and execution (production=True).
            if excluded_tickers and hasattr(df, 'columns'):
                cols_to_drop = [c for c in excluded_tickers if c in df.columns]
                if cols_to_drop:
                    df = df.drop(columns=cols_to_drop)
            if 'universe' not in k and 'trading_dates' not in k and 'all_dates' not in k and 'sector_mapping' not in k:
                if universe.lower() != 'spy':
                    df = df.astype("float32")
            df.to_parquet(f'{out_dir}/{k}.parquet')
            _p174_written += 1
            if already_written is not None:
                already_written.add(k)
        if already_written is not None:
            print(f'[PriceDataLoader] Patch 174: wrote {_p174_written} '
                  f'parquet(s), skipped {_p174_skipped} already-written')

    def get_trading_dates(self, start_trading=None, end_trading=None,
                          use_data=True, daily_closes=pd.Series(), all_dates=[], max_lookback=255, rebalance='daily'):
        """
            Generates a date array containing the dates you wish to trade on. Considers
            the NYSE trading calendar.

            NOTE: When using a weekly rebalance, this can have some strange effects on the first and last week of the year.

            Parameters
            ----------
            max_lookback : int, default 200
                The maximum lookback needed for your backtest.
            rebalance : {'daily', 'weekly', 'month-end', 'month-start'}, default 'daily'
                The frequency you wish to rebalance your portfolio. You have a choice
                of 'daily', 'weekly', 'month-end' or 'month-start'.
            start_trading : datetime, default None
                The date the first trade could happen.
            end_trading : datetime, default None
                The end of the backtest.
            offset : int, default 0
                If using a week or month based rebalance, use to offset the day which it will rebalance.
                Example: rebalance = "month-start", offset = 1 --> Trade 1 day after the start of the month.
                         rebalance = "week-end", offset = -3 --> Trade 3 days before the end of the week.
            use_data : bool, default True
                Whether or not you wish to use data.all_dates when getting valid dates.

            Raises
            ------
            ValueError
                If the rebalance does not contain "daily", "week", "month" in the string.

            Returns
            -------
            DatetimeIndex
                A list containing all valid dates that can be traded on at the desired
                rebalance frequency.

            """

        if use_data:
            all_dates = all_dates[max_lookback:]

            if start_trading is not None:
                start = start_trading
            else:
                start = all_dates[0]
            if end_trading is not None:
                end = end_trading
            else:
                end = all_dates[-1]
        else:
            start = start_trading
            end = end_trading

        nyse = mcal.get_calendar('NYSE')
        all_valid_dates = nyse.valid_days(start_date=start, end_date=end).tz_localize(None)
        rebalance = rebalance.lower()

        # Below loop edited by DanBarnes 21/10/2021 to edit weekly issue
        if rebalance != 'daily':
            dates_df = pd.DataFrame(range(len(all_valid_dates)), index=all_valid_dates, columns=['indx'])

            if 'month' in rebalance:
                dates_df['year'] = dates_df.index.year
                dates_df['month'] = dates_df.index.month
                grouped_trade_dates = dates_df.groupby(['year', 'month'], as_index=False)
            elif 'week' in rebalance:
                dates_df[['year', 'week', 'day']] = dates_df.index.isocalendar()
                grouped_trade_dates = dates_df.groupby(['year', 'week'], as_index=False)
            else:
                raise ValueError('Rebalance frequency must contain {daily, weekly, monthly}')

            if 'begin' in rebalance or 'start' in rebalance or 'first' in rebalance:
                trade_date_df = grouped_trade_dates['indx'].min()
            else:
                trade_date_df = grouped_trade_dates['indx'].max()

            if self.offset != 0:
                trade_date_keys = trade_date_df['indx'].array + self.offset
            else:
                trade_date_keys = trade_date_df['indx'].array

            trade_date_list = sorted(dates_df[dates_df.indx.isin(trade_date_keys)].index)
        else:
            trade_date_list = sorted(all_valid_dates)

        # When we offset we need to check if our 'offsetted' indexes are actually included in our full daily data range
        # Simple check to ensure are trading dates are valid
        if trade_date_list[0] < daily_closes.index[0]:
            trade_date_list = trade_date_list[1:]

        if trade_date_list[-1] > daily_closes.index[-1]:
            trade_date_list = trade_date_list[:-1]

        return trade_date_list

    @classmethod
    def create_strategy_Folder(cls, name):

        base = Path(PricePath.backtestPath)
        safe = name.strip()
        safe = safe.replace(" ", "_")

        strategy_dir = base / safe

        strategy_dir.mkdir(parents=True, exist_ok=True)
        (strategy_dir / "input").mkdir(exist_ok=True)
        (strategy_dir / "output").mkdir(exist_ok=True)