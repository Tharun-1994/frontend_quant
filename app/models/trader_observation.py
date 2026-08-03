from sqlalchemy import (
    Column, Integer, String, Date, DateTime,
    ForeignKey, Text
)
from sqlalchemy.sql import func
from app.database import Base


class TraderObservation(Base):
    """
    Free-floating trader observations NOT tied to a specific tradelist row.

    Examples:
      • "Fed minutes today — keep PullBack_X3 size light this week"
      • "Vas out Friday — watch for late substitution.csv"

    For per-position notes, use Tradelist.trader_notes instead.

    strategy_id NULL means the observation applies account-wide.
    """
    __tablename__ = "trader_observations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_id = Column(Integer, ForeignKey("strategies_bucket.id"),
                         nullable=True)
    # NULL = account-wide observation.

    observation_date = Column(Date, nullable=False)
    note = Column(Text, nullable=False)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    created_by = Column(String(100), nullable=True)

    def __repr__(self):
        return (f"<TraderObservation(date={self.observation_date}, "
                f"strategy_id={self.strategy_id})>")