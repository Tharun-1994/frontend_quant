from sqlalchemy import (
    Column, Integer, String, DateTime, Date,
    ForeignKey, Text
)
from sqlalchemy.sql import func
from app.database import Base


class EodRunLog(Base):
    """
    Audit row per orchestrator step per nightly run.

    The orchestrator writes a row when entering each step (status='RUNNING')
    and updates the same row to 'SUCCESS' | 'FAILED' | 'TIMEOUT' | 'SKIPPED'
    on completion. If a step crashes, status stays 'RUNNING' with NULL
    finished_at — that's how retry knows where to resume.

    Step granularity:
      • Universe-level steps (universe_update, exec_data_refresh) write one
        row each, strategy_id NULL.
      • Per-strategy steps (execution_step, overlay_apply, broker_write)
        write one row per strategy, strategy_id populated.

    retry_of self-FK chains a retry attempt to the original failed row.
    Both rows persist for audit.
    """
    __tablename__ = "eod_run_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_date = Column(Date, nullable=False)
    # The market day D the proposals target (NOT the calendar day the cron
    # fired). Monday-night's run for Tuesday's basket: run_date = Tuesday.

    step = Column(String(50), nullable=False)
    # 'universe_update' | 'exec_data_refresh' | 'execution_step' |
    # 'write_proposals' | 'overlay_apply' | 'broker_write'

    strategy_id = Column(Integer, ForeignKey("strategies_bucket.id"),
                         nullable=True)
    # NULL for universe-scope steps.

    retry_of = Column(Integer, ForeignKey("eod_run_log.id"), nullable=True)
    # Self-FK. NULL for first attempts; populated when this row is a retry
    # of an earlier FAILED/TIMEOUT row.

    started_at = Column(DateTime, server_default=func.now(), nullable=False)
    finished_at = Column(DateTime, nullable=True)

    status = Column(String(20), nullable=False, default="RUNNING")
    # 'RUNNING' | 'SUCCESS' | 'FAILED' | 'TIMEOUT' | 'SKIPPED'

    rows_affected = Column(Integer, nullable=True)
    # Step-specific tally: rows inserted, parquets refreshed, etc.

    error_msg = Column(Text, nullable=True)
    # Populated when status='FAILED'. Full stack trace OK.

    def __repr__(self):
        return (f"<EodRunLog(run_date={self.run_date}, step='{self.step}', "
                f"strategy_id={self.strategy_id}, status='{self.status}')>")