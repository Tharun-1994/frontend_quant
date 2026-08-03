"""
mechanic_registry.py
=====================
Single source of truth for every *mechanic* exposed in the strategy builder.

A "mechanic" is something the engine DOES to a trade (stop-loss, take-profit,
ranking, order type, filters, regime gating) — as opposed to an *indicator*,
which is a value you MEASURE inside a rule. Indicators live in
indicator_registry.py; mechanics live here. The two are deliberately separate
because a mechanic is not a rule atom: it has no LHS/RHS and no
section x side x regime availability — it is scalar config on the regime.

DEVELOPER RULE — when you add or change a mechanic:
  1. Add / edit the field(s) in schemas/strategy.py  (so the value can be saved)
  2. Add ONE entry to MECHANIC_REGISTRY below
  3. Restart the app — sync_mechanics auto-creates the DB row (blank description)
  4. Fill the description via the admin page (or seed_mechanic_descriptions.py)
  5. React picks it up automatically via GET /api/mechanics/meta

DO NOT add descriptions here. Descriptions live in the database only
(filled by a non-engineer). This file holds structural facts only:
key, group, the schema fields it controls, its option enums, and where it applies.

`option_values` is the centralised enum: it is the same list the RegimeCard
editor dropdowns can read, so adding a new option (e.g. a 4th stop-loss type)
updates BOTH the editor dropdown and the Rule-info reference from one place.
NOTE: option_values is display/selection metadata only — it never encodes
engine behaviour (the math for ATR_BASED etc. is, and stays, code).
"""

# ---------------------------------------------------------------------------
# Regime type constants  (must match regimeConfig.ts / indicator_registry.py)
# ---------------------------------------------------------------------------
NORMAL                = "Normal"
SIMPLE                = "Simple"
COMPLEX               = "Complex"
INDIVIDUAL_ETF_SIMPLE = "Individual ETFs - Simple"

EQUITY_REGIMES = [NORMAL, SIMPLE, COMPLEX]
ALL_REGIMES    = [NORMAL, SIMPLE, COMPLEX, INDIVIDUAL_ETF_SIMPLE]
TREND_REGIMES  = [INDIVIDUAL_ETF_SIMPLE, SIMPLE, COMPLEX]   # regimeConfig.marketTrendRules == true
COMPLEX_ONLY   = [COMPLEX]                                  # regimeConfig.volatilityRules == true
ETF_ONLY       = [INDIVIDUAL_ETF_SIMPLE]                    # regimeConfig.lookInsideBar == true

# ---------------------------------------------------------------------------
# Group constants  (each group = one tab in the Rule-info "Mechanics" view)
# ---------------------------------------------------------------------------
EXIT_RISK          = "Exit & Risk"
ORDER_EXEC         = "Order & Execution"
SELECTION_SIZING   = "Selection & Sizing"
CONCENTRATION      = "Concentration"
CALENDAR_LIQUIDITY = "Calendar & Liquidity"
REGIME             = "Regime"

# Ordered list — the frontend derives its tab order from this, so the tab bar
# is data-driven (no hardcoded tab list in React).
MECHANIC_GROUPS = [
    EXIT_RISK,
    ORDER_EXEC,
    SELECTION_SIZING,
    CONCENTRATION,
    CALENDAR_LIQUIDITY,
    REGIME,
]

# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------
LIVE        = "live"          # user-configurable today (all 21 below)
ENGINE_ONLY = "engine_only"   # exists in engine code, not yet wired to schema/UI
ROADMAP     = "roadmap"       # not built anywhere yet
# Only LIVE mechanics are seeded today. ENGINE_ONLY / ROADMAP are reserved so a
# future "Roadmap" tab can reuse this exact registry without a schema change.

# ---------------------------------------------------------------------------
# Helper — build an option-value row
# ---------------------------------------------------------------------------
def _opt(field, value, label):
    """One selectable option for a mechanic's dropdown field."""
    return {"field": field, "value": value, "label": label}


# ---------------------------------------------------------------------------
# MECHANIC_REGISTRY
# ---------------------------------------------------------------------------
# Fields:
#   display_name        — label shown in the Rule-info table / drawer header
#   group               — one of MECHANIC_GROUPS (the tab it appears under)
#   config_fields       — the schemas/strategy.py field(s) the trader sets for
#                         this mechanic (drives the drawer's "Where you set it")
#   option_values       — list of _opt(...) rows; the centralised enum for any
#                         dropdown field. [] for free-number / boolean mechanics.
#   applies_to_regimes  — regime types that expose this mechanic (from
#                         regimeConfig feature flags). Used to label "ETF only"
#                         etc. in the reference.
#   status              — LIVE | ENGINE_ONLY | ROADMAP
#   sort_order          — display order within the group (lower = first)
# ---------------------------------------------------------------------------

