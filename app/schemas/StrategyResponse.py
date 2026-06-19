

from pydantic import BaseModel, Field
from datetime import date, datetime
from typing import Optional, List


class StrategyResponse(BaseModel):
    id: int
    name: str
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    rebalance: Optional[str] = None
    created_at: Optional[datetime] = None
    min_price: Optional[float] = None
    min_quantity: Optional[float] = None
    system_type: Optional[str] = None
    market_regime_type: Optional[str] = None
    execution_enabled: Optional[bool] = False

    class Config:
        from_attributes = True










