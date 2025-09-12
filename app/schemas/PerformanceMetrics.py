from pydantic import BaseModel
from typing import List, Optional
from datetime import date
import numpy as np
from scipy.stats import linregress
import pandas as pd
from app.constants.static_config import SPY_RETURNS
from app.loader import strategy_stat_functions


class DrawdownRecord(BaseModel):
    start_date: date
    end_date: date
    length: int
    max_dd: float
    avg_dd: float


class YearlyReturn(BaseModel):
    year: int
    strategy: float
    trades_per_year: int
    spy: Optional[float]  # can be None for N/A


class PerformanceMetrics(BaseModel):
    total_profit: float
    total_trades: int
    avg_trade_profit: float
    max_drawdown: float
    win_rate_pct: float
    profit_factor: Optional[float]
    sharpe_ratio: float
    k_ratio: float
    avg_trade_len: float
    top10_dd: List[DrawdownRecord]
    yearly_returns: List[YearlyReturn]

    @staticmethod
    def calculate_performance(
        equity_df: pd.DataFrame,
        tradelist_df: pd.DataFrame,
        starting_capital: float) -> "PerformanceMetrics":

        # --- Basic Metrics ---
        total_profit = round(tradelist_df['profit'].sum(), 2)
        total_trades = len(tradelist_df)
        avg_trade_profit = round(tradelist_df["profit"].mean(), 2)
        max_drawdown = round(equity_df['dailyDrawdown'].min(), 2)

        # --- Win Rate ---
        wins = (tradelist_df['profit'] > 0).sum()
        losses = (tradelist_df['profit'] < 0).sum()
        total = wins + losses
        win_rate_pct = round((wins / total) * 100, 2) if total > 0 else 0.0

        # --- Profit Factor ---
        total_profits = tradelist_df.loc[tradelist_df['profit'] > 0, 'profit'].sum()
        total_losses = abs(tradelist_df.loc[tradelist_df['profit'] < 0, 'profit'].sum())
        profit_factor = total_profits / total_losses if total_losses != 0 else None
        if profit_factor is not None:
            profit_factor = round(profit_factor, 2)

        # --- Sharpe Ratio ---
        daily_rets = equity_df['equityValue'].pct_change().dropna()
        sharpe = (daily_rets.mean() / daily_rets.std()) * np.sqrt(252)
        sharpe_ratio = round(sharpe, 3)

        # --- K-Ratio ---
        x = np.arange(len(equity_df))
        y = equity_df['equityValue'].values
        slope, intercept, r_value, p_value, std_err = linregress(x, y)
        k_ratio = round(slope / std_err, 3)

        # --- Avg Trade Length ---
        tradelist_df['entryDate'] = pd.to_datetime(tradelist_df['entryDate'])
        tradelist_df['exitDate'] = pd.to_datetime(tradelist_df['exitDate'])
        opens = tradelist_df['entryDate'].values.astype('datetime64[D]')
        closes = tradelist_df['exitDate'].values.astype('datetime64[D]')
        tradelist_df['trade_length_bd'] = np.busday_count(opens, closes)
        avg_trade_len = round(tradelist_df['trade_length_bd'].mean(), 2)

        # --- Top 10 Worst Drawdowns ---

        dd = equity_df[['dailyDrawdown']].copy()
        dd['in_dd'] = dd['dailyDrawdown'] < 0
        dd['grp'] = (dd['in_dd'] != dd['in_dd'].shift()).cumsum()
        events = (
            dd[dd['in_dd']]
            .groupby('grp')
            .apply(lambda g: pd.Series({
                'start_date': g.index[0].date(),
                'end_date': g.index[-1].date(),
                'length': len(g),
                'max_dd': round(g['dailyDrawdown'].min(), 2),
                'avg_dd': round(g['dailyDrawdown'].mean(), 2)
            }))
            .reset_index(drop=True)
        )
        top10_dd = [
            DrawdownRecord(**row)
            for row in events.sort_values('max_dd').head(10).to_dict(orient='records')
        ]

        # --- Yearly Returns ---
        yearly_returns_dict = strategy_stat_functions.monthly_returns(
            equity_df['equityValue'],
            starting_capital,
            False
        )['Total'].round(2).to_dict()

        # trades per year
        close_dates = pd.to_datetime(tradelist_df['exitDate'])
        yearly_trades = close_dates.dt.year.value_counts().sort_index().to_dict()

        yearly_returns = []
        for year, strategy_ret in yearly_returns_dict.items():
            yearly_returns.append(
                YearlyReturn(
                    year=year,
                    strategy=strategy_ret,
                    trades_per_year=yearly_trades.get(year, 0),
                    spy=SPY_RETURNS.get(year)
                )
            )

        return PerformanceMetrics(
            total_profit=total_profit,
            total_trades=total_trades,
            avg_trade_profit=avg_trade_profit,
            max_drawdown=max_drawdown,
            win_rate_pct=win_rate_pct,
            profit_factor=profit_factor,
            sharpe_ratio=sharpe_ratio,
            k_ratio=k_ratio,
            avg_trade_len=avg_trade_len,
            top10_dd=top10_dd,
            yearly_returns=yearly_returns
        )
