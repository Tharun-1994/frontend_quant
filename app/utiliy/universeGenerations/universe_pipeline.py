"""
universe_pipeline.py

Folder A. Iterates the universe registry and refreshes each one with a full
re-pull (1998 -> end_date, overwrite). A new slug in the registry with no folder
yet is created automatically by the store. This is the source of truth for
backtesting and execution.

The Norgate-post guard: Norgate posts the current session ~22:30. If this runs
for "today" before that, end_date is rolled back to the previous trading day so
a half / empty session is never written.
"""

from __future__ import annotations
import sys
from pathlib import Path
import datetime as dt

import pandas_market_calendars as mcal

from app.utiliy.synthetic_ticker_processor import load_synthetics_from_db, SyntheticTickerProcessor
from app.utiliy.universeGenerations.storage import DataStore, CsvDataStore
from app.utiliy.universeGenerations.universe_registry import REGISTRY
from app.utiliy.universeGenerations.daily_data_generator import DailyDataGenerator


class UniversePipeline:


    def __init__(self, store: DataStore, registry=REGISTRY, num_of_cpus=6,
                 norgate_post_hour=22, synthetic_processor=None):
        self.store = store
        self.registry = registry
        self.generator = DailyDataGenerator(num_of_cpus=num_of_cpus)
        self.norgate_post_hour = norgate_post_hour
        # LRA Patch 15: optional SyntheticTickerProcessor. None = legacy behaviour
        # (no synthetic computation; pipeline behaves exactly as before Patch 15).
        self.synthetic_processor = synthetic_processor

    def run(self, end_date=None, only=None):
        end_date = self._resolve_end_date(end_date)
        specs = [s for s in self.registry if only is None or s.slug in only]

        # LRA Patch 15: fail fast if a spec declares synthetics but no processor wired.
        needs_synthetics = any(getattr(s, 'synthetics', None) for s in specs)
        if needs_synthetics and self.synthetic_processor is None:
            raise RuntimeError(
                'A UniverseSpec declares synthetics but UniversePipeline was '
                'constructed without a synthetic_processor. Build one via '
                'load_synthetics_from_db(session) and pass it to UniversePipeline.'
            )

        for spec in specs:
            action = 'update' if self.store.has_universe(spec.slug) else 'create'
            print(f'[pipeline] {action}: {spec.slug} (-> {end_date})')
            dataset = self.generator.generate(spec, end_date)

            # LRA Patch 15: apply synthetics + drop sources between generate and write.
            # Silently skipped for specs that don't declare synthetics.
            spec_synth = getattr(spec, 'synthetics', None)
            if self.synthetic_processor and spec_synth:
                self.synthetic_processor.apply(
                    dataset,
                    symbols=spec_synth,
                    drop_sources=getattr(spec, 'drop_source_tickers', None),
                )
                # Manifest num_tickers was stamped pre-synthetic; refresh from Close.
                close_df = dataset.fields.get('Close')
                if close_df is not None:
                    dataset.manifest['num_tickers'] = close_df.shape[1]
                print(f'[pipeline] {spec.slug}: applied synthetics {spec_synth}, '
                      f'dropped {getattr(spec, "drop_source_tickers", None) or []}')

            self.store.write_universe(dataset)
            print(f'[pipeline] {spec.slug}: '
                  f'{dataset.manifest["num_tickers"]} tickers, '
                  f'last bar {dataset.manifest["last_data_date"]}')

    def _resolve_end_date(self, end_date):
        today = dt.date.today()
        if end_date is None:
            end_date = today
        if end_date >= today and dt.datetime.now().hour < self.norgate_post_hour:
            end_date = self._previous_trading_day(today)
            print(f'[pipeline] before Norgate post hour -> end_date {end_date}')
        return end_date

    @staticmethod
    def _previous_trading_day(ref):
        nyse = mcal.get_calendar('NYSE')
        valid = nyse.valid_days(ref - dt.timedelta(days=10), ref).tz_localize(None)
        prior = [d.date() for d in valid if d.date() < ref]
        return prior[-1] if prior else ref - dt.timedelta(days=1)


if __name__ == '__main__':
    FOLDER_A = r'C:\Tharun\Projects\backtest_data\universes'

    # LRA Patch 15: load synthetic_tickers from DB and build the processor.
    # Make app.* imports resolve when this file is executed as a standalone script
    # (by default Python only puts this file's directory on sys.path).

    _PROJECT_ROOT = Path(__file__).resolve().parents[3]   # .../frontend_quant
    if str(_PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(_PROJECT_ROOT))



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
            print(f'[pipeline] loaded {len(synthetics)} synthetic(s) from DB: '
                  f'{[s.symbol for s in synthetics]}')
        else:
            print('[pipeline] synthetic_tickers table empty -> no processor needed')
    except Exception as e:
        print(f'[pipeline] synthetics not loaded ({type(e).__name__}: {e}). '
              f'Run will raise if any spec declares synthetics.')

    pipeline = UniversePipeline(
        store=CsvDataStore(FOLDER_A),
        num_of_cpus=10,
        synthetic_processor=processor,
    )

    # First verification run: one universe, diff against your existing folder.
    pipeline.run(only={'liquid500'})

    # Nightly (Task Scheduler): refresh the whole registry.
    # pipeline.run(only={'russell3000'})
