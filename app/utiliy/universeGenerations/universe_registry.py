"""
universe_registry.py

Declares the universes the pipeline iterates. Adding a UniverseSpec with a new
slug auto-creates its folder on the next pipeline run (full backfill); an
existing slug just gets refreshed.

Universe key conventions (what UniverseProvider understands):
  'Liquid_500'  -> read maintained membership CSV (liquid_500_csv required)
  'Russell 3000'/'S&P 500'/... -> Norgate watchlist '<name> Current & Past'
  ['SPY', ...]  -> explicit ticker list (no membership timeseries; always "in")
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass
class UniverseSpec:
    slug: str               # folder name, e.g. 'liquid500'
    universe: object        # universe key understood by UniverseProvider
    start_date: dt.date     # backfill start
    liquid_500_csv: str = None
    price_adjust: str = 'TOTALRETURN'
    padding: str = 'NONE'
    fields: list = None     # None -> PriceProvider.DEFAULT_FIELDS
    # LRA Patch 14: synthetic-ticker hooks for LONG_SHORT universes.
    # synthetics:          synthetic symbols to compute. Looked up in the
    #                      synthetic_tickers DB table. None or [] -> skip.
    # drop_source_tickers: source tickers to remove from the dataset AFTER
    #                      synthetics are computed (e.g. ['CHFUSD','USDAUD'] for LRA).
    synthetics: list = None
    drop_source_tickers: list = None


# Patch 89: source-of-truth membership lives inside the universe folder
# alongside daily_*.csv, NOT in the legacy liquid_universe/Final_Liquid_500_QAS
# location. Single file: CsvDataStore.read_universe('liquid500'),
# _fetch_membership_window(universe_key='Liquid_500'), and
# extend_liquid500_membership all read/write this same path.
LIQUID_500_CSV = (
    r'C:\Tharun\Projects\backtest_data\universes\liquid500\liquid500.csv'
)

REGISTRY = [
    # Patch 91: legacy-parity REGISTRY order. sp1500 + russell3000 MUST be
    # extended BEFORE liquid500 because the liquid500 membership service's
    # OTC restriction reads from universes/sp1500/sp1500.csv and
    # universes/russell3000/russell3000.csv (legacy-pattern: maintained
    # local files, NOT Norgate-direct). For each manual-button click the
    # loop processes dependencies first, then liquid500's membership
    # pre-step finds them up to date. spy stays last (no dependency).
    UniverseSpec('sp500',       'S&P 500',            dt.date(1998, 1, 28)),
    UniverseSpec('sp1500',      'S&P Composite 1500', dt.date(1998, 1, 28)),
    UniverseSpec('russell3000', 'Russell 3000',       dt.date(2000, 6, 29)),
    UniverseSpec('liquid500',   'Liquid_500',         dt.date(1998, 1, 28), liquid_500_csv=LIQUID_500_CSV),
    UniverseSpec('spy',         ['SPY'],              dt.date(1998, 1, 28)),
    # UniverseSpec('index',     ['$SPX', '$RUI', '$RUT'], dt.date(1998, 1, 28)),
    # UniverseSpec('sp100',     'S&P 100',            dt.date(1998, 1, 28)),
    # UniverseSpec('nasdaq100', 'Nasdaq 100',         dt.date(1998, 1, 28)),

    # LRA Patch 14: LONG_SHORT universe — 14 tradable tickers. CHFAUD is synthesized
    # from CHFUSD * USDAUD; sources are dropped after compute (Patch 15 hook).
    # Uncomment to enable the next pipeline run.
    # UniverseSpec(
    #     slug='lra_14',
    #     universe=['EEM', 'EFA', 'SPY', 'IWM', 'QQQ', 'VNQ', 'GLD', 'TLT', 'IEF',
    #               'AGG', 'LQD', 'CHFUSD', 'USDAUD', 'UUP', 'RINF'],
    #     start_date=dt.date(2003, 1, 1),
    #     synthetics=['CHFAUD'],
    #     drop_source_tickers=['CHFUSD', 'USDAUD'],
    # ),
]

