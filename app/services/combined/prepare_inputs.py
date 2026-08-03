# Patch 135: materialize the combined book's data dependencies into its OWN
# input folder ({BASE}/{combined}/input/gate/), making the strategy
# self-contained like every other strategy:
#   DAILY_closes_spy.parquet / DAILY_highs_spy.parquet / DAILY_lows_spy.parquet
#   DAILY_closes.parquet   (universe closes, for prev-close sizing)
# Sources, per file, in order:
#   1. the first member's dedicated file (DAILY_{field}_{ticker}.parquet)
#   2. the ticker's column in the member's wide parquet (DAILY_{field}.parquet)
#   3. Patch 135: the shared index folder's daily_ohlcv_{ticker}.csv
#      (written by generate_index_prices.py for every registered index)
# Copies refresh when the source is newer (mtime). Loud failure names every
# path tried.
import os
import shutil

import pandas as pd

from app.constants.PricePath import PricePath

_OHLCV_COL = {"closes": "Close", "highs": "High", "lows": "Low"}


class InputPrepError(RuntimeError):
    pass


def _fresh(dst: str, src: str) -> bool:
    return os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src)


def _ensure_ticker_field(dst_dir: str, member_input: str, field: str, ticker: str):
    dst = os.path.join(dst_dir, f"DAILY_{field}_{ticker}.parquet")
    dedicated = os.path.join(member_input, f"DAILY_{field}_{ticker}.parquet")
    wide = os.path.join(member_input, f"DAILY_{field}.parquet")

    if os.path.exists(dedicated):
        if not _fresh(dst, dedicated):
            shutil.copy2(dedicated, dst)
        return
    if os.path.exists(wide):
        if _fresh(dst, wide):
            return
        df = pd.read_parquet(wide)
        match = [c for c in df.columns if c.lower() == ticker.lower()]
        if match:
            df[[match[0]]].rename(columns={match[0]: ticker}).to_parquet(dst)
            return

    # Patch 135: shared index folder — daily_ohlcv_{ticker}.csv has the full
    # OHLCV for every index registered in generate_index_prices.py.
    ohlcv = os.path.join(PricePath.index_path, f"daily_ohlcv_{ticker.lower()}.csv")
    if os.path.exists(ohlcv):
        if _fresh(dst, ohlcv):
            return
        idx_df = pd.read_csv(ohlcv, index_col="Date", parse_dates=True)
        col = _OHLCV_COL[field]
        if col not in idx_df.columns:
            raise InputPrepError(
                f"{ohlcv} has no '{col}' column — regenerate index prices")
        idx_df[[col]].rename(columns={col: ticker}).sort_index().to_parquet(dst)
        return

    raise InputPrepError(
        f"Condition ticker {field} unavailable — tried {dedicated}, "
        f"column '{ticker}' in {wide}, and {ohlcv}")


def _ensure_closes(dst_dir: str, member_input: str):
    src = os.path.join(member_input, "DAILY_closes.parquet")
    dst = os.path.join(dst_dir, "DAILY_closes.parquet")
    if not os.path.exists(src):
        raise InputPrepError(f"Universe closes parquet missing: {src}")
    if not _fresh(dst, src):
        shutil.copy2(src, dst)


def prepare_combined_inputs(combined_dir: str, member_dir: str,
                            member_universe_relpath: str, ticker: str) -> str:
    """Returns the gate-data directory inside the combined strategy folder."""
    member_input = os.path.join(member_dir, member_universe_relpath)
    if not os.path.isdir(member_input):
        raise InputPrepError(
            f"First member's input folder not found: {member_input} — "
            f"run the member's data generation/backtest first")
    dst_dir = os.path.join(combined_dir, "input", "gate")
    os.makedirs(dst_dir, exist_ok=True)
    for field in ("closes", "highs", "lows"):
        _ensure_ticker_field(dst_dir, member_input, field, ticker)
    _ensure_closes(dst_dir, member_input)
    return dst_dir