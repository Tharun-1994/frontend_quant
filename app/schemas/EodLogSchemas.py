from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime, date
from fastapi.encoders import jsonable_encoder


class EodRunLogOut(BaseModel):
    """
    One orchestrator step row. Used by the EOD Status page (Spec F4).
    """
    id: int
    run_date: date
    step: str
    strategy_id: Optional[int] = None
    retry_of: Optional[int] = None
    started_at: datetime
    finished_at: Optional[datetime] = None
    status: str
    rows_affected: Optional[int] = None
    error_msg: Optional[str] = None

    def to_dict(self):
        return jsonable_encoder(self)

    class Config:
        from_attributes = True


class RetryStepRequest(BaseModel):
    """
    Body for POST /api/eod/retry-step.

    Re-runs a single step that previously FAILED or TIMEOUT'd. Inserts a NEW
    eod_run_log row with retry_of pointing at the original; the original row
    is never overwritten.

    `strategy_id` is required for per-strategy steps (execution_step,
    overlay_apply, broker_write) and ignored for universe-level steps
    (universe_update, exec_data_refresh).
    """
    run_date: date
    step: str = Field(pattern="^(universe_update|exec_data_refresh|execution_step|"
                              "write_proposals|overlay_apply|broker_write)$")
    strategy_id: Optional[int] = None