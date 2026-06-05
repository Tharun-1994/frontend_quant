"""
sync_mechanics.py
=================
Compares MECHANIC_REGISTRY against the database and inserts any missing rows.

SAFE TO RE-RUN AT ANY TIME — it never overwrites prose:
  - mechanic_definitions : content fields (what_it_is, how_it_works, why_use_it,
                           how_to_use_it, example_rule, example_explanation,
                           params_description, caution_note) are NEVER touched
                           if a row already exists. Only structural metadata is
                           refreshed (display_name, group, status, sort_order).

Mechanics have no availability table — they are regime config, not rule atoms —
so this is the indicator-sync pattern minus the availability step. The structural
extras (config_fields, option_values, applies_to_regimes) are NOT stored in the
DB; the route merges them from MECHANIC_REGISTRY at request time.

HOW TO RUN:
  Option A — on app startup (automatic):
      Call sync_mechanics() from main.py after Base.metadata.create_all()
  Option B — manually from the command line:
      python -m app.services.sync_mechanics
  Option C — via the admin API endpoint (Step 6):
      POST /api/admin/mechanics/sync
"""

from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models.MechanicDefinition import MechanicDefinition
from app.constants.mechanic_registry import MECHANIC_REGISTRY


# ─── Result container ────────────────────────────────────────────────────────

class SyncResult:
    def __init__(self):
        self.definitions_inserted  = []   # new mechanic keys added
        self.definitions_refreshed = []   # existing keys whose metadata was refreshed
        self.orphaned_keys         = []   # keys in DB but not in registry (flagged only)
        self.errors                = []   # anything that went wrong

    def summary(self) -> str:
        lines = [
            "── Mechanic sync complete ─────────────────────────────────",
            f"  Definitions inserted : {len(self.definitions_inserted)}",
            f"  Definitions refreshed: {len(self.definitions_refreshed)}",
        ]
        if self.orphaned_keys:
            lines.append(
                f"  Orphaned keys (in DB, not in registry): "
                f"{', '.join(self.orphaned_keys)}"
            )
        if self.definitions_inserted:
            lines.append(
                f"  NEW mechanics (need descriptions): "
                f"{', '.join(self.definitions_inserted)}"
            )
        if self.errors:
            lines.append("  ERRORS:")
            for e in self.errors:
                lines.append(f"    - {e}")
        lines.append("───────────────────────────────────────────────────────")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "definitions_inserted":  self.definitions_inserted,
            "definitions_refreshed": self.definitions_refreshed,
            "orphaned_keys":         self.orphaned_keys,
            "errors":                self.errors,
        }


# ─── Core sync function ───────────────────────────────────────────────────────

def sync_mechanics(db: Session) -> SyncResult:
    """
    Main sync function. Pass any SQLAlchemy session.
    Returns a SyncResult with a full report of what changed.
    Writes structural metadata only; prose is never created or overwritten here.
    """
    result = SyncResult()

    # ── Step 1: Check for orphaned DB rows ───────────────────────────────────
    # Keys that exist in the DB but are no longer in the registry.
    # We flag them but never delete — data is never destroyed automatically.
    existing_keys = {
        row.mechanic_key
        for row in db.query(MechanicDefinition.mechanic_key).all()
    }
    registry_keys = set(MECHANIC_REGISTRY.keys())
    result.orphaned_keys = sorted(existing_keys - registry_keys)

    # ── Step 2: Sync mechanic_definitions ────────────────────────────────────
    for key, entry in MECHANIC_REGISTRY.items():
        try:
            existing = (
                db.query(MechanicDefinition)
                .filter(MechanicDefinition.mechanic_key == key)
                .first()
            )

            if existing is None:
                # New mechanic — insert structural metadata, blank prose.
                # A non-engineer fills the prose via seed/admin.
                new_row = MechanicDefinition(
                    mechanic_key = key,
                    display_name = entry["display_name"],
                    group        = entry["group"],
                    status       = entry["status"],
                    sort_order   = entry["sort_order"],
                    # Content fields intentionally blank — filled via seed/admin
                    what_it_is          = None,
                    how_it_works        = None,
                    why_use_it          = None,
                    how_to_use_it       = None,
                    example_rule        = None,
                    example_explanation = None,
                    params_description  = None,
                    caution_note        = None,
                )
                db.add(new_row)
                result.definitions_inserted.append(key)

            else:
                # Existing mechanic — refresh structural metadata only.
                # NEVER touch prose (what_it_is … caution_note).
                existing.display_name = entry["display_name"]
                existing.group        = entry["group"]
                existing.status       = entry["status"]
                existing.sort_order   = entry["sort_order"]
                result.definitions_refreshed.append(key)

        except Exception as e:
            result.errors.append(f"definitions[{key}]: {e}")

    # ── Step 3: Commit ────────────────────────────────────────────────────────
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        result.errors.append(f"commit failed: {e}")

    return result


# ─── Entry point for manual CLI run ─────────────────────────────────────────

def run():
    db = SessionLocal()
    try:
        print("Running mechanic sync...")
        result = sync_mechanics(db)
        print(result.summary())
        if result.definitions_inserted:
            print(
                "\nNext step: fill descriptions (seed_mechanic_descriptions.py "
                f"or the admin page) for: {', '.join(result.definitions_inserted)}"
            )
    finally:
        db.close()


if __name__ == "__main__":
    run()