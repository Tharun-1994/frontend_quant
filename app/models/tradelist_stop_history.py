from sqlalchemy import (
    Column, Integer, String, Numeric, DateTime,
    ForeignKey
)
from sqlalchemy.sql import func
from app.database import Base


class TradelistStopHistory(Base):
    """
    Append-only audit of every stop-price adjustment trader makes on a
    LIVE tradelist row via the Holdings page.

    Written by PATCH /api/tradelist/{id}/stop (Spec F1).

    Engine reads tradelist.current_stop_price for exit checks, NOT
    initial_stop_price. This table is the full history of changes.

    Phase 1: webapp does NOT push changes to IBKR — trader updates the
    broker manually in parallel. Drift between webapp and IBKR is the
    trader's responsibility per scope.
    """
    __tablename__ = "tradelist_stop_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tradelist_id = Column(Integer, ForeignKey("tradelist.id"), nullable=False)

    old_stop_price = Column(Numeric(12, 4), nullable=True)
    new_stop_price = Column(Numeric(12, 4), nullable=False)

    changed_at = Column(DateTime, server_default=func.now(), nullable=False)
    changed_by = Column(String(100), nullable=True)

    reason = Column(String(255), nullable=True)
    # Short note: "trailed to BE", "tightened pre-FOMC", etc.

    def __repr__(self):
        return (f"<StopHistory(tradelist_id={self.tradelist_id}, "
                f"{self.old_stop_price} -> {self.new_stop_price})>")