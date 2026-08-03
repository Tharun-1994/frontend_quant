"""
daily_data_generator.py

Produces one universe's dataset (prices + membership + manifest) for a spec.
Storage-agnostic on purpose: it returns a UniverseDataset and never touches
disk -- the DataStore decides where it lands. UniverseProvider answers "who is
in the universe", PriceProvider answers "what are their prices", this glues them.
"""

from __future__ import annotations

from app.utiliy.universeGenerations.universe_provider import UniverseProvider
from app.utiliy.universeGenerations.price_provider import PriceProvider
from app.utiliy.universeGenerations.storage import UniverseDataset
from app.utiliy.universeGenerations.universe_registry import UniverseSpec


class DailyDataGenerator:

    def __init__(self, num_of_cpus=6):
        self.num_of_cpus = num_of_cpus

    def generate(self, spec: UniverseSpec, end_date) -> UniverseDataset:
        provider = UniverseProvider(
            universe=spec.universe,
            start_date=spec.start_date,
            end_date=end_date,
            num_of_cpus=self.num_of_cpus,
            padding=spec.padding,
            liquid_500_csv=spec.liquid_500_csv)

        fields = PriceProvider(
            num_of_cpus=self.num_of_cpus,
            padding=spec.padding,
            price_adjust=spec.price_adjust,
        ).get_prices(
            tickers=provider.tickers,
            start_date=spec.start_date,
            end_date=end_date,
            fields=spec.fields)

        closes = fields.get('Close')
        last_bar = None if closes is None or closes.empty else closes.index.max()

        # Patch 105: one-line verdict per universe. If last_bar < end_date the
        # ceiling came from the local Norgate DB (see the [price_provider]
        # STALE SOURCE WARNING above for the per-ticker breakdown).
        if last_bar is not None:
            _short = (last_bar.date() < end_date) if end_date else False
            print(f'[daily_data_generator] {spec.slug}: requested '
                  f'[{spec.start_date} .. {end_date}], built '
                  f'{len(provider.tickers)} tickers, last bar '
                  f'{last_bar:%Y-%m-%d}'
                  + (f'  <-- SHORT of requested {end_date}' if _short else
                     '  (matches requested end)'))

        manifest = {
            'slug': spec.slug,
            'universe': str(spec.universe),
            'price_adjust': spec.price_adjust,
            'start_date': spec.start_date,
            'end_date': end_date,
            'num_tickers': len(provider.tickers),
            'last_data_date': last_bar,
        }
        return UniverseDataset(spec.slug, fields, provider.membership, manifest)