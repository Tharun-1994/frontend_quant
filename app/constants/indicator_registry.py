"""
indicator_registry.py
=====================
Single source of truth for every indicator in the backtest engine.

DEVELOPER RULE — when you add a new indicator:
  1. Wire it in GeneratePricesIndicators._compute_rule_indicators (Python)
  2. Add ONE entry to INDICATOR_REGISTRY below
  3. Restart the app — the sync script auto-creates the DB row
  4. React picks it up automatically via GET /api/indicators/meta

DO NOT add descriptions here. Descriptions live in the database only.
This file holds structural facts only: key, metadata, and availability.
"""

# ---------------------------------------------------------------------------
# Regime type constants  (must match regimeConfig.ts keys exactly)
# ---------------------------------------------------------------------------
NORMAL                = "Normal"
SIMPLE                = "Simple"
COMPLEX               = "Complex"
INDIVIDUAL_ETF_SIMPLE = "Individual ETFs - Simple"

EQUITY_REGIMES = [NORMAL, SIMPLE, COMPLEX]
ALL_REGIMES    = [NORMAL, SIMPLE, COMPLEX, INDIVIDUAL_ETF_SIMPLE]

# ---------------------------------------------------------------------------
# Section constants
# ---------------------------------------------------------------------------
ENTRY         = "entry"
EXIT          = "exit"
MARKET_REGIME = "market_regime"
VOLATILITY    = "volatility"

# ---------------------------------------------------------------------------
# Side constants
# ---------------------------------------------------------------------------
LHS = "lhs"   # left-hand side  — the indicator being measured
RHS = "rhs"   # right-hand side — the value being compared against

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _avail(regime_types, sections, side, context_note=None, sort_order=99):
    rows = []
    for regime in regime_types:
        for section in sections:
            rows.append({
                "regime_type":   regime,
                "section":       section,
                "side":          side,
                "context_note":  context_note,
                "sort_order":    sort_order,
            })
    return rows


# ---------------------------------------------------------------------------
# INDICATOR_REGISTRY
# ---------------------------------------------------------------------------
# Fields:
#   display_name        — label shown in UI dropdowns and pill display
#   category            — Momentum | Trend | Volatility | Price | Volume | Risk-adjusted
#   has_lookback        — bool
#   default_lookback    — int or None
#   has_params          — bool (extra config fields beyond lookback)
#   params              — structured list consumed by React form inputs
#   params_description  — plain English description for admin page
#   kind                — "boolean" for true/false indicators, None otherwise
#   has_range           — True if the indicator exposes a range % field
#   universe_restriction— string or None
#   caution_note        — string or None
#   sort_order          — int (lower = shown first)
#   availability        — list of dicts from _avail()
# ---------------------------------------------------------------------------

