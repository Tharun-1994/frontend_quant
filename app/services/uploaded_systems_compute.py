"""
uploaded_systems_compute.py
===========================
Pure compute for the System Comparison feature (no DB, no FastAPI here so it
stays unit-testable). The HTTP layer (routes/uploaded_systems.py) handles file
storage + DB rows and translates the ValueErrors raised here into HTTP 400s.

An uploaded system is just a pair of CSVs (equity + tradelist). We normalise
them into the SAME DataFrame shape the engine's Equity.json/TradeList.json take,
then reuse PerformanceMetrics.calculate_performance unchanged — so an uploaded
system and an engine strategy can never disagree on how a metric is computed.

Required, after alias resolution:
  equity    -> a date column (the first column, or one named date/timestamp)
               + equityValue. dailyDrawdown is derived if absent.
  tradelist -> profit, entryDate, exitDate.
"""

import numpy as np
import pandas as pd

from app.schemas.PerformanceMetrics import PerformanceMetrics
from app.constants.static_config import SPY_RETURNS

# Stable, high-contrast palette; colours are assigned by position in the
# compare request so the chart traces and the table columns always match.
PALETTE = ["#4f46e5", "#059669", "#d97706", "#dc2626",
           "#0891b2", "#7c3aed", "#ca8a04", "#db2777"]


# ── Ingestion / normalisation ─────────────────────────────────────────────────

def _resolve(cols_lower, candidates):
    for c in candidates:
        if c in cols_lower:
            return cols_lower[c]
    return None


def normalize_equity(df: pd.DataFrame) -> pd.DataFrame:
    """-> DatetimeIndex('date') frame with equityValue (float) and
    dailyDrawdown (float, <=0). Raises ValueError on an unusable file."""
    cols_lower = {str(c).lower().strip(): c for c in df.columns}

    eq_col = _resolve(cols_lower, ("equityvalue", "equity_value", "equity", "nav"))
    if eq_col is None:
        raise ValueError(
            f"Equity CSV needs an 'equityValue' column. Found: {list(df.columns)}"
        )

    # Date: prefer a date-named column, else the first column (engine export
    # writes the date as an unnamed first column -> 'Unnamed: 0').
    date_col = _resolve(cols_lower, ("date", "dates", "timestamp", "datetime"))
    if date_col is None:
        date_col = df.columns[0]

    idx = pd.to_datetime(df[date_col], errors="coerce")
    out = pd.DataFrame(
        {"equityValue": pd.to_numeric(df[eq_col], errors="coerce")}
    )
    out.index = idx
    out.index.name = "date"
    out = out[~out.index.isna()].dropna(subset=["equityValue"]).sort_index()
    if out.empty:
        raise ValueError("Equity CSV had no valid (date, equityValue) rows.")

    dd_col = _resolve(cols_lower, ("dailydrawdown", "drawdown"))
    if dd_col is not None:
        dd = pd.to_numeric(df[dd_col], errors="coerce")
        dd.index = idx
        # Engine stores drawdown as a positive magnitude; the rest of the app
        # works with it negative (see backtest.py: dailyDrawdown * -1).
        out["dailyDrawdown"] = -dd.reindex(out.index).abs()
    else:
        out["dailyDrawdown"] = out["equityValue"] - out["equityValue"].cummax()

    return out


def normalize_trades(df: pd.DataFrame) -> pd.DataFrame:
    """-> frame with profit (float), entryDate, exitDate (datetime). Extra
    columns are ignored. Raises ValueError on an unusable file."""
    cols_lower = {str(c).lower().strip(): c for c in df.columns}

    def need(cands, label):
        col = _resolve(cols_lower, cands)
        if col is None:
            raise ValueError(
                f"Tradelist CSV needs a '{label}' column. Found: {list(df.columns)}"
            )
        return col

    p = need(("profit", "pnl", "p&l", "pl", "netprofit", "net_profit"), "profit")
    ed = need(("entrydate", "entry date", "entry_date", "entry"), "entryDate")
    xd = need(("exitdate", "exit date", "exit_date", "exit"), "exitDate")

    out = pd.DataFrame({
        "profit": pd.to_numeric(df[p], errors="coerce"),
        "entryDate": pd.to_datetime(df[ed], errors="coerce"),
        "exitDate": pd.to_datetime(df[xd], errors="coerce"),
    }).dropna(subset=["profit", "entryDate", "exitDate"])

    if out.empty:
        raise ValueError("Tradelist CSV had no valid (profit, entryDate, exitDate) rows.")
    return out


# ── Per-system metrics (reuses calculate_performance) ──────────────────────────

def system_metrics(eq_df: pd.DataFrame, tr_df: pd.DataFrame, starting_capital: float):
    """Return (metrics_dict, yearly_dict). Reuses the app's
    calculate_performance for everything it already produces, and adds the two
    comparison-only rows (CAGR %, Max DD %)."""
    pm = PerformanceMetrics.calculate_performance(
        eq_df.copy(), tr_df.copy(), starting_capital
    )
    eqv = eq_df["equityValue"]
    days = (eq_df.index[-1] - eq_df.index[0]).days
    yrs = days / 365.25 if days > 0 else 1.0
    final = float(eqv.iloc[-1])
    cagr = ((final / starting_capital) ** (1.0 / yrs) - 1.0) * 100.0 if final > 0 else 0.0


    metrics = {
        "total_profit": pm.total_profit,
        "cagr_pct": round(cagr, 2),
        "sharpe": pm.sharpe_ratio,
        "max_dd": pm.max_drawdown,
        "win_rate_pct": pm.win_rate_pct,
        "profit_factor": pm.profit_factor,
        "k_ratio": pm.k_ratio,
        "trades": pm.total_trades,
    }
    yearly = {str(int(yr.year)): yr.strategy for yr in pm.yearly_returns}
    return metrics, yearly


