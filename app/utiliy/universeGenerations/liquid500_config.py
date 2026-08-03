"""
liquid500_config.py
===================
Selection constants for the Liquid 500 universe membership service.

Ported verbatim from the legacy
    Universe_Liq_500_Bifilter_R3000_SnP_1500_PRICE_DROP.py  (most_liquid_otc)

These are the static contract that the webapp migration must reproduce
exactly. Do not tune them — tuning drifts the universe away from the
historical tradelist and breaks parity with the production strategies that
depend on this membership (QAS, TNH, RPT_*, Day_Trading_Ronnan_*).
"""

# Patch 88 — selection constants (legacy parity)

# Universe size: legacy `universe_size=500`
UNIVERSE_SIZE = 500

# Price-drop filter:
#   drop names where  (unadjusted close < $5)  AND  (adjusted close < 0.7 * 125-day max adjusted close)
# legacy `data.min_dollar_unadjusted = 5`
MIN_DOLLAR_UNADJUSTED = 5.0
PRICE_DROP_THRESHOLD  = 0.7      # adj_close < threshold * max_125_close → drop

# Rolling windows
# legacy: `Turnovers.rolling(200).mean()` and `data.daily_closes.rolling(125).max()`
ADV_ROLLING_WINDOW = 200         # for the 200-day mean dollar volume ranking
MAX_ROLLING_WINDOW = 125         # for the 125-day max close (price-drop filter)

# Sector cap (30% per TRBC level-1 sector, evaluated iteratively after top-500
# selection — drop bottom-ranked names from over-cap sectors, re-take top
# 500, recompute, until no sector breaches the cap).
# legacy: `limit_per_sector = ceil(universe_size * 0.3)`  → 150 names max
SECTOR_PCT_LIMIT = 0.30

# Sector classification: TRBC level 1 (Economic Sector)
# legacy: `norgatedata.classification_at_level(x, 'TRBC', 'Name', 1)`
TRBC_LEVEL        = 1
TRBC_SCHEME_NAME  = 'TRBC'
TRBC_RESULT_TYPE  = 'Name'

# Norgate watchlists / index names
# legacy:
#   `nd.watchlist_symbols('US Listed Stocks Easy Current & Past')`
#   `sp_universe = '.../S&P_Composite_1500_most_recent.csv'`  → S&P Composite 1500
#   `r3000_universe = '.../NEW_Russell_3000_universe.csv'`    → Russell 3000
US_LISTED_WATCHLIST = 'US Listed Stocks Easy Current & Past'
SP1500_INDEX_NAME   = 'S&P Composite 1500'
SP1500_WATCHLIST    = 'S&P Composite 1500 Current & Past'
R3000_INDEX_NAME    = 'Russell 3000'
R3000_WATCHLIST     = 'Russell 3000 Current & Past'

# Rebalance frequency: composition recomputed on NYSE month-start trading
# days only; ffilled between.
# legacy: `get_valid_dates(max_lookback=0, rebalance='month-start')`
REBALANCE = 'month-start'

# Lookback buffer for the per-month-start Norgate pull.
# Need ≥ 200 trading days before D for a valid 200-day rolling mean, plus
# the 125-day max-close window (subset of the 200), plus a few-day buffer
# for trading holidays / data gaps.  220 trading days ≈ 11 months.
LOOKBACK_TRADING_DAYS = 220

# Number of parallel worker threads for Norgate price pulls. norgatedata is
# thread-safe for read operations (HTTP under the hood). 10 keeps the wall
# clock manageable without hammering the API.
NORGATE_POOL_SIZE = 10

# Safety cap inside the sector-cap iterative drop loop. If it doesn't
# converge in this many iterations something is structurally wrong with the
# data (e.g. every surviving name in the same sector).
SECTOR_CAP_MAX_ITERATIONS = 50

# Patch 89: version control for the source-of-truth membership CSV.
# Before every write to liquid500.csv, the current file is copied to
# _versions/liquid500_YYYYMMDD_HHMMSS.csv. Backup happens FIRST — if it
# fails, the write does not proceed (loud over silent). Older versions
# are pruned when the count exceeds MAX_VERSIONS_TO_KEEP.
VERSIONS_FOLDER_NAME  = '_versions'
MAX_VERSIONS_TO_KEEP  = 30