"""tradelist_run_journal — Patch 112: per-run state journal for revert.

Every Position Manager run journals, inside its own transaction:
  kind='PRE'     — one row per tradelist row the run MAY touch, with a JSON
                   snapshot of all runner-mutable fields taken BEFORE any step.
  kind='CREATED' — one row per tradelist row the run INSERTED
                   (PROPOSED / SUBSTITUTE_POOL), snapshot_json NULL.

Revert (run_revert.py) restores every PRE snapshot verbatim, deletes every
CREATED row, and marks the eod_run_log row REVERTED. Because the runner uses
a single work transaction, journal and changes commit or roll back together —
a FAILED run leaves nothing to revert.

The journal doubles as a state-change audit trail: for any tradelist row you
can read back what each nightly run saw before it acted.
"""
from sqlalchemy import (
    Column, Integer, String, Date, DateTime, Text, ForeignKey
)
from sqlalchemy.sql import func
from app.database import Base


class TradelistRunJournal(Base):
    __tablename__ = "tradelist_run_journal"

    id = Column(Integer, primary_key=True, index=True)

    eod_run_log_id = Column(Integer, ForeignKey("eod_run_log.id"),
                            nullable=False, index=True)
    strategy_id = Column(Integer, nullable=False, index=True)
    run_date = Column(Date, nullable=False)

    # NULL only never — CREATED rows also reference the inserted tradelist id.
    tradelist_id = Column(Integer, nullable=False)

    kind = Column(String(10), nullable=False)   # 'PRE' | 'CREATED'

    # JSON dict of runner-mutable fields (PRE) — see run_revert.SNAPSHOT_FIELDS.
    snapshot_json = Column(Text, nullable=True)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    def __repr__(self):
        return (f"<TradelistRunJournal(run_log={self.eod_run_log_id}, "
                f"tradelist_id={self.tradelist_id}, kind='{self.kind}')>")