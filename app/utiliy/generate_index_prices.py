"""
generate_index_prices.py
========================
Fetches index prices from Norgate and saves them to the shared index folder.

  Output folder : C:\\Tharun\\Projects\\backtest_data\\universes\\index\\

For each index the script produces:
  daily_closes_{key}.csv      — close prices only  (Date index, one column)
  daily_ohlcv_{key}.csv       — full OHLCV          (Date index, OHLCV columns)

The CSV format matches what PriceDataLoader.load_spy_close / load_ticker_close
expect, so no changes are needed when adding a new index — just add an entry
to INDEX_REGISTRY below and re-run.

HOW TO ADD A NEW INDEX
----------------------
Add one dict to INDEX_REGISTRY:

    'my_key': {
        'symbol'      : 'NORGATE_TICKER',   # exact Norgate symbol
        'adjustment'  : nd.StockPriceAdjustmentType.NONE,   # or TOTALRETURN
        'padding'     : nd.PaddingType.NONE,
        'start_date'  : '1998-01-01',
        'description' : 'Human-readable name',
    },

Then run:  python generate_index_prices.py  (or  python generate_index_prices.py --only my_key)
"""

import argparse
import sys
from pathlib import Path
from datetime import datetime

import pandas as pd
import norgatedata as nd

# ── Output path ───────────────────────────────────────────────────────────────

INDEX_FOLDER = Path(r'C:\Tharun\Projects\backtest_data\universes\index')

# ── Index registry ────────────────────────────────────────────────────────────
# Add new indexes here. Key becomes the filename suffix: daily_closes_{key}.csv

INDEX_REGISTRY = {

    'spy': {
        'symbol'      : 'SPY',
        'adjustment'  : nd.StockPriceAdjustmentType.TOTALRETURN,
        'padding'     : nd.PaddingType.NONE,
        'start_date'  : '1998-01-01',
        'description' : 'S&P 500 ETF (SPY)',
    },

    'vix': {
        'symbol'      : '$VIX',
        'adjustment'  : nd.StockPriceAdjustmentType.NONE,
        'padding'     : nd.PaddingType.NONE,
        'start_date'  : '1998-01-01',
        'description' : 'CBOE Volatility Index (VIX)',
    },

    'spxmcsum': {
        'symbol'      : '#SPXMCSUM',
        'adjustment'  : nd.StockPriceAdjustmentType.TOTALRETURN,
        'padding'     : nd.PaddingType.NONE,
        'start_date'  : '1998-01-01',
        'description' : 'S&P 500 McClellan Summation Index',
    },

    # ── Add more below as needed ──────────────────────────────────────────────
    # 'gld': {
    #     'symbol'      : 'GLD',
    #     'adjustment'  : nd.StockPriceAdjustmentType.TOTALRETURN,
    #     'padding'     : nd.PaddingType.NONE,
    #     'start_date'  : '2004-01-01',
    #     'description' : 'Gold ETF (GLD)',
    # },
    # 'qqq': {
    #     'symbol'      : 'QQQ',
    #     'adjustment'  : nd.StockPriceAdjustmentType.TOTALRETURN,
    #     'padding'     : nd.PaddingType.NONE,
    #     'start_date'  : '1999-01-01',
    #     'description' : 'Nasdaq-100 ETF (QQQ)',
    # },

}

# ── Core fetch + save ─────────────────────────────────────────────────────────

def fetch_index(key: str, config: dict) -> pd.DataFrame:
    """Pull OHLCV from Norgate for one index. Returns a DataFrame."""
    print(f"  Fetching {config['description']} ({config['symbol']}) from {config['start_date']}...")
    df: pd.DataFrame = nd.price_timeseries(
        config['symbol'],
        stock_price_adjustment_setting=config['adjustment'],
        padding_setting=config['padding'],
        start_date=config['start_date'],
        timeseriesformat='pandas-dataframe',
    )
    if df is None or df.empty:
        raise ValueError(f"Norgate returned no data for symbol '{config['symbol']}'")
    df.index.name = 'Date'
    df.index = pd.to_datetime(df.index)
    df.sort_index(inplace=True)
    return df


