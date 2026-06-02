from pydantic import BaseModel
from typing import Optional, List


class AvailabilityOut(BaseModel):
    id: int
    indicator_key: str
    regime_type: str
    section: str
    side: str
    context_note: Optional[str] = None
    sort_order: int

    class Config:
        from_attributes = True


class IndicatorOut(BaseModel):
    id: int
    indicator_key: str
    display_name: str
    category: Optional[str] = None
    what_it_is: Optional[str] = None
    how_it_works: Optional[str] = None
    why_use_it: Optional[str] = None
    how_to_use_it: Optional[str] = None
    example_rule: Optional[str] = None
    example_explanation: Optional[str] = None
    has_lookback: bool
    default_lookback: Optional[int] = None
    has_params: bool
    params_description: Optional[str] = None
    universe_restriction: Optional[str] = None
    caution_note: Optional[str] = None
    sort_order: int
    is_complete: bool
    availability: List[AvailabilityOut] = []

    class Config:
        from_attributes = True


class IndicatorUpdateRequest(BaseModel):
    """
    Fields the admin page can update.
    Only content fields — structural metadata is controlled by indicator_registry.py.
    All fields are optional so partial saves work.
    """
    what_it_is: Optional[str] = None
    how_it_works: Optional[str] = None
    why_use_it: Optional[str] = None
    how_to_use_it: Optional[str] = None
    example_rule: Optional[str] = None
    example_explanation: Optional[str] = None