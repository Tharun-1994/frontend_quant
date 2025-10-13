from sqlalchemy import Column, Integer, String, Float, Date, DateTime
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

    # Relationship to MarketRegime (defined in another file)
    regimes = relationship("MarketRegime", back_populates="strategy")