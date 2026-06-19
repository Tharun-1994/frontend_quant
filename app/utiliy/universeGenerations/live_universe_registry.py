"""
live_universe_registry.py — Norgate-refreshed registry for nightly EXECUTION.

Companion to universe_registry.py (the static backtest registry). The static
registry feeds backtest_data/universes/ and is manually maintained / frozen.
This registry feeds backtest_data/live_universes/ and is fully rewritten by
live_universe_pipeline.py every nightly run.

Each spec's start_date sets the Norgate pull window — kept at 2021-01-01 so
the data covers the 2023-01-01 execution start floor with ~2 years of
indicator-warmup buffer (long enough for SMA-252 / HV-252 to be valid by
2023-01-01).
"""

from __future__ import annotations
import datetime as dt
from app.utiliy.universeGenerations.universe_registry import UniverseSpec


LIVE_REGISTRY = [
    UniverseSpec(
        slug='sp500',
        universe='S&P 500',
        start_date=dt.date(2021, 1, 1),     # ~2 yrs buffer for indicator warmup
    ),
    UniverseSpec(
        slug='spy',
        universe=['SPY'],
        start_date=dt.date(2021, 1, 1),
    ),
]