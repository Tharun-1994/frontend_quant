"""
migrate_system_code.py — add system_code to strategies_bucket.

Idempotent — safe to re-run.
  - Skips ALTER if column already exists.
  - Does NOT backfill — system_code values are set manually via the UI
    or the set-system-code API endpoint after this migration runs.

Usage:
    python -m app.constants.migrate_system_code
"""
from sqlalchemy import text
from app.database import SessionLocal


def run():
    db = SessionLocal()
    try:
        # Check if column already exists (SQL Server)
        exists = db.execute(text("""
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = 'strategies_bucket'
            AND COLUMN_NAME = 'system_code'
        """)).scalar()

        if exists:
            print('[migrate_system_code] system_code column already exists — skipping')
            return

        db.execute(text("""
            ALTER TABLE strategies_bucket
            ADD system_code NVARCHAR(50) NULL
        """))
        db.commit()
        print('[migrate_system_code] Added system_code column to strategies_bucket')
        print('[migrate_system_code] Set values via the Strategy editor or:')
        print("  UPDATE strategies_bucket SET system_code = 'M_SDEQ_52' WHERE name = '<strategy_name>'")
    finally:
        db.close()


if __name__ == '__main__':
    run()