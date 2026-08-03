from pydantic import BaseModel
from typing import Optional, List


class OptionValueOut(BaseModel):
    """
    One selectable option for a mechanic's dropdown field.
    Mirror of AvailabilityOut for indicators — merged into the response from
    MECHANIC_REGISTRY by the route (not stored in the DB).
    """
    field: str          # which config field this option belongs to, e.g. "stoploss_type"
    value: str          # the value persisted on the strategy, e.g. "ATR_BASED"
    label: str          # human-readable label shown in the UI

    class Config:
        from_attributes = True


class MechanicOut(BaseModel):
    """
    Full mechanic detail returned to the frontend.

    The first block (id … is_complete) comes straight from the DB row
    (MechanicDefinition.to_dict()). The second block (config_fields,
    option_values, applies_to_regimes) is merged in by the route from
    mechanic_registry.py — it is NOT stored in the database.
    """
    id: int
    mechanic_key: str
    display_name: str
    group: Optional[str] = None
    what_it_is: Optional[str] = None
    how_it_works: Optional[str] = None
    why_use_it: Optional[str] = None
    how_to_use_it: Optional[str] = None
    example_rule: Optional[str] = None
    example_explanation: Optional[str] = None
    params_description: Optional[str] = None
    caution_note: Optional[str] = None
    status: str
    sort_order: int
    is_complete: bool

    # ── Merged from mechanic_registry.py by the route (not in the DB) ─────────
    config_fields: List[str] = []
    option_values: List[OptionValueOut] = []
    applies_to_regimes: List[str] = []

    class Config:
        from_attributes = True


class MechanicUpdateRequest(BaseModel):
    """
    Fields the admin page can update for one mechanic.
    Only content (prose) fields — structural metadata (group, status, sort_order)
    and the option enums are controlled by mechanic_registry.py.
    All fields are optional so partial saves work.

    NOTE: unlike IndicatorUpdateRequest, params_description and caution_note ARE
    editable here, because for mechanics they are prose stored in the DB rather
    than registry-controlled metadata.
    """
    what_it_is: Optional[str] = None
    how_it_works: Optional[str] = None
    why_use_it: Optional[str] = None
    how_to_use_it: Optional[str] = None
    example_rule: Optional[str] = None
    example_explanation: Optional[str] = None
    params_description: Optional[str] = None
    caution_note: Optional[str] = None