from pydantic import BaseModel
from typing import List, Optional
from datetime import date
import math
import numpy as np
from scipy.stats import linregress
import pandas as pd
from app.constants.static_config import SPY_RETURNS
from app.loader import strategy_stat_functions


def _clean_float(v) -> Optional[float]:
    """Coerce a cell to a JSON-safe float.

    Returns None for missing/NaN/inf so the payload stays valid JSON
    (NaN is not legal JSON and breaks strict parsers / axios).
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, 2)


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


class MonthlyReturnRow(BaseModel):
    """One calendar year of strategy returns broken out by month.

    ``months`` is always length 12 (Jan..Dec). A cell is None where the
    strategy had no equity history for that month (e.g. it started mid-year),
    which lets the frontend render that square as blank rather than 0.
    ``total`` mirrors the figure shown on the Yearly Returns tab.
    """
    year: int
    months: List[Optional[float]]
    total: Optional[float]


class MonthlyTradesRow(BaseModel):
    """One calendar year of closed-trade counts broken out by month.

    ``months`` is always length 12 (Jan..Dec). A cell is None for months
    outside the strategy's equity coverage (so the heatmap blanks those
    squares instead of showing a misleading 0); ``total`` is the year's
    trade count and matches ``trades_per_year`` on the Yearly Returns tab.
    """
    year: int
    months: List[Optional[int]]
    total: int


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
    monthly_returns: List[MonthlyReturnRow]
    monthly_trades: List[MonthlyTradesRow]

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
        # tradelist_df['trade_length_bd'] = np.busday_count(opens, closes)
        # avg_trade_len = round(tradelist_df['trade_length_bd'].mean(), 2)

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

        # --- Monthly / Yearly Returns ---
        # monthly_returns() returns a year x [1..12, 'Total'] matrix already.
        # Previously only the 'Total' column was consumed for the yearly table;
        # we now reuse the same matrix to drive the monthly-returns heatmap so
        # the two views can never disagree (single source of truth).
        monthly_df = strategy_stat_functions.monthly_returns(
            equity_df['equityValue'],
            starting_capital,
            False
        )

        yearly_returns_dict = monthly_df['Total'].round(2).to_dict()

        # trades per year (kept for the existing yearly table)
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

        # --- Monthly Returns matrix (Jan..Dec per year) ---
        # Months with no equity history (e.g. before the strategy's first
        # trade, or after its last) are blanked (None) rather than shown as a
        # misleading 0.00. Genuinely flat in-period months keep their 0.00.
        # This changes no totals: blanked months contributed 0 to the year.
        eq_periods = {(int(ts.year), int(ts.month)) for ts in equity_df.index}

        monthly_returns = []
        for year, row in monthly_df.iterrows():
            yr = int(year)
            months = [
                _clean_float(row.get(m)) if (yr, m) in eq_periods else None
                for m in range(1, 13)
            ]
            monthly_returns.append(
                MonthlyReturnRow(
                    year=yr,
                    months=months,
                    total=_clean_float(row.get('Total')),
                )
            )

        # --- Monthly Trades matrix (closed-trade counts, Jan..Dec per year) ---
        mt_series = close_dates.groupby(
            [close_dates.dt.year, close_dates.dt.month]
        ).size()
        mt_lookup = {(int(y), int(m)): int(c) for (y, m), c in mt_series.items()}

        monthly_trades = []
        for year in monthly_df.index:
            yr = int(year)
            counts = [
                mt_lookup.get((yr, m), 0) if (yr, m) in eq_periods else None
                for m in range(1, 13)
            ]
            monthly_trades.append(
                MonthlyTradesRow(
                    year=yr,
                    months=counts,
                    total=int(yearly_trades.get(yr, 0)),
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
            avg_trade_len=0,
            top10_dd=top10_dd,
            yearly_returns=yearly_returns,
            monthly_returns=monthly_returns,
            monthly_trades=monthly_trades,
        )