# ── Comparison equity + drawdown figure (Plotly JSON, two stacked panels) ──────

def build_figure(loaded, scale: str = "indexed") -> dict:
    """loaded: list of (name, eq_df, starting_capital) in display order. Equity
    overlaid on top, % underwater drawdown overlaid below, one colour per system.
    In 'absolute' scale the top panel shows cumulative profit (equity minus that
    system's starting capital), so every system starts at ~0 and ends at its
    total profit."""
    data = []
    for i, (name, eq_df, starting_capital) in enumerate(loaded):
        color = PALETTE[i % len(PALETTE)]
        eqv = eq_df["equityValue"]
        eq_y = (eqv - starting_capital) if scale == "absolute" else (eqv / eqv.iloc[0] * 100.0)
        # Drawdown panel in actual currency (equity below its running peak), not %.
        # Same dailyDrawdown series calculate_performance reduces to "Max DD", so the
        # panel's trough lines up exactly with the Max DD figure in the table.
        dd_cur = eq_df["dailyDrawdown"]
        x = eq_df.index.strftime("%Y-%m-%d").tolist()
        data.append({
            "x": x, "y": eq_y.round(4).tolist(),
            "type": "scatter", "mode": "lines", "name": name, "legendgroup": name,
            "line": {"color": color, "width": 2},
            "hovertemplate": name + ": %{y:,.1f}<br>%{x|%Y-%m-%d}<extra></extra>",
            "xaxis": "x", "yaxis": "y",
        })
        data.append({
            "x": x, "y": dd_cur.round(2).tolist(),
            "type": "scatter", "mode": "lines", "name": name, "legendgroup": name,
            "showlegend": False, "fill": "tozeroy", "fillcolor": color + "22",
            "line": {"color": color, "width": 1.5},
            "hovertemplate": name + " DD: $%{y:,.0f}<br>%{x|%Y-%m-%d}<extra></extra>",
            "xaxis": "x2", "yaxis": "y2",
        })

    eq_title = "Profit ($)" if scale == "absolute" else "Equity (indexed to 100)"
    layout = {
        "height": 560,
        # Roomier top band for the legend; wider left gutter for the y-axis title.
        "margin": {"t": 60, "b": 46, "l": 72, "r": 20},
        "plot_bgcolor": "white", "paper_bgcolor": "white",
        "font": {"family": "Inter, sans-serif", "size": 12, "color": "#333"},
        "hovermode": "x unified",
        # Legend sits in its own band at the very top (anchored from its bottom
        # edge so extra rows grow upward), so long system names never overlap the
        # panel labels or the y-axis ticks.
        "legend": {"orientation": "h", "y": 1.04, "x": 0, "yanchor": "bottom"},
        # Panel labels live on the y-axes (rotated, left of the ticks) rather than
        # as floating annotations — the annotations were what collided with the
        # legend and the tick labels.
        "xaxis": {"domain": [0, 1], "anchor": "y", "showticklabels": False,
                  "gridcolor": "rgba(0,0,0,0.05)"},
        "yaxis": {"domain": [0.40, 1], "gridcolor": "rgba(0,0,0,0.06)",
                  "title": {"text": eq_title, "standoff": 8}},
        "xaxis2": {"domain": [0, 1], "anchor": "y2", "matches": "x", "title": "Date",
                   "gridcolor": "rgba(0,0,0,0.05)"},
        "yaxis2": {"domain": [0, 0.30], "tickprefix": "$",
                   "gridcolor": "rgba(0,0,0,0.06)",
                   "title": {"text": "Drawdown ($)", "standoff": 8}},
    }
    return {"data": data, "layout": layout}


# ── Top-level payload for the compare endpoint ─────────────────────────────────

def build_compare_payload(loaded, scale: str = "indexed") -> dict:
    """loaded: list of dicts {id, name, eq_df, tr_df, starting_capital} in the
    order the user selected them. Returns the Plotly figure plus the combined
    metrics+yearly table data (systems as columns, SPY as benchmark)."""
    systems_out = []
    all_years = set()
    fig_input = []
    for i, s in enumerate(loaded):
        color = PALETTE[i % len(PALETTE)]
        metrics, yearly = system_metrics(s["eq_df"], s["tr_df"], s["starting_capital"])
        all_years.update(int(y) for y in yearly)
        systems_out.append({
            "id": s["id"], "name": s["name"], "color": color,
            "metrics": metrics, "yearly": yearly,
        })
        fig_input.append((s["name"], s["eq_df"], s["starting_capital"]))

    figure = build_figure(fig_input, scale)
    spy = {str(int(k)): round(float(v), 2) for k, v in SPY_RETURNS.items()}
    return {
        "figure": figure,
        "table": {
            "systems": systems_out,
            "spy": spy,
            "years": sorted(all_years),
            "metric_rows": [
                {"key": "total_profit", "label": "Total Profit"},
                {"key": "cagr_pct", "label": "CAGR %"},
                {"key": "sharpe", "label": "Sharpe"},
                {"key": "max_dd", "label": "Max DD"},
                {"key": "win_rate_pct", "label": "Win Rate %"},
                {"key": "profit_factor", "label": "Profit Factor"},
                {"key": "k_ratio", "label": "K-Ratio"},
                {"key": "trades", "label": "Trades"},
            ],
        },
    }