"""
live_universe_pipeline.py — nightly Norgate refresh for EXECUTION universes only.

Mirrors universe_pipeline.py exactly except:
  - reads LIVE_REGISTRY instead of REGISTRY
  - writes to backtest_data/live_universes/ instead of backtest_data/universes/
  - pulls only ~5 years of data (2021 → today) per spec.start_date

The static backtest universes (backtest_data/universes/) are NEVER touched
by this script. That preserves the frozen tradelists used for manager demos.
"""

from __future__ import annotations
import sys
from pathlib import Path

# Make app.* imports resolve when run as `python -m`
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.utiliy.universeGenerations.universe_pipeline import UniversePipeline
from app.utiliy.universeGenerations.storage import CsvDataStore
from app.utiliy.universeGenerations.live_universe_registry import LIVE_REGISTRY
from app.utiliy.synthetic_ticker_processor import (
    load_synthetics_from_db, SyntheticTickerProcessor,
)
from app.constants.PricePath import PricePath


if __name__ == '__main__':
    LIVE_FOLDER = PricePath.live_universes_root   # backtest_data/live_universes

    # Same synthetics loading as the static pipeline — LIVE_REGISTRY doesn't
    # currently declare any synthetics, but build the processor so future
    # additions don't blow up.
    processor = None
    try:
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            synthetics = load_synthetics_from_db(db)
        finally:
            db.close()
        if synthetics:
            processor = SyntheticTickerProcessor(synthetics)
            print(f'[live_pipeline] loaded {len(synthetics)} synthetic(s) from DB')
        else:
            print('[live_pipeline] synthetic_tickers table empty -> no processor needed')
    except Exception as e:
        print(f'[live_pipeline] synthetics not loaded ({type(e).__name__}: {e})')

    pipeline = UniversePipeline(
        store=CsvDataStore(LIVE_FOLDER),
        registry=LIVE_REGISTRY,         # ← uses live registry, not static REGISTRY
        num_of_cpus=10,
        synthetic_processor=processor,
    )
    pipeline.run()