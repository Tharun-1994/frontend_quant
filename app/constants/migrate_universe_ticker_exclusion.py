"""
migrate_universe_ticker_exclusion.py — create universe_ticker_exclusion table
and seed it with the current hardcoded exclusion list.

Idempotent — safe to re-run.

Usage:
    python -m app.constants.migrate_universe_ticker_exclusion
"""
from sqlalchemy import text
from app.database import SessionLocal

# Current hardcoded list from synthetic_ticker_processor.py — seeded on creation.
INITIAL_EXCLUSIONS = [
    ('GOOG',  'dual-class share — use GOOGL instead'),
    ('EA',    'manual exclusion'),
    ('TRUE',  'manual exclusion'),
    ('EXAS',  'manual exclusion'),
    ('ABT',   'manual exclusion'),
    ('IBM',   'manual exclusion'),
    ('CFLT',  'manual exclusion'),
    ('SPXC',  'manual exclusion'),
    ('WBD',   'manual exclusion'),
    ('NFLX',  'manual exclusion'),
    ('PSKY',  'manual exclusion'),
    ('HOLX',  'manual exclusion'),
    ('RVMD',  'manual exclusion'),
    ('VTYX',  'manual exclusion'),
    ('OS',    'manual exclusion'),
    ('ALGT',  'manual exclusion'),
    ('SNCY',  'manual exclusion'),
    ('IONQ',  'manual exclusion'),
    ('SKYT',  'manual exclusion'),
    ('FOX',   'dual-class share'),
    ('LBRDA', 'manual exclusion'),
    ('ZG',    'dual-class share — use Z instead'),
    ('NUVL',  'manual exclusion'),
]


def run():
    db = SessionLocal()
    try:
        # Create table if not exists
        exists = db.execute(text("""
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_NAME = 'universe_ticker_exclusion'
        """)).scalar()

        if not exists:
            db.execute(text("""
                CREATE TABLE universe_ticker_exclusion (
                    id       INT IDENTITY(1,1) PRIMARY KEY,
                    ticker   NVARCHAR(20)  NOT NULL,
                    reason   NVARCHAR(200) NULL,
                    added_by NVARCHAR(100) NULL,
                    added_at DATETIME      NOT NULL DEFAULT GETDATE(),
                    active   BIT           NOT NULL DEFAULT 1,
                    CONSTRAINT uq_universe_ticker UNIQUE (ticker)
                )
            """))
            db.commit()
            print('[migrate_exclusions] Created universe_ticker_exclusion table')
        else:
            print('[migrate_exclusions] Table already exists — seeding only missing tickers')

        # Seed initial exclusions (skip if already present)
        seeded = 0
        for ticker, reason in INITIAL_EXCLUSIONS:
            already = db.execute(text(
                "SELECT COUNT(*) FROM universe_ticker_exclusion WHERE ticker = :t"
            ), {'t': ticker}).scalar()
            if not already:
                db.execute(text(
                    "INSERT INTO universe_ticker_exclusion (ticker, reason, added_by) "
                    "VALUES (:t, :r, 'system_migration')"
                ), {'t': ticker, 'r': reason})
                seeded += 1

        db.commit()
        print(f'[migrate_exclusions] Seeded {seeded} ticker(s)')
        print('[migrate_exclusions] Done')
    except Exception as e:
        db.rollback()
        print(f'[migrate_exclusions] FAILED: {e}')
        raise
    finally:
        db.close()


if __name__ == '__main__':
    run()