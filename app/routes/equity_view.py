"""
equity_view.py
==============
Strategy-name-based equity chart and performance endpoints.
(Separate from the ID-based endpoints in backtest.py.)

Endpoints
---------
GET /api/strategy/{strategy_name}/equity       — Plotly chart JSON
GET /api/strategy/{strategy_name}/performance  — performance metrics JSON
"""

import json
import logging
import os

import pandas as pd
import plotly.graph_objects as go
from _plotly_utils.utils import PlotlyJSONEncoder
from fastapi import APIRouter, HTTPException
from plotly.subplots import make_subplots
from starlette.responses import JSONResponse

from app.constants.PricePath import PricePath
from app.schemas.PerformanceMetrics import PerformanceMetrics

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/strategy", tags=["equity_view"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_strategy_data(strategy_name: str):
    """Load tradelist and equity DataFrames from JSON output files."""
    output_path = PricePath.getCommonOutputPath()
    trades_path = os.path.join(output_path, "TradeList.json")
    equity_path = os.path.join(output_path, "Equity.json")

    if not os.path.exists(trades_path) or not os.path.exists(equity_path):
        raise FileNotFoundError(
            f"Missing output files for strategy '{strategy_name}'"
        )

    trades_df = pd.read_json(trades_path).T
    trades_df["entryDate"] = pd.to_datetime(trades_df["entryDate"])
    trades_df["exitDate"]  = pd.to_datetime(trades_df["exitDate"])

    equity_df = pd.read_json(equity_path).T
    equity_df.index.name    = "date"
    equity_df               = equity_df.reset_index()
    equity_df["date"]       = pd.to_datetime(equity_df["date"])
    equity_df["dailyDrawdown"] = equity_df["dailyDrawdown"] * -1

    return trades_df, equity_df


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/{strategy_name}/equity", summary="Equity + drawdown chart as Plotly JSON")
def get_equity_chart(strategy_name: str):
    equity_path = os.path.join(PricePath.getCommonOutputPath(), "Equity.json")
    if not os.path.exists(equity_path):
        raise HTTPException(status_code=404, detail="No equity data found")

    equity_df = pd.read_json(equity_path).T
    equity_df.index.name       = "date"
    equity_df                  = equity_df.reset_index()
    equity_df["date"]          = pd.to_datetime(equity_df["date"])
    equity_df["equityValue"]   = equity_df["equityValue"] - 37_500

    fig = make_subplots(
        rows=2, cols=1,
        row_heights=[0.8, 0.2],
        specs=[[{"secondary_y": True}], [{"secondary_y": False}]],
        shared_xaxes=True,
        subplot_titles=["Equity", "Drawdown"],
        vertical_spacing=0.05,
    )

    fig.add_trace(
        go.Scatter(
            x=equity_df["date"],
            y=equity_df["equityValue"],
            name="Equity",
            mode="lines",
            line=dict(color="#0F766E", width=2),
            hovertemplate="Equity: %{y:,.0f}<br>Date: %{x|%Y-%m-%d}<extra></extra>",
        ),
        row=1, col=1,
    )

    fig.add_trace(
        go.Scatter(
            x=equity_df["date"],
            y=equity_df["dailyDrawdown"],
            name="Drawdown",
            mode="lines",
            fill="tozeroy",
            line=dict(color="rgba(220,38,38,0.8)", width=1.5),
            hovertemplate="Drawdown: %{y:,.0f}<br>Date: %{x|%Y-%m-%d}<extra></extra>",
        ),
        row=2, col=1,
    )

    fig.update_layout(
        height=520,
        margin=dict(t=40, b=30, l=50, r=30),
        plot_bgcolor="white",
        paper_bgcolor="white",
        font=dict(family="Inter, sans-serif", size=12, color="#333"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        hovermode="x unified",
    )
    fig.update_xaxes(showgrid=True, gridwidth=0.5, gridcolor="rgba(0,0,0,0.05)")
    fig.update_yaxes(showgrid=True, gridwidth=0.5, gridcolor="rgba(0,0,0,0.05)")

    return JSONResponse(
        content=json.loads(json.dumps(fig, cls=PlotlyJSONEncoder))
    )


@router.get("/{strategy_name}/performance", response_model=PerformanceMetrics,
            summary="Performance metrics JSON")
def get_performance(strategy_name: str):
    try:
        trades_df, equity_df = _load_strategy_data(strategy_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("get_performance failed for %s", strategy_name)
        raise HTTPException(status_code=500, detail=str(e))

    return PerformanceMetrics.calculate_performance(trades_df, equity_df, 100_000)