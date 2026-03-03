import json
import os
from http.client import HTTPException

import plotly.graph_objects as golo
import pandas as pd
from _plotly_utils.utils import PlotlyJSONEncoder
from fastapi.encoders import jsonable_encoder
from plotly.subplots import make_subplots
from starlette.responses import JSONResponse
from starlette.templating import Jinja2Templates
from fastapi import  Request
from app.constants.PricePath import PricePath
from app.constants.static_config import SPY_RETURNS
from app.loader import strategy_stat_functions
from app.routes.backtest import router
import pandas as pd
import numpy as np
from scipy.stats import linregress


templates = Jinja2Templates(directory=r"C:\Tharun\Projects\SourceCode\frontend_quant\app\templates")
@router.get("/api/strategy/{strategy_name}/equity")
def get_equity_chart_json(request: Request,strategy_name: str):
    equity_path = f"{PricePath.getCommonOutputPath()}/Equity.json"
    if not os.path.exists(equity_path):
        return JSONResponse(content={"error": "No equity data found"}, status_code=404)

    equity_df = pd.read_json(equity_path).T
    equity_df.index.name = "date"
    equity_df = equity_df.reset_index()
    equity_df['date'] = pd.to_datetime(equity_df['date'])  # ✅ Parse date column
    equity_df['equityValue'] = equity_df['equityValue'] - 37500
    if equity_df is not None:
        fig = make_subplots(
            rows=2, cols=1,
            row_heights=[0.8, 0.2],
            specs=[[{'secondary_y': True}], [{'secondary_y': False}]],

            shared_xaxes=True,
            subplot_titles=['Equity', 'Drawdown'],
            vertical_spacing=0.05
        )

        # Equity line
        fig.add_trace(golo.Scatter(
            x=equity_df['date'], y=equity_df['equityValue'],
            name='Equity',
            mode='lines',
            line=dict(color='#0F766E', width=2),
            hovertemplate='Equity: %{y:,.0f}<br>Date: %{x|%Y-%m-%d}<extra></extra>'
        ), row=1, col=1)

        # Drawdown fill
        fig.add_trace(golo.Scatter(
            x=equity_df['date'], y=equity_df['dailyDrawdown'],
            name='Drawdown',
            mode='lines',
            fill='tozeroy',
            line=dict(color='rgba(220, 38, 38, 0.8)', width=1.5),
            hovertemplate='Drawdown: %{y:,.0f}<br>Date: %{x|%Y-%m-%d}<extra></extra>'
        ), row=2, col=1)

        fig.update_layout(
            height=520,
            margin=dict(t=40, b=30, l=50, r=30),
            plot_bgcolor='white',
            paper_bgcolor='white',
            font=dict(family='Inter, sans-serif', size=12, color='#333'),
            legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1),
            hovermode='x unified'
        )

        fig.update_xaxes(showgrid=True, gridwidth=0.5, gridcolor='rgba(0,0,0,0.05)')
        fig.update_yaxes(showgrid=True, gridwidth=0.5, gridcolor='rgba(0,0,0,0.05)')

        plot_json = json.loads(json.dumps(fig, cls=PlotlyJSONEncoder))  # ✅ Convert to native Python dict
        return JSONResponse(content=plot_json)





def load_strategy_data(strategy_name: str):
    """Load tradelist and equity from JSON files into DataFrames."""
    trades_path = os.path.join(PricePath.getCommonOutputPath(), f"TradeList.json")
    equity_path = os.path.join(PricePath.getCommonOutputPath(), f"Equity.json")
    if not os.path.exists(trades_path) or not os.path.exists(equity_path):
        raise FileNotFoundError("Missing trades or equity JSON for strategy.")
    trades = pd.read_json(trades_path).T
    trades['entryDate'] = pd.to_datetime(trades['entryDate'])
    trades['exitDate'] = pd.to_datetime(trades['exitDate'])

    equity_df = pd.read_json(equity_path).T
    equity_df.index.name = "date"
    equity_df = equity_df.reset_index()
    equity_df['date'] = pd.to_datetime(equity_df['date'])  # ✅ Parse date column
    equity_df['dailyDrawdown'] = equity_df['dailyDrawdown'] *-1
    return trades, equity_df


def compute_performance(trades: pd.DataFrame, equity: pd.DataFrame, starting_capital: float = 100000) -> dict:
    m = {}
    # (same logic as provided)
    m['Total Profit'] = round(trades['profit'].sum(), 2)
    m['Total Trades'] = len(trades)
    m['Avg Trade Profit'] = round(trades['profit'].mean(), 2)
    m['Max Drawdown'] = round(equity['dailyDrawdown'].min(), 2)
    wins, losses = (trades['profit'] > 0).sum(), (trades['profit'] < 0).sum()
    total = wins + losses
    m['Win Rate %'] = round((wins / total) * 100, 2) if total else 0.0
    tp, tl = trades.loc[trades['profit']>0,'profit'].sum(), abs(trades.loc[trades['profit']<0,'profit'].sum())
    m['profit_factor'] = round(tp/tl,2) if tl else None
    daily_rets = equity['equityValue'].pct_change().dropna()
    m['Sharpe Ratio'] = round((daily_rets.mean()/daily_rets.std())*np.sqrt(252), 3)
    x, y = np.arange(len(equity)), equity['equityValue'].values
    slope, _, _, _, stderr = linregress(x, y)
    m['K-Ratio'] = round(slope/stderr, 3)
    # trade lengths
    # trades['entryDate'], trades['exitDate'] = pd.to_datetime(trades['entryDate']), pd.to_datetime(trades['exitDate'])
    # bd = np.busday_count(trades['entryDate'].dropna().dt.date.values.astype('datetime64[D]'), trades['exitDate'].dropna().dt.date.values.astype('datetime64[D]'))
    # m['avg_trade_len'] = round(bd.mean(), 2)
    # drawdown events
    # dd = equity[['dailyDrawdown']].copy()
    # dd['in_dd'] = dd['dailyDrawdown'] < 0
    # dd['grp'] = (dd['in_dd'] != dd['in_dd'].shift()).cumsum()
    # events = (dd[dd['in_dd']].groupby('grp').apply(lambda g: pd.Series({
    #     'start_date': g.index[0].date(),
    #     'end_date': g.index[-1].date(),
    #     'length': len(g),
    #     'max_dd': round(g['dailyDrawdown'].min(),2),
    #     'avg_dd': round(g['dailyDrawdown'].mean(),2)
    # })).reset_index(drop=True))
    # m['top10_dd'] = events.sort_values('max_dd').head(10).to_dict(orient='records')
    # yearly returns & trades
    # monthly = strategy_stat_functions.monthly_returns(equity['equityValue'], starting_capital, False)
    # m['Yearly returns'] = monthly['Total'].round(2).to_dict()
    # # SPY returns
    # m['SPY returns'] = SPY_RETURNS
    # m['yearly_trades'] = trades['close_date'].dt.year.value_counts().sort_index().to_dict()
    return m


@router.get("/api/strategy/{strategy_name}/performance")
def get_performance_json(strategy_name: str):
    """Return performance metrics JSON for a given strategy."""
    try:
        trades, equity = load_strategy_data(strategy_name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Strategy data not found")
    perf = compute_performance(trades, equity)
    clean = {}
    for k, v in perf.items():
        if isinstance(v, np.integer):
            clean[k] = int(v)
        elif isinstance(v, np.floating):
            clean[k] = float(v)
        else:
            clean[k] = v

    # now JSON-safe:
    return JSONResponse(content=clean)