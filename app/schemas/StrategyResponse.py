

from pydantic import BaseModel, Field
from datetime import date, datetime
from typing import Optional, List


class StrategyResponse(BaseModel):
    id: int
    strategy_name: str
    universe: str
    slots: Optional[int]
    capital: Optional[float]
    created_at: datetime










