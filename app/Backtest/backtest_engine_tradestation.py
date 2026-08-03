import sys
import math

from fastapi import APIRouter
from tqdm import tqdm
from datetime import date
import datetime as dt
from datetime import datetime, timedelta
import os
import pandas as pd
import logging

from app.Backtest.backtest_engine_etf import ETFBacktestEngine
from app.schemas import StrategyRequest

logging.basicConfig(filename="logfilename.log", filemode='w', level=logging.WARN)

class BacktestEngine:


    def backtest_tradestation(self, strategy_data: StrategyRequest):
        engine = ETFBacktestEngine()
        return engine.run(
            strategy_data=strategy_data,
            base_input_path=f'C:/Tharun/Projects/backtest_data/{strategy_data.name}/input/{strategy_data.regimes[0].universe.lower()}',
            output_path=f'C:/Tharun/Projects/backtest_data/{strategy_data.name}/output'
        )

        # return portfolio_Dict







    def complete_backtest_all(portfolio_dict, count, datecode_str, curr_posn_dict):
        print('')












    # if __name__ == '__main__':
    #
    #     # configs = Properties()
    #     # pp = Project_Paths()
    #
    #     with open(f'{pp.project_path}RPTT/application_one_param.properties', 'rb') as read_prop:
    #         configs.load(read_prop)
    #
    #     # base_path = r'C:\Users\Nick_Elmer\Documents\BarnesD\spy_breakout'
    #     outpath = r'C:\Tharun\Source\Qlib_Demo'
    #
    #     # ---------------------- PRICE DATA STATIC
    #     # The date trading will start
    #     # The date trading will end - left off last two years so we dont over fit
    #
    #     strategy_name = configs.get("strategy_name").data
    #     day, month, year = configs.get("start_date").data.split('-')
    #     start_date = dt.date(int(year), int(month), int(day))
    #
    #     day, month, year = configs.get("end_date").data.split('-')
    #     end_date = dt.date.today()
    #     # end_date = dt.date(int(year), int(month), int(day))
    #
    #     day, month, year = configs.get("lookback_date").data.split('-')
    #     lookback_date = dt.date(int(year), int(month), int(day))
    #
    #     rebalance = configs.get("rebalance").data  # 'daily', 'weekly', 'month-end', 'month-start'
    #
    #     offset = 0
    #     max_lookback = int(configs.get("max_lookback").data)
    #
    #     data_source = configs.get("data_source").data  # Either 'Norgate' or 'local_csv
    #
    #     stock_data_path = r'{}'.format(configs.get("stock_data_path").data)  # folder path  # folder path
    #     # FOR NORGATE
    #     data_fields_needed = ['Open', 'High', 'Low', 'Close',
    #                           'Unadjusted Close','Volume','Turnover']  # The fields needed. If `check_stop_loss` is used, need OHLC
    #     data_adjustment = 'TotalReturn'  # The type of adjustment of the data
    #
    #     # ---------------------- STRATEGY STATIC
    #     starting_cash = 12500 # 2 dp. float
    #     max_share_amount = 6
    #
    #     strategy_params = configs.get("strategy_params_2").data
    #     strategy_params = literal_eval(strategy_params)
    #     # strategy_params['stoploss_pct'] =np.nan
    #     # strategy_params['takeprof_pct'] = np.nan
    #
    #     strategy_params['start_date'] = start_date
    #
    #
    #     strategy_params_3 = configs.get("strategy_params_3").data
    #     strategy_params_3 = literal_eval(strategy_params_3)
    #     strategy_params_3['start_date'] = start_date
    #
    #     # strategy_params_4 = configs.get("strategy_params_4").data
    #     # strategy_params_4 = literal_eval(strategy_params_4)
    #     # strategy_params_4['start_date'] = start_da0te
    #
    #     exe_start_date = np.datetime64(configs.get("exe_start_date").data)
    #     in_out_sampling = {'end_trim_percent': 10,
    #                        'random_month_percent': 25}
    #
    #     # State if we need to generate entry/exit signals or if we can pick them up from somewhere
    #     generate_signals = True
    #     signal_path = r''
    #     # Do you want to run only on insample ***
    #     run_in_sample_test = False
    #     execution_instr = False
    #     execution_lookback = 10
    #
    #     pricedata = PriceData(start_dt=lookback_date,
    #                           end_dt=end_date,
    #                           rebalance=rebalance,
    #                           offset=offset,
    #                           universe='Liquid_500',
    #                           max_lkback=max_lookback,
    #                           data_source=data_source,
    #                           data_path=stock_data_path,
    #                           in_out_sampling=in_out_sampling,
    #                           fields=data_fields_needed,
    #                           price_adjust=data_adjustment,
    #                           num_of_cpus=6)
    #
    #     pricedata.get_benchmark_prices(ticker='SPY', price_type='all')
    #     pricedata.daily_opens_spy = pricedata.spy_prices['Open']
    #     pricedata.daily_closes_spy = pricedata.spy_prices['Close']
    #     pricedata.daily_highs_spy = pricedata.spy_prices['High']
    #     pricedata.daily_lows_spy = pricedata.spy_prices['Low']
    #     pricedata.daily_closes_spy_unadjusted = pricedata.spy_prices['Unadjusted Close']
    #
    #     pricedata.get_vix_prices_df(ticker='$vix', interval='D')
    #
    #     padding_setting = nd.PaddingType.NONE
    #     priceadjust = nd.StockPriceAdjustmentType.TOTALRETURN
    #     symbol = '#SPXMCSUM'
    #     start_date_spy = '2001-01-01'
    #     timeseriesformat = 'pandas-dataframe'
    #     summation_index = nd.price_timeseries(symbol,
    #                                stock_price_adjustment_setting=priceadjust,
    #                                padding_setting=padding_setting,
    #                                start_date=start_date_spy,
    #                                timeseriesformat=timeseriesformat)
    #
    #
    #     pricedata.summation_index = summation_index['Close']
    #     pricedata.moving_average_sum = pricedata.summation_index.ewm(5).mean()
    #
    #
    #
    #     # Before backtest starts
    #     # pricedata.
    #     if run_in_sample_test:
    #         pricedata.get_in_out_sample_dates_fixed()
    #
    #     optimise_enable = configs.get("optimise_enable").data
    #     enable_kill_Switch = bool(configs.get("enable_kill_Switch").data)
    #
    #     # ------------------***     START of BACKTEST SCRIPT    ***------------------
    #     start_time = time.time()
    #     split_time = time.time()
    #     # 1. create price data object - NEED TO FIX THE IN OUT SAMPLING. ITS HORRIBLE. NEED BETTER LOGIC
    #     '''
    #     PriceData is the class where we will be loading the local_csv or Norgate data for open,high,low,close
    #     and we will get the valid trading days.
    #     SO we are initializing it with basic parameters
    #     '''
    #
    #
    #
    #
    #     print("\n--- Price Data retreived in %s seconds ---" % (round(time.time() - split_time, 2)))
    #     split_time = time.time()
    #
    #     # 2. Initiate strat class and do required steps (I DONT THINK WE NEED STARTING CASH AND MAX SHARES IN STRAT)
    #     '''
    #     This rsi_strat is a place for creating strategy parameters and initializing
    #       the entry_pass and exit_pass.
    #     '''
    #     strat_1 = rpt_strat(pricedata,
    #                         strat_params=strategy_params_3,
    #                         generate_signals=generate_signals,
    #                         signal_path=signal_path,
    #                         starting_capital=starting_cash,
    #                         max_share_amnt=max_share_amount,myname='one')
    #
    #     strat_2 = rpt_strat(pricedata,
    #                         strat_params=strategy_params_3,
    #                         generate_signals=generate_signals,
    #                         signal_path=signal_path,
    #                         starting_capital=starting_cash,
    #                         max_share_amnt=max_share_amount,myname='two')
    #
    #
    #     strategy_dict = {}
    #     strategy_dict['one'] = strat_1
    #
    #
    #
    #     # print("\n--- Generated strategy data %s seconds ---" % (round(time.time() - split_time, 2)))
    #     split_time = time.time()
    #     # 3. Initiate our portfolio
    #     '''
    #     Portfolio is the replica of Orders class
    #     1.entering trade
    #     2. closing trade
    #     3. Executing Entry and Exit signals
    #     '''
    #     portfolio_dict = {}
    #     portfolio_phantom_legacy = Portfolio(pricedata,
    #                                          strategy_params,
    #                                          starting_capital=starting_cash,
    #                                          max_amnt_shares=max_share_amount,
    #                                          in_sample_test=run_in_sample_test, sector_limit=4)
    #
    #     portfolio_dict['one'] = portfolio_phantom_legacy
    #
    #     phantom_two = Portfolio(pricedata,
    #                                          strategy_params,
    #                                          starting_capital=starting_cash,
    #                                          max_amnt_shares=max_share_amount,
    #                                          in_sample_test=run_in_sample_test, sector_limit=4)
    #
    #     portfolio_dict['two'] = phantom_two
    #
    #
    #
    #     # 4. Run Backtest
    #     run_backtest(data_obj=pricedata, strategy_dict=strategy_dict, portfolio_Dict=portfolio_dict, start_date=start_date,exe_start_date=exe_start_date)


backtestEngine = BacktestEngine()