def save_index(key: str, df: pd.DataFrame, folder: Path) -> dict:
    """
    Save CSV files for one index.

    Writes:
      daily_closes_{key}.csv  — Close column only   (matches load_spy_close format)
      daily_ohlcv_{key}.csv   — Full OHLCV           (future use)

    Returns a dict of {description: path} for the summary log.
    """
    folder.mkdir(parents=True, exist_ok=True)
    saved = {}

    # 1. Closes CSV — single column named after the key (lowercase)
    #    Compatible with PriceDataLoader.load_spy_close / load_ticker_close
    close_col = 'Close'
    if close_col not in df.columns:
        # Some Norgate series only return Close; handle gracefully
        close_col = df.columns[0]
        print(f"    [NOTE] 'Close' column not found, using '{close_col}' instead.")

    closes_df = df[[close_col]].rename(columns={close_col: key})
    closes_path = folder / f'daily_closes_{key}.csv'
    closes_df.to_csv(closes_path, index=True, index_label='Date')
    saved['closes_csv'] = closes_path

    # 2. Full OHLCV CSV — for future indicators that need High/Low/Volume
    ohlcv_cols = [c for c in ['Open', 'High', 'Low', 'Close', 'Volume', 'Unadjusted Close', 'Turnover']
                  if c in df.columns]
    ohlcv_path = folder / f'daily_ohlcv_{key}.csv'
    df[ohlcv_cols].to_csv(ohlcv_path, index=True, index_label='Date')
    saved['ohlcv_csv'] = ohlcv_path

    return saved


def generate_one(key: str, config: dict, folder: Path) -> bool:
    """Fetch and save one index. Returns True on success."""
    try:
        df = fetch_index(key, config)
        saved = save_index(key, df, folder)
        rows = len(df)
        start = df.index[0].strftime('%Y-%m-%d')
        end   = df.index[-1].strftime('%Y-%m-%d')
        print(f"  ✓  {key:<12}  {rows} rows  {start} → {end}")
        for desc, path in saved.items():
            print(f"       {desc:<14}  {path}")
        return True
    except Exception as e:
        print(f"  ✗  {key:<12}  ERROR: {e}")
        return False


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Fetch index prices from Norgate and save to the shared index folder.')
    parser.add_argument(
        '--only', nargs='+', metavar='KEY',
        help='Only regenerate the specified keys (e.g. --only vix spxmcsum).')
    parser.add_argument(
        '--folder', metavar='PATH', default=str(INDEX_FOLDER),
        help=f'Override output folder (default: {INDEX_FOLDER}).')
    args = parser.parse_args()

    folder = Path(args.folder)
    keys_to_run = args.only if args.only else list(INDEX_REGISTRY.keys())

    # Validate keys
    unknown = [k for k in keys_to_run if k not in INDEX_REGISTRY]
    if unknown:
        print(f"ERROR: Unknown key(s): {unknown}")
        print(f"Available keys: {list(INDEX_REGISTRY.keys())}")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  Index Price Generator")
    print(f"  Run time  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Output    : {folder}")
    print(f"  Indexes   : {keys_to_run}")
    print(f"{'='*60}\n")

    results = {}
    for key in keys_to_run:
        config = INDEX_REGISTRY[key]
        results[key] = generate_one(key, config, folder)

    # Summary
    ok  = [k for k, v in results.items() if v]
    err = [k for k, v in results.items() if not v]
    print(f"\n{'='*60}")
    print(f"  Done — {len(ok)} succeeded, {len(err)} failed")
    if err:
        print(f"  Failed : {err}")
    print(f"{'='*60}\n")

    if err:
        sys.exit(1)


if __name__ == '__main__':
    main()
