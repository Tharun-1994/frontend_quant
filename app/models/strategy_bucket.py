from sqlalchemy import Column, Integer, String, Float, Numeric, Boolean, Date, DateTime
from sqlalchemy.ext.declarative import declarative_base
import datetime
from app.database import Base
from sqlalchemy.orm import relationship


class StrategyBucket(Base):
    __tablename__ = "strategies_bucket"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name = Column(String(255), nullable=False)

    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)

    rebalance = Column(String(255), nullable=True)

    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    min_price = Column(Float, nullable=True)
    min_quantity = Column(Float, nullable=True)

    system_type = Column(String(50), nullable=True)
    market_regime_type = Column(String(50), nullable=True)

    # ── Execution layer (Spec A1 — additive) ──────────────────────────────────
    # production_capital is the live execution sizing. Separate from
    # MarketRegime.capital (which is the backtest sizing baked into the equity
    # curve). NULL = strategy not yet live; orchestrator skips it.
    # Changed via the Strategy editor; forward-looking only (no retro-resize).
    production_capital = Column(Numeric(18, 2), nullable=True)

    # execution_enabled is the per-strategy kill switch. Default FALSE prevents
    # accidental going-live the moment a strategy is saved.
    # Flipping ON requires production_capital > 0 (frontend + route validation).
    execution_enabled = Column(Boolean, nullable=False, default=False)

    # Stamped by the route that updates production_capital. Display only.
    last_capital_change_at = Column(DateTime, nullable=True)
    last_capital_change_by = Column(String(100), nullable=True)

    # ── Relationships ─────────────────────────────────────────────────────────
    regimes = relationship("MarketRegime", back_populates="strategy")