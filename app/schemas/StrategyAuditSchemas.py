from pydantic import BaseModel
from typing import Optional
from datetime import datetime, date
from fastapi.encoders import jsonable_encoder


class StrategyProductionCapitalHistoryOut(BaseModel):
    """
    One capital-change audit row. Used by the Capital History tab on
    Strategy Detail (Spec E2).
    """
    id: int
    strategy_id: int
    # Patch 53: NULL for legacy strategy-level rows; populated for regime-level.
    regime_id: Optional[int] = None
    old_capital: Optional[float] = None
    new_capital: float
    changed_at: datetime
    changed_by: Optional[str] = None
    reason: Optional[str] = None

    def to_dict(self):
        return jsonable_encoder(self)

    class Config:
        from_attributes = True


class TraderObservationOut(BaseModel):
    """
    One free-floating observation. Used by the Operator dashboard /
    Strategy Detail observations panel.

    For per-position notes see Tradelist.trader_notes (different surface).
    """
    id: int
    strategy_id: Optional[int] = None
    observation_date: date
    note: str
    created_at: datetime
    created_by: Optional[str] = None

    def to_dict(self):
        return jsonable_encoder(self)

    class Config:
        from_attributes = True


class TraderObservationCreate(BaseModel):
    """
    POST /api/trader-observations — create a new observation.
    strategy_id NULL = account-wide.
    """
    strategy_id: Optional[int] = None
    observation_date: date
    note: str
    created_by: Optional[str] = None