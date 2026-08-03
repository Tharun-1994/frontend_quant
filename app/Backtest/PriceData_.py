from tqdm import tqdm
import norgatedata
import pandas as pd
import numpy as np
import os
import pandas_market_calendars as mcal
from math import floor, ceil
import plotly.graph_objects as go
import datetime as dt




from plotly.subplots import make_subplots

import time
import multiprocessing as mp
from plotly.offline import plot

from q_lib.Strategy_Executions.paths_contants import Project_Paths
from q_lib.TechnicalIndicators import RSI, ADX


# can either have the static variables in the init func or move them to get stock data func?


class PriceData:
    def __init__(self, start_dt, end_dt, data_source='Norgate', data_path=None, max_lkback=0, rebalance='daily',
                 offset=0, in_out_sampling=None, universe='S&P 500', price_adjust='TOTALRETURN', padding='NONE',
                 interval='D', fields=['Open', 'High', 'Low', 'Close'], num_of_cpus=6,enable_all_valid=False):
        # ****---- Core Attr
        self.start_date = start_dt
        self.end_date = end_dt
        self.max_lookback = max_lkback
        self.rebalance = rebalance
        self.offset = offset
        self.data_source = data_source
        self.data_path = data_path  # optional if we read csv
        self.in_out_sampling_dict = in_out_sampling  # optimisation
        # ****---- Noregate Attrs
        self.universe = universe
        self.interval = interval
        self.price_adjust = price_adjust.upper()
        self.padding = padding.upper()
        self.enable_all_valid = enable_all_valid
        self.fields = fields
        self.num_of_cpus = num_of_cpus
        # ****---- Final dataframes containing prices
        self.daily_closes = None
        self.daily_opens = None
        self.daily_lows = None
        self.daily_highs = None
        self.daily_universes = None
        self.weekly_closes_spy =None
        self.closes_vix = None
        self.moving_avg_vix = None
        self.read_price_data()

    def read_price_data(self):
        if self.data_source == 'local_csv':
            try:
                self.run_import_local_csv(self.data_path)
            except:
                print('Please check stock data directory is correct!')

            self.get_trading_dates()
        elif self.data_source == 'Norgate':
            # Have to pull daily data
            self.run_import_norgatedata(universe=self.universe,
                                        start_dt=self.start_date,
                                        end_dt=self.end_date,
                                        intrval=self.interval,
                                        price_adjust=self.price_adjust,
                                        fields=self.fields,
                                        num_of_cpus=self.num_of_cpus,padding=self.padding)
            self.get_trading_dates()
        else:
            return print('Please check data source is correct!')

        return print('\nStock data retrieved and saved to Data object...')

    def run_import_local_csv(self, stock_data_path: str):
        """
            Imports csv data from a defined folder.

            Inside the folder, there must be a minimum of the following two files:
                daily_closes.csv
                daily_opens.csv

            If you are using an intra-day stop-loss or take profit, must also have the following:
                daily_highs.csv
                daily_lows.csv

            Names of the files are strict, and must be of the correct format, which is the tickers as the column headers, dates
            as the index values and the values being the respective prices.

            The data read in will be stored in the data variable (eg. data.daily_closes). Any additional files in the folder
            will also be stored in data with the respective name of the file.

            Parameters
            ----------
            stock_data_path : str
                The location of the folder containg the price data files.
            start_date : pandas.Timestamp
                Date from which the data will start.
            end_date : pandas.Timestamp
                Date from which the data will start.

            Raises
            ------
            TypeError
                Stock data is not of type str
            """
        # Dictionary has keys as the file names (without the extension) and the values are dataframes containing the data.
        all_files = {fname[:-4]: pd.read_csv(stock_data_path + '\\' + fname, index_col=0, parse_dates=True) for
                     fname in
                     os.listdir(stock_data_path) if 'daily' in fname}
        # Using pandas_market_calendars to get the NYSE trading calendar.
        nyse = mcal.get_calendar('NYSE')
        # trading_dates = nyse.valid_days(self.start_date, self.end_date).tz_localize(None)  # Make dates timezone naive
        ticker_map_df = pd.read_csv(rf'{stock_data_path}\ticker_mapping.csv')
        setattr(self, 'ticker_mapping', dict(zip(ticker_map_df.numbers, ticker_map_df.symbols)))
        # bull and bear periods
        setattr(self, 'bull_periods',
                pd.read_csv(rf'{stock_data_path}\bull.csv', parse_dates=['start_dates', 'end_dates'],
                            dayfirst=True))
        setattr(self, 'bear_periods',
                pd.read_csv(rf'{stock_data_path}\bear.csv', parse_dates=['start_dates', 'end_dates'],
                            dayfirst=True))
        for fname, daily_data in all_files.items():
            if 'universes' not in fname:
                daily_data.columns = [self.ticker_mapping[int(x)] for x in daily_data.columns]
            # else:
            #     # pass # because my universe data is in ticker letters no numbers
            #     # for i in daily_data.columns:
            #     #     daily_data[i] = daily_data[i].map(self.ticker_mapping)
            daily_data = daily_data.loc[self.start_date:self.end_date]
            # daily_data = daily_data.reindex(trading_dates, method='ffill')  # Any missing dates are forward-filled
            # daily_data.dropna(how='all', inplace=True)  # Removing any dates where no stocks have data.
            setattr(self, fname, daily_data)

        setattr(self, 'all_dates', self.daily_closes.index)
        return

    # Norgate Methods
    def run_import_norgatedata(self,
                               universe='S&P 500',
                               start_dt=dt.date(1998, 1, 1),
                               end_dt=None,
                               intrval='D',
                               price_adjust='NONE',
                               padding='NONE',
                               fields=['Open', 'High', 'Low', 'Close'],
                               num_of_cpus=6,enable_all_valid=True):

        pp = Project_Paths()

        if isinstance( self.universe, list):
            # self.universe_builder(universe, start_dt, end_dt, num_of_cpus, padding)
            ticker_groups = np.array_split(self.universe, num_of_cpus)
        elif self.universe != 'Liquid_500':
            self.universe_builder(universe, start_dt, end_dt, num_of_cpus, padding)
            ticker_groups = np.array_split(list(self.daily_universes.columns), num_of_cpus)
        else:
            if (dt.datetime.now().time().hour >= 22) and (dt.datetime.now().time().hour <= 23):
                self.daily_universes = pd.read_csv(
                    f'{pp.liquid_universe_night}Final_Liquid_500_QAS/Dan_US_Liquid_500_most_recent_5_price_drop.csv')
            else:
                self.daily_universes = pd.read_csv(
                    f'{pp.liquid_universe}Final_Liquid_500_QAS/Dan_US_Liquid_500_most_recent_5_price_drop.csv')
            # del self.daily_universes['Unnamed: 0']
            self.daily_universes['Date'] = pd.to_datetime(self.daily_universes['Date'])
            self.daily_universes.set_index('Date', inplace=True)
            ticker_groups = np.array_split(list(self.daily_universes.columns), num_of_cpus)

        try:
            if self.daily_universes is not None and len(self.daily_universes) > 0 and 'RDFN' in self.daily_universes.columns:
                self.daily_universes = self.daily_universes.drop('RDFN', axis=1)
        except Exception as e:
            print()



        # Create a manager dict to collate results and define the number of CPUs you want to utilise
        # (NOT SURE WHAT IS BEST/OPTIMUM, can we use all the cpus?)
        manager = mp.Manager()
        pricedf_list = manager.list()
        # open_list = manager.list()
        # low_list = manager.list()
        # high_list = manager.list()
        processes = []

        for i in range(num_of_cpus):
            p = mp.Process(target=self.mp_pull_norgatedata_v2,
                           args=(ticker_groups[i], pricedf_list, start_dt, end_dt, intrval, price_adjust, padding,
                                 fields))
            processes.append(p)
            p.start()

        for proc in processes:
            proc.join()

        price_df = pd.DataFrame()
        for i in range(len(pricedf_list)):
            price_df = pd.concat([pricedf_list[i], price_df], axis=1)

        for field in fields:
            temp_df = price_df.T[price_df.columns.get_level_values(1) == field].T
            temp_df.columns = [col[0] for col in temp_df.columns]

            if self.enable_all_valid:
                nyse = mcal.get_calendar('NYSE')
                all_valid_dates = nyse.valid_days(start_date=start_dt, end_date=end_dt).tz_localize(None)
                # temp_df = temp_df.reindex(all_valid_dates.date).dropna(how='all')
                temp_df = temp_df.loc[all_valid_dates]

            setattr(self, f'daily_{field.lower().replace(" ","")}s', temp_df)

        setattr(self, 'all_dates', self.daily_closes.index)
        return

    def universe_builder(self, universe, start_dt, end_dt=None, num_of_cpus=6, padding='NONE'):
        if isinstance(self.universe, list):
            universe_tickers = universe
        else:
            universe_tickers = norgatedata.watchlist_symbols(f'{universe} Current & Past')


        ticker_groups = np.array_split(universe_tickers, num_of_cpus)
        padding = eval(f'norgatedata.PaddingType.{padding}')

        # Create a manager dict to collate results and define the number of CPUs you want to utilise
        # (NOT SURE WHAT IS BEST/OPTIMUM, can we use all the cpus?)
        manager = mp.Manager()
        universe_dict = manager.list()
        processes = []

        for i in range(num_of_cpus):
            p = mp.Process(target=self.mp_universe_fetcher,
                           args=(ticker_groups[i], universe, start_dt, end_dt, universe_dict, padding))
            processes.append(p)
            p.start()

        for proc in processes:
            proc.join()

        # bring final df together
        daily_uni = pd.DataFrame()
        for i in range(len(universe_dict)):
            daily_uni = pd.concat([daily_uni, universe_dict[i]], axis=1)

        # set class attr
        self.daily_universes = daily_uni.loc[start_dt:end_dt].dropna(axis=1, how='all')
        del manager
        del universe_dict
        del processes
        return

    @staticmethod
    def mp_universe_fetcher(tickers, universe, start_dt, end_dt, universe_list, padding):
        df = pd.DataFrame()
        for ticker in tickers:
            daily_universe = norgatedata.index_constituent_timeseries(ticker,
                                                                      universe,
                                                                      padding_setting=padding,
                                                                      start_date=start_dt,
                                                                      end_date=end_dt,
                                                                      timeseriesformat='pandas-dataframe',
                                                                      )
            daily_universe.columns = [ticker]
            df = pd.concat([daily_universe, df], axis=1)

        del daily_universe
        universe_list.append(df)

        return

    @staticmethod
    def mp_pull_norgatedata_v2(tickers, pricedf_list, start_dt, end_dt, intrval, price_adjust, padding, fields):

        price_adjust = eval(f'norgatedata.StockPriceAdjustmentType.{price_adjust}')
        padding = eval(f'norgatedata.PaddingType.{padding}')

        price_df = pd.DataFrame()

        for ticker in tickers:
            try:
                prices = norgatedata.price_timeseries(ticker,
                                                      stock_price_adjustment_setting=price_adjust,
                                                      padding_setting=padding,
                                                      start_date=start_dt,
                                                      end_date=end_dt,
                                                      timeseriesformat='pandas-dataframe',
                                                      interval=intrval,
                                                      # datetimeformat='datetime')
                                                      fields=fields)

                prices.columns = pd.MultiIndex.from_product([[ticker], prices.columns])
                price_df = pd.concat([prices, price_df], axis=1)
            except:
                print(ticker)

        pricedf_list.append(price_df)
        del price_df

        return

    def add_noregate_data(self, tickers, start_dt, end_dt, intrval, price_adjust, padding,
                          fields=['Open', 'High', 'Low', 'Close']):
        # will take a ticker list and append the data to existing dataframes
        price_adjust = eval(f'norgatedata.StockPriceAdjustmentType.{price_adjust}')
        padding = eval(f'norgatedata.PaddingType.{padding}')

        for ticker in tickers:
            prices = norgatedata.price_timeseries(ticker,
                                                  stock_price_adjustment_setting=price_adjust,
                                                  padding_setting=padding,
                                                  start_date=start_dt,
                                                  end_date=end_dt,
                                                  timeseriesformat='pandas-dataframe',
                                                  interval=intrval,
                                                  # datetimeformat='datetime'
                                                  )

            for field in fields:
                temp_df = eval(f'self.daily_{field.lower()}s')
                temp_df[ticker] = prices[field]

        return

    # Core Functions
    def get_trading_dates(self, start_trading=None, end_trading=None, use_data=True):
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
            all_dates = self.all_dates[self.max_lookback:]

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
        rebalance = self.rebalance.lower()

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
        if trade_date_list[0] < self.daily_closes.index[0]:
            trade_date_list = trade_date_list[1:]

        if trade_date_list[-1] > self.daily_closes.index[-1]:
            trade_date_list = trade_date_list[:-1]

        # Then need to apply a filter for the start/end dates we specify in the function

        # DB - NOT SURE WHAT THE BELOW IS ACTUALLY DOING
        # if end_trading is not None and use_data:
        #     final_index = data.all_dates.get_loc(date_list[-1])
        #     data.all_dates = data.all_dates[:final_index+1]

        # return date_list
        setattr(self, 'trading_dates', trade_date_list)
        return print('\nTrading dates added...')

    def get_weekly_data(self):
        # Store Weekly close dates (ie last day of week, typically Fri) to append to our weekly data for consistency
        if self.data_source == 'Norgate':
            data_dt_indx = self.daily_closes.index
            data_dt_df = data_dt_indx.isocalendar()
            data_dt_df.reset_index(inplace=True)
            wkly_close_dates = data_dt_df.groupby(['year', 'week'], as_index=False).last()['Date'].values
            wkly_timestamps = [pd.Timestamp(x) for x in wkly_close_dates]
            setattr(self, 'wkly_close_timestamps', wkly_timestamps)

            # Create data.weekly_* from daily prices
            daily_closes = self.daily_closes.copy()
            daily_closes[['year', 'week', 'day']] = daily_closes.index.isocalendar()
            weekly_closes = daily_closes.groupby(['year', 'week'], as_index=False).last()
            weekly_closes['Date'] = self.wkly_close_timestamps
            weekly_closes.set_index('Date', inplace=True)
            setattr(self, 'weekly_closes', weekly_closes.drop(['year', 'week', 'day'], axis=1))

            daily_opens = self.daily_opens.copy()
            daily_opens[['year', 'week', 'day']] = daily_opens.index.isocalendar()
            weekly_opens = daily_opens.groupby(['year', 'week'], as_index=False).first()
            weekly_opens['Date'] = self.wkly_close_timestamps
            weekly_opens.set_index('Date', inplace=True)
            setattr(self, 'weekly_opens', weekly_opens.drop(['year', 'week', 'day'], axis=1))

            daily_lows = self.daily_lows.copy()
            daily_lows[['year', 'week', 'day']] = daily_lows.index.isocalendar()
            weekly_lows = daily_lows.groupby(['year', 'week'], as_index=False).min()
            weekly_lows['Date'] = self.wkly_close_timestamps
            weekly_lows.set_index('Date', inplace=True)
            setattr(self, 'weekly_lows', weekly_lows.drop(['year', 'week', 'day'], axis=1))

            daily_highs = self.daily_highs.copy()
            daily_highs[['year', 'week', 'day']] = daily_highs.index.isocalendar()
            weekly_highs = daily_highs.groupby(['year', 'week'], as_index=False).max()
            weekly_highs['Date'] = self.wkly_close_timestamps
            weekly_highs.set_index('Date', inplace=True)
            setattr(self, 'weekly_highs', weekly_highs.drop(['year', 'week', 'day'], axis=1))
        else:
            data_dt_indx = self.daily_closes.index
            data_dt_df = data_dt_indx.isocalendar()
            data_dt_df.reset_index(inplace=True)
            wkly_close_dates = data_dt_df.groupby(['year', 'week'], as_index=False).last()['index'].values
            wkly_timestamps = [pd.Timestamp(x) for x in wkly_close_dates]
            setattr(self, 'wkly_close_timestamps', wkly_timestamps)

            # Create data.weekly_* from daily prices
            daily_closes = self.daily_closes.copy()
            daily_closes[['year', 'week', 'day']] = daily_closes.index.isocalendar()
            weekly_closes = daily_closes.groupby(['year', 'week'], as_index=False).last()
            weekly_closes['index'] = self.wkly_close_timestamps
            weekly_closes.set_index('index', inplace=True)
            setattr(self, 'weekly_closes', weekly_closes.drop(['year', 'week', 'day'], axis=1))

            daily_opens = self.daily_opens.copy()
            daily_opens[['year', 'week', 'day']] = daily_opens.index.isocalendar()
            weekly_opens = daily_opens.groupby(['year', 'week'], as_index=False).first()
            weekly_opens['index'] = self.wkly_close_timestamps
            weekly_opens.set_index('index', inplace=True)
            setattr(self, 'weekly_opens', weekly_opens.drop(['year', 'week', 'day'], axis=1))

            daily_lows = self.daily_lows.copy()
            daily_lows[['year', 'week', 'day']] = daily_lows.index.isocalendar()
            weekly_lows = daily_lows.groupby(['year', 'week'], as_index=False).min()
            weekly_lows['index'] = self.wkly_close_timestamps
            weekly_lows.set_index('index', inplace=True)
            setattr(self, 'weekly_lows', weekly_lows.drop(['year', 'week', 'day'], axis=1))

            daily_highs = self.daily_highs.copy()
            daily_highs[['year', 'week', 'day']] = daily_highs.index.isocalendar()
            weekly_highs = daily_highs.groupby(['year', 'week'], as_index=False).max()
            weekly_highs['index'] = self.wkly_close_timestamps
            weekly_highs.set_index('index', inplace=True)
            setattr(self, 'weekly_highs', weekly_highs.drop(['year', 'week', 'day'], axis=1))

        return print('Saved Weekly data to Data object.')

    # Optimisation
    def get_in_out_sample_dates_fixed(self):
        """
                Splits the data into two subsets containing in-sample and out-of-sample dates.

                Trim off a specified percent of the data to leave in the out-of-sample and also remove a random percentage of months
                from each year to place into the out-of-sample array.

                Parameters
                ----------
                sampling_param_dict : dict
                    Dictionary containing two key-value pairs:
                        "end_trim_percent": float
                        "random_month_percent": float

                Returns
                -------
                tuple
                    Tuple countaining two arrays. The first being the in-sample date array and the second being the out-of-sample
                    date array.
                """
        # import random  # For the removing of moneths from each year

        trim_off_end = self.in_out_sampling_dict['end_trim_percent']
        random_month_remove_pct = self.in_out_sampling_dict['random_month_percent']

        all_dates = self.all_dates.copy()  # Making a copy so we don't affect the original
        num_to_trim = ceil(len(all_dates) * trim_off_end / 100)  # The integer number of dates to remove (rounding up)

        trimmed_dates = all_dates[:-num_to_trim]
        self.all_dates = trimmed_dates.copy()  # Stops all trading from happening at the end of the backtest
        all_years = trimmed_dates.year.unique()

        # Initialising the output arrays
        is_dates = pd.DatetimeIndex([])
        oos_dates = pd.DatetimeIndex([])

        oos_bounds = {}
        for year in all_years:  # Loop over the years and select a random number from each year.
            year_dates = trimmed_dates[trimmed_dates.year == year]  # Filtering to the dates in the year
            # Not all years will have all 12 months. The first and last year might not be the entire year
            year_months = year_dates.month.unique()  # Finding all months in this trading year.
            num_months_to_remove = round(random_month_remove_pct * len(year_months) / 100)
            if num_months_to_remove == 0:
                # Nothing to do
                continue

            # oos_months = random.sample(range(year_months.min(), year_months.max()+1), num_months_to_remove)
            # oos_months = [2,5]
            if year == 1998:
                oos_months = [11, 12]
            elif year == 1999:
                oos_months = [3, 7]
            elif year == 2000:
                oos_months = [2, 5]
            elif year == 2001:
                oos_months = [1, 10]
            elif year == 2002:
                oos_months = [3, 9]
            elif year == 2003:
                oos_months = [1, 7]
            elif year == 2004:
                oos_months = [10, 12]
            elif year == 2005:
                oos_months = [2, 7]
            elif year == 2006:
                oos_months = [4, 5]
            elif year == 2007:
                oos_months = [5, 6]
            elif year == 2008:
                oos_months = [11, 12]
            elif year == 2009:
                oos_months = [1, 2]
            elif year == 2010:
                oos_months = [3, 4]
            elif year == 2011:
                oos_months = [2, 5]
            elif year == 2012:
                oos_months = [1, 9]
            elif year == 2013:
                oos_months = [7, 10]
            elif year == 2014:
                oos_months = [11, 12]
            elif year == 2015:
                oos_months = [1, 12]
            elif year == 2016:
                oos_months = [5, 6]
            elif year == 2017:
                oos_months = [2, 5]
            elif year == 2018:
                oos_months = [1, 10]
            elif year == 2019:
                oos_months = [4, 9]
            elif year == 2020:
                print('2020')
                oos_months = [6, 12]
            elif year == 2021:
                print('2021')
                oos_months = [6, 9]
            else:
                print('An error has occurred with the "oos" and the "is" dates')

            for month in oos_months:
                temp_dts = year_dates[year_dates.month == month]
                oos_bounds[f'{year}_{month}'] = (temp_dts.min(), temp_dts.max())
            # Splitting the dates into those that are in the random selection (to out-of-sample dates) and those that
            # are not in the random selection (in-sample dates)
            oos_dates = oos_dates.append(year_dates[year_dates.month.isin(oos_months)])
            is_dates = is_dates.append(year_dates[~year_dates.month.isin(oos_months)])

        oos_dates = oos_dates.append(all_dates[-num_to_trim:])  # Add the trimmed dates to the out-of-sample

        setattr(self, 'is_dates', is_dates)
        setattr(self, 'oos_dates', oos_dates)
        setattr(self, 'oos_bounds', oos_bounds)
        return print('Retreived in and out of sample dates.')

    def get_in_out_sample_dates(self):
        r'''
            Splits the data into two subsets containing in-sample and out-of-sample dates.

            Trim off a specified percent of the data to leave in the out-of-sample and also remove a random percentage of months
            from each year to place into the out-of-sample array.

            Parameters
            ----------
            sampling_param_dict : dict
                Dictionary containing two key-value pairs:
                    "end_trim_percent": float
                    "random_month_percent": float

            Returns
            -------
            tuple
                Tuple countaining two arrays. The first being the in-sample date array and the second being the out-of-sample
                date array.
            '''
        import random  # For the removing of moneths from each year

        trim_off_end = self.in_out_sampling_dict['end_trim_percent']
        random_month_remove_pct = self.in_out_sampling_dict['random_month_percent']

        all_dates = self.all_dates.copy()  # Making a copy so we don't affect the original
        num_to_trim = ceil(len(all_dates) * trim_off_end / 100)  # The integer number of dates to remove (rounding up)

        trimmed_dates = all_dates[:-num_to_trim]
        self.all_dates = trimmed_dates.copy()  # Stops all trading from happening at the end of the backtest
        all_years = trimmed_dates.year.unique()

        # Initialising the output arrays
        is_dates = pd.DatetimeIndex([])
        oos_dates = pd.DatetimeIndex([])

        for year in all_years:  # Loop over the years and select a random number from each year.
            year_dates = trimmed_dates[trimmed_dates.year == year]  # Filtering to the dates in the year
            # Not all years will have all 12 months. The first and last year might not be the entire year
            year_months = year_dates.month.unique()  # Finding all months in this trading year.
            num_months_to_remove = round(random_month_remove_pct * len(year_months) / 100)
            if num_months_to_remove == 0:
                # Nothing to do
                continue

            oos_months = random.sample(range(year_months.min(), year_months.max() + 1), num_months_to_remove)

            # Splitting the dates into those that are in the random selection (to out-of-sample dates) and those that
            # are not in the random selection (in-sample dates)
            oos_dates = oos_dates.append(year_dates[year_dates.month.isin(oos_months)])
            is_dates = is_dates.append(year_dates[~year_dates.month.isin(oos_months)])

        oos_dates = oos_dates.append(all_dates[-num_to_trim:])  # Add the trimmed dates to the out-of-sample

        setattr(self, 'is_dates', is_dates)
        setattr(self, 'oos_dates', oos_dates)
        return print('Retreived in and out of sample dates.')

    # Get additional data
    def get_benchmark_prices(self, ticker='SPY', price_type='close',interval='D'):
        close_df =pd.DataFrame()
        # will take a ticker list and append the data to existing dataframes
        if self.data_source == 'Norgate':
            price_adjust = eval(f'norgatedata.StockPriceAdjustmentType.TOTALRETURN')
            padding = eval(f'norgatedata.PaddingType.NONE')

            prices = norgatedata.price_timeseries(ticker,
                                                  stock_price_adjustment_setting=price_adjust,
                                                  padding_setting=padding,
                                                  start_date=self.start_date,
                                                  end_date=self.end_date,
                                                  timeseriesformat='pandas-dataframe',
                                                  interval=interval)
                                                  # datetimeformat='datetime'
           # spy_prices = nd.price_timeseries(symbol,
            #                                  stock_price_adjustment_setting=priceadjust,
            #                                  padding_setting=padding_setting,
            #                                  start_date=data.start_date,  # start_date_sp500,
            #                                  timeseriesformat=timeseriesformat)

            if price_type == 'all':

                prices = pd.DataFrame(prices)
                self.spy_prices = prices


                # for field in fields:
                #     temp_df = eval(f'self.daily_{field.lower()}s')
                #     temp_df[ticker] = prices[field]
            elif price_type == 'close':
                    close_df = prices['Close'].rename(ticker)

            if interval == 'W':
                self.weekly_closes_spy = prices['Close']
        else:
            close_df = self.daily_closes['SPY']

        setattr(self, f'benchmark', close_df)

    def get_moving_average_data(self, period=None, ma_type='simple', price='close'):
        for per in period:
            if ma_type.upper() == 'SIMPLE' or ma_type.upper() == 'SMA':
                if price.lower() == 'close':
                    ma_df = self.daily_closes.rolling(window=per).mean()
                    attr_str = f'SMA_{per}'
            elif ma_type.upper() == 'EXP' or ma_type.upper() == 'EMA':
                if price.lower() == 'close':
                    ma_df = self.daily_closes.sort_index()
                    ma_df = ma_df.ewm(span=per, min_periods=0, adjust=False, ignore_na=False).mean()
                    attr_str = f'EMA_{per}'
                    pass

            setattr(self, attr_str, ma_df)
        return print('Moving Average data written to price data object.')

    def get_moving_average_data_price_series(self, price_series, period=None, ma_type='simple', price='spy'):
        for per in period:
            if ma_type.upper() == 'SIMPLE' or ma_type.upper() == 'SMA':
                if price.lower() == 'spy':
                    ma_df = price_series.rolling(window=per).mean()
                    attr_str = f'SMA_{per}'
            elif ma_type.upper() == 'EXP' or ma_type.upper() == 'EMA':
                if price.lower() == 'spy':
                    ma_df = price_series.sort_index()
                    ma_df = ma_df.ewm(span=per, min_periods=0, adjust=False, ignore_na=False).mean()
                    attr_str = f'EMA_{per}'
                    pass

            setattr(self, attr_str, ma_df)

        return print('Moving Average data written to price data object.')
    def get_RSI_df(self, period=14):
        rsi = RSI(self.daily_closes, n=period)
        setattr(self, f'RSI_{period}', rsi)
        return rsi

    def get_RSI_df_price_series(self, prices, period=14):
        rsi = RSI(prices, n=period)
        setattr(self, f'RSI_{period}', rsi)
        return rsi
    '''
    Function to find 'Bull' or 'Bear' using the  RSI value 
    '''
    def bull_or_bear_using_rsi(self, priceseries,period=12,rsiValue= 20):
        rsi = self.get_RSI_df_price_series(priceseries,period)
        return rsi > rsiValue

    def get_hist_vol_data(self, period=52, timeframe='daily'):
        if timeframe == 'daily':
            hist_vol_df = np.log(self.daily_closes / self.daily_closes.shift(1)).rolling(period).std() * np.sqrt(252)
        elif timeframe == 'weekly':
            hist_vol_df = np.log(self.weekly_closes / self.weekly_closes.shift(1)).rolling(period).std() * np.sqrt(252)
        # hist_vol_df.drop(self.hist_vol_df.index[-1], inplace=True)
        setattr(self, 'hist_vol', hist_vol_df)
        return hist_vol_df

    def get_hist_vol_data_price_series(self,prices, period=52, timeframe='daily'):
        if timeframe == 'daily':
            hist_vol_df = np.log(prices / prices.shift(1)).rolling(period).std() * np.sqrt(252)
        elif timeframe == 'weekly':
            hist_vol_df = np.log(prices / prices.shift(1)).rolling(period).std() * np.sqrt(252)
        # hist_vol_df.drop(self.hist_vol_df.index[-1], inplace=True)
        setattr(self, 'hist_vol', hist_vol_df)
        return hist_vol_df


    def get_congestion_index_df(self, length=25, smoothing=3):
        ci_df = round((((self.daily_closes / self.daily_closes.shift(length - 1) - 1) * 100) /
                       (self.daily_highs.rolling(window=length).max() / self.daily_lows.rolling(
                           window=length).min() - 1)))

        ci_df.replace(np.nan, 0, inplace=True)
        setattr(self, f'CI_{length}_{smoothing}', ci_df.ewm(span=smoothing).mean())

        return round(ci_df.ewm(span=smoothing).mean())

    # Plots
    def get_stock_chart(self, stock=None, timeframe='daily', ma=False, ma_type='simp',
                        ma_lookbacks=[200], display_plot=True):
        print(f'Plotting {timeframe} graph for {stock}...')
        if stock is None:
            return print('Please pass a stocker ticker using the stock variable!')
        if timeframe == 'daily':
            fig = go.Figure(data=[go.Candlestick(x=self.daily_closes.index,
                                                 open=self.daily_opens[stock],
                                                 high=self.daily_highs[stock],
                                                 low=self.daily_lows[stock],
                                                 close=self.daily_closes[stock],
                                                 name='Daily Price')])
            fig.update_layout(title=f'{stock} Stock Price Evolution',
                              xaxis_rangeslider_visible=False,
                              template='plotly_dark')
        elif timeframe == 'weekly':
            self.get_weekly_data()

            fig = go.Figure(data=[go.Candlestick(x=self.weekly_closes.index,
                                                 open=self.weekly_opens[stock],
                                                 high=self.weekly_highs[stock],
                                                 low=self.weekly_lows[stock],
                                                 close=self.weekly_closes[stock],
                                                 name='Weekly Price')],
                            )
            fig.update_layout(title=f'{stock} Stock Price Evolution',
                              xaxis_rangeslider_visible=False,
                              template='plotly_dark')

        index_df = pd.DataFrame(self.daily_closes[stock].copy())
        index_df.columns = ['close']
        if ma:
            # Add in moving averages

            ma_colors = ['yellow', 'lime', 'orange', 'darkturquoise', 'mediumslateblue']
            for i in ma_lookbacks:
                if ma_type == 'simp':
                    index_df[f'SMA_{i}'] = index_df['close'].rolling(window=i).mean()
                elif ma_type == 'exp':
                    pass

            for i in ma_lookbacks:
                if ma_type == 'simp':
                    index_df[f'SMA_{i}'] = index_df['close'].rolling(window=i).mean()
                elif ma_type == 'exp':
                    pass
            # add moving averages to plot
            for i in range(len(ma_lookbacks)):
                if ma_type == 'simp':
                    fig.add_trace(go.Scatter(x=index_df.index, y=index_df[f'SMA_{ma_lookbacks[i]}'],
                                             name=f'SMA_{ma_lookbacks[i]}',
                                             line=dict(color=ma_colors[i],
                                                       dash='dot')))
                elif ma_type == 'exp':
                    fig.add_trace(go.Scatter(x=index_df.index, y=index_df[f'EMA_{ma_lookbacks[i]}'],
                                             name=f'EMA_{ma_lookbacks[i]}',
                                             line=dict(color=ma_colors[i],
                                                       dash='dot')))

        if display_plot:
            plot(fig)

        return fig

    def plot_ta_chart(self, ticker_key='SPY', timeframe='daily', ma=False, ma_type='simp', ma_lookbacks=[200],
                      equity=False):
        if timeframe == 'weekly':
            self.get_weekly_data(self)
            index_closes = self.weekly_closes[ticker_key]
        elif timeframe == 'daily':
            index_closes = self.daily_closes[ticker_key]
            index_closes.name = 'close'
            index_df = pd.DataFrame(index_closes)
            index_df['open'] = self.daily_opens[ticker_key]
            index_df['high'] = self.daily_highs[ticker_key]
            index_df['low'] = self.daily_lows[ticker_key]
            index_df['volume'] = self.daily_volumes[ticker_key]

        # Create SubPlot
        fig = make_subplots(rows=2, cols=1,
                            specs=[[{'secondary_y': True}],
                                   [{'secondary_y': False}]],
                            row_width=[0.3, 0.7],
                            shared_xaxes=True,
                            subplot_titles=('Equity', 'Drawdown'))
        # OHLC candlstick plot
        fig.add_trace(go.Candlestick(x=index_df.index,
                                     open=index_df['open'],
                                     high=index_df['high'],
                                     low=index_df['low'],
                                     close=index_df['close'],
                                     name=ticker_key),
                      row=1, col=1, secondary_y=False)

        fig.update_layout(title=f'Strategy performance plotted against {ticker_key}',
                          xaxis_rangeslider_visible=False,
                          legend=dict(
                              yanchor="top",
                              y=0.99,
                              xanchor="left",
                              x=0.01,
                              orientation='h'),
                          template='plotly_dark')
        # template='plotly_dark')

        # # HISTORICAL VOLATILITY
        # vol_lookback = 52
        # index_df[f'hist_vol_{vol_lookback}'] = HistoricVolatility(index_df['close'], n=vol_lookback)
        # add rolling historic vol to subplot
        # fig.add_trace(go.Scatter(x=index_df.index, y=index_df['hist_vol_52'],
        #                          name=f'rolling_hist_vol_{vol_lookback}'),
        #               row=2, col=1)

        # # add VOLUME
        # fig.add_trace(go.Bar(x=index_df.index, y=index_df['volume'],
        #                      name='volume',
        #                      marker=dict(color='white')),
        #               row=3, col=1)

        # plot our EQUTIY CURVE over our Stock data
        if equity:
            fig.add_trace(go.Scatter(x=self.equity_curve.index, y=self.equity_curve.values,
                                     name=f'{ticker_key} Equity Curve', line=dict(color='fuchsia')),
                          row=1, col=1, secondary_y=True)
            fig.update_layout(yaxis2=dict(scaleanchor='y', scaleratio=0.001))

        # Add in MOVING AVERAGES to the stock data
        # ma_lookbacks = [10, 20, 50, 100, 200]
        if ma:
            # Add in moving averages

            ma_colors = ['yellow', 'lime', 'orange', 'darkturquoise', 'mediumslateblue']
            for i in ma_lookbacks:
                if ma_type == 'simp':
                    index_df[f'SMA_{i}'] = index_df['close'].rolling(window=i).mean()
                elif ma_type == 'exp':
                    pass

            for i in ma_lookbacks:
                if ma_type == 'simp':
                    index_df[f'SMA_{i}'] = index_df['close'].rolling(window=i).mean()
                elif ma_type == 'exp':
                    pass
            # add moving averages to plot
            for i in range(len(ma_lookbacks)):
                if ma_type == 'simp':
                    fig.add_trace(go.Scatter(x=index_df.index, y=index_df[f'SMA_{ma_lookbacks[i]}'],
                                             name=f'SMA_{ma_lookbacks[i]}',
                                             line=dict(color=ma_colors[i],
                                                       dash='dot')),
                                  row=1, col=1)
                elif ma_type == 'exp':
                    fig.add_trace(go.Scatter(x=index_df.index, y=index_df[f'EMA_{ma_lookbacks[i]}'],
                                             name=f'EMA_{ma_lookbacks[i]}',
                                             line=dict(color=ma_colors[i],
                                                       dash='dot')),
                                  row=1, col=1)

            # # add in MACD to subplot
            # macd_df = MACD(index_df['close'])
            # # plot our MACD line = long_ema - short_ema and then plot the signal line as well histogram of the difference
            # fig.add_trace(go.Scatter(x=macd_df.index, y=macd_df['MACD_line'],
            #                          name='MACD line',
            #                          line=dict(color='blue')),
            #               row=2, col=1)
            # fig.add_trace(go.Scatter(x=macd_df.index, y=macd_df['signal_line'],
            #                          name='signal line',
            #                          line=dict(color='orange')),
            #               row=2, col=1)
            # colours = {'True': 'green',
            #            'False': 'red'}
            # fig.add_trace(go.Bar(x=macd_df.index, y=macd_df['MACD_diff'], marker={'color': 'green'}, name='MACD diff'),
            #               row=2, col=1)

        fig.show()

        return fig

    def plot_moving_avg_graph(self, ticker_key='SPY', timeframe='daily', ma_type='simp', ma_lookbacks=[200]):
        if timeframe == 'weekly':
            self.get_weekly_data(self)
            index_closes = self.weekly_closes[ticker_key]
        elif timeframe == 'daily':
            index_closes = self.daily_closes[ticker_key]
            index_closes.name = 'close'
            index_df = pd.DataFrame(index_closes)
            index_df['open'] = self.daily_opens[ticker_key]
            index_df['high'] = self.daily_highs[ticker_key]
            index_df['low'] = self.daily_lows[ticker_key]
            index_df['volume'] = self.daily_volumes[ticker_key]

        # Create SubPlot
        fig = make_subplots(rows=2, cols=1,
                            specs=[[{'secondary_y': True}],
                                   [{'secondary_y': False}]],
                            row_width=[0.3, 0.7],
                            shared_xaxes=True)
        # OHLC candlstick plot
        fig.add_trace(go.Candlestick(x=index_df.index,
                                     open=index_df['open'],
                                     high=index_df['high'],
                                     low=index_df['low'],
                                     close=index_df['close'],
                                     name=ticker_key),
                      row=1, col=1, secondary_y=False)

        fig.update_layout(title=f'Moving Average Distance - {ticker_key}',
                          xaxis_rangeslider_visible=False,
                          legend=dict(
                              yanchor="top",
                              y=0.99,
                              xanchor="left",
                              x=0.01,
                              orientation='h'),
                          template='plotly_dark')

        ma_colors = ['yellow', 'lime', 'orange', 'darkturquoise', 'mediumslateblue']
        for i in ma_lookbacks:
            if ma_type == 'simp':
                index_df[f'SMA_{i}'] = index_df['close'].rolling(window=i).mean()
                index_df[f'SMA_{i}_dist%'] = ((index_df['close'] / index_df[f'SMA_{i}']) - 1) * 100
            elif ma_type == 'exp':
                pass

        # add moving averages to plot
        for i in range(len(ma_lookbacks)):
            if ma_type == 'simp':
                fig.add_trace(go.Scatter(x=index_df.index, y=index_df[f'SMA_{ma_lookbacks[i]}'],
                                         name=f'SMA_{ma_lookbacks[i]}',
                                         line=dict(color=ma_colors[i],
                                                   dash='dot')),
                              row=1, col=1)

                fig.add_trace(go.Scatter(x=index_df.index, y=index_df[f'SMA_{ma_lookbacks[i]}_dist%'],
                                         name=f'SMA_{ma_lookbacks[i]}_dist%'),  # ,fill='tozeroy', opacity=0.05),
                              row=2, col=1)
            elif ma_type == 'exp':
                fig.add_trace(go.Scatter(x=index_df.index, y=index_df[f'EMA_{ma_lookbacks[i]}'],
                                         name=f'EMA_{ma_lookbacks[i]}',
                                         line=dict(color=ma_colors[i],
                                                   dash='dot')),
                              row=1, col=1)
                fig.add_trace(go.Scatter(x=index_df.index, y=index_df[f'EMA_{ma_lookbacks[i]}_dist%'],
                                         name=f'EMA_{ma_lookbacks[i]}_dist%'),  # , fill='tozeroy', opacity=0.05),
                              row=2, col=1)

        for i in range(len(self.bull_periods)):
            fig.add_vrect(x0=self.bull_periods.iloc[i]['start_dates'],
                          x1=self.bull_periods.iloc[i]['end_dates'],
                          # annotation_text=self.bull_periods.iloc[i]['Name'],
                          # annotation_position='top left',
                          fillcolor='green',
                          opacity=0.2, line_width=0)

        for i in range(len(self.bear_periods)):
            fig.add_vrect(x0=self.bear_periods.iloc[i]['start_dates'],
                          x1=self.bear_periods.iloc[i]['end_dates'],
                          # annotation_text=self.bear_periods.iloc[i]['Name'],
                          # annotation_position='top left',
                          fillcolor='red',
                          opacity=0.2, line_width=0)

        fig.show()
        return

    def get_stock_price_df(self, ticker=''):
        stock_closes = self.daily_closes[ticker]
        stock_closes.name = 'close'
        stock_df = pd.DataFrame(stock_closes)
        stock_df['open'] = self.daily_opens[ticker]
        stock_df['high'] = self.daily_highs[ticker]
        stock_df['low'] = self.daily_lows[ticker]
        stock_df['volume'] = self.daily_volumes[ticker]

        return stock_df

    def get_vix_prices_df(self,ticker,interval='D'):

        price_adjust = eval(f'norgatedata.StockPriceAdjustmentType.TOTALRETURN')
        padding = eval(f'norgatedata.PaddingType.NONE')

        prices = norgatedata.price_timeseries(ticker,
                                              stock_price_adjustment_setting=price_adjust,
                                              padding_setting=padding,
                                              start_date=self.start_date,
                                              end_date=self.end_date,
                                              timeseriesformat='pandas-dataframe',
                                              interval=interval)
        if not prices.empty:
            self.closes_vix = prices['Close']
            self.open_vix = prices['Open']
        return

    def get_ADX_df(self, period=10):
        adx = ADX(self.daily_highs, self.daily_lows, self.daily_closes, length=period)
        setattr(self, f'ADX_{period}', adx)
        return adx

    # @staticmethod
    # def get_price_analysis_df(price_df):
    #     final_df = price_df.copy()
    #
    #     # Generate all technical indicators you want - examples below
    #     # Moving Averages
    #     ma_type = 'simple'
    #     periods = [10, 20, 50, 75, 100, 150, 200]
    #     for per in periods:
    #         if ma_type.lower() == 'simple':
    #             final_df[f'SMA_{per}'] = price_df.rolling(window=per).mean()
    #         elif ma_type.lower() == 'exp':
    #             return
    #
    #     # RSI
    #     periods = [14]
    #     for per in periods:
    #         final_df[f'RSI_{per}'] = RSI(price_df, n=per)
    #
    #     bear = pd.read_csv(fr'{bull_bear_path}\bear.csv')
    #     bear['start_dates'] = pd.to_datetime(bear['start_dates'], format='%d/%m/%Y')
    #     bear['end_dates'] = pd.to_datetime(bear['end_dates'], format='%d/%m/%Y')
    #     bear_df = pd.DataFrame()
    #     for i in range(len(bear)):
    #         name = bear.iloc[i]['Name']
    #         strt = bear.iloc[i]['start_dates']
    #         end = bear.iloc[i]['end_dates']
    #         temp_df = final_df.loc[strt:end].copy()
    #         temp_df['Name'] = name
    #         temp_df = round(temp_df, 4)
    #         bear_df = pd.concat([bear_df, temp_df])
    #
    #     bull = pd.read_csv(fr'{bull_bear_path}\bull.csv')
    #     bull['start_dates'] = pd.to_datetime(bull['start_dates'], format='%d/%m/%Y')
    #     bull['end_dates'] = pd.to_datetime(bull['end_dates'], format='%d/%m/%Y')
    #     bull_df = pd.DataFrame()
    #     for i in range(len(bull)):
    #         name = bull.iloc[i]['Name']
    #         strt = bull.iloc[i]['start_dates']
    #         end = bull.iloc[i]['end_dates']
    #         temp_df = final_df.loc[strt:end].copy()
    #         temp_df['Name'] = name
    #         temp_df = round(temp_df, 4)
    #         bull_df = pd.concat([bull_df, temp_df])
    #
    #     writer = pd.ExcelWriter(fr'{print_outpath}\price_analysis.xlsx', engine='xlsxwriter')
    #
    #     final_df.to_excel(writer, sheet_name='prices')
    #     bull_df.to_excel(writer, sheet_name='up_markets')
    #     bear_df.to_excel(writer, sheet_name='down_markets')
    #
    #     writer.save()
    #     return 1

    @staticmethod
    def plot_spy_ma(data_obj):
        spy = data_obj.daily_closes['SPY']
        spy_df = pd.DataFrame(spy)

        # Create SubPlot
        fig = make_subplots(rows=1, cols=1)
        fig.update_layout(title='SPY Close Distance above MA',
                          template='plotly_dark',
                          xaxis_rangeslider_visible=True
                          )

        ma_type = 'simple'
        periods = [20, 50, 100, 200]
        for per in periods:
            if ma_type.lower() == 'simple':
                spy_df[f'SMA_{per}'] = spy.rolling(window=per).mean()
                spy_df[f'SMA_{per}_dist%'] = ((spy_df['SPY'] / spy_df[f'SMA_{per}']) - 1) * 100

                if max(spy_df[f'SMA_{per}_dist%']) > max_dist:
                    max_dist = max(spy_df[f'SMA_{per}_dist%'])
                if min(spy_df[f'SMA_{per}_dist%']) < min_dist:
                    min_dist = min(spy_df[f'SMA_{per}_dist%'])

                fig.add_trace(go.Scatter(x=spy_df.index, y=spy_df[f'SMA_{per}_dist%'],
                                         name=f'SMA_{per}_dist%'),
                              row=1, col=1)
            elif ma_type.lower() == 'exp':
                pass

        for i in range(len(data_obj.bull_periods)):
            fig.add_vrect(x0=data_obj.bull_periods.iloc[i]['start_dates'],
                          x1=data_obj.bull_periods.iloc[i]['end_dates'],
                          annotation_text=data_obj.bull_periods.iloc[i]['Name'],
                          annotation_position='top left',
                          fillcolor='green',
                          opacity=0.2, line_width=0)

        for i in range(len(data_obj.bear_periods)):
            fig.add_vrect(x0=data_obj.bear_periods.iloc[i]['start_dates'],
                          x1=data_obj.bear_periods.iloc[i]['end_dates'],
                          annotation_text=data_obj.bear_periods.iloc[i]['Name'],
                          annotation_position='top left',
                          fillcolor='red',
                          opacity=0.2, line_width=0)
        fig.show()
        return 1


