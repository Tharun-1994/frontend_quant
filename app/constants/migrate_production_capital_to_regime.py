"""
One-time migration (Patch 48 / step 1): add marketregime.production_capital
and backfill it from the strategy-level strategies_bucket.production_capital.

Why: production_capital is moving from the strategy to the market regime so
each regime can carry its own live sizing (regime.capital + regime.slots are
already per-regime). This script only ADDS + BACKFILLS the new column; it does
NOT drop strategies_bucket.production_capital — that strategy-level field stays
authoritative until the wiring + UI move land (steps 3-6). Backtest sizing on
regime.capital is untouched.

DDL is included because this is a brand-new column and main.py's create_all
will not ALTER an existing SQL Server table to add it.

Idempotent — safe to re-run:
  - skips the ALTER if the column already exists
  - backfills only regimes whose production_capital IS NULL (won't clobber
    values a user has since set per-regime)

Usage (PyCharm Run, or):
    python -m app.constants.migrate_production_capital_to_regime
"""
from sqlalchemy import text

from app.database import SessionLocal


def run():
    db = SessionLocal()
    try:
        # 1. Add the column if it isn't there yet (SQL Server).
        exists = db.execute(text(
            "SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = 'marketregime' "
            "AND COLUMN_NAME = 'production_capital'"
        )).fetchone()

        if exists:
            print("[migrate] marketregime.production_capital already present — skip ALTER")
        else:
            db.execute(text(
                "ALTER TABLE marketregime "
                "ADD production_capital DECIMAL(18,2) NULL"
            ))
            db.commit()
            print("[migrate] added column marketregime.production_capital")

        # 2. Backfill from the strategy-level field. Only fill NULLs so a
        #    re-run never overwrites a per-regime value set later.
        result = db.execute(text(
            "UPDATE mr "
            "SET mr.production_capital = sb.production_capital "
            "FROM marketregime mr "
            "INNER JOIN strategies_bucket sb ON mr.strategy_id = sb.id "
            "WHERE mr.production_capital IS NULL "
            "AND sb.production_capital IS NOT NULL"
        ))
        db.commit()
        print(f"[migrate] backfilled production_capital on {result.rowcount} regime row(s) "
              f"from their strategy")
    finally:
        db.close()


if __name__ == "__main__":
    run()