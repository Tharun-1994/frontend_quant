"""
One-time migration: convert legacy safety_net_type scalar + freeze/resume
fields into the new safety_nets_json list shape.

Run once after Patches A1-A5 are deployed. Idempotent — safe to re-run.

Usage:
    python -m scripts.migrate_safety_nets_to_list
"""
import json

from app.database import SessionLocal
# adjust import to match your project
from app.models.market_regime import MarketRegime

def run():
    db = SessionLocal()
    try:
        rows = db.query(MarketRegime).all()
        migrated, skipped = 0, 0
        for r in rows:
            # Skip rows that already have a non-empty list
            if r.safety_nets_json:
                skipped += 1
                continue

            type_ = (r.safety_net_type or "none").lower()
            if type_ == "none":
                # No safety net active. Leave safety_nets_json NULL.
                continue

            if type_ == "simple":
                # Wrap the existing freeze/resume trees + timings into one item.
                params = {
                    "freeze_rules_tree":  json.loads(r.freeze_rules_tree_json)  if r.freeze_rules_tree_json  else None,
                    "resume_rules_tree":  json.loads(r.resume_rules_tree_json)  if r.resume_rules_tree_json  else None,
                    "freeze_timing":      r.freeze_timing or "open",
                    "resume_timing":      r.resume_timing or "open",
                }
                r.safety_nets_json = json.dumps([{"type": "simple", "params": params}])
                migrated += 1

            elif type_ == "spy_volatility":
                # Stage 3c hasn't shipped yet — leave params empty,
                # user will fill them in via the new UI.
                r.safety_nets_json = json.dumps([{"type": "spy_volatility", "params": {}}])
                migrated += 1

        db.commit()
        print(f"safety_nets migration: {migrated} migrated, {skipped} already had list")
    finally:
        db.close()

if __name__ == "__main__":
    run()