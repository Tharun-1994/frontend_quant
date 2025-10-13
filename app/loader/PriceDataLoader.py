from app.constants.PricePath import PricePath
import pandas as pd
import pandas_market_calendars as mcal

from app.constants.static_config import UNIVERSES_Codes


class PriceDataLoader:
    def __init__(self, base_path):
        self.base_path = base_path

    def load_all(self,rebalance='',universe=''):
        return {
            f'{rebalance}_closes': pd.read_csv(PricePath.close(self.base_path),index_col=['Date'],parse_dates=True),
            f'{rebalance}_opens': pd.read_csv(PricePath.opens(self.base_path),index_col=['Date'],parse_dates=True),
            f'{rebalance}_highs': pd.read_csv(PricePath.highs(self.base_path),index_col=['Date'],parse_dates=True),
            f'{rebalance}_lows': pd.read_csv(PricePath.lows(self.base_path),index_col=['Date'],parse_dates=True),
            f'{universe}_universe': pd.read_csv(PricePath.universe(f'{self.base_path}/{universe}'),index_col=['Date'],parse_dates=True),
            f'{rebalance}_unadjusted_closes': pd.read_csv(PricePath.unadjustedCloses(self.base_path),index_col=['Date'],parse_dates=True),
        }

    def load_spy_close(self,rebalance=''):
        return {
            f'{rebalance}_closes_spy': pd.read_csv(PricePath.spy_closes(PricePath.index_path),
                                                          index_col=['Date'], parse_dates=True)
        }

    def uploadCommonPath(self,price_data={}, universe=""):

        for k in price_data.keys():

            if 'universe' not in k and 'trading_dates' not in k and 'all_dates' not in k :
                # print(k)
                price_data[k] = price_data[k].astype("float32")
            price_data[k].to_parquet(f'{PricePath.getBacktestInputPath(universe=universe)}/{k}.parquet')
            # price_data[k].to_csv(f'{PricePath.getBacktestInputPath(universe=universe)}/{k}.csv')




    def get_trading_dates(self, start_trading=None, end_trading=None,
                          use_data=True,daily_closes=pd.Series(),all_dates=[],max_lookback = 255,rebalance='daily'):
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



    # def crsi_exploration(self):
    #     print("CRSI")
        # CRSI = CRSI(prices=pricedata.daily_closes, RSI_length=strategy_params["crsi_length"], UpDown_length=2,
        #             ROC_length=100)