MECHANIC_REGISTRY = {

    # ══ Exit & Risk ═══════════════════════════════════════════════════════════
    "stop_loss": {
        "display_name":       "Stop-loss",
        "group":              EXIT_RISK,
        "config_fields":      ["stoploss_type", "stoploss_pct", "stoploss_dollar",
                               "atr_lookback_stp", "stoploss_timing"],
        "option_values": [
            _opt("stoploss_type", "NORMAL",       "Normal (percent of entry)"),
            _opt("stoploss_type", "ATR_BASED",    "ATR based (volatility-scaled)"),
            _opt("stoploss_type", "DOLLAR_BASED", "Dollar based (fixed $ from entry)"),
        ],
        "applies_to_regimes": ALL_REGIMES,
        "status":             LIVE,
        "sort_order":         1,
    },

    "take_profit": {
        "display_name":       "Take-profit",
        "group":              EXIT_RISK,
        "config_fields":      ["takeprofit_type", "takeprofit_pct", "takeprofit_dollar",
                               "atr_lookback_tp", "takeprofit_timing"],
        "option_values": [
            _opt("takeprofit_type", "NORMAL",       "Normal (percent of entry)"),
            _opt("takeprofit_type", "ATR_BASED",    "ATR based (volatility-scaled)"),
            _opt("takeprofit_type", "DOLLAR_BASED", "Dollar based (fixed $ from entry)"),
        ],
        "applies_to_regimes": ALL_REGIMES,
        "status":             LIVE,
        "sort_order":         2,
    },

    "max_time": {
        "display_name":       "Max time (time-based exit)",
        "group":              EXIT_RISK,
        "config_fields":      ["max_time"],
        "option_values":      [],
        "applies_to_regimes": ALL_REGIMES,
        "status":             LIVE,
        "sort_order":         3,
    },

    "risk_timing": {
        "display_name":       "Risk timing (EOD vs Intraday)",
        "group":              EXIT_RISK,
        "config_fields":      ["stoploss_timing", "takeprofit_timing"],
        "option_values": [
            _opt("timing", "EOD",      "End of day (checked on the daily close)"),
            _opt("timing", "INTRADAY", "Intraday (checked against the intraday path)"),
        ],
        "applies_to_regimes": ALL_REGIMES,  # INTRADAY is only offered where regimeConfig.intradayTiming is true
        "status":             LIVE,
        "sort_order":         4,
    },

    # ══ Order & Execution ═════════════════════════════════════════════════════
    "order_type": {
        "display_name":       "Order type",
        "group":              ORDER_EXEC,
        "config_fields":      ["order_type", "limit_pct", "atr_limit_lookback"],
        "option_values": [
            _opt("order_type", "NORMAL",    "Normal (market at the chosen bar)"),
            _opt("order_type", "LIMIT",     "Limit (percent away from reference)"),
            _opt("order_type", "LIMIT_ATR", "Limit ATR (ATR-scaled limit offset)"),
        ],
        "applies_to_regimes": ALL_REGIMES,
        "status":             LIVE,
        "sort_order":         1,
    },

    "signal_timing": {
        "display_name":       "Signal timing (entry & exit)",
        "group":              ORDER_EXEC,
        "config_fields":      ["entry_timing", "exit_timing"],
        "option_values": [
            _opt("timing", "Next bar Open",  "Next bar open (most realistic)"),
            _opt("timing", "This Bar Close", "This bar close (same-bar fill)"),
            _opt("timing", "EOD Close",      "End-of-day close"),
        ],
        "applies_to_regimes": ALL_REGIMES,
        "status":             LIVE,
        "sort_order":         2,
    },

    "look_inside_bar": {
        "display_name":       "Look inside bar",
        "group":              ORDER_EXEC,
        "config_fields":      ["is_look_inside_bar"],
        "option_values":      [],   # boolean toggle
        "applies_to_regimes": ETF_ONLY,
        "status":             LIVE,
        "sort_order":         3,
    },

    "rebalance_constraints": {
        "display_name":       "Rebalance & entry constraints",
        "group":              ORDER_EXEC,
        "config_fields":      ["rebalance", "min_price", "min_quantity"],
        "option_values": [
            _opt("rebalance", "DAILY",   "Daily"),
            _opt("rebalance", "WEEKLY",  "Weekly"),
            _opt("rebalance", "MONTHLY", "Monthly"),
        ],
        "applies_to_regimes": ALL_REGIMES,  # strategy-level settings
        "status":             LIVE,
        "sort_order":         4,
    },

    # ══ Selection & Sizing ════════════════════════════════════════════════════
    "ranking": {
        "display_name":       "Ranking (portfolio-level)",
        "group":              SELECTION_SIZING,
        "config_fields":      ["ranking", "ranking_lookback", "ranking_order"],
        "option_values": [
            _opt("ranking_order", "Ascending",  "Ascending (take the lowest values)"),
            _opt("ranking_order", "Descending", "Descending (take the highest values)"),
        ],
        "applies_to_regimes": EQUITY_REGIMES,  # regimeConfig.ranking == true
        "status":             LIVE,
        "sort_order":         1,
    },

    "top_n_selection": {
        "display_name":       "Top-N comparison (rule-level)",
        "group":              SELECTION_SIZING,
        "config_fields":      ["value_type", "ranking_order", "value"],
        "option_values": [
            _opt("value_type", "value",          "Value (fixed number)"),
            _opt("value_type", "indicator_price","Indicator / price"),
            _opt("value_type", "top_n",          "Top N (raw — ranked across all tickers)"),
            _opt("value_type", "top_n_universe", "Top N (within active universe)"),
        ],
        "applies_to_regimes": ALL_REGIMES,  # set inside the rule builder
        "status":             LIVE,
        "sort_order":         2,
    },

    "universe": {
        "display_name":       "Universe",
        "group":              SELECTION_SIZING,
        "config_fields":      ["universe"],
        "option_values": [
            _opt("universe", "sp500",       "S&P 500"),
            _opt("universe", "sp100",       "S&P 100"),
            _opt("universe", "nasdaq100",   "Nasdaq 100"),
            _opt("universe", "russell3000", "Russell 3000"),
            _opt("universe", "liquid500",   "Liquid 500"),
        ],
        "applies_to_regimes": EQUITY_REGIMES,  # ETF regimes use the ETF selector instead
        "status":             LIVE,
        "sort_order":         3,
    },

    "capital_slots": {
        "display_name":       "Capital & slots (position sizing)",
        "group":              SELECTION_SIZING,
        "config_fields":      ["capital", "slots"],
        "option_values":      [],
        "applies_to_regimes": ALL_REGIMES,
        "status":             LIVE,
        "sort_order":         4,
    },

    # ══ Concentration ═════════════════════════════════════════════════════════
    "sector_filter": {
        "display_name":       "Sector filter",
        "group":              CONCENTRATION,
        "config_fields":      ["sector_level", "sector_limit"],
        "option_values":      [],
        "applies_to_regimes": EQUITY_REGIMES,  # shown alongside ranking
        "status":             LIVE,
        "sort_order":         1,
    },

    "duplicates": {
        "display_name":       "Duplicate positions",
        "group":              CONCENTRATION,
        "config_fields":      ["max_duplicates", "max_duplicate_sets"],
        "option_values":      [],
        "applies_to_regimes": ALL_REGIMES,
        "status":             LIVE,
        "sort_order":         2,
    },

    "gap_filter": {
        "display_name":       "Gap filter",
        "group":              CONCENTRATION,
        "config_fields":      ["gap_filter_pct"],
        "option_values":      [],
        "applies_to_regimes": ALL_REGIMES,
        "status":             LIVE,
        "sort_order":         3,
    },

    # ══ Calendar & Liquidity ══════════════════════════════════════════════════
    "banned_months": {
        "display_name":       "Banned months",
        "group":              CALENDAR_LIQUIDITY,
        "config_fields":      ["banned_months"],
        "option_values":      [],
        "applies_to_regimes": ALL_REGIMES,
        "status":             LIVE,
        "sort_order":         1,
    },

    "tdom_filters": {
        "display_name":       "TDOM filters (trading-day-of-month / weekday)",
        "group":              CALENDAR_LIQUIDITY,
        "config_fields":      ["tdom_filters"],   # list of {tdom, weekday, banned_months}
        "option_values":      [],
        "applies_to_regimes": ALL_REGIMES,
        "status":             LIVE,
        "sort_order":         2,
    },

    "vol_turnover_filter": {
        "display_name":       "Vol / turnover filter (regime-aware)",
        "group":              CALENDAR_LIQUIDITY,
        "config_fields":      ["vol_filter"],     # {enabled, spy_ticker, vol_pct_bull/bear, turnover_pct_bull/bear}
        "option_values":      [],
        "applies_to_regimes": ALL_REGIMES,
        "status":             LIVE,
        "sort_order":         3,
    },

    # ══ Regime ════════════════════════════════════════════════════════════════
    "market_trend_rules": {
        "display_name":       "Market-trend rules & regime ticker",
        "group":              REGIME,
        "config_fields":      ["market_trend_type", "market_trend_rules_tree", "regime_ticker"],
        "option_values":      [],
        "applies_to_regimes": TREND_REGIMES,
        "status":             LIVE,
        "sort_order":         1,
    },

    "freeze_resume": {
        "display_name":       "Freeze / resume rules",
        "group":              REGIME,
        "config_fields":      ["freeze_rules_tree", "resume_rules_tree", "volatility_rules_tree"],
        "option_values":      [],
        "applies_to_regimes": COMPLEX_ONLY,
        "status":             LIVE,
        "sort_order":         2,
    },

    "close_on_regime_exit": {
        "display_name":       "Close positions when this regime ends",
        "group":              REGIME,
        "config_fields":      ["close_positions_on_regime_exit"],
        "option_values":      [],   # boolean toggle
        "applies_to_regimes": TREND_REGIMES,
        "status":             LIVE,
        "sort_order":         3,
    },

}