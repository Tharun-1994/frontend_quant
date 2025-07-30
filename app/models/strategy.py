from sqlalchemy import Column, Integer, String, Date, DECIMAL, DateTime, Text, Numeric

from sqlalchemy.sql import func
from app.database import Base


class Strategy(Base):
    __tablename__ = "strategies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    universe = Column(String, nullable=False)
    slots = Column(Integer)
    capital = Column(Numeric(18, 2))
    start_date = Column(Date)
    end_date = Column(Date)
    stoploss_pct = Column(Numeric(5, 2))
    takeprofit_pct = Column(Numeric(5, 2))
    entry_rules = Column(String, nullable=False)   # Store JSON as text
    exit_rules = Column(String, nullable=False)
    ranking = Column(String(255))
    created_at = Column(DateTime, server_default=func.now())

    def __repr__(self):
        return (f"<Strategy(id={self.id}, name='{self.name}', universe='{self.universe}', "
                f"slots={self.slots}, capital={self.capital}, start_date={self.start_date}, "
                f"end_date={self.end_date}, stoploss_pct={self.stoploss_pct}, "
                f"takeprofit_pct={self.takeprofit_pct}, ranking='{self.ranking}')>")

