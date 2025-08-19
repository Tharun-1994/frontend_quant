# -*- coding: utf-8 -*-
"""
Created on Wed Sep 16 16:08:13 2020

@author: Nick Elmer
"""
import pandas as pd
import numpy as np

def monthly_returns(equity, starting_amount=100000, compounding=False):
    years = equity.index.year.unique()
    months = range(1, 13)
    returns_df = pd.DataFrame(index=years, columns=months)
    daily_profits = equity.diff()
    for year in years:
        this_year_profits = daily_profits[daily_profits.index.year == year]
        this_year_equity = equity[equity.index.year == year]
        for month in months:
            try:
                this_month_profits = this_year_profits[this_year_profits.index.month == month]
                profit = this_month_profits.sum()
                
                if compounding:
                    this_month_start = this_year_equity[this_year_equity.index.month == month].iloc[0]
                    profit = (100 * profit) / (this_month_start + starting_amount)
                    
                returns_df.at[year, month] = profit
                
            except IndexError:
                continue
                
        year_profit = this_year_profits.sum()
        if compounding:
            this_year_start = this_year_equity.iloc[0]
            year_profit = (100 * year_profit) / (this_year_start + starting_amount)
        returns_df.at[year, 'Total'] = year_profit
            
    if not compounding:
        returns_df *= 100 / starting_amount
    
            
    return returns_df.astype(float).round(2)

def drawdown_stats(equity, starting_amount):
    drawdown = equity - equity.cummax()
    max_dd = drawdown.min()
    max_dd_percent = 100 * max_dd / starting_amount
    max_dd_date = drawdown.idxmin()
    max_dd_start = drawdown.where(drawdown==0, np.nan).loc[:max_dd_date].last_valid_index()
    max_dd_end = drawdown.where(drawdown==0, np.nan).loc[max_dd_date:].first_valid_index()
    max_dd_length = len(drawdown[max_dd_start:max_dd_end])
    stats = {'Max Drawdown': max_dd,
             'Max Drawdown %': max_dd_percent,
             'Length of Max Drawdown': max_dd_length}
    return drawdown, stats
