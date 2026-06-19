from sqlalchemy import Column, Integer, String, Numeric, Boolean, DateTime
from sqlalchemy.sql import func
from app.database import Base


class AccountRiskConfig(Base):
    """
    Singleton row (id=1) holding account-wide execution policy.

    All caps below are SOFT — they generate suggested_elide annotations on
    the morning Basket Review page but never block trader override.

    `execution_paused` is the only HARD lever — the global kill switch.
    When TRUE the nightly orchestrator skips ALL strategies for the night,
    writes a SKIPPED row to eod_run_log, and alerts.

    Seed row inserted by migration 001 with execution_paused=1 (parked).
    Trader unparks via the AccountRiskConfig editor page (Spec E3).
    """
    __tablename__ = "account_risk_config"

    id = Column(Integer, primary_key=True, default=1)
    # Always 1. Application layer enforces singleton.

    account_capital = Column(Numeric(18, 2), nullable=True)
    # Total account capital, used by the concentration annotator to compute
    # pct_of_book per ticker / per sector.

    max_strategies_per_ticker = Column(Integer, nullable=True)
    # NULL = no cap. Soft. Tags lowest-ranked overflow rows as suggested_elide.

    max_pct_per_ticker = Column(Numeric(5, 2), nullable=True)
    # NULL = no cap. Soft. Percent of account_capital.

    max_pct_per_sector = Column(Numeric(5, 2), nullable=True)
    # NULL = no cap. Soft. Percent of account_capital, by GICS sector.

    execution_paused = Column(Boolean, nullable=False, default=True)
    # HARD lever. Seeded TRUE on fresh installs so nothing auto-trades.

    updated_at = Column(DateTime, server_default=func.now(),
                        onupdate=func.now(), nullable=False)
    updated_by = Column(String(100), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "account_capital": float(self.account_capital) if self.account_capital is not None else None,
            "max_strategies_per_ticker": self.max_strategies_per_ticker,
            "max_pct_per_ticker": float(self.max_pct_per_ticker) if self.max_pct_per_ticker is not None else None,
            "max_pct_per_sector": float(self.max_pct_per_sector) if self.max_pct_per_sector is not None else None,
            "execution_paused": self.execution_paused,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "updated_by": self.updated_by,
        }

    def __repr__(self):
        return (f"<AccountRiskConfig(paused={self.execution_paused}, "
                f"capital={self.account_capital})>")