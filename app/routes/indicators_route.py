"""
indicators.py
=============
Four endpoints powering the Indicators page and the admin management page.

  GET  /api/indicators/meta            — frontend: full registry as JSON (no DB)
  GET  /api/indicators                 — public: filtered indicator list
  GET  /api/indicators/{key}           — public: single indicator full detail
  POST /api/indicators/{key}           — admin:  save descriptions for one indicator
  POST /api/admin/indicators/sync      — admin:  re-run registry sync on demand
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional

from app.database import get_db
from app.models.IndicatorDefinition import IndicatorDefinition, IndicatorAvailability
from app.schemas.Indicatorschemas import IndicatorOut, IndicatorUpdateRequest
from app.services.sync_indicators import sync_indicators
from app.constants.indicator_registry import INDICATOR_REGISTRY

router = APIRouter(prefix="/api", tags=["indicators"])


# ── Helper: attach availability rows to an indicator ─────────────────────────

def _attach_availability(indicator: IndicatorDefinition, db: Session) -> dict:
    data = indicator.to_dict()
    avail_rows = (
        db.query(IndicatorAvailability)
        .filter(IndicatorAvailability.indicator_key == indicator.indicator_key)
        .order_by(IndicatorAvailability.section, IndicatorAvailability.sort_order)
        .all()
    )
    data["availability"] = [r.to_dict() for r in avail_rows]
    return data


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/indicators/meta
# ─────────────────────────────────────────────────────────────────────────────
# IMPORTANT: this route must be declared BEFORE /api/indicators/{key}
# otherwise FastAPI matches "meta" as a key parameter.
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/indicators/meta", summary="Full registry metadata for the React frontend")
def get_indicators_meta():
    """
    Returns every indicator's structural metadata as JSON.
    Read directly from INDICATOR_REGISTRY — no DB query needed.

    Used by the React IndicatorRegistry context to drive:
      - Indicator dropdowns (display_name per regime/section/side)
      - Lookback field visibility (has_lookback)
      - Param input rendering (params array)
      - Boolean indicator detection (kind == "boolean")
      - Range field visibility (has_range)

    Adding a new indicator only requires updating indicator_registry.py.
    React picks it up automatically on next app start.
    """
    result = []
    for key, entry in INDICATOR_REGISTRY.items():
        result.append({
            "indicator_key":        key,
            "display_name":         entry["display_name"],
            "category":             entry["category"],
            "has_lookback":         entry["has_lookback"],
            "default_lookback":     entry["default_lookback"],
            "has_params":           entry["has_params"],
            "params":               entry.get("params", []),
            "kind":                 entry.get("kind", None),
            "has_range":            entry.get("has_range", False),
            "universe_restriction": entry["universe_restriction"],
            "caution_note":         entry["caution_note"],
            "sort_order":           entry["sort_order"],
            "availability":         entry["availability"],
        })
    return result


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/indicators
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/indicators", summary="List indicators with optional filters")
def get_indicators(
    regime_type:  Optional[str] = Query(None),
    section:      Optional[str] = Query(None),
    side:         Optional[str] = Query(None),
    category:     Optional[str] = Query(None),
    incomplete_only: bool       = Query(False),
    db: Session = Depends(get_db),
):
    query = db.query(IndicatorDefinition)

    if regime_type or section or side:
        avail_query = db.query(IndicatorAvailability.indicator_key)
        if regime_type:
            avail_query = avail_query.filter(
                IndicatorAvailability.regime_type == regime_type
            )
        if section:
            avail_query = avail_query.filter(
                IndicatorAvailability.section == section
            )
        if side:
            avail_query = avail_query.filter(
                IndicatorAvailability.side == side
            )
        matched_keys = [row.indicator_key for row in avail_query.distinct().all()]
        query = query.filter(IndicatorDefinition.indicator_key.in_(matched_keys))

    if category:
        query = query.filter(IndicatorDefinition.category == category)

    if incomplete_only:
        query = query.filter(
            (IndicatorDefinition.what_it_is == None) |
            (IndicatorDefinition.how_it_works == None) |
            (IndicatorDefinition.why_use_it == None) |
            (IndicatorDefinition.how_to_use_it == None) |
            (IndicatorDefinition.example_rule == None) |
            (IndicatorDefinition.example_explanation == None)
        )

    indicators = (
        query
        .order_by(IndicatorDefinition.category, IndicatorDefinition.sort_order)
        .all()
    )
    return [_attach_availability(ind, db) for ind in indicators]


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/indicators/{key}
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/indicators/{key}", summary="Get full detail for one indicator")
def get_indicator(key: str, db: Session = Depends(get_db)):
    indicator = (
        db.query(IndicatorDefinition)
        .filter(IndicatorDefinition.indicator_key == key)
        .first()
    )
    if not indicator:
        raise HTTPException(
            status_code=404,
            detail=f"Indicator '{key}' not found. "
                   f"If this is a new indicator, run POST /api/admin/indicators/sync first."
        )
    return _attach_availability(indicator, db)


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/indicators/{key}
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/indicators/{key}", summary="Save descriptions for one indicator (admin)")
def update_indicator(
    key: str,
    body: IndicatorUpdateRequest,
    db: Session = Depends(get_db),
):
    indicator = (
        db.query(IndicatorDefinition)
        .filter(IndicatorDefinition.indicator_key == key)
        .first()
    )
    if not indicator:
        raise HTTPException(status_code=404, detail=f"Indicator '{key}' not found.")

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(indicator, field, value)

    db.commit()
    db.refresh(indicator)

    return {
        "success": True,
        "indicator_key": key,
        "is_complete": indicator.is_complete(),
        "updated_fields": list(update_data.keys()),
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/admin/indicators/sync
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/admin/indicators/sync", summary="Re-run indicator registry sync (admin)")
def run_sync(db: Session = Depends(get_db)):
    result = sync_indicators(db)
    return result.to_dict()