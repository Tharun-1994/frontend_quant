"""
live_equity_snapshot.py — daily mark-to-market equity record for live execution.

One row per strategy per trading day, written by runner.py Step E after commit.
Formula mirrors Portfolio_.mark_to_market():
  equity = unused_capital + sum(filled_qty * close_price for each LIVE position)
  unused_capital = production_capital - sum(intended_capital for LIVE rows)

Enables:
  - Live equity chart stitched to backtest equity in EquityTab
  - Portfolio drawdown monitoring in execution
  - Historical P&L tracking per strategy
"""
from sqlalchemy import Column, Integer, Numeric, Date, ForeignKey, UniqueConstraint
from app.database import Base


class LiveEquitySnapshot(Base):
    __tablename__ = 'live_equity_snapshot'
    __table_args__ = (
        UniqueConstraint('strategy_id', 'snapshot_date', name='uq_live_equity_strategy_date'),
    )

    id               = Column(Integer, primary_key=True, autoincrement=True)
    strategy_id      = Column(Integer, ForeignKey('strategies_bucket.id'), nullable=False, index=True)
    snapshot_date    = Column(Date, nullable=False, index=True)

    # Capital base at time of snapshot
    production_capital = Column(Numeric(18, 2), nullable=True)

    # Equity components
    open_position_count = Column(Integer, nullable=False, default=0)
    deployed_capital    = Column(Numeric(18, 4), nullable=True)   # sum(intended_capital) for LIVE rows
    unused_capital      = Column(Numeric(18, 4), nullable=True)   # production_capital - deployed_capital
    market_value        = Column(Numeric(18, 4), nullable=True)   # sum(filled_qty * close_price)
    equity              = Column(Numeric(18, 4), nullable=True)   # unused_capital + market_value

    # P&L vs cost basis
    unrealised_pnl  = Column(Numeric(18, 4), nullable=True)   # equity - production_capital
    unrealised_pct  = Column(Numeric(10, 6), nullable=True)   # unrealised_pnl / production_capital

    # Drawdown (computed from running max equity)
    max_equity      = Column(Numeric(18, 4), nullable=True)   # running max equity to date
    drawdown        = Column(Numeric(18, 4), nullable=True)   # equity - max_equity (≤ 0)
    drawdown_pct    = Column(Numeric(10, 6), nullable=True)   # drawdown / max_equity

    def __repr__(self):
        return (f"<LiveEquitySnapshot strategy_id={self.strategy_id} "
                f"date={self.snapshot_date} equity={self.equity}>")