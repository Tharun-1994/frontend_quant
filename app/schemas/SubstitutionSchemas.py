from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime, date
from fastapi.encoders import jsonable_encoder


class SubstitutionOverrideOut(BaseModel):
    """
    One trader override action read back. Used by the Basket Review page
    audit panel and the future trader-history view.

    SQL column is `override_action`; Python attribute and API field are `action`
    via the SQLAlchemy rename trick (matches MechanicDefinition.group → mechanic_group).
    """
    id: int
    strategy_id: int
    override_date: date
    version: int
    original_symbol: str
    substitute_symbol: Optional[str] = None
    action: str
    adjusted_capital: Optional[float] = None
    csv_source_path: Optional[str] = None
    uploaded_at: datetime
    uploaded_by: Optional[str] = None

    def to_dict(self):
        return jsonable_encoder(self)

    class Config:
        from_attributes = True


class SubstitutionOverrideCreate(BaseModel):
    """
    Input shape for inserting an override. Used by:
      • the substitution.csv parser (Spec D1)
      • a future webapp Basket Review action

    `action` must be one of: 'elide' | 'substitute' | 'adjust_capital' | 'half_size'.
    `substitute_symbol` is required for action='substitute' (validated route-side
    and must exist in tonight's SUBSTITUTE_POOL for the strategy).
    `adjusted_capital` is required for action='adjust_capital'.
    """
    strategy_id: int
    override_date: date
    original_symbol: str
    action: str = Field(pattern="^(elide|substitute|adjust_capital|half_size)$")
    substitute_symbol: Optional[str] = None
    adjusted_capital: Optional[float] = None
    csv_source_path: Optional[str] = None
    uploaded_by: Optional[str] = None