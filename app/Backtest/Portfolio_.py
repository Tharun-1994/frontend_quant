import math
from math import floor
import pandas as pd
import numpy as np
from plotly.offline import plot
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import datetime as dt

from q_lib.Sector_Manager_reverse_polarity import Sector_Manager_reverse_polarity


class Orders:
    def __init__(self):
        self.order_book = {'ticker': ['amount', 'long/short', 'price']}

    def place_market_order(self, ticker, amount, lng_shrt):
        return 'trade_booking_confim_details'

    def place_limit_order(self, ticker, amount, long_short, price):
        return 'limit order placed'

    def check_execution(self, current_price):
        return 'trade_booking_confim_details'


class Portfolio(Orders):
    def __init__(self, data_obj, strat_params, starting_capital=100000, max_amnt_shares=10, in_sample_test=False,
                 optimise=False, risk_mngmnt_params=None,sector_level=0,sector_limit=0,min_to_enter=None):
        self.price_data = data_obj
        self.starting_capital = starting_capital
        self.max_amount_shares = max_amnt_shares
        self.in_sample_test = in_sample_test
        self.optimise = optimise
        # Trackers
        self.agg_holdings = dict()  # aggregate exposure for total portfolio risk managment
        self.live_trade_val_tracker = dict()  # unique trade id to handle multple trades of same ticker
        self.trade_log = dict()  # nested dic containing all details
        self.wealth_track = {}
        self.utility_track = {}
        self.utility_value_track = {}
        self.blacklist = {}
        self.trade_daycount = {}
        # self.trade_track = {x: {} for x in range(self.max_amount_shares)}
        # self.trade_keymap = dict()
        self.trade_count = 0
        self.unused_capital = starting_capital
        # Money management parameters
        self.strat_params = strat_params
        self.risk_mngmnt_params = risk_mngmnt_params
        self.freeze_out_days = 0
        self.count_shorter = 0
        self.count_medium = 0
        self.count_longer = 0
        self.sector_concentration={}
        self.sector_level = sector_level
        self.sector_limit = sector_limit
        self.sm = Sector_Manager_reverse_polarity()
        self.max_equity = tuple((0, self.starting_capital))
        self.drawdown={}
        self.vol_triggeres_indexes =[]
        self.vol_back_live_indexes =[]
        self.current_position_industries = {}
        self.min_to_enter = min_to_enter

        # self.compounding_capital = dict((smallItem, 0) for smallItem in range(self.strat_params['start_date'].year, data_obj.end_date.year + 1))
        # self.compounding_capital[self.strat_params['start_date'].year] = self.starting_capital




    # ------------------ ****** CORE FUNCTIONS ****** --------------
    def enter_trade(self, ticker, trade_date, price, long_short, amount, reason, price_used,rentry_price=np.nan,capital=10000,pair=0,limit_price=0):
        """
        This is the method that writes our trades into the portfolio, by updating our trade log and live trade tracker.
        We can develop this to include writing custom stoploss or take profit parameters to each trade rather than
        global values.

        Parameters
        ----------
        ticker (str): Ticker symbol for stock we are trading
        trade_date
        price
        long_short
        amount
        reason
        price_used

        Returns
        -------

        """
        self.trade_log[f'{ticker}_{self.trade_count}'] = {'symbol': ticker,
                                                          'direction': long_short,
                                                          'amount': amount,
                                                          'open_date': trade_date,
                                                          'open_price': price,
                                                          'limit_price': limit_price,
                                                          'open_value': round(price * amount, 2),
                                                          'open_reason': reason,
                                                          'close_date': None,
                                                          'close_price': None,
                                                          'close_value': None,
                                                          'close_reason': None,
                                                          'time_in_trade': None,
                                                          'profit': None,
                                                          'profit%': None,
                                                          'max_profit%': None,
                                                          'min_profit%': None,
                                                          'entry_timing': price_used,
                                                          'exit_timing': None,
                                                          'value_track': None,
                                                          'capital': capital,
                                                          'pair': pair
                                                          }  # should i add in custom money management
        # I have changed to not include our open value, but i am going to old store EOD
        # self.live_trade_val_tracker[f'{ticker}_{self.trade_count}'] = [round(price * amount, 2)]

        if rentry_price and 'rentry' in reason.lower():
            self.trade_log[f'{ticker}_{self.trade_count}']['entry_price'] = rentry_price
        else:
            self.trade_log[f'{ticker}_{self.trade_count}']['entry_price'] = price

        self.live_trade_val_tracker[f'{ticker}_{self.trade_count}'] = dict()
        self.unused_capital -= round(price * amount, 2)

        # For trade tracking
        # free_keys = [x for x in range(self.max_amount_shares) if x not in self.trade_keymap.values()]
        # self.trade_keymap[f'{ticker}_{self.trade_count}'] = free_keys[0]
        # self.trade_track[f'{ticker}_{self.trade_count}'] = {trade_date: round(price * amount, 2)}
        self.trade_daycount[f'{ticker}_{self.trade_count}'] = 0
        self.trade_count += 1

        return

    def close_trade(self, trade_id, trade_date, price, reason, price_used):
        # update trade log
        self.trade_log[trade_id]['close_date'] = trade_date
        self.trade_log[trade_id]['close_price'] = price
        self.trade_log[trade_id]['close_value'] = round(price * self.trade_log[trade_id]['amount'], 2)
        self.trade_log[trade_id]['close_reason'] = reason
        self.trade_log[trade_id]['exit_timing'] = price_used
        self.trade_log[trade_id]['time_in_trade'] = (trade_date - self.trade_log[trade_id]['open_date']).days
        self.trade_log[trade_id]['profit'] = round(self.trade_log[trade_id]['close_value'] - self.trade_log[trade_id][
            'open_value'], 2)
        self.trade_log[trade_id]['profit%'] = round(self.trade_log[trade_id]['profit'] / self.trade_log[trade_id][
            'open_value'], 4)

        # update trade value track, put it in trade log, and then remove from live trade holdings
        self.live_trade_val_tracker[trade_id][trade_date] = self.trade_log[trade_id]['close_value']

        self.trade_log[trade_id]['max_profit%'] = round((max(self.live_trade_val_tracker[trade_id].values()) /
                                                         self.trade_log[trade_id]['open_value']) - 1, 4)
        self.trade_log[trade_id]['min_profit%'] = round((min(self.live_trade_val_tracker[trade_id].values()) /
                                                         self.trade_log[trade_id]['open_value']) - 1, 4)
        self.trade_log[trade_id]['value_track'] = self.live_trade_val_tracker[trade_id]
        self.live_trade_val_tracker.pop(trade_id)  # remove the trade from live tracker

        del self.trade_daycount[trade_id]
        # self.trade_track[self.trade_keymap[trade_id]].update({trade_date: self.trade_log[trade_id]['close_value']})
        # self.trade_keymap.pop(trade_id)

        self.unused_capital += self.trade_log[trade_id]['close_value']  # bank the profits
        return

    # ------------------ ****** Portfolio Management ****** --------------
    def mark_to_market_hedge(self, trade_date, killswitch_bool=False):
        equity = self.unused_capital
        live_symbols=[]
        if len(self.live_trade_val_tracker) > 0:
            for k in self.live_trade_val_tracker.keys():



                ticker = self.trade_log[k]['symbol']
                live_symbols.append(ticker)
                amount = self.trade_log[k]['amount']


                close_price = self.price_data.daily_spy_closes.loc[trade_date]

                self.live_trade_val_tracker[k][trade_date] = round(amount * close_price, 2)
                # self.trade_track[self.trade_keymap[k]].update({trade_date: round(amount * close_price, 2)})
                equity += round(amount * close_price, 2)

                self.trade_daycount[k] += 1

        self.wealth_track[trade_date] = equity
        self.utility_track[trade_date] = len(self.live_trade_val_tracker)
        if equity > self.max_equity[1]:
            self.max_equity = (trade_date, equity)
        dd = round(self.max_equity[1] - list(self.wealth_track.values())[-1],2)
        self.drawdown[trade_date] = -dd

        if killswitch_bool:
            self.freeze_out_days -= 1

        # stop loss blacklist update
        blacklist_copy = self.blacklist.copy()
        for k in blacklist_copy.keys():
            blacklist_copy[k] -= 1
            if blacklist_copy[k] == 0:
                del self.blacklist[k]
        self.blacklist = {}
        for key in blacklist_copy:
            if blacklist_copy[key] != 0:
                self.blacklist[key] = blacklist_copy[key]

        # sector update
        sector_concentration_copy = self.sector_concentration.copy()
        for k in sector_concentration_copy.keys():
            if len(sector_concentration_copy[k]) == 0:
                del self.sector_concentration[k]


        # if trade_date.year in self.compounding_capital and self.compounding_capital[trade_date.year] ==0:
        #     self.compounding_capital[trade_date.year] = list(self.wealth_track.values())[-1]
        #     self.starting_capital = list(self.wealth_track.values())[-1]


        return


    def mark_to_market_hedge_1(self, trade_date, killswitch_bool=False):
        equity = self.unused_capital
        live_symbols=[]
        if len(self.live_trade_val_tracker) > 0:
            for k in self.live_trade_val_tracker.keys():



                ticker = self.trade_log[k]['symbol']
                live_symbols.append(ticker)
                amount = self.trade_log[k]['amount']


                close_price = self.price_data.daily_closes_spy.loc[trade_date]

                self.live_trade_val_tracker[k][trade_date] = round(amount * close_price, 2)
                # self.trade_track[self.trade_keymap[k]].update({trade_date: round(amount * close_price, 2)})
                equity += round(amount * close_price, 2)

                self.trade_daycount[k] += 1

        self.wealth_track[trade_date] = equity
        self.utility_track[trade_date] = len(self.live_trade_val_tracker)
        if equity > self.max_equity[1]:
            self.max_equity = (trade_date, equity)
        dd = round(self.max_equity[1] - list(self.wealth_track.values())[-1],2)
        self.drawdown[trade_date] = -dd

        if killswitch_bool:
            self.freeze_out_days -= 1

        # stop loss blacklist update
        blacklist_copy = self.blacklist.copy()
        for k in blacklist_copy.keys():
            blacklist_copy[k] -= 1
            if blacklist_copy[k] == 0:
                del self.blacklist[k]
        self.blacklist = {}
        for key in blacklist_copy:
            if blacklist_copy[key] != 0:
                self.blacklist[key] = blacklist_copy[key]

        # sector update
        sector_concentration_copy = self.sector_concentration.copy()
        for k in sector_concentration_copy.keys():
            if len(sector_concentration_copy[k]) == 0:
                del self.sector_concentration[k]


        # if trade_date.year in self.compounding_capital and self.compounding_capital[trade_date.year] ==0:
        #     self.compounding_capital[trade_date.year] = list(self.wealth_track.values())[-1]
        #     self.starting_capital = list(self.wealth_track.values())[-1]


        return
    def close_part_of_trade(self, trade_id, trade_date, price, reason, price_used,amount_to_close):

        amount_to_close = round(amount_to_close)
        amount_remaining = self.trade_log[trade_id]['amount'] - amount_to_close
        new_open_value = self.trade_log[trade_id]['open_price'] * amount_to_close
        profit = (amount_to_close * price) - new_open_value



        # update trade log
        self.trade_log[trade_id]['close_date'] = trade_date
        self.trade_log[trade_id]['close_price'] = price
        self.trade_log[trade_id]['open_value'] = new_open_value
        self.trade_log[trade_id]['amount'] = amount_to_close
        self.trade_log[trade_id]['close_value'] = round(price * amount_to_close, 2)
        self.trade_log[trade_id]['close_reason'] = reason
        self.trade_log[trade_id]['exit_timing'] = price_used
        self.trade_log[trade_id]['time_in_trade'] = (trade_date - self.trade_log[trade_id]['open_date']).days
        self.trade_log[trade_id]['profit'] = round(profit, 2)
        self.trade_log[trade_id]['profit%'] = round(profit / new_open_value, 4)

        # update trade value track, put it in trade log, and then remove from live trade holdings
        self.live_trade_val_tracker[trade_id][trade_date] = round(price * amount_to_close, 2)

        self.trade_log[trade_id]['max_profit%'] = round((max(self.live_trade_val_tracker[trade_id].values()) /
                                                         new_open_value) - 1, 4)
        self.trade_log[trade_id]['min_profit%'] = round((min(self.live_trade_val_tracker[trade_id].values()) /
                                                         new_open_value) - 1, 4)
        self.trade_log[trade_id]['value_track'] = self.live_trade_val_tracker[trade_id]

        self.unused_capital += self.trade_log[trade_id]['close_value']  # bank the profits
        self.live_trade_val_tracker.pop(trade_id)  # remove the trade from live tracker


        if '_b_' in trade_id:
            n = int(trade_id.split('_b_')[1])
            trade_id_cp = trade_id.split('_b_')[0] + '_b_' + str(n+1)
        else:
            trade_id_cp = trade_id + '_b_' + str(0)

        self.trade_log[f'{trade_id_cp}'] = {'symbol': trade_id.split('_')[0],
                                                          'direction': 'long',
                                                          'amount': amount_remaining,
                                                          'open_date': self.trade_log[trade_id]['open_date'],
                                                          'open_price': self.trade_log[trade_id]['open_price'],
                                                          'open_value': round(self.trade_log[trade_id]['open_price'] * amount_remaining, 2),
                                                          'open_reason': self.trade_log[trade_id]['open_reason'],
                                                          'close_date': None,
                                                          'close_price': None,
                                                          'close_value': None,
                                                          'close_reason': None,
                                                          'time_in_trade': None,
                                                          'profit': None,
                                                          'profit%': None,
                                                          'max_profit%': None,
                                                          'min_profit%': None,
                                                          'entry_timing': price_used,
                                                          'exit_timing': None,
                                                          'value_track': None,
                                                          'capital': self.trade_log[trade_id]['capital'],
                                                          'pair': 0
                                                          }  # should i add in custom money management

        self.live_trade_val_tracker[f'{trade_id_cp}'] = dict()
        self.trade_daycount[f'{trade_id_cp}'] = self.trade_daycount[trade_id]
        # self.trade_count += 1

        del self.trade_daycount[trade_id]


        return

    def execute_entry_signals(self, buy_signals, trade_date, max_single_stock=1,reason_sig = ''):
        # have some logic to exit this function;
        # 1.no buysignals,
        if buy_signals is None:
            return

        if len(buy_signals) == 0:
            return
        # 2.no trade slots left
        if len(self.live_trade_val_tracker) == self.max_amount_shares:
            return
        # 3.end of back test
        if trade_date == self.price_data.all_dates[-1]:
            return
        # 4. if we are optimising and are trying to trade on out of sample dates
        if self.in_sample_test and trade_date in self.price_data.oos_dates:
            return
        # 5 freeze out check
        if self.freeze_out_days > 0:
            return

        # if self.strat_params['killSwitch'] and self.strat_params['suspension_period'] > 0:
        #     self.strat_params['suspension_period'] = max(self.strat_params['suspension_period'] -1,0)
        #     return
        # print(len(buy_signals))
        # this function is passed a list of buy signals which we can then rank
        # we can apply trade filtering and position sizing
        # once we know our filtered signals we can then execute order
        #  can use super to access the Order class and check orders
        #  it will return trade confirmation with details for us to update portf
        # super().place_market_order('AAPL', '500', 'long')

        # Can decide whata prices you want to execute using
        # price_series = (self.price_data.daily_opens.loc[trade_date] +
        #                 self.price_data.daily_closes.loc[trade_date] +
        #                 self.price_data.daily_highs.loc[trade_date] +
        #                 self.price_data.daily_lows.loc[trade_date]) / 4
        price_series = self.price_data.daily_opens.loc[trade_date]
        price_used = 'Open'
        # # check what signals are in SP500 index and if there is a valid price tomorrow. COMMON FUNC
        buy_signals = self.buy_signal_checks(buy_signals, trade_date)
        # # custom trade ranking or filtering - CUSTOM FUNC
        # ranked_signals = self.spy_breakout_trade_ranking(valid_signals, trade_date)
        # for tick in buy_signals:
        try:

            for tick in buy_signals.index:
                if tick in self.blacklist:
                    continue

                sector = self.sm.convert_symbol_to_sector(tick)
                if sector in self.sector_concentration and len(self.sector_concentration[sector]) >= self.sector_limit:
                    continue

                if len(self.live_trade_val_tracker) < self.max_amount_shares:
                    curr_hold = [x for x in self.live_trade_val_tracker if x.split('_')[0] == tick]
                    if len(curr_hold) == max_single_stock:
                        # print(f'Cant buy anymore {tick} on {trade_date}, hold too much.')
                        pass
                    else:
                        price = price_series[tick]
                        # amount = self.postition_sizing(tick, trade_date)
                        amount = floor((self.starting_capital / self.max_amount_shares) / price)
                        direction = 'long'
                        reason = 'Buy Signal ' + reason_sig

                        self.enter_trade(tick, trade_date, price, direction, amount, reason, price_used)

                        if self.strat_params['enable_sector_filteration']:

                            key = tick
                            sector = self.sm.convert_symbol_to_sector(key)

                            if sector in self.sector_concentration and len(
                                    self.sector_concentration[sector]) < self.sector_limit:
                                # self.sector_concentration[sector] += 1
                                sameElement = [x for x in self.sector_concentration[sector] if x == key]
                                if len(sameElement) == 0:
                                    self.sector_concentration[sector].append(key)
                            elif sector in self.sector_concentration:
                                continue
                            else:
                                # self.sector_concentration[sector] = 1
                                self.sector_concentration.setdefault(sector, []).append(key)


                else:
                    # for l_t_key in self.live_trade_val_tracker.keys():
                    #     key = l_t_key.split('_')[0]
                    #     sector = self.sm.convert_symbol_to_sector(key)
                    #
                    #     if sector in self.sector_concentration and len(
                    #             self.sector_concentration[sector]) < self.sector_limit:
                    #         # self.sector_concentration[sector] += 1
                    #         sameElement = [x for x in self.sector_concentration[sector] if x == key]
                    #         if len(sameElement) == 0:
                    #             self.sector_concentration[sector].append(key)
                    #     elif sector in self.sector_concentration:
                    #         continue
                    #     else:
                    #         # self.sector_concentration[sector] = 1
                    #         self.sector_concentration.setdefault(sector, []).append(key)
                    return
        except Exception as e:
            print(e)
        if self.strat_params['enable_sector_filteration']:

            for l_t_key in self.live_trade_val_tracker.keys():
                key = l_t_key.split('_')[0]
                sector = self.sm.convert_symbol_to_sector(key)

                if sector in self.sector_concentration and len(self.sector_concentration[sector]) < self.sector_limit:
                    # self.sector_concentration[sector] += 1
                    sameElement = [x for x in self.sector_concentration[sector] if x == key]
                    if len(sameElement) == 0:
                        self.sector_concentration[sector].append(key)
                elif sector in self.sector_concentration:
                    continue
                else:
                    # self.sector_concentration[sector] = 1
                    self.sector_concentration.setdefault(sector, []).append(key)

        # We now have to update our portfolio trackers
        return

    def execute_entry_signals_short(self, buy_signals, trade_date, max_single_stock=1,less_than_mv=False,above_200=False):
        # have some logic to exit this function;
        # 1.no buysignals,
        if buy_signals is None:
            return

        if len(buy_signals) == 0:
            return
        # 2.no trade slots left
        if len(self.live_trade_val_tracker) == self.max_amount_shares:
            return
        # 3.end of back test
        if trade_date == self.price_data.all_dates[-1]:
            return
        # 4. if we are optimising and are trying to trade on out of sample dates
        if self.in_sample_test and trade_date in self.price_data.oos_dates:
            return
        # 5 freeze out check
        if self.freeze_out_days > 0:
            return

        # if self.strat_params['killSwitch'] and self.strat_params['suspension_period'] > 0:
        #     self.strat_params['suspension_period'] = max(self.strat_params['suspension_period'] -1,0)
        #     return
        # print(len(buy_signals))
        # this function is passed a list of buy signals which we can then rank
        # we can apply trade filtering and position sizing
        # once we know our filtered signals we can then execute order
        #  can use super to access the Order class and check orders
        #  it will return trade confirmation with details for us to update portf
        # super().place_market_order('AAPL', '500', 'long')

        # Can decide whata prices you want to execute using
        # price_series = (self.price_data.daily_opens.loc[trade_date] +
        #                 self.price_data.daily_closes.loc[trade_date] +
        #                 self.price_data.daily_highs.loc[trade_date] +
        #                 self.price_data.daily_lows.loc[trade_date]) / 4
        price_series = self.price_data.daily_opens.loc[trade_date]
        price_used = 'Open'
        # # check what signals are in SP500 index and if there is a valid price tomorrow. COMMON FUNC
        buy_signals = self.buy_signal_checks(buy_signals, trade_date)
        # # custom trade ranking or filtering - CUSTOM FUNC
        # ranked_signals = self.spy_breakout_trade_ranking(valid_signals, trade_date)
        # for tick in buy_signals:
        try:

            for tick in buy_signals.index:
                if tick in self.blacklist:
                    continue

                sector = self.sm.convert_symbol_to_sector(tick)
                if sector in self.sector_concentration and len(self.sector_concentration[sector]) >= self.sector_limit:
                    continue

                if len(self.live_trade_val_tracker) < self.max_amount_shares:
                    curr_hold = [x for x in self.live_trade_val_tracker if x.split('_')[0] == tick]
                    if len(curr_hold) == max_single_stock:
                        # print(f'Cant buy anymore {tick} on {trade_date}, hold too much.')
                        pass
                    else:
                        price = price_series[tick]
                        # amount = self.postition_sizing(tick, trade_date)

                        amount = -floor((135000 / self.max_amount_shares) / price)
                        if amount >= -10:
                            continue
                        direction = 'long'
                        reason=''
                        if less_than_mv:
                            reason = 'Bear Signal'
                        else:
                            reason = 'Bull Signal'

                        self.enter_trade(tick, trade_date, price, direction, amount, reason, price_used)

                        if self.strat_params['enable_sector_filteration']:

                            key = tick
                            sector = self.sm.convert_symbol_to_sector(key)

                            if sector in self.sector_concentration and len(
                                    self.sector_concentration[sector]) < self.sector_limit:
                                # self.sector_concentration[sector] += 1
                                sameElement = [x for x in self.sector_concentration[sector] if x == key]
                                if len(sameElement) == 0:
                                    self.sector_concentration[sector].append(key)
                            elif sector in self.sector_concentration:
                                continue
                            else:
                                # self.sector_concentration[sector] = 1
                                self.sector_concentration.setdefault(sector, []).append(key)


                else:
                    # for l_t_key in self.live_trade_val_tracker.keys():
                    #     key = l_t_key.split('_')[0]
                    #     sector = self.sm.convert_symbol_to_sector(key)
                    #
                    #     if sector in self.sector_concentration and len(
                    #             self.sector_concentration[sector]) < self.sector_limit:
                    #         # self.sector_concentration[sector] += 1
                    #         sameElement = [x for x in self.sector_concentration[sector] if x == key]
                    #         if len(sameElement) == 0:
                    #             self.sector_concentration[sector].append(key)
                    #     elif sector in self.sector_concentration:
                    #         continue
                    #     else:
                    #         # self.sector_concentration[sector] = 1
                    #         self.sector_concentration.setdefault(sector, []).append(key)
                    return
        except Exception as e:
            print(e)
        if self.strat_params['enable_sector_filteration']:

            for l_t_key in self.live_trade_val_tracker.keys():
                key = l_t_key.split('_')[0]
                sector = self.sm.convert_symbol_to_sector(key)

                if sector in self.sector_concentration and len(self.sector_concentration[sector]) < self.sector_limit:
                    # self.sector_concentration[sector] += 1
                    sameElement = [x for x in self.sector_concentration[sector] if x == key]
                    if len(sameElement) == 0:
                        self.sector_concentration[sector].append(key)
                elif sector in self.sector_concentration:
                    continue
                else:
                    # self.sector_concentration[sector] = 1
                    self.sector_concentration.setdefault(sector, []).append(key)

        # We now have to update our portfolio trackers
        return

    def execute_entry_signals_on_close(self, buy_signals, trade_date, max_single_stock=1):
        # have some logic to exit this function;
        # 1.no buysignals,
        if buy_signals is None:
            return

        if len(buy_signals) == 0:
            return
        # 2.no trade slots left
        if len(self.live_trade_val_tracker) == self.max_amount_shares:
            return
        # 3.end of back test
        if trade_date == self.price_data.all_dates[-1]:
            return
        # 4. if we are optimising and are trying to trade on out of sample dates
        if self.in_sample_test and trade_date in self.price_data.oos_dates:
            return
        # 5 freeze out check
        if self.freeze_out_days > 0:
            return

        price_series = self.price_data.daily_closes.loc[trade_date]
        price_used = 'Open'
        # # check what signals are in SP500 index and if there is a valid price tomorrow. COMMON FUNC
        buy_signals = self.buy_signal_checks(buy_signals, trade_date)
        # # custom trade ranking or filtering - CUSTOM FUNC
        # ranked_signals = self.spy_breakout_trade_ranking(valid_signals, trade_date)
        # for tick in buy_signals:
        try:

            for tick in buy_signals.index:
                if tick in self.blacklist:
                    continue

                sector = self.sm.convert_symbol_to_sector(tick)
                if sector in self.sector_concentration and len(self.sector_concentration[sector]) >= self.sector_limit:
                    continue

                if len(self.live_trade_val_tracker) < self.max_amount_shares:
                    curr_hold = [x for x in self.live_trade_val_tracker if x.split('_')[0] == tick]
                    if len(curr_hold) == max_single_stock:
                        # print(f'Cant buy anymore {tick} on {trade_date}, hold too much.')
                        pass
                    else:
                        price = price_series[tick]
                        # amount = self.postition_sizing(tick, trade_date)
                        amount = -floor((135000 / self.max_amount_shares) / price)
                        if amount == 0:
                            continue
                        direction = 'long'
                        reason = 'Buy Signal'

                        self.enter_trade(tick, trade_date, price, direction, amount, reason, price_used)

                        if self.strat_params['enable_sector_filteration']:

                            key = tick
                            sector = self.sm.convert_symbol_to_sector(key)

                            if sector in self.sector_concentration and len(
                                    self.sector_concentration[sector]) < self.sector_limit:
                                # self.sector_concentration[sector] += 1
                                sameElement = [x for x in self.sector_concentration[sector] if x == key]
                                if len(sameElement) == 0:
                                    self.sector_concentration[sector].append(key)
                            elif sector in self.sector_concentration:
                                continue
                            else:
                                # self.sector_concentration[sector] = 1
                                self.sector_concentration.setdefault(sector, []).append(key)


                else:

                    return
        except Exception as e:
            print(e)
        if self.strat_params['enable_sector_filteration']:

            for l_t_key in self.live_trade_val_tracker.keys():
                key = l_t_key.split('_')[0]
                sector = self.sm.convert_symbol_to_sector(key)

                if sector in self.sector_concentration and len(self.sector_concentration[sector]) < self.sector_limit:
                    # self.sector_concentration[sector] += 1
                    sameElement = [x for x in self.sector_concentration[sector] if x == key]
                    if len(sameElement) == 0:
                        self.sector_concentration[sector].append(key)
                elif sector in self.sector_concentration:
                    continue
                else:
                    # self.sector_concentration[sector] = 1
                    self.sector_concentration.setdefault(sector, []).append(key)

        # We now have to update our portfolio trackers
        return


    def execute_exit_signals(self, exit_signals, trade_date):
        if len(exit_signals) == 0:
            return
        if len(self.live_trade_val_tracker) == 0:
            return

        live_pos = [x.split('_')[0] for x in self.live_trade_val_tracker.keys()]

        if len(set(live_pos).intersection(exit_signals)) == 0:
            return
        else:
            # price_series = (self.price_data.daily_opens.loc[trade_date] +
            #                 self.price_data.daily_closes.loc[trade_date] +
            #                 self.price_data.daily_highs.loc[trade_date] +
            #                 self.price_data.daily_lows.loc[trade_date]) / 4

            price_series = self.price_data.daily_closes.loc[trade_date]
            price_used = 'close price'

            for x in set(live_pos).intersection(exit_signals):
                trade_ids = [i for i in self.live_trade_val_tracker.keys() if i.split('_')[0] == x]
                for j in trade_ids:
                    self.close_trade(j, trade_date, price_series[x], reason='Exit Signal', price_used=price_used)
                '''
                sector_concentrtion => minus 1 if we remove a trade
                '''
                sector = self.sm.convert_symbol_to_sector(x)
                if sector in self.sector_concentration:
                    self.sector_concentration[sector] = [y for y in  self.sector_concentration[sector] if y != x]

            sector_concentration_copy = self.sector_concentration.copy()
            for k in sector_concentration_copy.keys():
                if len(sector_concentration_copy[k]) == 0:
                    del self.sector_concentration[k]



        return

    def check_limit_orders_long_combine(self, limit_order_dict, trade_date,max_single_stock=1,reason=''):
        if len(limit_order_dict) == 0:
            return
        # 2.no trade slots left
        if len(self.live_trade_val_tracker) == self.max_amount_shares:
            return
        # 3.end of back test
        # if trade_date == self.price_data.all_dates[-1]:
        #     return
        # 4. if we are optimising and are trying to trade on out of sample dates
        if self.in_sample_test and trade_date in self.price_data.oos_dates:
            return

        for k, v in limit_order_dict.items():

            v= round(v,2)

            # v = v *(1 + 0.02)
            if len(self.live_trade_val_tracker) == self.max_amount_shares:
                break
            # if self.sector_level == 3 and self.sm.l3_industires[k].iloc[0] in self.current_position_industries\
            #         and len(self.current_position_industries[self.sm.l3_industires[k].iloc[0]]) == self.sector_limit:
            #     break
            index_of_day =  self.price_data.daily_closes.index.get_loc(trade_date)
            if len(self.live_trade_val_tracker) < self.max_amount_shares:
                curr_hold = [x for x in self.live_trade_val_tracker if x.split('_')[0] == k]
                if len(curr_hold) == max_single_stock:
                    # print(f'Cant buy anymore {tick} on {trade_date}, hold too much.')
                    continue
                # where k = ticker and v = limit order price
                yesterday_close_price = self.price_data.daily_closes[k].iloc[index_of_day-1]
                if self.price_data.daily_opens.loc[trade_date][k] < v:
                    amount = floor((self.starting_capital / self.max_amount_shares) /yesterday_close_price)
                    if amount == 0 :
                        continue

                    open_price = self.price_data.daily_opens.loc[trade_date][k]
                    open_price = round(open_price, 2)

                    self.enter_trade(k, trade_date, open_price, 'long', amount, f'Limit order entry using Open price {reason}',
                                     'open',capital=(self.starting_capital / self.max_amount_shares),limit_price=v)
                    # if self.sm.l3_industires[k].iloc[0] not in self.current_position_industries:
                    #     self.current_position_industries[self.sm.l3_industires[k].iloc[0]]={k:1}
                    # else:
                    #     self.current_position_industries[self.sm.l3_industires[k].iloc[0]][k] = 1

                elif self.price_data.daily_lows.loc[trade_date][k] < v:
                    # if limit price has been hit then we enter trade.
                    amount = floor((self.starting_capital / self.max_amount_shares) / yesterday_close_price)
                    if amount == 0 :
                        continue
                    self.enter_trade(k, trade_date, v, 'long', amount, f'Limit order entry using Limit order price  {reason}',
                                     'limit_price',capital=(self.starting_capital / self.max_amount_shares),limit_price=v)

                    # if self.sm.l3_industires[k].iloc[0] not in self.current_position_industries:
                    #     self.current_position_industries[self.sm.l3_industires[k].iloc[0]]={k:1}
                    # else:
                    #     self.current_position_industries[self.sm.l3_industires[k].iloc[0]][k] = 1
        return

    def check_stoploss_intraday_rptt(self,price_series,trade_date,a_t_r,stoploss_pct,main_positions=[]):

        index_day = self.price_data.daily_closes.index.get_loc(trade_date)
        if len(self.live_trade_val_tracker) == 0:
            return

        if stoploss_pct is None or stoploss_pct == 0:
            return

        stopped_trades = []
        live_trades = self.live_trade_val_tracker.copy()

        if len(main_positions) == 0:
            live_positions = [x.split('_')[0] for x in self.live_trade_val_tracker.keys()]
            live_positions = set(live_positions)
        else:
            live_positions = main_positions
            live_positions = set(live_positions)


        for i in live_positions:

            if trade_date == dt.date(2025, 4, 4):
                if i.split('_')[0] == 'KMI' or i.split('_')[0] == 'WMB':
                    continue

            allow_everytrade = True
            ticker = i


            open_trades = [x for x in self.live_trade_val_tracker.keys() if
                           x.split('_')[0] == i]

            todays_low = self.price_data.daily_lows[ticker].loc[trade_date]
            todays_high = self.price_data.daily_highs[ticker].loc[trade_date]
            todays_open = self.price_data.daily_opens[ticker].loc[trade_date]
            todays_close = self.price_data.daily_closes[ticker].loc[trade_date]
            exit_price = 0
            open_ignore = True
            if len(open_trades)>0:
                stoplossPrice = self.trade_log[open_trades[0]]['entry_price'] - \
                                round(((stoploss_pct) * a_t_r[ticker].iloc[index_day-1]),2)
                stoplossPrice = round(stoplossPrice,2)
                if trade_date == self.trade_log[open_trades[0]]['open_date']:
                    # allow_everytrade = (todays_close < stoplossPrice) or (todays_close <= (self.trade_log[open_trades[0]]['limit_price'] * 0.996))
                    if round(self.price_data.daily_opens[ticker].loc[trade_date],2) == round(self.trade_log[open_trades[0]]['open_price'],2):
                        open_ignore= False


                if allow_everytrade:
                    # stopped_trades.append(i)
                    entry_value = 0
                    current_number_of_shares = 0
                    for k in open_trades:
                        entry_value = entry_value + self.trade_log[k]['open_value']
                        current_number_of_shares = current_number_of_shares + self.trade_log[k]['amount']

                    # min_value_today = current_number_of_shares * todays_low
                    # stop_value = (((1 - self.strat_params['stoploss_pct']) * a_t_r[ticker].loc[trade_date])) * entry_value

                    # if min_value_today < stop_value:
                    # stop_price = stop_value / current_number_of_shares
                    hit = False
                    if (todays_open <= stoplossPrice) and open_ignore:
                        exit_price = todays_open
                        price_used = 'open'
                        hit = True

                    elif todays_low <= stoplossPrice <= todays_high:
                        exit_price = stoplossPrice
                        price_used = 'Intraday'
                        hit =True
                    exit_price = round(exit_price,2)
                    if hit:
                        for op in open_trades:
                            self.close_trade(op, trade_date, exit_price,
                                             reason=f'Stopped Out close :{todays_close} ',
                                             price_used=price_used)



                # elif self.price_data.daily_lows[ticker].loc[trade_date] <= (stoplossPrice) <= \
                #         self.price_data.daily_highs[ticker].loc[trade_date]:
                #     stopped_trades.append(i)
                #     for k in open_trades:
                #         self.close_trade(k, trade_date, (stoplossPrice), reason='intraday Stopped Out',
                #                          price_used=price_used)



        return stopped_trades


    def check_stoploss_intraday_rptt_v1(self,price_series,trade_date,a_t_r,stoploss_pct,main_positions=[]):

        index_day = self.price_data.daily_closes.index.get_loc(trade_date)
        if len(self.live_trade_val_tracker) == 0:
            return

        if stoploss_pct is None or stoploss_pct == 0:
            return

        stopped_trades = []
        live_trades = self.live_trade_val_tracker.copy()

        if len(main_positions) == 0:
            live_positions = [x.split('_')[0] for x in self.live_trade_val_tracker.keys()]
            live_positions = set(live_positions)
        else:
            live_positions = main_positions
            live_positions = set(live_positions)


        for i in live_positions:
            allow_everytrade = True
            ticker = i


            open_trades = [x for x in self.live_trade_val_tracker.keys() if
                           x.split('_')[0] == i]

            todays_low = self.price_data.daily_lows[ticker].loc[trade_date]
            todays_high = self.price_data.daily_highs[ticker].loc[trade_date]
            todays_open = self.price_data.daily_opens[ticker].loc[trade_date]
            todays_close = self.price_data.daily_closes[ticker].loc[trade_date]
            exit_price = 0
            open_ignore = True
            if len(open_trades)>0:
                stoplossPrice = self.trade_log[open_trades[0]]['entry_price'] - \
                                round(((stoploss_pct) * a_t_r[ticker].iloc[index_day-1]),2)
                stoplossPrice = round(stoplossPrice,2)


                stoploss_pct_default = ((stoplossPrice - self.trade_log[open_trades[0]]['entry_price'])\
                                       /self.trade_log[open_trades[0]]['entry_price'])*100

                if stoploss_pct_default <= -(self.strat_params['default_stoploss']*100):
                    stoplossPrice = self.trade_log[open_trades[0]]['entry_price'] * (1 - self.strat_params['default_stoploss'])
                    stoplossPrice = round(stoplossPrice, 2)

                if trade_date == self.trade_log[open_trades[0]]['open_date']:
                    # allow_everytrade = (todays_close < stoplossPrice) or (todays_close <= (self.trade_log[open_trades[0]]['limit_price'] * 0.996))
                    if round(self.price_data.daily_opens[ticker].loc[trade_date],2) == round(self.trade_log[open_trades[0]]['open_price'],2):
                        open_ignore= False


                if allow_everytrade:
                    # stopped_trades.append(i)
                    entry_value = 0
                    current_number_of_shares = 0
                    for k in open_trades:
                        entry_value = entry_value + self.trade_log[k]['open_value']
                        current_number_of_shares = current_number_of_shares + self.trade_log[k]['amount']

                    # min_value_today = current_number_of_shares * todays_low
                    # stop_value = (((1 - self.strat_params['stoploss_pct']) * a_t_r[ticker].loc[trade_date])) * entry_value

                    # if min_value_today < stop_value:
                    # stop_price = stop_value / current_number_of_shares
                    hit = False
                    if (todays_open <= stoplossPrice) and open_ignore:
                        exit_price = todays_open
                        price_used = 'open'
                        hit = True

                    elif todays_low <= stoplossPrice <= todays_high:
                        exit_price = stoplossPrice
                        price_used = 'Intraday'
                        hit =True
                    exit_price = round(exit_price,2)
                    if hit:
                        for op in open_trades:
                            self.close_trade(op, trade_date, exit_price,
                                             reason=f'Stopped Out close :{todays_close} ',
                                             price_used=price_used)



                # elif self.price_data.daily_lows[ticker].loc[trade_date] <= (stoplossPrice) <= \
                #         self.price_data.daily_highs[ticker].loc[trade_date]:
                #     stopped_trades.append(i)
                #     for k in open_trades:
                #         self.close_trade(k, trade_date, (stoplossPrice), reason='intraday Stopped Out',
                #                          price_used=price_used)



        return stopped_trades

    def execute_exit_signals_using_open(self, exit_signals, trade_date):
        if len(exit_signals) == 0:
            return
        if len(self.live_trade_val_tracker) == 0:
            return

        live_pos = [x.split('_')[0] for x in self.live_trade_val_tracker.keys()]

        if len(set(live_pos).intersection(exit_signals)) == 0:
            return
        else:
            # price_series = (self.price_data.daily_opens.loc[trade_date] +
            #                 self.price_data.daily_closes.loc[trade_date] +
            #                 self.price_data.daily_highs.loc[trade_date] +
            #                 self.price_data.daily_lows.loc[trade_date]) / 4

            # if self.price_data.daily_closes_spy.loc[trade_date] > self.price_data.moving_avg_spy.loc[trade_date]:
            #     print(True)

            price_series = self.price_data.daily_opens.loc[trade_date]
            price_used = 'open price'

            for x in set(live_pos).intersection(exit_signals):
                trade_ids = [i for i in self.live_trade_val_tracker.keys() if i.split('_')[0] == x]

                for j in trade_ids:
                    if len(self.live_trade_val_tracker[j]) == 0:
                        return
                    self.close_trade(j, trade_date, price_series[x], reason='Exit Signal', price_used=price_used)
                '''
                sector_concentrtion => minus 1 if we remove a trade.
                '''
                sector = self.sm.convert_symbol_to_sector(x)
                if sector in self.sector_concentration:
                    self.sector_concentration[sector] = [y for y in  self.sector_concentration[sector] if y != x]

            sector_concentration_copy = self.sector_concentration.copy()
            for k in sector_concentration_copy.keys():
                if len(sector_concentration_copy[k]) == 0:
                    del self.sector_concentration[k]



        return

    # ------------------ ****** Trade management ****** --------------
    def check_stop_loss(self, price_series, trade_date):
        # This is custom to my SPY breakout strat that requires the Close price to breach our take profit limits and then
        # we exit the trade the following day using the average price
        price_used = 'close'

        if len(self.live_trade_val_tracker) == 0:
            return

        if self.strat_params['stoploss_pct'] is None or self.strat_params['stoploss_pct'] == 0:
            return

        stopped_trades = []
        live_trades = self.live_trade_val_tracker.copy()
        for i in live_trades.keys():
            if len(self.live_trade_val_tracker[i]) == 0:
                return

            stp_loss_price = self.trade_log[i]['entry_price'] * (1 - self.strat_params['stoploss_pct'])

            if self.price_data.daily_closes.loc[trade_date][i.split('_')[0]] < stp_loss_price:

                stopped_trades.append(i)

                self.close_trade(i, trade_date, price_series[i.split('_')[0]], reason='Stopped Out',
                                 price_used=price_used)

                sector = self.sm.convert_symbol_to_sector(i.split('_')[0])
                if sector in self.sector_concentration:
                    self.sector_concentration[sector] = [x for x in  self.sector_concentration[sector] if x != i.split('_')[0]]

                if bool(self.strat_params['enable_stop_loss_enable']):
                    self.blacklist[i.split('_')[0]] = self.strat_params['stp_loss_break_time']

        sector_concentration_copy = self.sector_concentration.copy()
        for k in sector_concentration_copy.keys():
            if len(sector_concentration_copy[k]) == 0:
                del self.sector_concentration[k]

        return stopped_trades

    def check_stop_loss_using_phantom(self, price_series, trade_date,phantom_portf):
        # This is custom to my SPY breakout strat that requires the Close price to breach our take profit limits and then
        # we exit the trade the following day using the average price
        price_used = 'close'

        if len(self.live_trade_val_tracker) == 0:
            return

        if self.strat_params['stoploss_pct'] is None or self.strat_params['stoploss_pct'] == 0:
            return

        stopped_trades = []
        live_trades = self.live_trade_val_tracker.copy()
        trade_df = pd.DataFrame(phantom_portf.trade_log).T
        try:
            for i in live_trades.keys():
                if len(self.live_trade_val_tracker[i]) == 0:
                    return
                ticker = i.split('_')[0]
                # phantom_portf.trade_log[]

                stoploss_df = trade_df[(trade_df['symbol'] == i.split('_')[0]) & (trade_df['close_reason'].isnull())]

                stoplossPrice = stoploss_df['open_value'] * (1 - self.strat_params['stoploss_pct'])

                if self.price_data.daily_closes[ticker].loc[trade_date] <= (stoplossPrice / stoploss_df['amount']).iloc[0]:

                    stopped_trades.append(i)

                    self.close_trade(i, trade_date, price_series[i.split('_')[0]], reason='Stopped Out',
                                     price_used=price_used)

                    sector = self.sm.convert_symbol_to_sector(i.split('_')[0])
                    if sector in self.sector_concentration:
                        self.sector_concentration[sector] = [x for x in self.sector_concentration[sector] if
                                                             x != i.split('_')[0]]

                    if bool(self.strat_params['enable_stop_loss_enable']):
                        self.blacklist[i.split('_')[0]] = self.strat_params['stp_loss_break_time']

            sector_concentration_copy = self.sector_concentration.copy()
            for k in sector_concentration_copy.keys():
                if len(sector_concentration_copy[k]) == 0:
                    del self.sector_concentration[k]
        except Exception as e:
            print


        return stopped_trades

    def check_stoploss_in_close(self,price_series,trade_date):
        price_used = 'close'

        if len(self.live_trade_val_tracker) == 0:
            return

        if self.strat_params['stoploss_pct'] is None or self.strat_params['stoploss_pct'] == 0:
            return

        stopped_trades = []
        live_trades = self.live_trade_val_tracker.copy()
        for i in live_trades.keys():
            ticker = i.split('_')[0]
            stoplossPrice = self.trade_log[i]['open_value'] * (1 - self.strat_params['stoploss_pct'])

            if self.price_data.daily_closes[ticker].loc[trade_date] <= (stoplossPrice / self.trade_log[i]['amount']):
                stopped_trades.append(i)
                self.close_trade(i, trade_date, price_series[i.split('_')[0]], reason='Stopped Out',
                                 price_used=price_used)

                sector = self.sm.convert_symbol_to_sector(i.split('_')[0])
                if sector in self.sector_concentration:
                    self.sector_concentration[sector] = [x for x in  self.sector_concentration[sector] if x != i.split('_')[0]]

                if bool(self.strat_params['enable_stop_loss_enable']):
                    self.blacklist[i.split('_')[0]] = self.strat_params['stp_loss_break_time']

        sector_concentration_copy = self.sector_concentration.copy()
        for k in sector_concentration_copy.keys():
            if len(sector_concentration_copy[k]) == 0:
                del self.sector_concentration[k]

        return stopped_trades

    def check_stoploss_in_hedge_short(self,price_series,trade_date):
        price_used = 'close'

        if len(self.live_trade_val_tracker) == 0:
            return

        if self.strat_params['stoploss_pct'] is None or self.strat_params['stoploss_pct'] == 0:
            return

        stopped_trades = []
        live_trades = self.live_trade_val_tracker.copy()
        for i in live_trades.keys():
            ticker = i.split('_')[0]
            stoplossPrice = self.trade_log[i]['open_value'] * (1 + self.strat_params['stoploss_pct'])

            if self.price_data.daily_closes_spy.loc[trade_date] >= (stoplossPrice / self.trade_log[i]['amount']):
                stopped_trades.append(i)
                self.close_trade(i, trade_date, self.price_data.daily_closes_spy.loc[trade_date], reason='Stopped Out',
                                 price_used=price_used)

                sector = self.sm.convert_symbol_to_sector(i.split('_')[0])
                if sector in self.sector_concentration:
                    self.sector_concentration[sector] = [x for x in  self.sector_concentration[sector] if x != i.split('_')[0]]

                # if bool(self.strat_params['enable_stop_loss_enable']):
                #     self.blacklist[i.split('_')[0]] = self.strat_params['stp_loss_break_time']

        sector_concentration_copy = self.sector_concentration.copy()
        for k in sector_concentration_copy.keys():
            if len(sector_concentration_copy[k]) == 0:
                del self.sector_concentration[k]

        return stopped_trades

    def check_take_profit_in_hedge_short(self,price_series,trade_date):
        price_used = 'close'

        if len(self.live_trade_val_tracker) == 0:
            return

        if self.strat_params['takeprofit_pct'] is None or self.strat_params['takeprofit_pct'] == 0:
            return

        stopped_trades = []
        live_trades = self.live_trade_val_tracker.copy()
        for i in live_trades.keys():
            ticker = i.split('_')[0]
            stoplossPrice = self.trade_log[i]['open_value'] * (1 - self.strat_params['takeprofit_pct'])

            if self.price_data.daily_closes_spy.loc[trade_date] <= (stoplossPrice / self.trade_log[i]['amount']):
                stopped_trades.append(i)
                self.close_trade(i, trade_date, self.price_data.daily_closes_spy.loc[trade_date], reason='take profit Out',
                                 price_used=price_used)

                sector = self.sm.convert_symbol_to_sector(i.split('_')[0])
                if sector in self.sector_concentration:
                    self.sector_concentration[sector] = [x for x in  self.sector_concentration[sector] if x != i.split('_')[0]]

                # if bool(self.strat_params['enable_stop_loss_enable']):
                #     self.blacklist[i.split('_')[0]] = self.strat_params['stp_loss_break_time']

        sector_concentration_copy = self.sector_concentration.copy()
        for k in sector_concentration_copy.keys():
            if len(sector_concentration_copy[k]) == 0:
                del self.sector_concentration[k]

        return stopped_trades

    def check_stoploss_in_close_ind_stock(self,price_series,trade_date,i):
        price_used = 'close'

        if len(self.live_trade_val_tracker) == 0:
            return

        if self.strat_params['stoploss_pct'] is None or self.strat_params['stoploss_pct'] == 0:
            return

        stopped_trades = False

        ticker = i.split('_')[0]
        # stoplossPrice = self.trade_log[i]['open_value'] * (1 - self.strat_params['stoploss_pct'])
        stp_loss_price = self.trade_log[i]['entry_price'] * (1 - self.strat_params['stoploss_pct'])

        if self.price_data.daily_closes[ticker].loc[trade_date] <= (stp_loss_price):
            self.close_trade(i, trade_date, price_series[i.split('_')[0]], reason='Stopped Out',
                             price_used=price_used)
            stopped_trades =True


        return stopped_trades


    # def check_stoploss_intraday_rptt(self,price_series,trade_date,a_t_r):
    #
    #     index_day = self.price_data.daily_closes.index.get_loc(trade_date)
    #     if len(self.live_trade_val_tracker) == 0:
    #         return
    #
    #     if self.strat_params['stoploss_pct'] is None or self.strat_params['stoploss_pct'] == 0:
    #         return
    #
    #     stopped_trades = []
    #     live_trades = self.live_trade_val_tracker.copy()
    #
    #     live_positions = [x.split('_')[0] for x in self.live_trade_val_tracker.keys()]
    #     live_positions = set(live_positions)
    #
    #     for i in live_positions:
    #
    #         ticker = i
    #
    #
    #         open_trades = [x for x in self.live_trade_val_tracker.keys() if
    #                        x.split('_')[0] == i]
    #
    #         todays_low = self.price_data.daily_lows[ticker].loc[trade_date]
    #         todays_high = self.price_data.daily_highs[ticker].loc[trade_date]
    #         todays_open = self.price_data.daily_opens[ticker].loc[trade_date]
    #         exit_price = 0
    #
    #         if len(open_trades)>0:
    #             stoplossPrice = self.trade_log[open_trades[0]]['open_price'] - \
    #                             ((self.strat_params['stoploss_pct']) * a_t_r[ticker].iloc[index_day-1])
    #
    #             if True:
    #                 # stopped_trades.append(i)
    #                 entry_value = 0
    #                 current_number_of_shares = 0
    #                 for k in open_trades:
    #                     entry_value = entry_value + self.trade_log[k]['open_value']
    #                     current_number_of_shares = current_number_of_shares + self.trade_log[k]['amount']
    #
    #                 # min_value_today = current_number_of_shares * todays_low
    #                 # stop_value = (((1 - self.strat_params['stoploss_pct']) * a_t_r[ticker].loc[trade_date])) * entry_value
    #
    #                 # if min_value_today < stop_value:
    #                 # stop_price = stop_value / current_number_of_shares
    #                 hit = False
    #                 if (todays_open <= stoplossPrice):
    #                     exit_price = todays_open
    #                     price_used = 'open'
    #                     hit = True
    #
    #                 elif todays_low <= stoplossPrice <= todays_high:
    #                     exit_price = stoplossPrice
    #                     price_used = 'Intraday'
    #                     hit =True
    #
    #                 if hit:
    #                     for op in open_trades:
    #                         self.close_trade(op, trade_date, exit_price,
    #                                          reason='Stopped Out',
    #                                          price_used=price_used)
    #
    #
    #
    #             # elif self.price_data.daily_lows[ticker].loc[trade_date] <= (stoplossPrice) <= \
    #             #         self.price_data.daily_highs[ticker].loc[trade_date]:
    #             #     stopped_trades.append(i)
    #             #     for k in open_trades:
    #             #         self.close_trade(k, trade_date, (stoplossPrice), reason='intraday Stopped Out',
    #             #                          price_used=price_used)
    #
    #
    #
    #     return stopped_trades


    # def check_profit_intraday_rptt(self,trade_date,a_t_r):
    #     index_day = self.price_data.daily_closes.index.get_loc(trade_date)
    #
    #     if len(self.live_trade_val_tracker) == 0:
    #         return
    #
    #     if self.strat_params['profit_pct'] is None or self.strat_params['profit_pct'] == 0:
    #         return
    #
    #     stopped_trades = []
    #     live_trades = self.live_trade_val_tracker.copy()
    #
    #     live_positions = [x.split('_')[0] for x in self.live_trade_val_tracker.keys()]
    #     live_positions = set(live_positions)
    #
    #     for i in live_positions:
    #
    #         ticker = i
    #
    #
    #         open_trades = [x for x in self.live_trade_val_tracker.keys() if
    #                        x.split('_')[0] == i]
    #
    #         todays_low = self.price_data.daily_lows[ticker].loc[trade_date]
    #         todays_high = self.price_data.daily_highs[ticker].loc[trade_date]
    #         todays_open = self.price_data.daily_opens[ticker].loc[trade_date]
    #         exit_price = 0
    #
    #         if len(open_trades)>0:
    #             stoploss_amount = ((self.strat_params['stoploss_pct']) * a_t_r[ticker].iloc[index_day-1])
    #             profit_amount = ((self.strat_params['profit_pct']) * stoploss_amount )
    #             profit_price = self.trade_log[open_trades[0]]['open_price'] + profit_amount
    #
    #             if True:
    #                 # stopped_trades.append(i)
    #                 entry_value = 0
    #                 current_number_of_shares = 0
    #                 for k in open_trades:
    #                     entry_value = entry_value + self.trade_log[k]['open_value']
    #                     current_number_of_shares = current_number_of_shares + self.trade_log[k]['amount']
    #
    #                 # min_value_today = current_number_of_shares * todays_low
    #                 # stop_value = (((1 - self.strat_params['stoploss_pct']) * a_t_r[ticker].loc[trade_date])) * entry_value
    #
    #                 # if min_value_today < stop_value:
    #                 # stop_price = stop_value / current_number_of_shares
    #                 hit = False
    #                 if (todays_open >= profit_price):
    #                     exit_price = todays_open
    #                     price_used = 'open'
    #                     hit = True
    #
    #                 elif (todays_high >= profit_price>=todays_low):
    #                     exit_price = profit_price
    #                     price_used = 'Intraday'
    #                     hit =True
    #
    #                 if hit:
    #                     for op in open_trades:
    #                         self.close_trade(op, trade_date, exit_price,
    #                                          reason='Profit Taking',
    #                                          price_used=price_used)
    #
    #
    #
    #             # elif self.price_data.daily_lows[ticker].loc[trade_date] <= (stoplossPrice) <= \
    #             #         self.price_data.daily_highs[ticker].loc[trade_date]:
    #             #     stopped_trades.append(i)
    #             #     for k in open_trades:
    #             #         self.close_trade(k, trade_date, (stoplossPrice), reason='intraday Stopped Out',
    #             #                          price_used=price_used)
    #
    #
    #
    #     return stopped_trades
    def check_profit_intraday_rptt(self,trade_date,a_t_r,profit_pct,stoploss_pct,main_positions=[]):
        index_day = self.price_data.daily_closes.index.get_loc(trade_date)

        if len(self.live_trade_val_tracker) == 0:
            return

        if profit_pct is None or profit_pct == 0:
            return

        stopped_trades = []
        live_trades = self.live_trade_val_tracker.copy()

        if len(main_positions) == 0:
            live_positions = [x.split('_')[0] for x in self.live_trade_val_tracker.keys()]
            live_positions = set(live_positions)
        else:
            live_positions = main_positions
            live_positions = set(live_positions)



        for i in live_positions:
            allow_everytrade = True

            ticker = i


            open_trades = [x for x in self.live_trade_val_tracker.keys() if
                           x.split('_')[0] == i]

            todays_low = self.price_data.daily_lows[ticker].loc[trade_date]
            todays_high = self.price_data.daily_highs[ticker].loc[trade_date]
            todays_open = self.price_data.daily_opens[ticker].loc[trade_date]
            todays_close = self.price_data.daily_closes[ticker].loc[trade_date]
            exit_price = 0

            if len(open_trades)>0:

                stoploss_amount = ((stoploss_pct) * a_t_r[ticker].iloc[index_day-1])
                profit_amount = round(((profit_pct) * stoploss_amount ),2)
                profit_price = self.trade_log[open_trades[0]]['entry_price'] + profit_amount
                profit_price = round(profit_price, 2)

                if trade_date == self.trade_log[open_trades[0]]['open_date']:




                    if todays_open == self.trade_log[open_trades[0]]['open_price']:
                        allow_everytrade = True
                    else:
                        allow_everytrade = False

                    # if trade_date == self.price_data.all_dates[-1]:
                    #     if open_trades[0].split('_')[0] in ['TER', 'DDOG']:
                    #         allow_everytrade = True

                    # if open_trades[0].split('_')[0] in ['TER', 'DDOG']:
                    #     allow_everytrade = True



                    if todays_close >= profit_price:
                        for op in open_trades:
                            self.close_trade(op, trade_date, todays_close,
                                             reason=f'Profit Taking: {profit_price} ATR : {a_t_r[ticker].iloc[index_day-1]} Close price : {todays_close}',
                                             price_used='close price')
                        allow_everytrade = False
                    # continue



                if allow_everytrade:
                    # stopped_trades.append(i)
                    entry_value = 0
                    current_number_of_shares = 0
                    for k in open_trades:
                        entry_value = entry_value + self.trade_log[k]['open_value']
                        current_number_of_shares = current_number_of_shares + self.trade_log[k]['amount']

                    # min_value_today = current_number_of_shares * todays_low
                    # stop_value = (((1 - self.strat_params['stoploss_pct']) * a_t_r[ticker].loc[trade_date])) * entry_value

                    # if min_value_today < stop_value:
                    # stop_price = stop_value / current_number_of_shares
                    hit = False
                    if (todays_open >= profit_price):
                        exit_price = todays_open
                        price_used = 'open'
                        hit = True

                    elif (todays_high >= profit_price>=todays_low):
                        exit_price = profit_price
                        price_used = 'Intraday'
                        hit =True

                    if hit:
                        for op in open_trades:
                            self.close_trade(op, trade_date, exit_price,
                                             reason=f'Profit Taking: {profit_price}  ATR : {a_t_r[ticker].iloc[index_day-1]} Close price : {todays_close}',
                                             price_used=price_used)



                # elif self.price_data.daily_lows[ticker].loc[trade_date] <= (stoplossPrice) <= \
                #         self.price_data.daily_highs[ticker].loc[trade_date]:
                #     stopped_trades.append(i)
                #     for k in open_trades:
                #         self.close_trade(k, trade_date, (stoplossPrice), reason='intraday Stopped Out',
                #                          price_used=price_used)



        return stopped_trades


    def check_profit_intraday_rptt_v1(self,trade_date,a_t_r,profit_pct,stoploss_pct,main_positions=[]):
        index_day = self.price_data.daily_closes.index.get_loc(trade_date)

        if len(self.live_trade_val_tracker) == 0:
            return

        if profit_pct is None or profit_pct == 0:
            return

        stopped_trades = []
        live_trades = self.live_trade_val_tracker.copy()

        if len(main_positions) == 0:
            live_positions = [x.split('_')[0] for x in self.live_trade_val_tracker.keys()]
            live_positions = set(live_positions)
        else:
            live_positions = main_positions
            live_positions = set(live_positions)



        for i in live_positions:
            allow_everytrade = True

            ticker = i


            open_trades = [x for x in self.live_trade_val_tracker.keys() if
                           x.split('_')[0] == i]

            todays_low = self.price_data.daily_lows[ticker].loc[trade_date]
            todays_high = self.price_data.daily_highs[ticker].loc[trade_date]
            todays_open = self.price_data.daily_opens[ticker].loc[trade_date]
            todays_close = self.price_data.daily_closes[ticker].loc[trade_date]
            exit_price = 0

            if len(open_trades)>0:

                stoploss_amount = ((stoploss_pct) * a_t_r[ticker].iloc[index_day-1])
                profit_amount = round(((profit_pct) * stoploss_amount ),2)
                profit_price = self.trade_log[open_trades[0]]['entry_price'] + profit_amount

                profit_pct_default = ((profit_price - self.trade_log[open_trades[0]]['entry_price'])
                      / self.trade_log[open_trades[0]]['entry_price'])*100

                if profit_pct_default >= (self.strat_params['default_takeprofit'] * 100):
                    profit_price = self.trade_log[open_trades[0]]['entry_price'] * (1 + self.strat_params['default_takeprofit'])
                    profit_price = round(profit_price, 2)
                    sp = 'special case'
                else:
                    sp =' '
                profit_price = round(profit_price, 2)

                if trade_date == self.trade_log[open_trades[0]]['open_date']:

                    if todays_open == self.trade_log[open_trades[0]]['open_price']:
                        allow_everytrade = True
                    else:
                        allow_everytrade = False



                    if todays_close >= profit_price:
                        for op in open_trades:
                            self.close_trade(op, trade_date, todays_close,
                                             reason=f'Profit Taking: {profit_price} ATR : {a_t_r[ticker].iloc[index_day-1]} Close price : {todays_close}',
                                             price_used='close price')
                    continue



                if allow_everytrade:
                    # stopped_trades.append(i)
                    entry_value = 0
                    current_number_of_shares = 0
                    for k in open_trades:
                        entry_value = entry_value + self.trade_log[k]['open_value']
                        current_number_of_shares = current_number_of_shares + self.trade_log[k]['amount']

                    # min_value_today = current_number_of_shares * todays_low
                    # stop_value = (((1 - self.strat_params['stoploss_pct']) * a_t_r[ticker].loc[trade_date])) * entry_value

                    # if min_value_today < stop_value:
                    # stop_price = stop_value / current_number_of_shares
                    hit = False
                    if (todays_open >= profit_price):
                        exit_price = todays_open
                        price_used = 'open'
                        hit = True

                    elif (todays_high >= profit_price>=todays_low):
                        exit_price = profit_price
                        price_used = 'Intraday'
                        hit =True

                    if hit:
                        for op in open_trades:
                            self.close_trade(op, trade_date, exit_price,
                                             reason=f'Profit Taking: {profit_price}  ATR : {a_t_r[ticker].iloc[index_day-1]} Close price : {todays_close}',
                                             price_used=price_used)



                # elif self.price_data.daily_lows[ticker].loc[trade_date] <= (stoplossPrice) <= \
                #         self.price_data.daily_highs[ticker].loc[trade_date]:
                #     stopped_trades.append(i)
                #     for k in open_trades:
                #         self.close_trade(k, trade_date, (stoplossPrice), reason='intraday Stopped Out',
                #                          price_used=price_used)



        return stopped_trades

    def check_stoploss_in_open(self,price_series,trade_date):
        price_used = 'open'

        if len(self.live_trade_val_tracker) == 0:
            return

        if self.strat_params['stoploss_pct'] is None or self.strat_params['stoploss_pct'] == 0:
            return

        stopped_trades = []
        live_trades = self.live_trade_val_tracker.copy()
        for i in live_trades.keys():
            ticker = i.split('_')[0]
            stoplossPrice = self.trade_log[i]['open_value'] * (1 - self.strat_params['stoploss_pct'])

            if self.price_data.daily_opens[ticker].loc[trade_date] <= (stoplossPrice / self.trade_log[i]['amount']):
                stopped_trades.append(i)
                self.close_trade(i, trade_date, price_series[i.split('_')[0]], reason='Stopped Out',
                                 price_used=price_used)

                sector = self.sm.convert_symbol_to_sector(i.split('_')[0])
                if sector in self.sector_concentration:
                    self.sector_concentration[sector] = [x for x in  self.sector_concentration[sector] if x != i.split('_')[0]]

                if bool(self.strat_params['enable_stop_loss_enable']):
                    self.blacklist[i.split('_')[0]] = self.strat_params['stp_loss_break_time']

        sector_concentration_copy = self.sector_concentration.copy()
        for k in sector_concentration_copy.keys():
            if len(sector_concentration_copy[k]) == 0:
                del self.sector_concentration[k]

        return stopped_trades

    def check_stoploss_intraday(self,price_series,trade_date):
        price_used = 'open'

        if len(self.live_trade_val_tracker) == 0:
            return

        if self.strat_params['stoploss_pct'] is None or self.strat_params['stoploss_pct'] == 0:
            return

        stopped_trades = []
        live_trades = self.live_trade_val_tracker.copy()
        for i in live_trades.keys():
            ticker = i.split('_')[0]
            stoplossPrice = self.trade_log[i]['open_value'] * (1 - self.strat_params['stoploss_pct'])
            if self.price_data.daily_opens[ticker].loc[trade_date] <= (stoplossPrice / self.trade_log[i]['amount']):
                stopped_trades.append(i)
                self.close_trade(i, trade_date, price_series[i.split('_')[0]], reason='open price Stopped Out',
                                 price_used=price_used)

                sector = self.sm.convert_symbol_to_sector(i.split('_')[0])
                if sector in self.sector_concentration:
                    self.sector_concentration[sector] = [x for x in self.sector_concentration[sector] if
                                                         x != i.split('_')[0]]

                if bool(self.strat_params['enable_stop_loss_enable']):
                    self.blacklist[i.split('_')[0]] = self.strat_params['stp_loss_break_time']

            elif self.price_data.daily_opens[ticker].loc[trade_date] >= (stoplossPrice / self.trade_log[i]['amount']) and \
                self.price_data.daily_closes[ticker].loc[trade_date] <= (stoplossPrice / self.trade_log[i]['amount']):
                stopped_trades.append(i)
                self.close_trade(i, trade_date, (stoplossPrice / self.trade_log[i]['amount'])*0.998, reason='intraday Stopped Out',
                                 price_used=price_used)

                sector = self.sm.convert_symbol_to_sector(i.split('_')[0])
                if sector in self.sector_concentration:
                    self.sector_concentration[sector] = [x for x in  self.sector_concentration[sector] if x != i.split('_')[0]]

                if bool(self.strat_params['enable_stop_loss_enable']):
                    self.blacklist[i.split('_')[0]] = self.strat_params['stp_loss_break_time']

        sector_concentration_copy = self.sector_concentration.copy()
        for k in sector_concentration_copy.keys():
            if len(sector_concentration_copy[k]) == 0:
                del self.sector_concentration[k]

        return stopped_trades

    def check_take_profit_intraday(self, price_series, trade_date):
        # This is custom to my SPY breakout strat that requires the Close price to breach our take profit limits and then
        # we exit the trade the following day using the average price
        price_used = 'open'

        if len(self.live_trade_val_tracker) == 0:
            return

        if self.strat_params['takeprof_pct'] is None or self.strat_params['takeprof_pct'] == 0:
            return

        take_prof = self.strat_params['takeprof_pct']
        give_back = self.strat_params['giveback_pct']
        take_profit_trades = []
        live_trades = self.live_trade_val_tracker.copy()

        # Maxtime profit
        for i in live_trades.keys():
            ticker = i.split('_')[0]
            takeProfitPrice = self.trade_log[i]['open_value'] * (1 + self.strat_params['takeprof_pct'])

            if self.price_data.daily_opens[ticker].loc[trade_date] >= (takeProfitPrice / self.trade_log[i]['amount']):

                take_profit_trades.append(i)
                self.close_trade(i, trade_date, price_series[i.split('_')[0]], reason='Stopped Out',
                                 price_used=price_used)

                sector = self.sm.convert_symbol_to_sector(i.split('_')[0])
                if sector in self.sector_concentration:
                    self.sector_concentration[sector] = [x for x in self.sector_concentration[sector] if
                                                         x != i.split('_')[0]]

                if bool(self.strat_params['enable_stop_loss_enable']):
                    self.blacklist[i.split('_')[0]] = self.strat_params['stp_loss_break_time']

        sector_concentration_copy = self.sector_concentration.copy()
        for k in sector_concentration_copy.keys():
            if len(sector_concentration_copy[k]) == 0:
                del self.sector_concentration[k]

        return take_profit_trades

    def check_take_profit_in_open(self, price_series, trade_date):
        # This is custom to my SPY breakout strat that requires the Close price to breach our take profit limits and then
        # we exit the trade the following day using the average price
        price_used = 'open'

        if len(self.live_trade_val_tracker) == 0:
            return

        if self.strat_params['takeprof_pct'] is None or self.strat_params['takeprof_pct'] == 0:
            return

        take_prof = self.strat_params['takeprof_pct']
        give_back = self.strat_params['giveback_pct']
        take_profit_trades = []
        live_trades = self.live_trade_val_tracker.copy()

        # Maxtime profit
        for i in live_trades.keys():
            ticker = i.split('_')[0]
            takeProfitPrice = self.trade_log[i]['open_value'] * (1 + self.strat_params['takeprof_pct'])

            if self.price_data.daily_opens[ticker].loc[trade_date] >= (takeProfitPrice / self.trade_log[i]['amount']):

                take_profit_trades.append(i)
                self.close_trade(i, trade_date, price_series[i.split('_')[0]], reason='Stopped Out',
                                 price_used=price_used)

                sector = self.sm.convert_symbol_to_sector(i.split('_')[0])
                if sector in self.sector_concentration:
                    self.sector_concentration[sector] = [x for x in self.sector_concentration[sector] if
                                                         x != i.split('_')[0]]

                if bool(self.strat_params['enable_stop_loss_enable']):
                    self.blacklist[i.split('_')[0]] = self.strat_params['stp_loss_break_time']

        sector_concentration_copy = self.sector_concentration.copy()
        for k in sector_concentration_copy.keys():
            if len(sector_concentration_copy[k]) == 0:
                del self.sector_concentration[k]

        return take_profit_trades


    def check_take_profit_in_close(self, price_series, trade_date):
        # This is custom to my SPY breakout strat that requires the Close price to breach our take profit limits and then
        # we exit the trade the following day using the average price
        price_used = 'open'

        if len(self.live_trade_val_tracker) == 0:
            return

        if self.strat_params['takeprof_pct'] is None or self.strat_params['takeprof_pct'] == 0:
            return

        take_prof = self.strat_params['takeprof_pct']
        give_back = self.strat_params['giveback_pct']
        take_profit_trades = []
        live_trades = self.live_trade_val_tracker.copy()

        # Maxtime profit
        for i in live_trades.keys():
            ticker = i.split('_')[0]
            takeProfitPrice = self.trade_log[i]['open_value'] * (1 + self.strat_params['takeprof_pct'])

            if self.price_data.daily_closes[ticker].loc[trade_date] >= (takeProfitPrice / self.trade_log[i]['amount']):

                take_profit_trades.append(i)
                self.close_trade(i, trade_date, price_series[i.split('_')[0]], reason='Stopped Out',
                                 price_used=price_used)

                sector = self.sm.convert_symbol_to_sector(i.split('_')[0])
                if sector in self.sector_concentration:
                    self.sector_concentration[sector] = [x for x in self.sector_concentration[sector] if
                                                         x != i.split('_')[0]]

                if bool(self.strat_params['enable_stop_loss_enable']):
                    self.blacklist[i.split('_')[0]] = self.strat_params['stp_loss_break_time']

        sector_concentration_copy = self.sector_concentration.copy()
        for k in sector_concentration_copy.keys():
            if len(sector_concentration_copy[k]) == 0:
                del self.sector_concentration[k]

        return take_profit_trades

    def trailing_stop_loss_long(self, price_series, trade_date):
        # This is custom to my SPY breakout strat that requires the Close price to breach our take profit limits and then
        # we exit the trade the following day using the average price
        price_used = 'Average daily price'

        if len(self.live_trade_val_tracker) == 0:
            return

        if self.strat_params['stoploss_pct'] is None or self.strat_params['stoploss_pct'] == 0:
            return

        stop_loss = self.strat_params['stoploss_pct']
        live_trades = self.live_trade_val_tracker.copy()
        for i in live_trades.keys():
            if len(self.live_trade_val_tracker[i]) > 0:
                trade_equity = list(self.live_trade_val_tracker[i].values())
                if ((trade_equity[-1] / max(trade_equity)) - 1) \
                        <= -self.strat_params['stoploss_pct']:
                    self.close_trade(i, trade_date, price_series[i.split('_')[0]],
                                     reason=f'Trailing Stop {stop_loss * 100}%',
                                     price_used=price_used)
        return

    def trailing_stop_loss_short(self, price_series, trade_date):
        # This is custom to my SPY breakout strat that requires the Close price to breach our take profit limits and then
        # we exit the trade the following day using the average price
        price_used = 'close price'

        if len(self.live_trade_val_tracker) == 0:
            return

        if self.strat_params['acceptable_profit_perc'] is None or self.strat_params['acceptable_profit_perc'] == 0:
            return

        stop_loss = self.strat_params['acceptable_profit_perc']
        live_trades = self.live_trade_val_tracker.copy()
        for i in live_trades.keys():
            if len(self.live_trade_val_tracker[i]) > self.strat_params['allow_days']:
                trade_equity = list(self.live_trade_val_tracker[i].values())

                perc_inc = ((trade_equity[-1] - trade_equity[0]) / trade_equity[-1]) * 100
                if perc_inc <= -self.strat_params['acceptable_profit_perc']:

                    diff = max(trade_equity) - (trade_equity[-1])
                    give_back = diff * self.strat_params['give_back_prec']

                    if (trade_equity[-1] - trade_equity[0]) < give_back :
                        self.close_trade(i, trade_date, price_series[i.split('_')[0]],
                                         reason=f'Trailing Stop {stop_loss * 100}%',
                                         price_used=price_used)
        return

    def break_even_stop_loss(self, price_series, trade_date, prof_threshold, break_even_level=0):
        # This function will bring up the stop loss level to breakeven level when the profit threshold has been hit
        # if the threshold has not been hit then normal hard stop loss applies
        price_used = 'Average daily price'

        if len(self.live_trade_val_tracker) == 0:
            return

        if self.strat_params['stoploss_pct'] is None or self.strat_params['stoploss_pct'] == 0:
            return

        stopped_trades = []
        live_trades = self.live_trade_val_tracker.copy()
        for i in live_trades.keys():
            if len(self.live_trade_val_tracker[i]) > 0:
                trade_equity = list(self.live_trade_val_tracker[i].values())
                if ((trade_equity[-1] / self.trade_log[i]['open_value']) - 1) \
                        <= -self.strat_params['stoploss_pct']:
                    stopped_trades.append(i)
                    self.trade_log[i]['stoploss_%'] = f'{self.strat_params["stoploss_pct"] * 100}%'
                    self.trade_log[i]['stoploss_level'] = self.trade_log[i]['open_value'] * (
                            1 - self.strat_params['stoploss_pct'])
                    self.close_trade(i, trade_date, price_series[i.split('_')[0]], reason='Stopped Out',
                                     price_used=price_used)
                else:
                    if ((max(trade_equity) / self.trade_log[i]['open_value']) - 1) \
                            >= prof_threshold:
                        if ((trade_equity[-1] / self.trade_log[i]['open_value']) - 1) \
                                <= break_even_level:
                            stopped_trades.append(i)
                            self.trade_log[i]['stoploss_%'] = f'{break_even_level * 100}%'
                            self.trade_log[i]['stoploss_level'] = self.trade_log[i]['open_value'] * (
                                    1 - break_even_level)
                            self.close_trade(i, trade_date, price_series[i.split('_')[0]],
                                             reason=f'Stopped Out breakeven at {prof_threshold * 100}%',
                                             price_used=price_used)

        return stopped_trades

    def check_take_profit(self, price_series, trade_date):
        # This is custom to my SPY breakout strat that requires the Close price to breach our take profit limits and then
        # we exit the trade the following day using the average price
        price_used = 'close'

        if len(self.live_trade_val_tracker) == 0:
            return

        if self.strat_params['takeprof_pct'] is None or self.strat_params['takeprof_pct'] == 0:
            return

        take_prof = self.strat_params['takeprof_pct']
        give_back = self.strat_params['giveback_pct']
        take_profit_trades=[]
        live_trades = self.live_trade_val_tracker.copy()

        # Maxtime profit
        for i in live_trades.keys():


            if i in self.live_trade_val_tracker and len(self.live_trade_val_tracker[i]) > 0:
                trade_equity = list(self.live_trade_val_tracker[i].values())
                if give_back:
                    if ((max(trade_equity) / self.trade_log[i]['open_value']) - 1) \
                            >= take_prof:
                        if ((max(trade_equity) / trade_equity[-1]) - 1) \
                                >= give_back:
                            take_profit_trades.append(i)
                            self.close_trade(i, trade_date, price_series[i.split('_')[0]],
                                             reason=f'Take {take_prof * 100}% Profit with {give_back * 100}% giveback',
                                             price_used=price_used)

                            sector = self.sm.convert_symbol_to_sector(i.split('_')[0])
                            if sector in self.sector_concentration:
                                self.sector_concentration[sector] = [x for x in  self.sector_concentration[sector] if x != i.split('_')[0]]
                else:
                    take_profit_price = self.trade_log[i]['entry_price'] * (1 + self.strat_params['takeprof_pct'])

                    if self.price_data.daily_closes.loc[trade_date][i.split('_')[0]] > take_profit_price:
                        take_profit_trades.append(i)
                        self.close_trade(i, trade_date, price_series[i.split('_')[0]],
                                         reason=f'Take {take_prof * 100}% Profit',
                                         price_used=price_used)
                        sector = self.sm.convert_symbol_to_sector(i.split('_')[0])
                        if sector in self.sector_concentration:
                            self.sector_concentration[sector] = [x for x in  self.sector_concentration[sector] if x != i.split('_')[0]]
                        if bool(self.strat_params['enable_stop_loss_enable']):
                            self.blacklist[i.split('_')[0]] = self.strat_params['stp_loss_break_time']

        sector_concentration_copy = self.sector_concentration.copy()
        for k in sector_concentration_copy.keys():
            if len(sector_concentration_copy[k]) == 0:
                del self.sector_concentration[k]
        return take_profit_trades

    def check_take_profit_using_phantom(self, price_series, trade_date,phantom_portfolio):
        # This is custom to my SPY breakout strat that requires the Close price to breach our take profit limits and then
        # we exit the trade the following day using the average price
        price_used = 'open'

        if len(self.live_trade_val_tracker) == 0:
            return

        if self.strat_params['takeprof_pct'] is None or self.strat_params['takeprof_pct'] == 0:
            return

        take_prof = self.strat_params['takeprof_pct']
        give_back = self.strat_params['giveback_pct']
        take_profit_trades=[]
        live_trades = self.live_trade_val_tracker.copy()

        # Maxtime profit
        for i in live_trades.keys():


            if i in self.live_trade_val_tracker and len(self.live_trade_val_tracker[i]) > 0:
                trade_equity = list(self.live_trade_val_tracker[i].values())
                if give_back:
                    if ((max(trade_equity) / self.trade_log[i]['open_value']) - 1) \
                            >= take_prof:
                        if ((max(trade_equity) / trade_equity[-1]) - 1) \
                                >= give_back:
                            take_profit_trades.append(i)
                            self.close_trade(i, trade_date, price_series[i.split('_')[0]],
                                             reason=f'Take {take_prof * 100}% Profit with {give_back * 100}% giveback',
                                             price_used=price_used)

                            sector = self.sm.convert_symbol_to_sector(i.split('_')[0])
                            if sector in self.sector_concentration:
                                self.sector_concentration[sector] = [x for x in  self.sector_concentration[sector] if x != i.split('_')[0]]
                else:
                    if ((trade_equity[-1] / self.trade_log[i]['open_value']) - 1) \
                            >= self.strat_params['takeprof_pct']:
                        take_profit_trades.append(i)
                        self.close_trade(i, trade_date, price_series[i.split('_')[0]],
                                         reason=f'Take {take_prof * 100}% Profit',
                                         price_used=price_used)
                        sector = self.sm.convert_symbol_to_sector(i.split('_')[0])
                        if sector in self.sector_concentration:
                            self.sector_concentration[sector] = [x for x in  self.sector_concentration[sector] if x != i.split('_')[0]]
                        if bool(self.strat_params['enable_stop_loss_enable']):
                            self.blacklist[i.split('_')[0]] = self.strat_params['stp_loss_break_time']

        sector_concentration_copy = self.sector_concentration.copy()
        for k in sector_concentration_copy.keys():
            if len(sector_concentration_copy[k]) == 0:
                del self.sector_concentration[k]
        return take_profit_trades

    def check_maxTime(self,trade_date,price_series):
        live_trades = self.live_trade_val_tracker.copy()
        for i in live_trades.keys():
            if self.trade_daycount[i] >= self.strat_params['exit_maxtime']:
                # reset tradecount update
                self.close_trade(i, trade_date, price_series[i.split('_')[0]], reason='Dropped due to MaxTime',
                                 price_used='Close')

        return
    def buy_signal_checks(self, buy_signals, trade_date):
        valid_entries = []
        # Ensures the ticker is in the SP500 universe and will chekc if there is a valid price tomorrow
        # Entry Signal
        if buy_signals is None or len(buy_signals) == 0:
            return []
        try:
            index_today = self.price_data.daily_universes.index.get_loc(trade_date)
            todays_universe = self.price_data.daily_universes.iloc[index_today].dropna().astype(bool)
            todays_universe = todays_universe[todays_universe]

            index_today_close = self.price_data.daily_opens.index.get_loc(trade_date)
            valid_price_tmmrw = self.price_data.daily_closes.iloc[index_today_close + 1].dropna()

            # valid_entries = buy_signals[buy_signals.index.isin(todays_universe.values)]
            valid_entries = buy_signals[buy_signals.index.isin(todays_universe.index)]
            # valid_entries = valid_entries[valid_entries.index.isin(valid_price_tmmrw.index)]
            valid_entries = valid_entries[valid_entries.index.isin(valid_price_tmmrw.index)]

        except Exception as e:
            print(e)

        return valid_entries

    def lookahead_checks(self, trade_date):
        if len(self.live_trade_val_tracker) == 0:
            return
        if trade_date == self.price_data.all_dates[-1]:
            return

        try:
            index_today = self.price_data.daily_universes.index.get_loc(trade_date)
            tmmrw_universe = self.price_data.daily_universes.iloc[index_today + 1].dropna()

            index_today_close = self.price_data.daily_closes.index.get_loc(trade_date)
            valid_price_tmmrw = self.price_data.daily_closes.iloc[index_today_close + 1].dropna()

            price_series = self.price_data.daily_closes.loc[trade_date]

            live_trades = self.live_trade_val_tracker.copy()
            for k in live_trades.keys():

                # if int(k.split('_')[1]) >= self.strat_params['exit_maxtime']:

                if k.split('_')[0] in valid_price_tmmrw.index:
                    if not k.split('_')[0] in tmmrw_universe.index:
                        self.close_trade(k, trade_date, price_series[k.split('_')[0]], reason='Dropped out of Index',
                                         price_used='Close')
                        sector = self.sm.convert_symbol_to_sector(k.split('_')[0])
                        if sector in self.sector_concentration:
                            self.sector_concentration[sector] = [x for x in  self.sector_concentration[sector] if x != k.split('_')[0]]
                else:
                    if math.isnan(price_series[k.split('_')[0]]):
                        self.close_trade(k, trade_date, self.price_data.daily_closes.iloc[index_today_close-1][k.split('_')[0]], reason='Price feed died',
                                         price_used='Close')
                    else:
                        self.close_trade(k, trade_date, price_series[k.split('_')[0]], reason='Price feed died',
                                     price_used='Close')
                    sector = self.sm.convert_symbol_to_sector(k.split('_')[0])
                    if sector in self.sector_concentration:
                        self.sector_concentration[sector] = [x for x in  self.sector_concentration[sector] if x != k.split('_')[0]]

            sector_concentration_copy = self.sector_concentration.copy()
            for k in sector_concentration_copy.keys():
                if len(sector_concentration_copy[k]) == 0:
                    del self.sector_concentration[k]
        except KeyError:
            pass

        return

    # ------------------ ****** Portfolio Management ****** --------------
    def mark_to_market(self, trade_date, killswitch_bool=False):
        equity = self.unused_capital
        today_holding_value = 0
        live_symbols=[]
        if len(self.live_trade_val_tracker) > 0:
            for k in self.live_trade_val_tracker.keys():



                ticker = self.trade_log[k]['symbol']
                live_symbols.append(ticker)
                amount = self.trade_log[k]['amount']

                if math.isnan(self.price_data.daily_closes.loc[trade_date][ticker]):
                    close_price = 0
                else:
                    close_price = self.price_data.daily_closes.loc[trade_date][ticker]

                self.live_trade_val_tracker[k][trade_date] = round(amount * close_price, 2)
                # self.trade_track[self.trade_keymap[k]].update({trade_date: round(amount * close_price, 2)})
                equity += round(amount * close_price, 2)
                today_holding_value += self.trade_log[k]['open_value']

                self.trade_daycount[k] += 1

        self.wealth_track[trade_date] = equity
        self.utility_track[trade_date] = len(self.live_trade_val_tracker)
        self.utility_value_track[trade_date] = today_holding_value
        if equity > self.max_equity[1]:
            self.max_equity = (trade_date, equity)
        dd = round(self.max_equity[1] - list(self.wealth_track.values())[-1],2)
        self.drawdown[trade_date] = -dd

        if killswitch_bool:
            self.freeze_out_days -= 1

        # stop loss blacklist update
        blacklist_copy = self.blacklist.copy()
        for k in blacklist_copy.keys():
            blacklist_copy[k] -= 1
            if blacklist_copy[k] == 0:
                del self.blacklist[k]
        self.blacklist = {}
        for key in blacklist_copy:
            if blacklist_copy[key] != 0:
                self.blacklist[key] = blacklist_copy[key]

        # sector update
        sector_concentration_copy = self.sector_concentration.copy()
        for k in sector_concentration_copy.keys():
            if len(sector_concentration_copy[k]) == 0:
                del self.sector_concentration[k]


        # if trade_date.year in self.compounding_capital and self.compounding_capital[trade_date.year] ==0:
        #     self.compounding_capital[trade_date.year] = list(self.wealth_track.values())[-1]
        #     self.starting_capital = list(self.wealth_track.values())[-1]


        return


    def sector_filteration(self,entry_tickers):
        # if len(self.live_trade_val_tracker.keys())>0:
            # # print()
            # for l_t_key in self.live_trade_val_tracker.keys():
            #     key = l_t_key.split('_')[0]
            #     sector = self.sm.convert_symbol_to_sector(key)
            #
            #     if sector in self.sector_concentration and len(self.sector_concentration[sector]) < self.sector_limit:
            #         # self.sector_concentration[sector] += 1
            #         sameElement = [x for x in self.sector_concentration[sector] if x == key]
            #         if len(sameElement) == 0:
            #             self.sector_concentration[sector].append(key)
            #     elif sector in self.sector_concentration:
            #         continue
            #     else:
            #         # self.sector_concentration[sector] = 1
            #         self.sector_concentration.setdefault(sector, []).append(key)

            # return entry_tickers

        # else:
        if len(self.live_trade_val_tracker.keys())<=0 and len(entry_tickers)>0:
            first_entry_sector_filter = {}
            for tick in entry_tickers.index:
                sector = self.sm.convert_symbol_to_sector(tick)

                if sector in first_entry_sector_filter :
                    if len(first_entry_sector_filter[sector]) < self.sector_limit:
                        first_entry_sector_filter[sector].append(tick)

                else:
                    first_entry_sector_filter.setdefault(sector,[]).append(tick)

            filtered_entry_tickers = [item for sublist in list(first_entry_sector_filter.values()) for item in sublist]
            return entry_tickers[filtered_entry_tickers]

        return entry_tickers





    def end_of_backtest_hedge(self, trade_date):
        price = self.price_data.daily_spy_closes.loc[trade_date]



        live_trades = self.live_trade_val_tracker.copy()
        for i in live_trades.keys():
            self.close_trade(i, trade_date, price, reason='End of Backtest', price_used='Close')

        # insert the last days equity to tracker
        self.wealth_track[trade_date] = self.unused_capital

        return

    def end_of_backtest_hedge_1(self, trade_date):
        price = self.price_data.daily_closes_spy.loc[trade_date]



        live_trades = self.live_trade_val_tracker.copy()
        for i in live_trades.keys():
            self.close_trade(i, trade_date, price, reason='End of Backtest', price_used='Close')

        # insert the last days equity to tracker
        self.wealth_track[trade_date] = self.unused_capital

        return


    def end_of_backtest(self, trade_date):
        price_series = self.price_data.daily_closes.loc[trade_date]



        live_trades = self.live_trade_val_tracker.copy()
        for i in live_trades.keys():


            self.close_trade(i, trade_date, price_series[i.split('_')[0]], reason='End of Backtest', price_used='Close')

            sector = self.sm.convert_symbol_to_sector(i.split('_')[0])
            if sector in self.sector_concentration:
                self.sector_concentration[sector] = [x for x in  self.sector_concentration[sector] if x != i.split('_')[0]]

        # insert the last days equity to tracker
        self.wealth_track[trade_date] = self.unused_capital

        sector_concentration_copy = self.sector_concentration.copy()
        for k in sector_concentration_copy.keys():
            if len(sector_concentration_copy[k]) == 0:
                del self.sector_concentration[k]
        return

    def end_of_backtest_lra_trix(self, trade_date):


        price_series = self.price_data.daily_closes_spy.loc[trade_date]

        live_trades = self.live_trade_val_tracker.copy()
        for i in live_trades.keys():
            self.close_trade(i, trade_date, price_series, reason='End of Backtest', price_used='Close')

            sector = self.sm.convert_symbol_to_sector(i.split('_')[0])
            if sector in self.sector_concentration:
                self.sector_concentration[sector] = [x for x in  self.sector_concentration[sector] if x != i.split('_')[0]]

        # insert the last days equity to tracker
        self.wealth_track[trade_date] = self.unused_capital

        sector_concentration_copy = self.sector_concentration.copy()
        for k in sector_concentration_copy.keys():
            if len(sector_concentration_copy[k]) == 0:
                del self.sector_concentration[k]
        return

    def risk_management(self, trade_date):
        killswitch = False
        if len(self.live_trade_val_tracker) == 0:
            return killswitch
        # CHARLES FREEZE OUT - v1 based on open equity dropping a certain amount in a give time fframe
        # CHARLES FREEZE OUT
        # prices_series = self.price_data.daily_closes.loc[trade_date]
        # lookback = self.strat_params['freeze_lookback']
        # limit = self.strat_params['freeze_limit']

        if len(self.wealth_track) > self.strat_params['freeze_lookback']:
            if list(self.wealth_track.values())[-1] - list(self.wealth_track.values()
                                                           )[-1 - self.strat_params['freeze_lookback']] < - \
            self.strat_params['freeze_limit']:
                killswitch = True
                # live_trades = self.live_trade_val_tracker.copy()
                # for i in live_trades.keys():
                #     self.close_trade(i, trade_date, self.price_data.daily_closes.loc[trade_date][
                #         i.split('_')[0]], reason='Freeze out closes', price_used='Close')
                #
                # self.freeze_out_days += self.strat_params['freeze_out_days']

        # Freeze out v2 - based on CI index, if the SPY Congestion index drops more than our limit over a predefined period, close out.
        # if (self.price_data.lt_ci_roc.loc[trade_date]['SPY'] < - self.strat_params['ci_roc_limit']) and (
        #         self.price_data.lt_ci.loc[trade_date]['SPY'] > 20):
        #     live_trades = self.live_trade_val_tracker.copy()
        #     for i in live_trades.keys():
        #         self.close_trade(i, trade_date, self.price_data.daily_closes.loc[trade_date][
        #             i.split('_')[0]], reason='Freeze out closes', price_used='Close')
        #
        #     self.freeze_out_days += self.strat_params['freeze_out_days']

        return killswitch

    def equity_based_killswitch(self, lookback=5, limit=7000, days_out=5):
        if len(self.live_trade_val_tracker) == 0:
            return False
        # This is to catch us from continually add
        if self.freeze_out_days > 0:
            return False

        # v1 based on open equity dropping a certain amount in a give time frame
        if len(self.wealth_track) > lookback:
            if list(self.wealth_track.values())[-1] - list(self.wealth_track.values())[-lookback] < - limit:
                # self.freeze_out_days += days_out
                return True

        return False


    def equity_based_killswitch_hh(self, index_today=5,rhh_lookback=0, lookback=5,sample_lookback=0,RHH_drop =1000):
        if len(self.live_trade_val_tracker) == 0:
            return False
        # This is to catch us from continually add
        if self.freeze_out_days > 0:
            return False

        # v1 based on open equity dropping a certain amount in a give time frame
        if len(self.wealth_track) > rhh_lookback:
            hh_set = pd.Series(self.wealth_track)[index_today-rhh_lookback:index_today]
            subset = pd.Series(self.wealth_track)[index_today - sample_lookback: index_today]

            if len(hh_set) == 0:
                return False
            hh = hh_set.max()
            hh_index = hh_set[hh_set == hh].index[-1]
            _dict = {}
            ref_date = pd.to_datetime(hh_index)

            for d, v in subset.iteritems():
                dd = pd.to_datetime(d)
                is_after_ref = dd > ref_date
                if is_after_ref:
                    _dict[d] = v

            filtered = pd.Series(_dict)

            diff = hh - filtered
            max_diff = diff.max()

            return max_diff > RHH_drop

        return False

    def equity_based_killswitch_hh_pct_changes(self, lookback=5, limit_pct=0.5,limit=5000):
        pct =0
        if len(self.live_trade_val_tracker) == 0:
            return False
        # This is to catch us from continually add
        if self.freeze_out_days > 0:
            return False
        if len(self.wealth_track) > lookback:
            # diff_allow = list(self.wealth_track.values())[-1] - list(self.wealth_track.values())[-1 - lookback] < -limit
            # if diff_allow:
            pct = (limit / (len(self.live_trade_val_tracker) * 10000)) * 100

            return pct > limit_pct


        return False

    def freeze_Equity_curve(self, killSwitchBool,days_out=5):

        if self.freeze_out_days > 0:
            return False

        if killSwitchBool:
            self.freeze_out_days += days_out if killSwitchBool else 0
            return True

        return False


    # ---------------- ****** CUSTOM FUNCTIONS TO REUSE (MUST HAVE STANDARD INPUTS) ****** --------------
    def rsi_filter(self, trade_date, ticker_series, period=14, level=70, above_below='above'):
        # I pass it ticker_series as i take straight from bool df but can easily pass list and ammend
        # exec(f'rsi_df = self.price_data.RSI_{period}.loc[trade_date][ticker_series.index]')
        ticker_rsi = eval(f'self.price_data.RSI_{period}.loc[trade_date][ticker_series.index]')
        if above_below == 'above':
            filtered_rsi = ticker_rsi[ticker_rsi > level]
        else:
            filtered_rsi = ticker_rsi[ticker_rsi < level]

        return filtered_rsi

    def rsi_filter_2(self, trade_date, ticker_series, period=14, min=None, max=None):
        # I pass it ticker_series as i take straight from bool df but can easily pass list and ammend
        # exec(f'rsi_df = self.price_data.RSI_{period}.loc[trade_date][ticker_series.index]')
        ticker_rsi = eval(f'self.price_data.RSI_{period}.loc[trade_date][ticker_series.index]')
        if min is None:
            pass
        else:
            ticker_rsi = ticker_rsi[ticker_rsi > min]

        if max is None:
            pass
        else:
            ticker_rsi = ticker_rsi[ticker_rsi < max]

        return ticker_rsi

    def ma_filter(self, trade_date, ticker_series, ma_type='SMA', period=50, min=None, max=None):
        # I pass it ticker_series as i take straight from bool df but can easily pass list and ammend

        ticker_ma = eval(f'(self.price_data.daily_closes.loc[trade_date, ticker_series.index] / '
                         f'self.price_data.{ma_type.upper()}_{period}.loc[trade_date, ticker_series.index]) - 1')

        if min is None:
            pass
        else:
            ticker_ma = ticker_ma[ticker_ma > min]

        if max is None:
            pass
        else:
            ticker_ma = ticker_ma[ticker_ma < max]

        return ticker_ma

    def ma_cross_filter(self, trade_date, ticker_series, ma_type='SMA', period_1=50, period_2=200, min=None, max=None):
        # I pass it ticker_series as i take straight from bool df but can easily pass list and ammend

        ticker_ma = eval(f'(self.price_data.{ma_type.upper()}_{period_1}.loc[trade_date, ticker_series.index] / '
                         f'self.price_data.{ma_type.upper()}_{period_2}.loc[trade_date, ticker_series.index]) - 1')

        if min is None:
            pass
        else:
            ticker_ma = ticker_ma[ticker_ma > min]

        if max is None:
            pass
        else:
            ticker_ma = ticker_ma[ticker_ma < max]

        return ticker_ma

    def spy_breakout_trade_ranking(self, buy_signals, trade_date):
        ranked_signals = self.price_data.hist_vol.loc[trade_date][buy_signals.index].sort_values(ascending=True)

        # rsi_filtered = self.rsi_filter(trade_date, ranked_signals, period=7, level=70, above_below='above')
        #
        # signals_out = rsi_filtered
        return ranked_signals

    def trade_filtering_MA_db_v01(self, buy_signals, date):
        filtered_signals = []
        return filtered_signals

    def postition_sizing(self, ticker, date):
        num_of_shares = 0
        return num_of_shares

    def post_update(self,trade_date, data,strategy):
        # Equity Loss trigger for continuous loss on equity_loss_trigger parameter in the before Everything Starts
            if len(self.wealth_track) > self.strat_params['sample_days']:

                # Calculating the previous sample days profit with currrent profit to check the difference
                diff = list(self.wealth_track.values())[-self.strat_params['sample_days']-1] - list(self.wealth_track.values())[-1]
                # if the difference is greater the equity loss amount then turning on the killSwitch and assign the suspension period.
                # once the suspension period is assigned then it will be decremented in the trade_open()
                if diff >= strategy.strategy_params['equity_loss_trigger']:
                    self.strat_params['killSwitch'] = True
                    self.strat_params['suspension_period'] = self.strat_params['constant_suspension_period']
                else:
                    # if the difference is lesser the equity loss amount then turning off the killSwitch
                    self.strat_params['killSwitch'] = False

            if self.strat_params['killSwitch']:
                live_trades = self.live_trade_val_tracker.copy()
                price_series = self.price_data.daily_closes.loc[trade_date]
                for i in live_trades.keys():
                    if len(self.live_trade_val_tracker[i]) == 0:
                        return
                    self.close_trade(i, trade_date, price_series[i.split('_')[0]], reason='Kill switch',price_used='Close')
                    sector = self.sm.convert_symbol_to_sector(i.split('_')[0])
                    if sector in self.sector_concentration:
                        self.sector_concentration[sector] = [x for x in  self.sector_concentration[sector] if x != i.split('_')[0]]

                sector_concentration_copy = self.sector_concentration.copy()
                for k in sector_concentration_copy.keys():
                    if len(sector_concentration_copy[k]) == 0:
                        del self.sector_concentration[k]

            return

    def execute_limit_orders(self, buy_signals,limit_order_prices, trade_date, max_single_stock=1):
        # have some logic to exit this function;
        # 1.no buysignals,
        if buy_signals is None:
            return

        if len(buy_signals) == 0:
            return
        # 2.no trade slots left
        if len(self.live_trade_val_tracker) == self.max_amount_shares:
            return
        # 3.end of back test
        if trade_date == self.price_data.all_dates[-1]:
            return
        # 4. if we are optimising and are trying to trade on out of sample dates
        if self.in_sample_test and trade_date in self.price_data.oos_dates:
            return
        # 5 freeze out check
        if self.freeze_out_days > 0:
            return

        # print(len(buy_signals))
        # this function is passed a list of buy signals which we can then rank
        # we can apply trade filtering and position sizing
        # once we know our filtered signals we can then execute order
        #  can use super to access the Order class and check orders
        #  it will return trade confirmation with details for us to update portf
        # super().place_market_order('AAPL', '500', 'long')


        price_series = self.price_data.daily_highs.loc[trade_date]
        price_used = 'daily high'

        # # check what signals are in SP500 index and if there is a valid price tomorrow. COMMON FUNC
        buy_signals = self.buy_signal_checks(buy_signals, trade_date)
        # # custom trade ranking or filtering - CUSTOM FUNC
        # ranked_signals = self.spy_breakout_trade_ranking(valid_signals, trade_date)
        for tick in buy_signals.index:
            if tick in self.blacklist.keys():
                pass
            ''' Added another condition to check the limit order price crossed the daily high of the ticker '''
            if len(self.live_trade_val_tracker) < self.max_amount_shares and limit_order_prices[tick] >= price_series[tick]:
                curr_hold = [x for x in self.live_trade_val_tracker if x.split('_')[0] == tick]
                if len(curr_hold) == max_single_stock:
                    # print(f'Cant buy anymore {tick} on {trade_date}, hold too much.')
                    pass
                else:
                    price = limit_order_prices[tick]
                    # amount = self.postition_sizing(tick, trade_date)
                    amount = floor((self.starting_capital / self.max_amount_shares) / price)
                    direction = 'short'
                    reason = 'Buy Signal'

                    self.enter_trade(tick, trade_date, price, direction, -amount, reason, price_used)
            else:
                return
        # We now have to update our portfolio trackers
        return

    def check_limit_orders(self, limit_order_dict, trade_date,max_single_stock=1,reason=''):
        index_today = self.price_data.daily_closes.index.get_loc(trade_date)
        missed_trades = {}

        if len(limit_order_dict) == 0:
            return missed_trades
        # 2.no trade slots left
        if len(self.live_trade_val_tracker) == self.max_amount_shares:
            return missed_trades
        # 3.end of back test
        # if trade_date == self.price_data.all_dates[-1]:
        #     return missed_trades
        # 4. if we are optimising and are trying to trade on out of sample dates
        if self.in_sample_test and trade_date in self.price_data.oos_dates:
            return missed_trades

        for k, v in limit_order_dict.items():



            # v = v *(1 + 0.02)
            if len(self.live_trade_val_tracker) == self.max_amount_shares:
                break
            # if self.sector_level == 3 and self.sm.l3_industires[k].iloc[0] in self.current_position_industries\
            #         and len(self.current_position_industries[self.sm.l3_industires[k].iloc[0]]) == self.sector_limit:
            #     break

            if len(self.live_trade_val_tracker) < self.max_amount_shares:
                curr_hold = [x for x in self.live_trade_val_tracker if x.split('_')[0] == k]
                if len(curr_hold) == max_single_stock:
                    # print(f'Cant buy anymore {tick} on {trade_date}, hold too much.')
                    continue
                # where k = ticker and v = limit order price

                if self.price_data.daily_opens.loc[trade_date][k] > v:
                    amount = -floor((self.starting_capital / self.max_amount_shares) /self.price_data.daily_closes.iloc[index_today-1][k])
                    if amount >= -self.min_to_enter :
                        continue
                    self.enter_trade(k, trade_date, self.price_data.daily_opens.loc[trade_date][k], 'short', amount, f'Limit order entry using Open price'+reason,
                                     'open',limit_price=v)
                    # if self.sm.l3_industires[k].iloc[0] not in self.current_position_industries:
                    #     self.current_position_industries[self.sm.l3_industires[k].iloc[0]]={k:1}
                    # else:
                    #     self.current_position_industries[self.sm.l3_industires[k].iloc[0]][k] = 1

                elif self.price_data.daily_highs.loc[trade_date][k] > v:
                    # if limit price has been hit then we enter trade.
                    amount = -floor((self.starting_capital / self.max_amount_shares) / self.price_data.daily_closes.iloc[index_today-1][k])
                    if amount >= -self.min_to_enter :
                        continue
                    self.enter_trade(k, trade_date, v, 'short', amount, f'Limit order entry using Limit order price'+reason, 'Highs',limit_price=v)

                    # if self.sm.l3_industires[k].iloc[0] not in self.current_position_industries:
                    #     self.current_position_industries[self.sm.l3_industires[k].iloc[0]]={k:1}
                    # else:
                    #     self.current_position_industries[self.sm.l3_industires[k].iloc[0]][k] = 1
                else:
                    missed_trades[k] = v

        return missed_trades


    def check_limit_orders_combined_crdt(self, limit_order_dict, trade_date,max_single_stock=1,reason='',priority='',slot_cash=12500):

        index_today = self.price_data.daily_closes.index.get_loc(trade_date)
        missed_trades = {}

        if len(limit_order_dict) == 0:
            return
        # 2.no trade slots left
        if len(self.live_trade_val_tracker) == self.max_amount_shares:
            return
        # 3.end of back test
        # if trade_date == self.price_data.all_dates[-1]:
        #     return
        # 4. if we are optimising and are trying to trade on out of sample dates
        if self.in_sample_test and trade_date in self.price_data.oos_dates:
            return

        for k, v in limit_order_dict.items():

            trade_slot_size = (slot_cash / 3) * 2.5

            # v = v *(1 + 0.02)
            if len(self.live_trade_val_tracker) == self.max_amount_shares:
                break
            # if self.sector_level == 3 and self.sm.l3_industires[k].iloc[0] in self.current_position_industries\
            #         and len(self.current_position_industries[self.sm.l3_industires[k].iloc[0]]) == self.sector_limit:
            #     break

            if len(self.live_trade_val_tracker) < self.max_amount_shares:
                curr_hold = [x for x in self.live_trade_val_tracker if x.split('_')[0] == k]
                if len(curr_hold) == max_single_stock:
                    # print(f'Cant buy anymore {tick} on {trade_date}, hold too much.')
                    continue
                # where k = ticker and v = limit order price
                if len(curr_hold) > 0 :
                    trade_slot_size = (slot_cash / 3) * 1.25

                if priority == 'five':
                    trade_slot_size = (slot_cash / 3)

                if self.price_data.daily_opens.loc[trade_date][k] > v:
                    amount = -floor(trade_slot_size /self.price_data.daily_closes.iloc[index_today-1][k])
                    if amount >= -self.min_to_enter :
                        continue
                    self.enter_trade(k, trade_date, self.price_data.daily_opens.loc[trade_date][k], 'short', amount, f'Limit order entry using Open price'+reason,
                                     'open',limit_price=v,capital=trade_slot_size)
                    # if self.sm.l3_industires[k].iloc[0] not in self.current_position_industries:
                    #     self.current_position_industries[self.sm.l3_industires[k].iloc[0]]={k:1}
                    # else:
                    #     self.current_position_industries[self.sm.l3_industires[k].iloc[0]][k] = 1

                elif self.price_data.daily_highs.loc[trade_date][k] > v:
                    # if limit price has been hit then we enter trade.
                    amount = -floor(trade_slot_size / self.price_data.daily_closes.iloc[index_today-1][k])
                    if amount >= -self.min_to_enter :
                        continue
                    self.enter_trade(k, trade_date, v, 'short', amount, f'Limit order entry using Limit order price'+reason, 'Highs',limit_price=v,capital=trade_slot_size)

                    # if self.sm.l3_industires[k].iloc[0] not in self.current_position_industries:
                    #     self.current_position_industries[self.sm.l3_industires[k].iloc[0]]={k:1}
                    # else:
                    #     self.current_position_industries[self.sm.l3_industires[k].iloc[0]][k] = 1
                else:
                    missed_trades[k] = v

        return missed_trades


    def check_limit_orders_combined_long_crdt(self, limit_order_dict, trade_date,max_single_stock=1,reason='',priority='',slot_cash=12500,below=False,ticker_count={}):

        index_today = self.price_data.daily_closes.index.get_loc(trade_date)
        missed_trades = {}

        if len(limit_order_dict) == 0:
            return
        # 2.no trade slots left
        if len(self.live_trade_val_tracker) == self.max_amount_shares:
            return
        # 3.end of back test
        # if trade_date == self.price_data.all_dates[-1]:
        #     return
        # 4. if we are optimising and are trying to trade on out of sample dates
        if self.in_sample_test and trade_date in self.price_data.oos_dates:
            return

        for k, v in limit_order_dict.items():

            trade_slot_size = (slot_cash / 3) * 2

            # v = v *(1 + 0.02)
            if len(self.live_trade_val_tracker) == self.max_amount_shares:
                break
            # if self.sector_level == 3 and self.sm.l3_industires[k].iloc[0] in self.current_position_industries\
            #         and len(self.current_position_industries[self.sm.l3_industires[k].iloc[0]]) == self.sector_limit:
            #     break

            if len(self.live_trade_val_tracker) < self.max_amount_shares:
                curr_hold = [x for x in self.live_trade_val_tracker if x.split('_')[0] == k]
                if len(curr_hold) == max_single_stock:
                    # print(f'Cant buy anymore {tick} on {trade_date}, hold too much.')
                    continue
                # where k = ticker and v = limit order price
                if not below :
                    if len(curr_hold) == 1 :
                        trade_slot_size = (slot_cash / 3) * 1.5

                    if len(curr_hold) == 2:
                        trade_slot_size = (slot_cash / 3) * 0.5

                    if priority == 'six':
                        if len(curr_hold) == 1:
                            trade_slot_size = (slot_cash / 3) * 1.5


                    if len(ticker_count) > 0 and k in ticker_count :
                        if ticker_count[k] == 1:
                            trade_slot_size = (slot_cash / 3) * 1.5

                        if ticker_count[k] == 2:
                            trade_slot_size = (slot_cash / 3) * 0.5

                        if priority == 'six':
                            if ticker_count[k] == 1:
                                trade_slot_size = (slot_cash / 3) * 1.5
                            else:
                                trade_slot_size = (slot_cash / 3) * 0.5

                else:
                    if len(curr_hold) == 1:
                        trade_slot_size = (slot_cash / 3) * 0.5

                    if len(curr_hold) == 2:
                        trade_slot_size = (slot_cash / 3) * 0.5

                    if priority == 'six':
                        if len(curr_hold) == 1:
                            trade_slot_size = (slot_cash / 3) * 1.5

                    if len(ticker_count) > 0 and k in ticker_count:
                        if ticker_count[k] == 1:
                            trade_slot_size = (slot_cash / 3) * 1.5

                        if ticker_count[k] == 2:
                            trade_slot_size = (slot_cash / 3) * 0.5

                        if priority == 'six':
                            if ticker_count[k] == 1:
                                trade_slot_size = (slot_cash / 3) * 1.5
                            else:
                                trade_slot_size = (slot_cash / 3) * 0.5

                if trade_slot_size > 9000:
                    print(True)

                if  self.price_data.daily_opens.loc[trade_date][k] < v:
                    amount = floor(trade_slot_size /self.price_data.daily_closes.iloc[index_today-1][k])
                    if amount <= self.min_to_enter :
                        continue
                    self.enter_trade(k, trade_date, self.price_data.daily_opens.loc[trade_date][k], 'long', amount, f'Limit order entry using Open price'+reason,
                                     'open',limit_price=v)

                elif self.price_data.daily_lows.loc[trade_date][k] <= v <= self.price_data.daily_highs[k].loc[trade_date]:
                    # if limit price has been hit then we enter trade.
                    amount = floor(trade_slot_size / self.price_data.daily_closes.iloc[index_today-1][k])
                    if amount <= self.min_to_enter :
                        continue
                    self.enter_trade(k, trade_date, v, 'long', amount, f'Limit order entry using Limit order price'+reason, 'Highs',limit_price=v)

                else:
                    missed_trades[k] = v

        return missed_trades


    def check_limit_orders_long(self, limit_order_dict, trade_date,max_single_stock=1,reason='',quantity_contraints = False):
        missed_trades = {}
        index_Day = self.price_data.daily_closes.index.get_loc(trade_date)
        if len(limit_order_dict) == 0:
            return
        # 2.no trade slots left
        if len(self.live_trade_val_tracker) == self.max_amount_shares:
            return
        # 3.end of back test
        # if trade_date == self.price_data.all_dates[-1]:
        #     return
        # 4. if we are optimising and are trying to trade on out of sample dates
        if self.in_sample_test and trade_date in self.price_data.oos_dates:
            return

        for k, v in limit_order_dict.items():

            v = round(v,2)

            # v = v *(1 + 0.02)
            if len(self.live_trade_val_tracker) == self.max_amount_shares:
                break
            # if self.sector_level == 3 and self.sm.l3_industires[k].iloc[0] in self.current_position_industries\
            #         and len(self.current_position_industries[self.sm.l3_industires[k].iloc[0]]) == self.sector_limit:
            #     break

            if len(self.live_trade_val_tracker) < self.max_amount_shares:
                curr_hold = [x for x in self.live_trade_val_tracker if x.split('_')[0] == k]
                if len(curr_hold) == max_single_stock:
                    # print(f'Cant buy anymore {tick} on {trade_date}, hold too much.')
                    continue
                # where k = ticker and v = limit order price

                if self.price_data.daily_opens.loc[trade_date][k] < v:
                    amount = floor((self.starting_capital / self.max_amount_shares) /self.price_data.daily_closes.iloc[index_Day-1][k])
                    if quantity_contraints:
                        if amount < self.min_to_enter:
                            continue

                    if amount == 0 :
                        continue
                    self.enter_trade(k, trade_date, self.price_data.daily_opens.loc[trade_date][k], 'long', amount, f'Limit order entry using Open price '+reason,
                                     'open',capital=(self.starting_capital / self.max_amount_shares),limit_price=v)
                    # if self.sm.l3_industires[k].iloc[0] not in self.current_position_industries:
                    #     self.current_position_industries[self.sm.l3_industires[k].iloc[0]]={k:1}
                    # else:
                    #     self.current_position_industries[self.sm.l3_industires[k].iloc[0]][k] = 1

                elif self.price_data.daily_lows.loc[trade_date][k] <= v <= self.price_data.daily_highs[k].loc[trade_date]:
                    # if limit price has been hit then we enter trade.
                    amount = floor((self.starting_capital / self.max_amount_shares) / self.price_data.daily_closes.iloc[index_Day-1][k])
                    if quantity_contraints:
                        if amount < self.min_to_enter:
                            continue
                    if amount == 0 :
                        continue
                    self.enter_trade(k, trade_date, v, 'long', amount, f'Limit order entry using Limit order price '+reason, 'limit_price',capital=(self.starting_capital / self.max_amount_shares),limit_price=v)
                else:
                    missed_trades[k] = v
                    # if self.sm.l3_industires[k].iloc[0] not in self.current_position_industries:
                    #     self.current_position_industries[self.sm.l3_industires[k].iloc[0]]={k:1}
                    # else:
                    #     self.current_position_industries[self.sm.l3_industires[k].iloc[0]][k] = 1
        return missed_trades


    def check_limit_orders_for_Add(self, limit_order_dict, trade_date,max_single_stock=1,reason='',enable_10k_13k=False,above_200=False):
        if len(limit_order_dict) == 0:
            return
        # 2.no trade slots left
        if len(self.live_trade_val_tracker) == self.max_amount_shares:
            return
        # 3.end of back test
        if trade_date == self.price_data.all_dates[-1]:
            return
        # 4. if we are optimising and are trying to trade on out of sample dates
        if self.in_sample_test and trade_date in self.price_data.oos_dates:
            return

        for k, v in limit_order_dict.items():



            # v = v *(1 + 0.02)
            if len(self.live_trade_val_tracker) == self.max_amount_shares:
                break
            # if self.sector_level == 3 and self.sm.l3_industires[k].iloc[0] in self.current_position_industries\
            #         and len(self.current_position_industries[self.sm.l3_industires[k].iloc[0]]) == self.sector_limit:
            #     break

            if len(self.live_trade_val_tracker) < self.max_amount_shares:
                curr_hold = [x for x in self.live_trade_val_tracker if x.split('_')[0] == k]
                if len(curr_hold) == max_single_stock:
                    # print(f'Cant buy anymore {tick} on {trade_date}, hold too much.')
                    continue
                # where k = ticker and v = limit order price
                capital=0
                if self.price_data.daily_opens.loc[trade_date][k] > v:
                    if not above_200:
                        if enable_10k_13k:
                            capital = 135000 / self.max_amount_shares
                            amount = -floor((135000 / self.max_amount_shares) /
                                            self.price_data.daily_opens.loc[trade_date][k])
                        else:
                            capital = (self.starting_capital / self.max_amount_shares)
                            amount = -floor((self.starting_capital / self.max_amount_shares) /
                                            self.price_data.daily_opens.loc[trade_date][k])
                    else:
                        capital = 50000 / self.max_amount_shares
                        amount = -floor((50000 / self.max_amount_shares) /
                                        self.price_data.daily_opens.loc[trade_date][k])



                    if amount >= -10 :
                        continue
                    self.enter_trade(k, trade_date, self.price_data.daily_opens.loc[trade_date][k], 'short', amount, f'Limit order entry using Open price'+reason,
                                     'open',capital=capital)
                    # if self.sm.l3_industires[k].iloc[0] not in self.current_position_industries:
                    #     self.current_position_industries[self.sm.l3_industires[k].iloc[0]]={k:1}
                    # else:
                    #     self.current_position_industries[self.sm.l3_industires[k].iloc[0]][k] = 1

                elif self.price_data.daily_highs.loc[trade_date][k] > v:
                    # if limit price has been hit then we enter trade.
                    if enable_10k_13k:
                        capital = 135000 / self.max_amount_shares
                        amount = -floor((135000 / self.max_amount_shares) / v)

                    else:
                        capital = (self.starting_capital / self.max_amount_shares)
                        amount = -floor((self.starting_capital / self.max_amount_shares) / v)
                    if amount >= -10 :
                        continue
                    self.enter_trade(k, trade_date, v, 'short', amount, f'Limit order entry using Limit order price'+reason, 'Highs',capital=capital)

                    # if self.sm.l3_industires[k].iloc[0] not in self.current_position_industries:
                    #     self.current_position_industries[self.sm.l3_industires[k].iloc[0]]={k:1}
                    # else:
                    #     self.current_position_industries[self.sm.l3_industires[k].iloc[0]][k] = 1
        return

    def check_limit_orders_open(self, limit_order_dict, trade_date,max_single_stock=1):
        if len(limit_order_dict) == 0:
            return
        # 2.no trade slots left
        if len(self.live_trade_val_tracker) == self.max_amount_shares:
            return
        # 3.end of back test
        if trade_date == self.price_data.all_dates[-1]:
            return
        # 4. if we are optimising and are trying to trade on out of sample dates
        if self.in_sample_test and trade_date in self.price_data.oos_dates:
            return

        price_series = self.price_data.daily_opens.loc[trade_date]
        price_used = 'Opens'

        entered_Trades=[]
        for k, v in limit_order_dict.items():
            curr_hold = [x for x in self.live_trade_val_tracker if x.split('_')[0] == k]
            if len(curr_hold) == max_single_stock:
                # print(f'Cant buy anymore {tick} on {trade_date}, hold too much.')
                pass
            else:
                # v = v *(1 + 0.02)
                if len(self.live_trade_val_tracker) < self.max_amount_shares:
                    # where k = ticker and v = limit order price
                    if price_series[k] > v:
                        # if limit price has been hit then we enter trade.
                        amount = -floor((self.starting_capital / self.max_amount_shares) / price_series[k])
                        self.enter_trade(k, trade_date, v, 'short', amount, f'Limit order entry using Daily Open', price_used)
                        entered_Trades.append(k)

            [limit_order_dict.pop(key) for key in entered_Trades]
        return


    def check_limit_orders_close(self, limit_order_dict, trade_date,max_single_stock=1,reason='',enable_10k_13k=False):
        if len(limit_order_dict) == 0:
            return
        # 2.no trade slots left
        if len(self.live_trade_val_tracker) == self.max_amount_shares:
            return
        # 3.end of back test
        if trade_date == self.price_data.all_dates[-1]:
            return
        # 4. if we are optimising and are trying to trade on out of sample dates
        if self.in_sample_test and trade_date in self.price_data.oos_dates:
            return

        for k, v in limit_order_dict.items():



            # v = v *(1 + 0.02)
            if len(self.live_trade_val_tracker) == self.max_amount_shares:
                break

            if len(self.live_trade_val_tracker) < self.max_amount_shares:
                curr_hold = [x for x in self.live_trade_val_tracker if x.split('_')[0] == k]
                if len(curr_hold) == max_single_stock:
                    # print(f'Cant buy anymore {tick} on {trade_date}, hold too much.')
                    continue
                # where k = ticker and v = limit order price

                if self.price_data.daily_closes.loc[trade_date][k] > v:
                    if enable_10k_13k:
                        amount = -floor((135000 / self.max_amount_shares) /
                                        self.price_data.daily_closes.loc[trade_date][k])
                    else:
                        amount = -floor((self.starting_capital / self.max_amount_shares) /
                                        self.price_data.daily_closes.loc[trade_date][k])



                    if amount == 0 :
                        continue
                    self.enter_trade(k, trade_date, self.price_data.daily_closes.loc[trade_date][k], 'short', amount, f'Limit order entry using Open price'+reason,
                                     'open')
                    # if self.sm.l3_industires[k].iloc[0] not in self.current_position_industries:
                    #     self.current_position_industries[self.sm.l3_industires[k].iloc[0]]={k:1}
                    # else:
                    #     self.current_position_industries[self.sm.l3_industires[k].iloc[0]][k] = 1


        return
    def check_stop_loss_for_sell(self,price_series,trade_date):
        price_used = 'close'

        if len(self.live_trade_val_tracker) == 0:
            return

        if self.strat_params['stoploss_pct'] is None or self.strat_params['stoploss_pct'] == 0:
            return

        stopped_trades = []
        live_trades = self.live_trade_val_tracker.copy()
        for i in live_trades.keys():
            ticker = i.split('_')[0]
            stoplossPrice = self.trade_log[i]['open_value'] * (1 + self.strat_params['stoploss_pct'])

            if self.price_data.daily_opens[ticker].loc[trade_date] > (stoplossPrice / self.trade_log[i]['amount']):
                stopped_trades.append(i)
                self.close_trade(i, trade_date, self.price_data.daily_opens[ticker].loc[trade_date], reason='Stopped Out',
                                 price_used=price_used)

                sector = self.sm.convert_symbol_to_sector(i.split('_')[0])
                if sector in self.sector_concentration:
                    self.sector_concentration[sector] = [x for x in  self.sector_concentration[sector] if x != i.split('_')[0]]

                if bool(self.strat_params['enable_stop_loss_enable']):
                    self.blacklist[i.split('_')[0]] = self.strat_params['stp_loss_break_time']

        sector_concentration_copy = self.sector_concentration.copy()
        for k in sector_concentration_copy.keys():
            if len(sector_concentration_copy[k]) == 0:
                del self.sector_concentration[k]

        return stopped_trades

    def check_stop_loss_for_short(self,trade_date):
        price_used = 'close'

        if len(self.live_trade_val_tracker) == 0:
            return

        if self.strat_params['stoploss_pct'] is None or self.strat_params['stoploss_pct'] == 0:
            return

        stopped_trades = []
        live_trades = self.live_trade_val_tracker.copy()
        for i in live_trades.keys():
            ticker = i.split('_')[0]
            stoplossPrice = self.trade_log[i]['open_price'] * (1 + self.strat_params['stoploss_pct'])

            if self.price_data.daily_closes[ticker].loc[trade_date] > (stoplossPrice):
                stopped_trades.append(i)
                self.close_trade(i, trade_date, self.price_data.daily_closes[ticker].loc[trade_date], reason='Stopped Out',
                                 price_used=price_used)

        #         sector = self.sm.convert_symbol_to_sector(i.split('_')[0])
        #         if sector in self.sector_concentration:
        #             self.sector_concentration[sector] = [x for x in  self.sector_concentration[sector] if x != i.split('_')[0]]
        #
        #         if bool(self.strat_params['enable_stop_loss_enable']):
        #             self.blacklist[i.split('_')[0]] = self.strat_params['stp_loss_break_time']
        #
        # sector_concentration_copy = self.sector_concentration.copy()
        # for k in sector_concentration_copy.keys():
        #     if len(sector_concentration_copy[k]) == 0:
        #         del self.sector_concentration[k]

        return stopped_trades


    def check_stop_loss_for_short_intraday(self,trade_date):
        price_used = 'close'

        if len(self.live_trade_val_tracker) == 0:
            return

        if self.strat_params['stoploss_pct'] is None or self.strat_params['stoploss_pct'] == 0:
            return

        stopped_trades = []
        live_trades = self.live_trade_val_tracker.copy()
        for i in live_trades.keys():
            ticker = i.split('_')[0]
            todays_high = self.price_data.daily_highs[ticker].loc[trade_date]
            todays_open = self.price_data.daily_opens[ticker].loc[trade_date]
            todays_low = self.price_data.daily_lows[ticker].loc[trade_date]
            stoplossPrice = self.trade_log[i]['open_price'] * (1 + self.strat_params['stoploss_pct'])

            min_value_today = self.trade_log[i]['amount'] * todays_high
            stop_value = (1 + self.strat_params['stoploss_pct']) * self.trade_log[i]['open_value']

            if min_value_today < stop_value:
                stop_price = stop_value / self.trade_log[i]['amount']
                if (todays_open >= stop_price):
                    self.close_trade(i, trade_date, todays_open,reason='Intraday Stoploss',price_used=price_used)
                elif todays_low <= stop_price <= todays_high:
                    self.close_trade(i, trade_date, stop_price, reason='Intraday Stoploss', price_used=price_used)

        return stopped_trades


    def check_stop_loss_for_long_intraday(self,trade_date):
        price_used = 'close'

        if len(self.live_trade_val_tracker) == 0:
            return

        if self.strat_params['stoploss_pct'] is None or self.strat_params['stoploss_pct'] == 0:
            return

        stopped_trades = []
        live_trades = self.live_trade_val_tracker.copy()
        for i in live_trades.keys():
            ticker = i.split('_')[0]
            todays_high = self.price_data.daily_highs[ticker].loc[trade_date]
            todays_open = self.price_data.daily_opens[ticker].loc[trade_date]
            todays_low = self.price_data.daily_lows[ticker].loc[trade_date]
            stoplossPrice = self.trade_log[i]['open_price'] * (1 - self.strat_params['stoploss_pct'])

            # min_value_today = self.trade_log[i]['amount'] * todays_low
            stop_value = (1 - self.strat_params['stoploss_pct']) * self.trade_log[i]['open_value']

            # if min_value_today < stop_value:
            stop_price = stop_value / self.trade_log[i]['amount']
            if (todays_open <= stop_price):
                self.close_trade(i, trade_date, todays_open,reason='Intraday Stoploss',price_used=price_used)
            elif todays_low <= stop_price <= todays_high:
                self.close_trade(i, trade_date, stop_price, reason='Intraday Stoploss', price_used=price_used)

        return stopped_trades

    def check_stop_loss_for_long(self,trade_date,stoploss_pct):
        price_used = 'close'

        if len(self.live_trade_val_tracker) == 0:
            return

        if stoploss_pct is None or stoploss_pct == 0:
            return

        stopped_trades = []
        live_trades = self.live_trade_val_tracker.copy()
        for i in live_trades.keys():
            ticker = i.split('_')[0]
            stoplossPrice = self.trade_log[i]['entry_price'] * (1 - stoploss_pct)

            if self.price_data.daily_closes[ticker].loc[trade_date] <= (stoplossPrice):
                stopped_trades.append(i)
                self.close_trade(i, trade_date, self.price_data.daily_closes[ticker].loc[trade_date], reason='Stopped Out',
                                 price_used=price_used)

        #         sector = self.sm.convert_symbol_to_sector(i.split('_')[0])
        #         if sector in self.sector_concentration:
        #             self.sector_concentration[sector] = [x for x in  self.sector_concentration[sector] if x != i.split('_')[0]]
        #
        #         if bool(self.strat_params['enable_stop_loss_enable']):
        #             self.blacklist[i.split('_')[0]] = self.strat_params['stp_loss_break_time']
        #
        # sector_concentration_copy = self.sector_concentration.copy()
        # for k in sector_concentration_copy.keys():
        #     if len(sector_concentration_copy[k]) == 0:
        #         del self.sector_concentration[k]

        return stopped_trades

    def check_portfolio_stoploss(self,lookback,limit,trade_date):
        price_used = 'close'

        if len(self.live_trade_val_tracker) == 0:
            return

        # if self.strat_params['portfolio_stoploss'] is None or self.strat_params['portfolio_stoploss'] == 0:
        #     return

        stopped_trades = []
        live_trades = self.live_trade_val_tracker.copy()

        if len(self.wealth_track) > lookback:
            if list(self.wealth_track.values())[-1] - list(self.wealth_track.values())[-lookback] < - limit:
                # self.freeze_out_days += days_out
                for i in live_trades.keys():
                    ticker = i.split('_')[0]
                    stopped_trades.append(i)
                    self.close_trade(i, trade_date, self.price_data.daily_closes[ticker].loc[trade_date],
                                     reason='Stopped Out',
                                     price_used=price_used)





        return

    # # ----------------- ****** POST RUN ANALYSIS ****** --------------
    #
    # def get_rolling_strategy_beta(self, data_obj, period=220, benchmark='SPY'):
    #     beta_df = pd.DataFrame(self.equity_curve.pct_change())
    #     beta_df.columns = ['equity']
    #     data_obj.get_benchmark_prices(benchmark)
    #     beta_df = pd.concat([beta_df, data_obj.benchmark.pct_change()], axis=1)
    #     cov_df = beta_df.rolling(period).cov().unstack()['equity']['close']
    #     var_market_df = beta_df['close'].to_frame().rolling(period).var()
    #
    #     beta_df['rolling_beta'] = (cov_df / (var_market_df.T)).T
    #
    #     return beta_df['rolling_beta']
    #
    # def get_rolling_strat_correlation(self, data_obj, period=220, benchmark='SPY'):
    #     corr_df = pd.DataFrame(self.equity_curve.pct_change())
    #     corr_df.columns = ['equity']
    #     data_obj.get_benchmark_prices(benchmark)
    #     corr_df = pd.concat([corr_df, data_obj.benchmark.pct_change()], axis=1)
    #     corr_df['rolling_correlation'] = corr_df.rolling(period).corr().unstack()['equity']['close']
    #
    #     return corr_df['rolling_correlation']
    #
    # def get_drawdown_stats(self):
    #     drawdown = (self.equity_curve - self.equity_curve.cummax())['equity']
    #     max_dd = drawdown.min()
    #     max_dd_percent = 100 * max_dd / self.starting_capital
    #     max_dd_date = drawdown.idxmin()
    #     max_dd_start = drawdown.where(drawdown == 0, np.nan).loc[:max_dd_date].last_valid_index()
    #     max_dd_end = drawdown.where(drawdown == 0, np.nan).loc[max_dd_date:].first_valid_index()
    #     max_dd_length = len(drawdown[max_dd_start:max_dd_end])
    #     # avg_dd =
    #     stats = {'Max Drawdown': max_dd,
    #              'Max Drawdown %': max_dd_percent,
    #              'Length of Max Drawdown': max_dd_length,
    #              'Average Drawdown': drawdown.mean()}  # I didn't do drawdown[drawdown<0].mean() because I wanted to include the 0's
    #     drawdown.name = 'drawdown'
    #     setattr(self, 'dd_series', drawdown)
    #     setattr(self, 'dd_stats', stats)
    #
    #     # DB added for drawdown analysis
    #     d = drawdown.copy()
    #     d.replace(0, np.nan, inplace=True)
    #     sparse_ts = d.astype(pd.SparseDtype('float'))
    #     block_locs = zip(sparse_ts.values.sp_index.to_block_index().blocs,
    #                      sparse_ts.values.sp_index.to_block_index().blengths)
    #     drawdown_dict = {'start_date': [],
    #                      'end_date': [],
    #                      'length': [],
    #                      'max_dd': [],
    #                      'avg_dd': []
    #                      }
    #     for start, length in block_locs:
    #         if length > 1:
    #             temp_series = d.iloc[start:(start + length)]
    #             drawdown_dict['start_date'].append(temp_series.index[0])
    #             drawdown_dict['end_date'].append(temp_series.index[-1])
    #             drawdown_dict['length'].append(length)
    #             drawdown_dict['max_dd'].append(temp_series.min().round(2))
    #             drawdown_dict['avg_dd'].append(temp_series.mean().round(2))
    #
    #     dd_df = pd.DataFrame(drawdown_dict)
    #     setattr(self, 'dd_df', dd_df)
    #
    #     return drawdown
    #
    # def get_summary(self, strat_params=None, export=False):
    #     trade_list = self.trade_list
    #     equity = self.equity_curve.copy()
    #     equity.replace(0, np.nan, inplace=True)
    #     winner_mask = trade_list['profit'] > 0
    #     loser_mask = trade_list['profit'] < 0
    #     profit_factor = (trade_list[winner_mask]['profit'].sum() / abs(trade_list[loser_mask]['profit'].sum())).round(2)
    #     exp_val = (1 / len(trade_list)) * ((len(trade_list[winner_mask]) * trade_list[winner_mask]['profit'].mean()) -
    #                                        (len(trade_list[loser_mask]) * abs(trade_list[loser_mask]['profit'].mean())))
    #     expectation = exp_val / abs(trade_list[loser_mask]['profit'].mean())
    #     cum_ret = self.equity_curve.iloc[-1]['equity'] / self.starting_capital
    #     ann_ret = (cum_ret ** (365 / (equity.index[-1] - equity.index[0]).days)) - 1
    #     ann_vol = equity['equity'].pct_change().std()
    #
    #     max_dd_index = self.dd_df['max_dd'].idxmin()
    #
    #     strat_str = ''
    #     if strat_params is not None:
    #         strat_str = "\n".join(f"{k: <20} {v}" for k, v in strat_params.items())
    #     summary_str = (
    #         f'------------------------------------------------------------|'
    #         f'\n              {self.name.upper()} Summary Performance          '
    #         f'\n------------------------------------------------------------|'
    #         f'\n{strat_str}'
    #         f'\n{"starting capital": <20} {self.starting_capital}\n'
    #         f''
    #         f'\n{"Total Profit ($):":<20}  {round(self.equity_curve.iloc[-1]["equity"] / 1000, 2):>10}k ({round(cum_ret * 100, 2)}%)'
    #         f'\n{"CAGR:":<20}  {round(ann_ret * 100, 2):>10}%'
    #         f'\n{"Volatility:":<20}  {round(ann_vol * 100, 2):>10}%\n'
    #         f'\n{"Total Trades:":<20}  {len(trade_list):>10}'
    #         f'\n{"Win Rate:":<20}  {round((len(trade_list[winner_mask]) / len(trade_list)) * 100, 2):>10}% ({len(trade_list[winner_mask])})\n'
    #         f'\n{"Avg Trade Profit ($):":<20}  {round(trade_list["profit"].mean(), 2):>10} ({round(trade_list["profit%"].mean() * 100, 2)}%)'
    #         f'\n{"Profit Factor:":<20}  {profit_factor:>10}'
    #         f'\n{"Expected Value ($):":<20}  {round(exp_val, 2):>10} \n'
    #         f'\n{"Max Drawdown ($):":<20}  {round(self.dd_df["max_dd"].min() / 1000, 2):>10}k ({round(self.dd_df["max_dd"].min() * 100 / self.starting_capital, 2)}%)'
    #         f'\n{"Len of Max Drawdown:":<20}  {self.dd_df.iloc[max_dd_index]["length"]:>10} days'
    #         f'\n{"Avg Drawdown ($):":<20}  {round(self.dd_df["max_dd"].mean() / 1000, 2):>10}k'
    #         f'\n------------------------------------------------------------|'
    #         f'\n                    Trade Analysis                          |'
    #         f'\n------------------------------------------------------------|'
    #         f'\n{"Total Trades:": <15} {len(trade_list): >6}           '
    #         f'\n{"Total Winners:": <15} {len(trade_list[winner_mask]): >6} '
    #         f' ({round((len(trade_list[winner_mask]) / len(trade_list)) * 100, 2)}%) '
    #         f'\n{"Total Losers:": <15} {len(trade_list[loser_mask]): >6} '
    #         f' ({round((len(trade_list[loser_mask]) / len(trade_list)) * 100, 2)}%) '
    #         f'\n------------------------------------------------------------|'
    #         f'\n                  Profit % Distrubtion \n'
    #         f'\n  All Trades    |      Winners      |       Losers      |'
    #         f'\nMin  =  {round(np.quantile(trade_list["profit%"], 0) * 100, 2):>5}%  |   Min  =  {round(np.quantile(trade_list[winner_mask]["profit%"] * 100, 0), 2):>5}%  |   Min  =  {round(np.quantile(trade_list[loser_mask]["profit%"], 0) * 100, 2):>5}%  |'
    #         f'\n1Q   =  {round(np.quantile(trade_list["profit%"], 0.25) * 100, 2):>5}%  |   1Q   =  {round(np.quantile(trade_list[winner_mask]["profit%"] * 100, 0.25), 2):>5}%  |   1Q   =  {round(np.quantile(trade_list[loser_mask]["profit%"], 0.25) * 100, 2):>5}%  |'
    #         f'\nMed  =  {round(np.quantile(trade_list["profit%"], 0.5) * 100, 2):>5}%  |   Med  =  {round(np.quantile(trade_list[winner_mask]["profit%"] * 100, 0.5), 2):>5}%  |   Med  =  {round(np.quantile(trade_list[loser_mask]["profit%"], 0.5), 2) * 100:>5}%  |'
    #         f'\n3Q   =  {round(np.quantile(trade_list["profit%"], 0.75) * 100, 2):>5}%  |   3Q   =  {round(np.quantile(trade_list[winner_mask]["profit%"] * 100, 0.75), 2):>5}%  |   3Q   =  {round(np.quantile(trade_list[loser_mask]["profit%"], 0.75) * 100, 2):>5}%  |'
    #         f'\nMax  =  {round(np.quantile(trade_list["profit%"], 1), 2) * 100:>5}%  |   Max  =  {round(np.quantile(trade_list[winner_mask]["profit%"] * 100, 1), 2):>5}%  |   Max  =  {round(np.quantile(trade_list[loser_mask]["profit%"], 1) * 100, 2):>5}%  |\n'
    #         f'\nAvg  =  {round(np.mean(trade_list["profit%"]) * 100, 2):>5}%  |   Avg  =  {round(np.mean(trade_list[winner_mask]["profit%"]) * 100, 2):>5}%  |   Avg  =  {round(np.mean(trade_list[loser_mask]["profit%"]) * 100, 2):>5}%  |'
    #         f'\n-------------------------------------------------------------|'
    #         f'\n                BizDays in Trade Distribution \n'
    #         f'\n  All Trades   |      Winners       |       Losers      |'
    #         f'\nMin  =  {round(np.quantile(trade_list["time_in_trade"], 0)):>5}  |   Min  =  {round(np.quantile(trade_list[winner_mask]["time_in_trade"], 0)):>5}    |   Min  =  {round(np.quantile(trade_list[loser_mask]["time_in_trade"], 0)):>5}   |'
    #         f'\n1Q   =  {round(np.quantile(trade_list["time_in_trade"], 0.25)):>5}  |   1Q   =  {round(np.quantile(trade_list[winner_mask]["time_in_trade"], 0.25)):>5}    |   1Q   =  {round(np.quantile(trade_list[loser_mask]["time_in_trade"], 0.25)):>5}   |'
    #         f'\nMed  =  {round(np.quantile(trade_list["time_in_trade"], 0.5)):>5}  |   Med  =  {round(np.quantile(trade_list[winner_mask]["time_in_trade"], 0.5)):>5}    |   Med  =  {round(np.quantile(trade_list[loser_mask]["time_in_trade"], 0.5)):>5}   |'
    #         f'\n3Q   =  {round(np.quantile(trade_list["time_in_trade"], 0.75)):>5}  |   3Q   =  {round(np.quantile(trade_list[winner_mask]["time_in_trade"], 0.75)):>5}    |   3Q   =  {round(np.quantile(trade_list[loser_mask]["time_in_trade"], 0.75)):>5}   |'
    #         f'\nMax  =  {round(np.quantile(trade_list["time_in_trade"], 1)):>5}  |   Max  =  {round(np.quantile(trade_list[winner_mask]["time_in_trade"], 1)):>5}    |   Max  =  {round(np.quantile(trade_list[loser_mask]["time_in_trade"], 1)):>5}   |\n'
    #         f'\nAvg  =  {round(np.mean(trade_list["time_in_trade"])):>5}  |   Avg  =  {round(np.mean(trade_list[winner_mask]["time_in_trade"])):>5}    |   Avg  =  {round(np.mean(trade_list[loser_mask]["time_in_trade"])):>5}   |'
    #         f'\n------------------------------------------------------------|'
    #         f'\n                    Drawdown Analysis                       |'
    #         f'\n------------------------------------------------------------|'
    #         f'\n{"stat1:":<20}  {round(self.equity_curve.iloc[-1]["equity"] / 1000, 2):>10}k ({round(cum_ret * 100, 2)}%)'
    #         f'\n{"stat2":<20}  {round(ann_ret * 100, 2):>10}%'
    #         f'\n{"stat3":<20}  {round(ann_vol * 100, 2):>10}%'
    #         f'\n{"stat4:":<20}  {len(trade_list):>10}'
    #         f'\n{"stat5":<20}  {round((len(trade_list[winner_mask]) / len(trade_list)) * 100, 2):>10}% ({len(trade_list[winner_mask])})\n'
    #         f'\n-------------------------------------------------------------|'
    #         f'\n                   Top 10 Worst Drawdowns \n'
    #         f'{self.dd_df.sort_values(["max_dd"]).iloc[:10].reset_index(drop=True)}'
    #     )
    #
    #     return summary_str
    #
    # def plot_equity_curve(self, data_obj, with_benchmark=False, benchmark='SPY'):
    #     # Create SubPlot
    #     fig = make_subplots(rows=3, cols=1,
    #                         specs=[[{'secondary_y': True}],
    #                                [{'secondary_y': False}],
    #                                [{'secondary_y': True}]],
    #                         row_width=[0.15, 0.15, 0.7],
    #                         shared_xaxes=True,
    #                         subplot_titles=['Equity', 'Drawdown', 'Utility'],
    #                         vertical_spacing=0.05)
    #
    #     # EQUITY CURVE
    #     fig.add_trace(go.Scatter(x=self.equity_curve.index, y=self.equity_curve['equity'].values,
    #                              name='Equity Curve', line=dict(color='fuchsia')),
    #                   row=1, col=1, secondary_y=False)
    #
    #     # DRAWDOWN
    #     fig.add_trace(go.Scatter(x=self.dd_series.index,
    #                              y=self.dd_series.values,
    #                              fill='tozeroy',
    #                              name='Drawdown'),
    #                   row=2, col=1)
    #
    #     # UTILITY
    #     fig.add_trace(go.Scatter(x=self.utility_s.index, y=self.utility_s.values, name='Utility',
    #                              fill='tozeroy'),
    #                   row=3, col=1, secondary_y=False)
    #
    #     if with_benchmark:
    #         #  BENCHMARK DATA
    #         index_closes = data_obj.daily_closes[benchmark]
    #         index_closes.name = 'close'
    #         index_df = pd.DataFrame(index_closes)
    #         index_df['open'] = data_obj.daily_opens[benchmark]
    #         index_df['high'] = data_obj.daily_highs[benchmark]
    #         index_df['low'] = data_obj.daily_lows[benchmark]
    #         index_df['volume'] = data_obj.daily_volumes[benchmark]
    #
    #         # OHLC candlstick plot
    #         fig.add_trace(go.Scatter(x=index_df.index, y=index_df['close'], name=benchmark),
    #                       row=1, col=1, secondary_y=True)
    #         # fig.add_trace(go.Candlestick(x=index_df.index,
    #         #                              open=index_df['open'],
    #         #                              high=index_df['high'],
    #         #                              low=index_df['low'],
    #         #                              close=index_df['close'],
    #         #                              name=benchmark),
    #         #               row=1, col=1, secondary_y=False)
    #         fig.update_layout(yaxis2=dict(scaleanchor='y', scaleratio=1000))
    #
    #         # # BETA & CORRELATION
    #         # beta_series = self.get_rolling_strategy_beta(data_obj, benchmark=benchmark)
    #         # fig.add_trace(go.Scatter(x=beta_series.index,
    #         #                          y=beta_series.values,
    #         #                          line=dict(color='darkturquoise'),
    #         #                          name='Beta'),
    #         #               row=3, col=1)
    #         #
    #         # corr_series = self.get_rolling_strat_correlation(data_obj, benchmark=benchmark)
    #         # fig.add_trace(go.Scatter(x=corr_series.index,
    #         #                          y=corr_series.values,
    #         #                          line=dict(color='orange'),
    #         #                          name='Correlation'),
    #         #               row=3, col=1)
    #
    #     if self.oos:
    #         for k in data_obj.oos_bounds.keys():
    #             fig.add_vrect(x0=data_obj.oos_bounds[k][0], x1=data_obj.oos_bounds[k][1],
    #                           fillcolor="red", opacity=0.25, line_width=0)
    #
    #     fig.update_layout(xaxis_rangeslider_visible=False,
    #                       legend=dict(
    #                           yanchor="top",
    #                           y=0.99,
    #                           xanchor="left",
    #                           x=0.01,
    #                           orientation='h'),
    #                       template='plotly_dark')
    #
    #     plot(fig, auto_open=True)
    #
    #     return fig
    #
    # def plot_ta_chart(self, data_obj, ticker_key='SPY', timeframe='daily', ma=False, ma_type='simp', equity=False):
    #     if timeframe == 'weekly':
    #         data_obj.get_weekly_data(data_obj)
    #         index_closes = data_obj.weekly_closes[ticker_key]
    #     elif timeframe == 'daily':
    #         index_closes = data_obj.daily_closes[ticker_key]
    #         index_closes.name = 'close'
    #         index_df = pd.DataFrame(index_closes)
    #         index_df['open'] = data_obj.daily_opens[ticker_key]
    #         index_df['high'] = data_obj.daily_highs[ticker_key]
    #         index_df['low'] = data_obj.daily_lows[ticker_key]
    #         index_df['volume'] = data_obj.daily_volumes[ticker_key]
    #
    #     # Create SubPlot
    #     fig = make_subplots(rows=2, cols=1,
    #                         specs=[[{'secondary_y': True}],
    #                                [{'secondary_y': False}]],
    #                         row_width=[0.3, 0.7],
    #                         shared_xaxes=True,
    #                         subplot_titles=('Equity', 'Drawdown'))
    #     # OHLC candlstick plot
    #     fig.add_trace(go.Candlestick(x=index_df.index,
    #                                  open=index_df['open'],
    #                                  high=index_df['high'],
    #                                  low=index_df['low'],
    #                                  close=index_df['close'],
    #                                  name=ticker_key),
    #                   row=1, col=1, secondary_y=False)
    #
    #     fig.update_layout(title=f'Strategy performance plotted against {ticker_key}',
    #                       xaxis_rangeslider_visible=False,
    #                       legend=dict(
    #                           yanchor="top",
    #                           y=0.99,
    #                           xanchor="left",
    #                           x=0.01,
    #                           orientation='h'))
    #     # template='plotly_dark')
    #
    #     # # HISTORICAL VOLATILITY
    #     # vol_lookback = 52
    #     # index_df[f'hist_vol_{vol_lookback}'] = HistoricVolatility(index_df['close'], n=vol_lookback)
    #     # add rolling historic vol to subplot
    #     # fig.add_trace(go.Scatter(x=index_df.index, y=index_df['hist_vol_52'],
    #     #                          name=f'rolling_hist_vol_{vol_lookback}'),
    #     #               row=2, col=1)
    #
    #     # # add VOLUME
    #     # fig.add_trace(go.Bar(x=index_df.index, y=index_df['volume'],
    #     #                      name='volume',
    #     #                      marker=dict(color='white')),
    #     #               row=3, col=1)
    #
    #     # plot our EQUTIY CURVE over our Stock data
    #     if equity:
    #         fig.add_trace(go.Scatter(x=self.equity_curve.index, y=self.equity_curve.values,
    #                                  name=f'{ticker_key} Equity Curve', line=dict(color='fuchsia')),
    #                       row=1, col=1, secondary_y=True)
    #         fig.update_layout(yaxis2=dict(scaleanchor='y', scaleratio=0.001))
    #
    #     # Add in MOVING AVERAGES to the stock data
    #     # ma_lookbacks = [10, 20, 50, 100, 200]
    #     if ma:
    #         # Add in moving averages
    #         ma_lookbacks = [10, 20, 50, 100, 200]
    #         ma_colors = ['yellow', 'lime', 'orange', 'darkturquoise', 'mediumslateblue']
    #         for i in ma_lookbacks:
    #             if ma == 'simp':
    #                 index_df[f'SMA_{i}'] = index_df['close'].rolling(window=i).mean()
    #             elif ma == 'exp':
    #                 pass
    #
    #         for i in ma_lookbacks:
    #             if ma == 'simp':
    #                 index_df[f'SMA_{i}'] = index_df['close'].rolling(window=i).mean()
    #             elif ma == 'exp':
    #                 pass
    #         # add moving averages to plot
    #         for i in range(len(ma_lookbacks)):
    #             if ma == 'simp':
    #                 fig.add_trace(go.Scatter(x=index_df.index, y=index_df[f'SMA_{ma_lookbacks[i]}'],
    #                                          name=f'SMA_{ma_lookbacks[i]}',
    #                                          line=dict(color=ma_colors[i],
    #                                                    dash='dot')),
    #                               row=1, col=1)
    #             elif ma == 'exp':
    #                 fig.add_trace(go.Scatter(x=index_df.index, y=index_df[f'EMA_{ma_lookbacks[i]}'],
    #                                          name=f'EMA_{ma_lookbacks[i]}',
    #                                          line=dict(color=ma_colors[i],
    #                                                    dash='dot')),
    #                               row=1, col=1)
    #
    #         # # add in MACD to subplot
    #         # macd_df = MACD(index_df['close'])
    #         # # plot our MACD line = long_ema - short_ema and then plot the signal line as well histogram of the difference
    #         # fig.add_trace(go.Scatter(x=macd_df.index, y=macd_df['MACD_line'],
    #         #                          name='MACD line',
    #         #                          line=dict(color='blue')),
    #         #               row=2, col=1)
    #         # fig.add_trace(go.Scatter(x=macd_df.index, y=macd_df['signal_line'],
    #         #                          name='signal line',
    #         #                          line=dict(color='orange')),
    #         #               row=2, col=1)
    #         # colours = {'True': 'green',
    #         #            'False': 'red'}
    #         # fig.add_trace(go.Bar(x=macd_df.index, y=macd_df['MACD_diff'], marker={'color': 'green'}, name='MACD diff'),
    #         #               row=2, col=1)
    #
    #     # fig.show()
    #
    #     return fig
