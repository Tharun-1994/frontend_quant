from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from fastapi.encoders import jsonable_encoder


class AccountRiskConfigOut(BaseModel):
    """
    GET /api/account-risk-config — singleton row.
    Drives the AccountRiskConfig editor page (Spec E3).
    """
    id: int
    account_capital: Optional[float] = None
    max_strategies_per_ticker: Optional[int] = None
    max_pct_per_ticker: Optional[float] = None
    max_pct_per_sector: Optional[float] = None
    execution_paused: bool
    updated_at: datetime
    updated_by: Optional[str] = None

    def to_dict(self):
        return jsonable_encoder(self)

    class Config:
        from_attributes = True


class AccountRiskConfigUpdate(BaseModel):
    """
    PATCH /api/account-risk-config — singleton row (id=1).
    All fields optional → partial update. The route bumps updated_at + updated_by.
    execution_paused requires a confirmation modal on the frontend.
    """
    account_capital: Optional[float] = None
    max_strategies_per_ticker: Optional[int] = None
    max_pct_per_ticker: Optional[float] = None
    max_pct_per_sector: Optional[float] = None
    execution_paused: Optional[bool] = None
    updated_by: Optional[str] = None


class StrategyExecutionUpdate(BaseModel):
    """
    PATCH /api/strategies/{id}/execution-config — fast-path for
    production_capital + execution_enabled inline edits on the strategy list.

    Full strategy save (with regimes etc.) goes through StrategyRequest as today;
    this dedicated route exists for inline edits without re-sending the whole
    strategy payload.

    The route writes:
      • strategies_bucket.production_capital + execution_enabled
      • strategies_bucket.last_capital_change_at + last_capital_change_by
      • appends a row to strategy_production_capital_history
    Validation: execution_enabled=TRUE requires production_capital > 0.
    """
    production_capital: Optional[float] = None
    execution_enabled: Optional[bool] = None
    reason: Optional[str] = None
    changed_by: Optional[str] = None