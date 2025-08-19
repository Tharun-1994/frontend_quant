# Universes (e.g., for dropdowns)
from dateutil.rrule import DAILY

UNIVERSES = {
    "sp500": "S&P 500",
    "r3000": "Russell 3000",
    "liquid_500": "Liquid 500"
}
UNIVERSES_Codes = {
    "S&P 500": "sp500",
    "Russell 3000": "r3000",
    "Liquid 500": "liquid_500"
}

# Indicators (for rule building)
INDICATORS = {
    "rsi": "RSI",
    "momentum": "Momentum",
    "adx": "ADX",
    "volatility": "Volatility",
    "sma": "SMA 50",
    "hv": "Historical Volatility"
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

FUNCTION_MAPPER ={
    "rsi": "RSI",
    "adx":"ADX",
    "hv":"HistoricVolatility",
}

REBALACE = {
    "daily": "DAILY",
    "weekly":"WEEKLY",
    "monthly" : "MONTHLY"
}

SIGNAL_TIMING = {
    "open" : "OPEN",
    "close" : "CLOSE"
}

RISK_TIMING = {
    "open" : "EOD",
    "close" : "INTRADAY"
}
RANKING_ORDERS = {
    "asc":"Ascending",
    "desc" :"Descending"
}

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