INDICATOR_REGISTRY = {

    # ── Momentum ─────────────────────────────────────────────────────────────

    "rsi": {
        "display_name":         "RSI",
        "category":             "Momentum",
        "has_lookback":         True,
        "default_lookback":     2,
        "has_params":           False,
        "params":               [],
        "params_description":   None,
        "kind":                 None,
        "has_range":            False,
        "universe_restriction": None,
        "caution_note":         None,
        "sort_order":           1,
        "availability": (
            _avail(EQUITY_REGIMES,          [ENTRY, EXIT, VOLATILITY], LHS, sort_order=1) +
            _avail([INDIVIDUAL_ETF_SIMPLE], [ENTRY, EXIT, VOLATILITY], LHS, sort_order=1)
        ),
    },

    "crsi": {
        "display_name":         "CRSI",
        "category":             "Momentum",
        "has_lookback":         False,
        "default_lookback":     2,
        "has_params":           False,
        "params":               [],
        "params_description":   None,
        "kind":                 None,
        "has_range":            False,
        "universe_restriction": "liquid500 and sp500 only",
        "caution_note": (
            "Lookback is fixed at 2 and cannot be changed. "
            "If used with the Russell 3000 universe the indicator silently "
            "produces no signal — rules will never fire. "
            "Only use CRSI when the universe is set to Liquid 500 or S&P 500."
        ),
        "sort_order": 2,
        "availability": (
            _avail(EQUITY_REGIMES, [ENTRY, EXIT], LHS, sort_order=2)
        ),
    },

    "roc": {
        "display_name":         "ROC",
        "category":             "Momentum",
        "has_lookback":         True,
        "default_lookback":     20,
        "has_params":           False,
        "params":               [],
        "params_description":   None,
        "kind":                 None,
        "has_range":            False,
        "universe_restriction": None,
        "caution_note": (
            "Returned as a percentage (e.g. 3.0 means +3%), not a decimal. "
            "Set thresholds accordingly — use 0 not 0.0."
        ),
        "sort_order": 3,
        "availability": (
            _avail(EQUITY_REGIMES, [ENTRY, EXIT], LHS, sort_order=3)
        ),
    },

    "relative_momentum": {
        "display_name":         "Relative Momentum",
        "category":             "Momentum",
        "has_lookback":         True,
        "default_lookback":     90,
        "has_params":           False,
        "params":               [],
        "params_description":   None,
        "kind":                 None,
        "has_range":            False,
        "universe_restriction": None,
        "caution_note":         None,
        "sort_order":           4,
        "availability": (
            _avail(EQUITY_REGIMES, [ENTRY, EXIT], LHS, sort_order=4)
        ),
    },

    # ── Trend ────────────────────────────────────────────────────────────────

    "sma": {
        "display_name":         "SMA",
        "category":             "Trend",
        "has_lookback":         True,
        "default_lookback":     200,
        "has_params":           False,
        "params":               [],
        "params_description":   None,
        "kind":                 None,
        "has_range":            False,
        "universe_restriction": None,
        "caution_note":         None,
        "sort_order":           1,
        "availability": (
            _avail(EQUITY_REGIMES,          [ENTRY, EXIT, VOLATILITY], LHS, sort_order=1) +
            _avail([INDIVIDUAL_ETF_SIMPLE], [ENTRY, EXIT, VOLATILITY], LHS, sort_order=1) +
            _avail(ALL_REGIMES, [MARKET_REGIME], LHS,
                   context_note="Applied to the regime ticker (SPY, VIX, GLD), not individual stocks.",
                   sort_order=1) +
            _avail(EQUITY_REGIMES,          [ENTRY, EXIT], RHS, sort_order=1) +
            _avail([INDIVIDUAL_ETF_SIMPLE], [ENTRY, EXIT], RHS, sort_order=1) +
            _avail(ALL_REGIMES, [MARKET_REGIME], RHS,
                   context_note="Applied to the regime ticker (SPY, VIX, GLD).",
                   sort_order=1)
        ),
    },

    "adx": {
        "display_name":         "ADX",
        "category":             "Trend",
        "has_lookback":         True,
        "default_lookback":     14,
        "has_params":           False,
        "params":               [],
        "params_description":   None,
        "kind":                 None,
        "has_range":            False,
        "universe_restriction": None,
        "caution_note": (
            "ADX measures trend strength only — not direction. "
            "A high ADX with a falling price means a strong downtrend, not an uptrend."
        ),
        "sort_order": 2,
        "availability": (
            _avail(EQUITY_REGIMES, [ENTRY, EXIT, VOLATILITY], LHS, sort_order=2)
        ),
    },

    "n_week_high_recent": {
        "display_name":         "N-Week High (recent)",
        "category":             "Trend",
        "has_lookback":         False,
        "default_lookback":     None,
        "has_params":           True,
        "params": [
            {"key": "n_week_days", "label": "N-week window (days)",        "type": "number", "default": 252, "min": 1},
            {"key": "within_days", "label": "Occurred within last (days)", "type": "number", "default": 20,  "min": 1},
        ],
        "params_description": (
            "n_week_days (default 252): the window in trading days that defines "
            "the 'N-week high' — e.g. 252 = 52-week high. "
            "within_days (default 20): how recently the high must have occurred — "
            "e.g. 20 means the high happened within the last 20 trading days."
        ),
        "kind":                 "boolean",
        "has_range":            False,
        "universe_restriction": None,
        "caution_note":         None,
        "sort_order":           3,
        "availability": (
            _avail(EQUITY_REGIMES, [ENTRY, EXIT], LHS, sort_order=3)
        ),
    },

    # ── Volatility ────────────────────────────────────────────────────────────

    "atr": {
        "display_name":         "ATR",
        "category":             "Volatility",
        "has_lookback":         True,
        "default_lookback":     14,
        "has_params":           False,
        "params":               [],
        "params_description":   None,
        "kind":                 None,
        "has_range":            False,
        "universe_restriction": None,
        "caution_note": (
            "ATR is a dollar amount, not a percentage. "
            "A $200 stock with ATR $4 and a $20 stock with ATR $4 have very "
            "different risk profiles."
        ),
        "sort_order": 1,
        "availability": (
            _avail(EQUITY_REGIMES,          [ENTRY, EXIT, VOLATILITY], LHS, sort_order=1) +
            _avail([INDIVIDUAL_ETF_SIMPLE], [ENTRY, EXIT, VOLATILITY], LHS, sort_order=1) +
            _avail(ALL_REGIMES, [MARKET_REGIME], LHS,
                   context_note="Applied to the regime ticker (SPY, VIX, GLD).",
                   sort_order=2) +
            _avail([INDIVIDUAL_ETF_SIMPLE], [ENTRY, EXIT], RHS, sort_order=2) +
            _avail(ALL_REGIMES, [MARKET_REGIME], RHS,
                   context_note="Applied to the regime ticker (SPY, VIX, GLD).",
                   sort_order=2)
        ),
    },

    "hv": {
        "display_name":         "Historical Volatility",
        "category":             "Volatility",
        "has_lookback":         True,
        "default_lookback":     20,
        "has_params":           False,
        "params":               [],
        "params_description":   None,
        "kind":                 None,
        "has_range":            False,
        "universe_restriction": None,
        "caution_note":         None,
        "sort_order":           2,
        "availability": (
            _avail(EQUITY_REGIMES, [ENTRY, EXIT, VOLATILITY], LHS, sort_order=2)
        ),
    },

    # ── Price ─────────────────────────────────────────────────────────────────

    "close": {
        "display_name":         "Close price",
        "category":             "Price",
        "has_lookback":         False,
        "default_lookback":     None,
        "has_params":           False,
        "params":               [],
        "params_description":   None,
        "kind":                 None,
        "has_range":            False,
        "universe_restriction": None,
        "caution_note":         None,
        "sort_order":           1,
        "availability": (
            _avail(EQUITY_REGIMES,          [ENTRY, EXIT, VOLATILITY], LHS, sort_order=1) +
            _avail([INDIVIDUAL_ETF_SIMPLE], [ENTRY, EXIT, VOLATILITY], LHS, sort_order=1) +
            _avail(ALL_REGIMES, [MARKET_REGIME], LHS,
                   context_note="Closing price of the regime ticker (SPY, VIX, GLD).",
                   sort_order=1) +
            _avail(EQUITY_REGIMES,          [ENTRY, EXIT], RHS, sort_order=1) +
            _avail([INDIVIDUAL_ETF_SIMPLE], [ENTRY, EXIT], RHS, sort_order=1)
        ),
    },

    "unadjusted_close": {
        "display_name":         "Unadjusted close",
        "category":             "Price",
        "has_lookback":         False,
        "default_lookback":     None,
        "has_params":           False,
        "params":               [],
        "params_description":   None,
        "kind":                 None,
        "has_range":            False,
        "universe_restriction": None,
        "caution_note":         None,
        "sort_order":           2,
        "availability": (
            _avail(EQUITY_REGIMES, [ENTRY, EXIT], LHS, sort_order=2)
        ),
    },

    "close_minus_open": {
        "display_name":         "Close minus open",
        "category":             "Price",
        "has_lookback":         False,
        "default_lookback":     None,
        "has_params":           False,
        "params":               [],
        "params_description":   None,
        "kind":                 None,
        "has_range":            False,
        "universe_restriction": None,
        "caution_note":         None,
        "sort_order":           3,
        "availability": (
            _avail(EQUITY_REGIMES, [ENTRY, EXIT], LHS, sort_order=3)
        ),
    },

    "range_close": {
        "display_name":         "Range close",
        "category":             "Price",
        "has_lookback":         False,
        "default_lookback":     None,
        "has_params":           True,
        "params": [
            {"key": "value_range_percent", "label": "Range % (0–100)", "type": "number", "default": 50, "min": 0},
        ],
        "params_description": (
            "value_range_percent (0–100): sets where in the day's High-Low range "
            "the comparison level sits. 50 = midpoint. 70 = top 30%. 30 = bottom 30%."
        ),
        "kind":                 None,
        "has_range":            True,
        "universe_restriction": "SPY universe only",
        "caution_note": (
            "Only available when the strategy universe is set to SPY. "
            "Calculated from SPY's High, Low, and Close."
        ),
        "sort_order": 4,
        "availability": (
            _avail([INDIVIDUAL_ETF_SIMPLE], [ENTRY, EXIT, VOLATILITY], LHS,
                   context_note="SPY universe only.", sort_order=4) +
            _avail(ALL_REGIMES, [MARKET_REGIME], LHS,
                   context_note="SPY intraday position — SPY universe only.", sort_order=3) +
            _avail([INDIVIDUAL_ETF_SIMPLE], [ENTRY, EXIT], RHS,
                   context_note="SPY universe only.", sort_order=3)
        ),
    },

    "vix_close": {
        "display_name":         "VIX close",
        "category":             "Price",
        "has_lookback":         False,
        "default_lookback":     None,
        "has_params":           False,
        "params":               [],
        "params_description":   None,
        "kind":                 None,
        "has_range":            False,
        "universe_restriction": None,
        "caution_note": (
            "Implemented internally as the 'close' indicator routed to the VIX ticker. "
            "In market regime rules, use the 'close' indicator with regime_ticker=VIX instead."
        ),
        "sort_order": 5,
        "availability": (
            _avail(EQUITY_REGIMES, [ENTRY, EXIT, VOLATILITY], LHS,
                   context_note="Uses close indicator routed to VIX ticker.", sort_order=5) +
            _avail(EQUITY_REGIMES, [ENTRY, EXIT], RHS,
                   context_note="VIX level as a dynamic comparison threshold.", sort_order=3)
        ),
    },

    # ── Calendar / Month filter ───────────────────────────────────────────────

    "month": {
        "display_name":         "Month of year",
        "category":             "Calendar",
        "has_lookback":         False,
        "default_lookback":     None,
        "has_params":           False,
        "params":               [],
        "params_description":   None,
        "kind":                 None,
        "has_range":            False,
        "universe_restriction": None,
        "caution_note": (
            "Use with the 'month in' operator and enter a comma-separated list "
            "of month numbers in the Label field (e.g. '5,6' for May and June). "
            "No parquet file is needed — the engine evaluates the calendar date directly."
        ),
        "sort_order": 6,
        "availability": (
            _avail(EQUITY_REGIMES, [VOLATILITY], LHS,
                   context_note="Calendar month filter for freeze/resume rules.", sort_order=6)
        ),
    },

    # ── Volume ────────────────────────────────────────────────────────────────

    "average_volume": {
        "display_name":         "Avg volume",
        "category":             "Volume",
        "has_lookback":         True,
        "default_lookback":     20,
        "has_params":           False,
        "params":               [],
        "params_description":   None,
        "kind":                 None,
        "has_range":            False,
        "universe_restriction": None,
        "caution_note":         None,
        "sort_order":           1,
        "availability": (
            _avail(EQUITY_REGIMES, [ENTRY, EXIT], LHS, sort_order=6)
        ),
    },

    # ── Risk-adjusted ─────────────────────────────────────────────────────────

    "sharpe": {
        "display_name":         "Sharpe ratio",
        "category":             "Risk-adjusted",
        "has_lookback":         False,
        "default_lookback":     None,
        "has_params":           True,
        "params": [
            {"key": "momentum_lookback", "label": "Momentum lookback", "type": "number", "default": 252, "min": 1},
            {"key": "vol_lookback",      "label": "Vol lookback",      "type": "number", "default": 252, "min": 1},
            {"key": "skip_days",         "label": "Skip days",         "type": "number", "default": 0,   "min": 0},
        ],
        "params_description": (
            "momentum_lookback (default 252): the window in days for measuring return. "
            "vol_lookback (default 252): the window in days for measuring volatility. "
            "skip_days (default 0): number of most-recent days to exclude from the "
            "return calculation — used to avoid short-term reversal noise."
        ),
        "kind":                 None,
        "has_range":            False,
        "universe_restriction": None,
        "caution_note":         None,
        "sort_order":           1,
        "availability": (
            _avail(EQUITY_REGIMES, [ENTRY, EXIT], LHS, sort_order=7)
        ),
    },

    "vol_bucket": {
            "display_name":         "Volume bucket",
            "category":             "Volume",
            "has_lookback":         False,
            "default_lookback":     None,
            "has_params":           True,
            "params": [
                {"key": "reset_month", "label": "Reset month (1-12)",         "type": "number", "default": 2,    "min": 1, "max": 12},
                {"key": "percentile",  "label": "Percentile cutoff (0-100)",  "type": "number", "default": 7.5,  "min": 0, "max": 100},
                {"key": "length",      "label": "Rolling window (days)",      "type": "number", "default": 21,   "min": 1},
            ],
            "params_description": (
                "reset_month (default 2 = February): the calendar month whose last trading "
                "day sets the annual liquidity threshold. "
                "percentile (default 7.5): the threshold is the Nth-percentile of universe "
                "members' single-day volume on that reset day — a stock must clear it to pass. "
                "length (default 21): rolling window in trading days for each stock's average "
                "volume (turnover / unadjusted close) that is compared against the threshold."
            ),
            "kind":                 None,
            "has_range":            False,
            "universe_restriction": None,
            "caution_note": (
                "Minimum-liquidity filter. The threshold is recomputed once per year on the "
                "reset month's last trading day and held until the next reset. Compare with == 1 "
                "to keep stocks that pass (1 = passes, 0 = fails)."
            ),
            "sort_order":           2,
            "availability": (
                _avail(EQUITY_REGIMES, [ENTRY], LHS, sort_order=8)
            ),
        },

}