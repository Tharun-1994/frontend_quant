from sqlalchemy import (
    Column, Integer, String, Numeric, DateTime,
    ForeignKey, Text
)
from sqlalchemy.sql import func
from app.database import Base


class StrategyProductionCapitalHistory(Base):
    """
    Append-only audit of every production_capital change on a strategy.

    Written by the PATCH route that updates strategies_bucket.production_capital
    (Spec E1 — Strategy editor). Frontend reads this for the Capital History
    tab on Strategy Detail (Spec E2).

    Never updated, never deleted.
    """
    __tablename__ = "strategy_production_capital_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_id = Column(Integer, ForeignKey("strategies_bucket.id"),
                         nullable=False)

    # Patch 52: per-regime granularity. NULL for legacy strategy-level rows
    # (pre-Patch 51); always populated for new rows written by save-marketregime-v2.
    regime_id = Column(Integer, ForeignKey("marketregime.id"), nullable=True)

    old_capital = Column(Numeric(18, 2), nullable=True)
    # NULL on the first capital set (strategy going from "not yet live" to live).

    new_capital = Column(Numeric(18, 2), nullable=False)

    changed_at = Column(DateTime, server_default=func.now(), nullable=False)
    changed_by = Column(String(100), nullable=True)

    reason = Column(Text, nullable=True)
    # Free-form: "Doubled after 3-month live test", "Halved due to drawdown".

    def __repr__(self):
        return (f"<CapitalHistory(strategy={self.strategy_id}, "
                f"{self.old_capital} -> {self.new_capital}, "
                f"at={self.changed_at})>")