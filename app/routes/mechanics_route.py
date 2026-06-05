"""
mechanics_route.py
==================
Endpoints powering the Rule-info "Mechanics" view and its admin management page.

  GET  /api/mechanics/meta          — frontend: full registry as JSON (no DB)
  GET  /api/mechanics/groups        — frontend: canonical tab order (MECHANIC_GROUPS)
  GET  /api/mechanics               — public: filtered mechanic list (DB + merged registry)
  GET  /api/mechanics/{key}         — public: single mechanic full detail
  POST /api/mechanics/{key}         — admin:  save descriptions for one mechanic
  POST /api/admin/mechanics/sync    — admin:  re-run registry sync on demand
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db
from app.models.MechanicDefinition import MechanicDefinition
from app.schemas.Mechanicschemas import MechanicOut, MechanicUpdateRequest
from app.services.sync_mechanics import sync_mechanics
from app.constants.mechanic_registry import MECHANIC_REGISTRY, MECHANIC_GROUPS

router = APIRouter(prefix="/api", tags=["mechanics"])


# ── Helper: merge registry structural facts onto a mechanic row ──────────────

def _attach_meta(mechanic: MechanicDefinition) -> dict:
    """
    Returns the DB row as a dict with the registry's structural extras merged in
    (config_fields, option_values, applies_to_regimes). These are NOT stored in
    the DB — mechanic_registry.py is their single source of truth. This is the
    mechanics' analogue of _attach_availability for indicators.
    """
    data = mechanic.to_dict()
    entry = MECHANIC_REGISTRY.get(mechanic.mechanic_key, {})
    data["config_fields"]      = entry.get("config_fields", [])
    data["option_values"]      = entry.get("option_values", [])
    data["applies_to_regimes"] = entry.get("applies_to_regimes", [])
    return data


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/mechanics/meta
# ─────────────────────────────────────────────────────────────────────────────
# IMPORTANT: this route must be declared BEFORE /api/mechanics/{key}
# otherwise FastAPI matches "meta" as a key parameter.
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/mechanics/meta", summary="Full registry metadata for the React frontend")
def get_mechanics_meta():
    """
    Returns every mechanic's structural metadata as JSON.
    Read directly from MECHANIC_REGISTRY — no DB query needed.

    Used by the React Mechanics view to drive:
      - the group tabs (group)
      - the centralised option enums (option_values) — the same source the
        RegimeCard editor dropdowns can read
      - which schema fields each mechanic controls (config_fields)
      - which regimes expose it (applies_to_regimes)

    Adding a new mechanic only requires updating mechanic_registry.py.
    React picks it up automatically on next app start.
    """
    result = []
    for key, entry in MECHANIC_REGISTRY.items():
        result.append({
            "mechanic_key":       key,
            "display_name":       entry["display_name"],
            "group":              entry["group"],
            "config_fields":      entry["config_fields"],
            "option_values":      entry["option_values"],
            "applies_to_regimes": entry["applies_to_regimes"],
            "status":             entry["status"],
            "sort_order":         entry["sort_order"],
        })
    return result


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/mechanics/groups
# ─────────────────────────────────────────────────────────────────────────────
# Canonical tab order. Declared before /{key} for the same reason as /meta.
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/mechanics/groups", summary="Ordered list of mechanic groups (tab order)")
def get_mechanic_groups():
    """Returns MECHANIC_GROUPS in display order so the frontend tab bar is data-driven."""
    return MECHANIC_GROUPS


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/mechanics
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/mechanics", summary="List mechanics with optional filters")
def get_mechanics(
    group:           Optional[str] = Query(None),
    regime_type:     Optional[str] = Query(None),
    incomplete_only: bool          = Query(False),
    db: Session = Depends(get_db),
):
    query = db.query(MechanicDefinition)

    if group:
        query = query.filter(MechanicDefinition.group == group)

    if incomplete_only:
        query = query.filter(
            (MechanicDefinition.what_it_is == None) |
            (MechanicDefinition.how_it_works == None) |
            (MechanicDefinition.why_use_it == None) |
            (MechanicDefinition.how_to_use_it == None) |
            (MechanicDefinition.example_rule == None) |
            (MechanicDefinition.example_explanation == None)
        )

    mechanics = (
        query
        .order_by(MechanicDefinition.group, MechanicDefinition.sort_order)
        .all()
    )
    result = [_attach_meta(m) for m in mechanics]

    # regime_type filter uses applies_to_regimes from the registry (not a DB column),
    # so it is applied in Python after the merge.
    if regime_type:
        result = [r for r in result if regime_type in r["applies_to_regimes"]]

    return result


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/mechanics/{key}
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/mechanics/{key}", summary="Get full detail for one mechanic")
def get_mechanic(key: str, db: Session = Depends(get_db)):
    mechanic = (
        db.query(MechanicDefinition)
        .filter(MechanicDefinition.mechanic_key == key)
        .first()
    )
    if not mechanic:
        raise HTTPException(
            status_code=404,
            detail=f"Mechanic '{key}' not found. "
                   f"If this is a new mechanic, run POST /api/admin/mechanics/sync first."
        )
    return _attach_meta(mechanic)


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/mechanics/{key}
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/mechanics/{key}", summary="Save descriptions for one mechanic (admin)")
def update_mechanic(
    key: str,
    body: MechanicUpdateRequest,
    db: Session = Depends(get_db),
):
    mechanic = (
        db.query(MechanicDefinition)
        .filter(MechanicDefinition.mechanic_key == key)
        .first()
    )
    if not mechanic:
        raise HTTPException(status_code=404, detail=f"Mechanic '{key}' not found.")

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(mechanic, field, value)

    db.commit()
    db.refresh(mechanic)

    return {
        "success": True,
        "mechanic_key": key,
        "is_complete": mechanic.is_complete(),
        "updated_fields": list(update_data.keys()),
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/admin/mechanics/sync
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/admin/mechanics/sync", summary="Re-run mechanic registry sync (admin)")
def run_sync(db: Session = Depends(get_db)):
    result = sync_mechanics(db)
    return result.to_dict()