from sqlalchemy import Column, Integer, String, Date, DECIMAL, DateTime, Text, Numeric, Float, ForeignKey, Boolean
from sqlalchemy.orm import relationship, declarative_base

from sqlalchemy.sql import func
from app.database import Base


class MarketRegime(Base):
    __tablename__ = "marketregime"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_id = Column(Integer, ForeignKey("strategies_bucket.id"))

    regime_type = Column(String(50), nullable=False)
    regime_ticker = Column(String(50), nullable=False)

    market_trend_type = Column(String(50))
    market_trend_rules = Column(String)  # JSON string of rules
    market_trend_rules_labels = Column(String)  # JSON string or comma list of labels

    volatility_rules = Column(String)  # JSON string
    volatility_rules_labels = Column(String)

    # entry_rules = Column(String, nullable=False)
    entry_rules_labels = Column(String)

    # exit_rules = Column(String, nullable=False)
    exit_rules_labels = Column(String)

    entry_timing = Column(String)
    exit_timing = Column(String)

    stoploss_type = Column(String(10))
    # Patch 72b: PORTFOLIO drawdown anchor — 'PEAK' (drawdown from all-time
    # peak equity) or 'DAILY' (single-day drop from previous close).
    # Required when stoploss_type=='PORTFOLIO'; NULL otherwise.
    portfolio_stoploss_anchor = Column(String(20), nullable=True)
    takeprofit_type = Column(String(10))
    stoploss_pct = Column(Numeric(5, 2))
    # Patch 99: max stop distance as % of anchor price (limit at entry,
    # entry_price for held positions). ATR offset capped at this. NULL/0 = off.
    stoploss_max_pct = Column(Numeric(5, 2), nullable=True)
    stoploss_dollar = Column(Numeric(5, 2))
    takeprofit_pct = Column(Numeric(5, 2))

    takeprofit_dollar = Column(Numeric(5, 2))

    stoploss_timing = Column(String)
    takeprofit_timing = Column(String)
    atr_lookback_stp = Column(Numeric(5, 2))
    atr_lookback_tp = Column(Numeric(5, 2))

    ranking = Column(String(255))
    ranking_lookback = Column(Numeric(5, 2))
    ranking_order = Column(String(10))

    order_type = Column(String(10))
    limit_pct = Column(Numeric(5, 2))
    # Patch 167 v2: mode-specific limit parameters as JSON (vol_filter_json
    # precedent). LIMIT_HV keys: hv_lookback, divider, lower, upper,
    # reduction. Future LIMIT_* modes add keys here -- no new columns.
    limit_params_json = Column(Text, nullable=True)
    atr_limit_lookback = Column(Numeric(5, 2))

    universe = Column(String(50))
    rebalance = Column(String(50))

    capital = Column(Numeric(18, 2))
    slots = Column(Integer)
    # Patch 48: per-regime live execution sizing. Mirrors
    # strategies_bucket.production_capital but at regime granularity (each
    # regime can deploy a different live capital). Backfilled from the
    # strategy-level field by migrate_production_capital_to_regime; the
    # execution wiring (step 3) sizes off this and falls back to `capital`
    # above when NULL. Backtest sizing always uses `capital` (untouched).
    production_capital = Column(Numeric(18, 2), nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    max_time = Column(Integer)

    banned_months = Column(String, default="[]")  # stored as JSON string: "[1,2,6]"

    market_trend_rules_tree_json = Column(Text, nullable=True)
    volatility_rules_tree_json = Column(Text, nullable=True)
    entry_rules_tree_json = Column(Text, nullable=True)
    exit_rules_tree_json = Column(Text, nullable=True)

    freeze_rules_tree_json = Column(Text, nullable=True)
    resume_rules_tree_json = Column(Text, nullable=True)

    is_look_inside_bar = Column(Boolean, default=False)

    # If True, open positions in this regime are force-closed at next open
    # when the market trend shifts away. False = positions exit normally.
    close_positions_on_regime_exit = Column(Boolean, default=False)

    sector_level = Column(Integer, nullable=True)
    sector_limit = Column(Integer, nullable=True)

    gap_filter_pct = Column(Numeric(5, 2), nullable=True)

    max_duplicates = Column(Integer, nullable=True)
    max_duplicate_sets = Column(Integer, nullable=True)

    # Stored as JSON string e.g. '[{"tdom":0,"banned_months":[3,4,5]}]'
    tdom_filters_json = Column(Text, nullable=True)

    # Stored as JSON string e.g. '{"enabled":true,"spy_ticker":"spy","vol_pct_bull":0.2,...}'
    vol_filter_json = Column(Text, nullable=True)

    freeze_timing = Column(String(10), default="open")
    resume_timing = Column(String(10), default="open")

    # Type of volatility safety net engaged for this regime.
    #   "none"           → no safety net; strategy trades freely
    #   "simple"         → stateless freeze/resume rule trees (current behaviour)
    #   "spy_volatility" → stateful 4-escape state machine (Stage 3)
    # Default "none" keeps existing strategies unchanged.
    safety_net_type = Column(String(20), default="none")
    # JSON-serialised list of SafetyNetItem objects.
    # See app/schemas/strategy.py::SafetyNetItem for the shape.
    # NULL or empty list means "no safety nets — strategy trades freely".
    safety_nets_json = Column(String)  # NVARCHAR(MAX) on SQL Server

    # LRA Patch 12: LONGSHORT system_type fields (all NULL for existing strategies)
    # Per-ticker static metadata: {ticker: {risk, color, hl_threshold}}
    ticker_classification = Column(Text, nullable=True)
    # Per-pair construction rule tree + match_action (e.g. swap_short_leg)
    pairing_entry_rules = Column(Text, nullable=True)
    # Per-pair rule-driven exit tree + match_action (unused by LRA, schema present)
    pairing_exit_rules = Column(Text, nullable=True)
    # Generic sizing policy: {mode: "capital_div_slots" | "fixed_dollar_per_leg", params: {...}}
    sizing_policy = Column(Text, nullable=True)
    # Pair lifecycle policy (non-rule): {max_hold_sessions, force_close, profit_exit}
    pair_exit_policy = Column(Text, nullable=True)

    # LRA Patch 34: per-leg entry rule trees for LONGSHORT pairs.
    # Each is the same JSON shape as Patch 28's evaluator expects:
    #   {type: "group", logic: "AND"|"OR", children: [...]}
    # where children are either group nodes or leaf nodes (indicator + operator
    # + value-or-value_indicator), or top_n_universe ranking leaves.
    entry_rules_tree_long = Column(Text, nullable=True)
    entry_rules_tree_short = Column(Text, nullable=True)

    # Spec A1: how many extra ranked candidates beyond `slots` to persist as
    # the substitute pool each night. Default 20. 0 disables substitution on
    # this regime. Set per-regime; SignalEngine reads this when building the
    # pool that the trader can promote to active via overlay-apply.
    substitute_pool_size = Column(Integer, nullable=True, default=20)

    # Hold Blackout: after a stock exits, block it from re-entry for
    # hold_blackout_days days. 0/NULL disables. hold_blackout_unit selects how
    # the days are counted in the engine ('calendar' or 'trading').
    hold_blackout_days = Column(Integer, nullable=True, default=0)
    hold_blackout_unit = Column(String(10), nullable=True, default="calendar")

    # Rebalance weekday: restrict entries to one weekday (0=Mon .. 4=Fri).
    # NULL = every day (default). With max_time this yields weekly rotation.
    rebalance_weekday = Column(Integer, nullable=True, default=None)

    # # RELATIONSHIP
    strategy = relationship("StrategyBucket", back_populates="regimes")