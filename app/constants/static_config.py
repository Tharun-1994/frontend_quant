# Universes (e.g., for dropdowns)
from dateutil.rrule import DAILY


timeframe_map = {
    'DAILY': 'D',
    'WEEKLY': 'W',
    'MONTHLY': 'M',
    'HOURLY': 'H',
    '5MIN': '5T',
    '15MIN': '15T',
    '30MIN': '30T',
    'QUARTERLY': 'Q',
    'YEARLY': 'Y'
}

UNIVERSES = {
    "sp500": "S&P 500",
    "russell3000": "Russell 3000",
    "liquid500": "Liquid 500",
    "spy" : "SPY",
    "sp100": "S&P 100",
    "nasdaq100": "Nasdaq 100",
    "lra14": "LRA 14",   # LRA Patch 44
}
UNIVERSES_Codes = {
    "S&P 500": "sp500",
    "Russell 3000": "russell3000",
    "Liquid 500": "liquid500",
    "SPY":"spy",
    "S&P 100": "sp100",
    "Nasdaq 100": "nasdaq100",
    "LRA 14": "lra14",   # LRA Patch 44
}

# Indicators (for rule building)
INDICATORS = {
    "rsi": "RSI",
    "adx": "ADX",
    "sma": "SMA",
    "hv": "Historical Volatility",
    "atr": "Avg True Range",
}

OPERATORS = {
    "<": "&lt;",
    ">": "&gt;",
    "==": "=="
}

CONNECTORS = {
    "&&": "AND",
    "||": "OR"
}

FUNCTION_MAPPER = {
    "rsi": "RSI",
    "adx":"ADX",
    "hv":"HistoricVolatility",
    "atr":"AvgTrueRange",
    "sma":"SMA",
    "relative_momentum": "ROC",
    "n_week_high_recent": "n_week_high_recent",
    "sharpe":"SharpeRatio",
    "rolling_vol": "RollingVolatility",
    "rolling_vol_close": "RollingVolatilityUnshifted",
    "ibs": "IBS",
    "daily_range_pct": "DailyRangePct",   # LRA Patch 16
    "roc": "ROC",
    "consec_down": "ConsecutiveDown",
    "rolling_vol_median": "RollingVolatilityMedian",
}

REBALANCE = {
    "daily": "DAILY",
    "weekly":"WEEKLY",
    "monthly" : "MONTHLY"
}

SIGNAL_TIMING = {
    "open" : "Next Day Morning",
    # "close" : "Today Close"
}

RISK_TIMING = {
    "eod" : "EOD",
    "intraday" : "INTRADAY"
}
RANKING_ORDERS = {
    "asc":"Ascending",
    "desc" :"Descending"
}

SYSTEM_TYPE = {
    "long":"LONG",
    # "short":"SHORT"
}

# Patch 60: STOPLOSS_TYPE extended with dollar_based (ETF only) and portfolio
# (portfolio-level kill switch). Adding a new type requires:
#   1. New entry in STOPLOSS_TYPE below
#   2. New entry in STOPLOSS_TYPE_REGIME_GATING below (which regimes allow it)
#   3. Mirror in React options.ts (Patch 61) and Java StaticConfig.java (Patch 62)
#   4. Engine handler in PortfolioServiceImplV2 (Patch 64)
STOPLOSS_TYPE = {
    "nrml": "NORMAL",
    "atr_based": "ATR_BASED",
    "dollar_based": "DOLLAR_BASED",
    "portfolio": "PORTFOLIO",
}
TAKEPROFIT_TYPE = {
    "nrml": "NORMAL",
    "atr_based": "ATR_BASED",
}

# Patch 60: stoploss-type → allowed regime types. ONE PLACE update.
# - ETF regimes accept only DOLLAR_BASED.
# - Non-ETF regimes accept NORMAL, ATR_BASED, PORTFOLIO.
# Mirrored in React options.ts (STOPLOSS_TYPE_REGIME_GATING).
STOPLOSS_TYPE_REGIME_GATING = {
    "NORMAL":       ["Normal", "Simple", "Complex"],
    "ATR_BASED":    ["Normal", "Simple", "Complex"],
    "PORTFOLIO":    ["Normal", "Simple", "Complex"],
    "DOLLAR_BASED": ["Individual ETFs - Simple"],
}

def allowed_stoploss_types_for_regime(regime_type):
    """Return list of valid stoploss_type values for the given regime_type.

    Empty list if regime_type is None/unknown. Used by save-marketregime-v2
    validation (Patch 66) to reject mismatched stoploss/regime combos.
    """
    if not regime_type:
        return []
    return [
        t for t, regimes in STOPLOSS_TYPE_REGIME_GATING.items()
        if regime_type in regimes
    ]

# Patch 72d: drawdown anchor for PORTFOLIO stoploss type.
# PEAK   — drawdown vs all-time peak equity (standard kill-switch).
# DAILY  — single-day drop from previous close (circuit breaker).
# Mirrored in React options.ts and Java StaticConfig.java.
PORTFOLIO_STOPLOSS_ANCHOR = {
    "peak":  "PEAK",
    "daily": "DAILY",
}
PORTFOLIO_STOPLOSS_ANCHOR_DEFAULT = "PEAK"

SPY_RETURNS = {
    2024: 24.89,
    2023: 26.19,
    2022: -18.17,
    2021: 28.75,
    2020: 18.37,
    2019: 31.22,
    2018: -4.56,
    2017: 21.70,
    2016: 12.00,
    2015: 1.25,
    2014: 13.46,
    2013: 32.31,
    2012: 15.99,
    2011: 1.89,
    2010: 15.06,
    2009: 26.37,
    2008: -36.81,
    2007: 5.14,
    2006: 15.85,
    2005: 4.83,
    2004: 10.70,
    2003: 28.11,
    2002: -21.54,
    2001: -11.81,
    2000: -9.73
}