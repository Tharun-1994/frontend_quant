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


# Maintained Liquid 500 membership file. If your pipeline keeps a separate
# night-built copy, point this at the correct one (or branch on the hour).
LIQUID_500_CSV = (
    r'C:\Tharun\Projects\backtest_data\liquid_universe'
    r'\Final_Liquid_500_QAS\Dan_US_Liquid_500_most_recent_5_price_drop.csv'
)

REGISTRY = [
    # UniverseSpec('liquid500',   'Liquid_500',   dt.date(1998, 1, 28), liquid_500_csv=LIQUID_500_CSV),
    # UniverseSpec('russell3000', 'Russell 3000', dt.date(2000, 6, 29)),
    UniverseSpec('sp500',       'S&P 500',      dt.date(1998, 1, 28)),
    UniverseSpec('spy',         ['SPY'],        dt.date(1998, 1, 28)),
    # UniverseSpec('index',     ['$SPX', '$RUI', '$RUT'], dt.date(1998, 1, 28)),
    # UniverseSpec('sp100', 'S&P 100', dt.date(1998, 1, 28))
    # UniverseSpec('nasdaq100', 'Nasdaq 100', dt.date(1998, 1, 28))

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

