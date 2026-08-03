"""
universe_ticker_exclusion.py — tickers excluded from ALL universe parquets.

Replaces the hardcoded cols_to_drop list in synthetic_ticker_processor.py.
Applied in PriceDataLoader.uploadCommonPath() before every parquet write,
covering both backtest and execution paths.

Soft-deleted rows (active=False) are ignored but preserved for audit.
"""
from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.sql import func
from app.database import Base


class UniverseTickerExclusion(Base):
    __tablename__ = 'universe_ticker_exclusion'

    id        = Column(Integer, primary_key=True, autoincrement=True)
    ticker    = Column(String(20),  nullable=False, unique=True)
    reason    = Column(String(200), nullable=True)
    added_by  = Column(String(100), nullable=True)
    added_at  = Column(DateTime,    server_default=func.now(), nullable=False)
    active    = Column(Boolean,     nullable=False, default=True)

    def __repr__(self):
        return f"<UniverseTickerExclusion ticker={self.ticker} active={self.active}>"