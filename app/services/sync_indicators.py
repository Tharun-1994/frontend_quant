"""
sync_indicators.py
==================
Compares INDICATOR_REGISTRY against the database and inserts any missing rows.

SAFE TO RE-RUN AT ANY TIME — it never overwrites existing data:
  - indicator_definitions : content fields (descriptions, examples) are NEVER touched
                            if a row already exists. Only metadata fields are refreshed
                            (display_name, category, has_lookback, etc.).
  - indicator_availability: rows are inserted if missing, skipped if already present.

HOW TO RUN:
  Option A — on app startup (automatic):
      Call sync_indicators() from main.py after Base.metadata.create_all()

  Option B — manually from the command line:
      python -m app.services.sync_indicators

  Option C — via the admin API endpoint (Step 4):
      POST /api/admin/indicators/sync
"""

from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models.IndicatorDefinition import IndicatorDefinition, IndicatorAvailability
from app.constants.indicator_registry import INDICATOR_REGISTRY


# ─── Result container ────────────────────────────────────────────────────────

class SyncResult:
    def __init__(self):
        self.definitions_inserted  = []   # new indicator keys added
        self.definitions_refreshed = []   # existing keys whose metadata was updated
        self.availability_inserted = []   # (key, regime, section, side) rows added
        self.availability_skipped  = []   # rows already present
        self.orphaned_keys         = []   # keys in DB but not in registry (flagged only)
        self.errors                = []   # anything that went wrong

    def summary(self) -> str:
        lines = [
            "── Sync complete ──────────────────────────────────────────",
            f"  Definitions  inserted : {len(self.definitions_inserted)}",
            f"  Definitions  refreshed: {len(self.definitions_refreshed)}",
            f"  Availability inserted : {len(self.availability_inserted)}",
            f"  Availability skipped  : {len(self.availability_skipped)}",
        ]
        if self.orphaned_keys:
            lines.append(
                f"  Orphaned keys (in DB, not in registry): "
                f"{', '.join(self.orphaned_keys)}"
            )
        if self.definitions_inserted:
            lines.append(
                f"  NEW indicators (need descriptions): "
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
            "availability_inserted": [
                {"indicator_key": r[0], "regime_type": r[1],
                 "section": r[2], "side": r[3]}
                for r in self.availability_inserted
            ],
            "availability_skipped":  len(self.availability_skipped),
            "orphaned_keys":         self.orphaned_keys,
            "errors":                self.errors,
        }


# ─── Core sync function ───────────────────────────────────────────────────────

def sync_indicators(db: Session) -> SyncResult:
    """
    Main sync function. Pass any SQLAlchemy session.
    Returns a SyncResult with a full report of what changed.
    """
    result = SyncResult()

    # ── Step 1: Check for orphaned DB rows ───────────────────────────────────
    # Keys that exist in the DB but are no longer in the registry.
    # We flag them but never delete — data is never destroyed automatically.
    existing_keys = {
        row.indicator_key
        for row in db.query(IndicatorDefinition.indicator_key).all()
    }
    registry_keys = set(INDICATOR_REGISTRY.keys())
    result.orphaned_keys = sorted(existing_keys - registry_keys)

    # ── Step 2: Sync indicator_definitions ───────────────────────────────────
    for key, entry in INDICATOR_REGISTRY.items():
        try:
            existing = (
                db.query(IndicatorDefinition)
                .filter(IndicatorDefinition.indicator_key == key)
                .first()
            )

            if existing is None:
                # New indicator — insert with blank content fields.
                # A non-engineer fills these in via the admin page.
                new_row = IndicatorDefinition(
                    indicator_key        = key,
                    display_name         = entry["display_name"],
                    category             = entry["category"],
                    has_lookback         = entry["has_lookback"],
                    default_lookback     = entry["default_lookback"],
                    has_params           = entry["has_params"],
                    params_description   = entry["params_description"],
                    universe_restriction = entry["universe_restriction"],
                    caution_note         = entry["caution_note"],
                    sort_order           = entry["sort_order"],
                    # Content fields intentionally blank — filled via admin page
                    what_it_is           = None,
                    how_it_works         = None,
                    why_use_it           = None,
                    how_to_use_it        = None,
                    example_rule         = None,
                    example_explanation  = None,
                )
                db.add(new_row)
                result.definitions_inserted.append(key)

            else:
                # Existing indicator — refresh metadata only.
                # NEVER touch content fields (what_it_is, how_it_works, etc.)
                existing.display_name         = entry["display_name"]
                existing.category             = entry["category"]
                existing.has_lookback         = entry["has_lookback"]
                existing.default_lookback     = entry["default_lookback"]
                existing.has_params           = entry["has_params"]
                existing.params_description   = entry["params_description"]
                existing.universe_restriction = entry["universe_restriction"]
                existing.caution_note         = entry["caution_note"]
                existing.sort_order           = entry["sort_order"]
                result.definitions_refreshed.append(key)

        except Exception as e:
            result.errors.append(f"definitions[{key}]: {e}")

    # Flush definitions before inserting availability rows
    # so the indicator_key exists if any DB constraint fires.
    db.flush()

    # ── Step 3: Sync indicator_availability ──────────────────────────────────
    for key, entry in INDICATOR_REGISTRY.items():
        for avail_row in entry["availability"]:
            try:
                regime  = avail_row["regime_type"]
                section = avail_row["section"]
                side    = avail_row["side"]

                existing = (
                    db.query(IndicatorAvailability)
                    .filter(
                        IndicatorAvailability.indicator_key == key,
                        IndicatorAvailability.regime_type   == regime,
                        IndicatorAvailability.section       == section,
                        IndicatorAvailability.side          == side,
                    )
                    .first()
                )

                if existing is None:
                    new_avail = IndicatorAvailability(
                        indicator_key = key,
                        regime_type   = regime,
                        section       = section,
                        side          = side,
                        context_note  = avail_row.get("context_note"),
                        sort_order    = avail_row.get("sort_order", 99),
                    )
                    db.add(new_avail)
                    result.availability_inserted.append((key, regime, section, side))
                else:
                    # Row exists — refresh context_note and sort_order only
                    existing.context_note = avail_row.get("context_note")
                    existing.sort_order   = avail_row.get("sort_order", 99)
                    result.availability_skipped.append((key, regime, section, side))

            except Exception as e:
                result.errors.append(
                    f"availability[{key}|{regime}|{section}|{side}]: {e}"
                )

    # ── Step 4: Commit everything ─────────────────────────────────────────────
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
        print("Running indicator sync...")
        result = sync_indicators(db)
        print(result.summary())
        if result.definitions_inserted:
            print(
                "\nNext step: open /admin/indicators and fill in descriptions "
                f"for: {', '.join(result.definitions_inserted)}"
            )
    finally:
        db.close()


if __name__ == "__main__":
    run()