if __name__ == '__main__':
    # ---------------------- PRICE DATA STATIC
    # ____Price data static
    start_date = dt.date(1998, 1, 28)  # The date trading will start
    end_date = None  # dt.date(2022, 2, 2)  # The date trading will end - left off last two years so we dont over fit

    rebalance = 'weekly'  # 'daily', 'weekly', 'month-end', 'month-start'
    offset = 1
    max_lookback = 0
    cpu_count = 10

    data_source = 'Norgate'  # Either 'Norgate' or 'local_csv
    stock_data_path = r'C:\\Tharun\\A Git Projects Prod\\Updated_tharun_prod\\q_lib'  # folder path
    # FOR NORGATE
    # ____Norgate static
    price_universe = 'Liquid 500'  # Can also be an index symbol, such as $SPX, $RUI etc.

    priceadjust = 'TOTALRETURN'  # NONE, CAPITAL, CAPITALSPECIAL, TOTALRETURN
    padding_setting = 'NONE'  # NONE, ALLMARKETDAYS, ALLWEEKDAYS, ALLCALENDARDAYS
    timeseriesformat = 'numpy-ndarray'  # 'numpy-recarray'
    interval = 'D'  # 'M'(onthly) or 'W'(eekly)
    date_format = 'datetime'  # date, datetime64nx/ms, m8d
    data_fields_needed = ['Open', 'High', 'Low', 'Close', 'Unadjusted Close']  # The fields needed. If `check_stop_loss` is used, need OHLC

    # _____ SET VARIABLES FOR IN/OUT SAMPLE AND EXECUTION INSTR
    run_in_sample_test = False
    in_out_sampling = {'end_trim_percent': 10,
                       'random_month_percent': 25}

    # ------------------***     START of BACKTEST SCRIPT    ***------------------
    start_time = time.time()
    split_time = time.time()
    # 1. create price data object - NEED TO FIX THE IN OUT SAMPLING. ITS HORRIBLE. NEED BETTER LOGIC
    pricedata = PriceData(start_dt=start_date,
                          end_dt=end_date,
                          rebalance=rebalance,
                          offset=offset,
                          max_lkback=max_lookback,
                          data_source=data_source,
                          data_path=stock_data_path,
                          universe=price_universe,
                          price_adjust=priceadjust,
                          padding=padding_setting,
                          num_of_cpus=cpu_count,
                          fields=data_fields_needed)
    # Get additional data relevant to the strat
    pricedata.add_noregate_data(['SPY'], start_date, end_date, 'D', priceadjust, padding_setting)
    # pricedata.get_benchmark_prices('SPY', price_type='Close')
    pricedata.get_weekly_data()
    pricedata.get_hist_vol_data(52, timeframe='weekly')

