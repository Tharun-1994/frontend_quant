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
    takeprofit_type = Column(String(10))
    stoploss_pct = Column(Numeric(5, 2))
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
    atr_limit_lookback = Column(Numeric(5, 2))

    universe = Column(String(50))
    rebalance = Column(String(50))

    capital = Column(Numeric(18, 2))
    slots = Column(Integer)

    created_at = Column(DateTime, server_default=func.now())
    max_time = Column(Integer)

    banned_months = Column(String, default="[]")  # stored as JSON string: "[1,2,6]"

    market_trend_rules_tree_json = Column(Text, nullable=True)
    volatility_rules_tree_json   = Column(Text, nullable=True)
    entry_rules_tree_json        = Column(Text, nullable=True)
    exit_rules_tree_json         = Column(Text, nullable=True)

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

    # # RELATIONSHIP
    strategy = relationship("StrategyBucket", back_populates="regimes")