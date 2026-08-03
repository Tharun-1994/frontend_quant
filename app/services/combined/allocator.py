# Patch 126: combined allocator — repeat-buy sizing now resolves through the
# active market-condition LABEL (cfg.ladder_by_label[label]) instead of the
# hardcoded above/below branch. Everything else identical to 119d:
# base ×, seed_count map, per-member overrides (applied last, legacy order),
# prev-close sizing, qty <= min_to_enter skip.
import math
from collections import defaultdict
from typing import Dict, List, Optional

import pandas as pd

from app.schemas.Combined import CombinedAllocationConfig, MemberOverrides
from app.services.combined.market_gate import build_gate_series, gate_for_entry_day


class AllocatorDataError(RuntimeError):
    pass


def _resolve_multiplier(cfg: CombinedAllocationConfig, label: str,
                        curr_hold: int, seed: int,
                        ov: Optional[MemberOverrides]) -> float:
    sizing = cfg.ladder_by_label.get(label)
    if sizing is None:
        raise AllocatorDataError(f"ladder_by_label missing entry for label '{label}'")
    mult = cfg.ladder.base
    if curr_hold == 1:
        mult = sizing.second_buy
    elif curr_hold == 2:
        mult = sizing.third_buy
    if ov is not None and ov.curr_hold_1 is not None and curr_hold == 1:
        mult = ov.curr_hold_1
    if seed > 0:
        if str(seed) in cfg.ladder.seed_count:
            mult = cfg.ladder.seed_count[str(seed)]
        if ov is not None:
            if seed == 1 and ov.seed_1 is not None:
                mult = ov.seed_1
            elif seed != 1 and ov.seed_other is not None:
                mult = ov.seed_other
    return mult


def _prev_close(closes: pd.DataFrame, symbol: str, entry_day, src_path: str = '') -> float:
    ts = pd.Timestamp(entry_day)
    if symbol not in closes.columns:
        raise AllocatorDataError(
            f"{symbol} missing from closes parquet ({src_path})")  # Patch 169
    try:
        idx = closes.index.get_loc(ts)
    except KeyError:
        raise AllocatorDataError(f"{entry_day} missing from closes parquet index")
    if idx == 0:
        raise AllocatorDataError(f"No prior close for {symbol} at {entry_day}")
    val = closes.iloc[idx - 1][symbol]
    if pd.isna(val) or val <= 0:
        raise AllocatorDataError(f"Bad prev close for {symbol} at {entry_day}: {val}")
    return float(val)


def run_allocator(member_trades: Dict[int, pd.DataFrame],
                  priority_order: List[int],
                  member_overrides: Dict[int, Optional[MemberOverrides]],
                  seed_sources: Dict[int, List[int]],
                  cfg: CombinedAllocationConfig,
                  spy_dir: str,
                  closes_parquet_path: str,
                  excluded_tickers: Optional[set] = None,
                  system_type: str = "LONG"):
    slot_cash = cfg.capital / cfg.base_slots
    # Patch 172: direction-aware P&L. The allocator RE-SIZES qty (ladder),
    # so it must recompute profit -- and shorts profit when price FALLS.
    # Legacy encodes this with negative amounts ((exit-entry) x -qty);
    # equivalent here: (entry-exit) x qty for SHORT books. Without this
    # every winning short booked as a loss (the monotonic -900k curve).
    _is_short = str(system_type or "").strip().upper() == "SHORT"
    gates = build_gate_series(spy_dir, cfg)
    closes = pd.read_parquet(closes_parquet_path)
    closes.index = pd.to_datetime(closes.index)

    # Patch 169: drop excluded tickers from member trade intake (legacy
    # cols_to_drop never reach screening; they must never reach allocation).
    if excluded_tickers:
        _ex = {t.upper() for t in excluded_tickers}
        for _sid, _df in list(member_trades.items()):
            _before = len(_df)
            member_trades[_sid] = _df[~_df['symbol'].str.upper().isin(_ex)]
            _dropped = _before - len(member_trades[_sid])
            if _dropped:
                print(f'[allocator] member {_sid}: dropped {_dropped} '
                      f'trade row(s) on excluded tickers')

    all_days = sorted({d for df in member_trades.values()
                       for d in df["entryDate"].unique()})
    out_rows, gate_audit = [], []

    for day in all_days:
        g = gate_for_entry_day(gates, day)
        gate_audit.append({"date": str(day)[:10], "trade_open": g.trade_open,
                           "label": g.label, "ibs": round(g.ibs, 4)})
        if cfg.gate.enabled and not g.trade_open:
            continue

        curr_hold: Dict[str, int] = defaultdict(int)
        slots_used = 0
        day_symbols: Dict[int, List[str]] = {
            sid: df[df["entryDate"] == day]["symbol"].tolist()
            for sid, df in member_trades.items()}

        for sid in priority_order:
            df = member_trades.get(sid)
            if df is None:
                continue
            ov = member_overrides.get(sid)
            seed: Dict[str, int] = defaultdict(int)
            for src in seed_sources.get(sid, []):
                for sym in day_symbols.get(src, []):
                    seed[sym] += 1

            for _, t in df[df["entryDate"] == day].iterrows():
                if slots_used >= cfg.max_slots:
                    break
                sym = t["symbol"]
                if curr_hold[sym] >= cfg.max_per_ticker:
                    continue
                mult = _resolve_multiplier(cfg, g.label,
                                           curr_hold[sym], seed.get(sym, 0), ov)
                size = (slot_cash / cfg.slot_divisor) * mult
                pc = _prev_close(closes, sym, day, src_path=closes_parquet_path)
                qty = math.floor(size / pc)
                if qty <= cfg.min_to_enter:
                    continue
                per_share = ((float(t["entryPrice"]) - float(t["exitPrice"]))
                             if _is_short else
                             (float(t["exitPrice"]) - float(t["entryPrice"])))  # Patch 172
                out_rows.append({**{k: t[k] for k in t.index},
                                 "quantity": qty,
                                 "profit": round(per_share * qty, 2),
                                 "capital": round(qty * float(t["entryPrice"]), 2),
                                 "system_source": sid,
                                 "condition_label": g.label,
                                 "tranche_curr_hold": curr_hold[sym],
                                 "tranche_seed": seed.get(sym, 0),
                                 "multiplier": mult})
                curr_hold[sym] += 1
                slots_used += 1

    return pd.DataFrame(out_rows), pd.DataFrame(gate_audit)