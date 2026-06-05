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

import datetime as dt

import pandas_market_calendars as mcal

from storage import DataStore, CsvDataStore
from universe_registry import REGISTRY
from daily_data_generator import DailyDataGenerator


class UniversePipeline:

    def __init__(self, store: DataStore, registry=REGISTRY, num_of_cpus=6,
                 norgate_post_hour=22):
        self.store = store
        self.registry = registry
        self.generator = DailyDataGenerator(num_of_cpus=num_of_cpus)
        self.norgate_post_hour = norgate_post_hour

    def run(self, end_date=None, only=None):
        end_date = self._resolve_end_date(end_date)
        specs = [s for s in self.registry if only is None or s.slug in only]

        for spec in specs:
            action = 'update' if self.store.has_universe(spec.slug) else 'create'
            print(f'[pipeline] {action}: {spec.slug} (-> {end_date})')
            dataset = self.generator.generate(spec, end_date)
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

    pipeline = UniversePipeline(
        store=CsvDataStore(FOLDER_A),
        num_of_cpus=10,
    )

    # First verification run: one universe, diff against your existing folder.
    # pipeline.run(only={'liquid500'})

    # Nightly (Task Scheduler): refresh the whole registry.
    pipeline.run()
