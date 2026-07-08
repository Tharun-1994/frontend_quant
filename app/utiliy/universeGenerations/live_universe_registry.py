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
from app.utiliy.universeGenerations.universe_registry import (
    UniverseSpec,
    LIQUID_500_CSV,  # Patch 92: source-of-truth membership path
)


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
    # Patch 92: liquid500 enabled for live execution.
    # live_universe_pipeline.py extends the source-of-truth membership
    # (universes/liquid500/liquid500.csv) to today BEFORE this spec is
    # processed (membership pre-step). The pipeline then reads that
    # membership and pulls ~5yr of Norgate prices for active members
    # into live_universes/liquid500/.
    UniverseSpec(
        slug='liquid500',
        universe='Liquid_500',
        start_date=dt.date(2021, 1, 1),     # ~5 yr rolling window for execution
        liquid_500_csv=LIQUID_500_CSV,
    ),
]