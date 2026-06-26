"""
migrate_substitution_reason.py — add reason_for_action to substitution_overrides.

Idempotent — safe to re-run.

Usage:
    python -m app.constants.migrate_substitution_reason
"""
from sqlalchemy import text
from app.database import SessionLocal


def run():
    db = SessionLocal()
    try:
        exists = db.execute(text("""
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = 'substitution_overrides'
            AND COLUMN_NAME = 'reason_for_action'
        """)).scalar()

        if exists:
            print('[migrate_substitution_reason] reason_for_action already exists — skipping')
            return

        db.execute(text("""
            ALTER TABLE substitution_overrides
            ADD reason_for_action NVARCHAR(500) NULL
        """))
        db.commit()
        print('[migrate_substitution_reason] Added reason_for_action to substitution_overrides')
    finally:
        db.close()


if __name__ == '__main__':
    run()