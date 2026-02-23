# -*- coding: utf-8 -*-
"""
Created on Wed Feb 26 15:10:19 2020

@author: Nick Elmer
"""

import logging





logging.basicConfig(filename="logfilename.log", filemode='w', level=logging.WARN)


class StrategySerivce:
    def __init__(self, price_data, strat_params, generate_signals=False, signal_path=None,
                 starting_capital=100000, max_share_amnt=10, forensics=False):
        # STATIC DATA
        self.max_share_amnt = max_share_amnt
        self.starting_capital = starting_capital
        self.generate_signals = generate_signals
        self.signal_path = signal_path
        self.price_data = price_data
        self.strategy_params = strat_params

        # ENTRY EXIT PASS
        self.entry_pass_df = None
        self.exit_pass_df = None


        if generate_signals:
            # STRATEGY PARAMETERS
            self.week_lookback = None

        self.initialise_strategy()

    def initialise_strategy(self):
        self.initialise_parameters(self.strategy_params)

        if self.generate_signals:
            self.create_strategy_data(self.price_data)

    def initialise_parameters(self, param_dict):
        for k, v in param_dict.items():
            setattr(self, k, v)
        # print('\nInitialised Strategy Parameters...')
        return




    def create_strategy_data(self, data):


        logging.info('Strategy preparation complete.')
        return

    def update_strat(self, data, trade_date):

        logging.info('Strategy Daily complete.')
        return



