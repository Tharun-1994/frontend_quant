"""
storage.py

The swappable persistence layer. Generation produces a UniverseDataset;
a DataStore decides how/where it lands. CSV today, parquet / cloud / DB later
-- swap the implementation, nothing upstream changes.

The Norgate-field -> filename mapping is a *storage* concern, so it lives in
CsvDataStore (a ParquetDataStore would name things differently / use one file).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd


@dataclass
class UniverseDataset:
    """The unit of generation: one universe's prices + membership + manifest."""
    slug: str
    fields: dict          # {field_name: DataFrame(dates x tickers)}
    membership: object = None   # DataFrame(dates x tickers) or None for a raw list
    manifest: dict = field(default_factory=dict)


class DataStore(ABC):

    @abstractmethod
    def has_universe(self, slug: str) -> bool:
        ...

    @abstractmethod
    def list_universes(self) -> list:
        ...

    @abstractmethod
    def write_universe(self, dataset: UniverseDataset) -> None:
        ...

    @abstractmethod
    def read_universe(self, slug: str) -> UniverseDataset:
        ...


class CsvDataStore(DataStore):

    FIELD_TO_FILENAME = {
        'Open':             'daily_opens.csv',
        'High':             'daily_highs.csv',
        'Low':              'daily_lows.csv',
        'Close':            'daily_closes.csv',
        'Volume':           'daily_volumes.csv',
        'Turnover':         'daily_turnovers.csv',
        'Unadjusted Close': 'daily_unadjusted.csv',
    }
    FILENAME_TO_FIELD = {v: k for k, v in FIELD_TO_FILENAME.items()}
    MANIFEST_FILE = 'manifest.json'
    PRESENCE_FILE = 'daily_closes.csv'   # used to detect an existing universe
    INDEX_NAME = 'Date'                  # every date index is written/read as this

    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)

    def _dir(self, slug):
        return self.base_dir / slug

    def has_universe(self, slug):
        return (self._dir(slug) / self.PRESENCE_FILE).exists()

    def list_universes(self):
        if not self.base_dir.exists():
            return []
        return sorted(p.name for p in self.base_dir.iterdir()
                      if p.is_dir() and (p / self.PRESENCE_FILE).exists())

    def write_universe(self, dataset):
        out = self._dir(dataset.slug)
        out.mkdir(parents=True, exist_ok=True)   # auto-creates the folder for a NEW universe

        for field_name, df in dataset.fields.items():
            fname = self.FIELD_TO_FILENAME.get(field_name)
            if fname is None:
                raise KeyError(f'No filename mapping for field {field_name!r}')
            df.index.name = self.INDEX_NAME
            df.to_csv(out / fname)

        if dataset.membership is not None:
            dataset.membership.index.name = self.INDEX_NAME
            dataset.membership.to_csv(out / f'{dataset.slug}.csv')

        manifest = dict(dataset.manifest)
        manifest['written_at'] = datetime.now().isoformat(timespec='seconds')
        (out / self.MANIFEST_FILE).write_text(
            json.dumps(manifest, indent=2, default=str))

    def read_universe(self, slug):
        out = self._dir(slug)
        if not self.has_universe(slug):
            raise FileNotFoundError(f'No universe stored at {out}')

        fields = {}
        for fname, field_name in self.FILENAME_TO_FIELD.items():
            fp = out / fname
            if fp.exists():
                df = pd.read_csv(fp, index_col=0, parse_dates=True)
                df.index.name = self.INDEX_NAME
                fields[field_name] = df

        membership = None
        mp = out / f'{slug}.csv'
        if mp.exists():
            membership = pd.read_csv(mp, index_col=0, parse_dates=True)
            membership.index.name = self.INDEX_NAME

        manifest = {}
        mf = out / self.MANIFEST_FILE
        if mf.exists():
            manifest = json.loads(mf.read_text())

        return UniverseDataset(slug, fields, membership, manifest)