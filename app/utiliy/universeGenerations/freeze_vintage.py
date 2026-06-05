"""
freeze_vintage.py

Folder B (manager copy). Snapshots universes from Folder A into a separate
frozen store, stamps the vintage, and optionally trims every series to a
freeze_end (e.g. 2025-12-31). It is *derived from A*, never appended to.

Because A is a single consistent Norgate vintage, trimming its rows stays
internally consistent -- there is no cross-vintage seam. Running this again is
a controlled rebase: B advances only on a date you chose, and the manifest
records exactly which vintage the manager's number reflects.
"""

from __future__ import annotations

import datetime as dt

from storage import DataStore, CsvDataStore, UniverseDataset


def freeze(source: DataStore, dest: DataStore, slugs,
           freeze_end=None, vintage=None):
    vintage = vintage or dt.date.today()

    for slug in slugs:
        src = source.read_universe(slug)

        fields = {f: (df.loc[:freeze_end] if freeze_end else df)
                  for f, df in src.fields.items()}
        membership = None
        if src.membership is not None:
            membership = (src.membership.loc[:freeze_end]
                          if freeze_end else src.membership)

        manifest = dict(src.manifest)
        manifest.update({
            'frozen_from': 'Folder A',
            'vintage': vintage,
            'freeze_end': freeze_end,
            'source_written_at': src.manifest.get('written_at'),
        })

        dest.write_universe(UniverseDataset(slug, fields, membership, manifest))
        print(f'[freeze] {slug}: vintage {vintage}, '
              f'end {freeze_end or "full"}')


if __name__ == '__main__':
    FOLDER_A = r'C:\Tharun\Projects\backtest_data\universes'
    FOLDER_B = r'C:\Tharun\Projects\backtest_data\universes_manager'

    freeze(
        source=CsvDataStore(FOLDER_A),
        dest=CsvDataStore(FOLDER_B),
        slugs=['liquid500'],
        freeze_end=dt.date(2025, 12, 31),   # the manager's frozen window
    )
