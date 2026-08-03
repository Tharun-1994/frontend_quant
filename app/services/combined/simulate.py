# Patch 141: combined Simulate — self-contained data layout.
#   inputs  : {BASE}/{combined}/input/gate/   (materialized by prepare step
#             from the first member's input folder)
#   outputs : {BASE}/{combined}/output/{Tradelist,Equity}.json + gate_audit.csv
import json
import os
from typing import Dict, List, Optional

import pandas as pd

from app.Settings import settings
from app.schemas.Combined import CombinedAllocationConfig, MemberOverrides
from app.services.combined.allocator import run_allocator
from app.services.combined.prepare_inputs import prepare_combined_inputs


class MemberOutputError(RuntimeError):
    pass


def _strategy_dir(strategy_name: str) -> str:
    return os.path.join(settings.BACKTEST_DATA_PATH, strategy_name)


def _load_member_tradelist(strategy_name: str) -> pd.DataFrame:
    # Patch 136: engine Tradelist.json is a DICT keyed by trade-id
    # ("SYMBOL_runid_N" -> trade record), not a list — orient accordingly.
    # The nested valueTracker blob is dropped (irrelevant to allocation).
    p = os.path.join(_strategy_dir(strategy_name), "output", "Tradelist.json")
    if not os.path.exists(p):
        # engine historically capitalises the L in some paths — accept both
        alt = os.path.join(_strategy_dir(strategy_name), "output", "TradeList.json")
        if os.path.exists(alt):
            p = alt
        else:
            raise MemberOutputError(
                f"Member '{strategy_name}' has no tradelist at {p} — "
                f"run its backtest first")
    with open(p) as f:
        raw = json.load(f)
    if isinstance(raw, dict):
        df = pd.DataFrame.from_dict(raw, orient="index")
        df.index.name = "trade_id"
        df = df.reset_index()
    else:
        df = pd.DataFrame(raw)
    df = df.drop(columns=["valueTracker"], errors="ignore")
    required = {"entryDate", "symbol", "entryPrice", "exitDate", "exitPrice"}
    missing = required - set(df.columns)
    if missing:
        raise MemberOutputError(
            f"Member '{strategy_name}' tradelist {p} missing fields: "
            f"{sorted(missing)} (found: {sorted(df.columns)[:12]}...)")
    if df.empty:
        raise MemberOutputError(f"Member '{strategy_name}' tradelist is empty ({p})")
    return df


def simulate_combined(combined_name: str,
                      members: List[dict],
                      cfg: CombinedAllocationConfig,
                      spy_dir: str = "",
                      excluded_tickers: Optional[set] = None,
                      system_type: str = "LONG") -> dict:
    ordered = sorted(members, key=lambda m: m["priority"])
    trades = {m["strategy_id"]: _load_member_tradelist(m["strategy_name"])
              for m in ordered}
    priority_order = [m["strategy_id"] for m in ordered]
    overrides: Dict[int, Optional[MemberOverrides]] = {
        m["strategy_id"]: m.get("overrides") for m in ordered}
    seeds: Dict[int, List[int]] = {
        m["strategy_id"]: m.get("seed_source_ids", []) for m in ordered}

    combined_dir = _strategy_dir(combined_name)
    gate_dir = prepare_combined_inputs(
        combined_dir=combined_dir,
        member_dir=_strategy_dir(ordered[0]["strategy_name"]),
        member_universe_relpath=cfg.member_input_relpath,
        ticker=cfg.market_conditions.ticker)
    closes_path = os.path.join(gate_dir, "DAILY_closes.parquet")

    combined_df, gate_df = run_allocator(
        trades, priority_order, overrides, seeds, cfg, gate_dir, closes_path,
        excluded_tickers=excluded_tickers,   # Patch 169
        system_type=system_type)             # Patch 172

    out_dir = os.path.join(combined_dir, "output")
    os.makedirs(out_dir, exist_ok=True)

    # Patch 137: write both files in the ENGINE'S native shapes — every
    # consumer (equity chart, tradelist tab, CSV download) does
    # pd.read_json(path).T, i.e. expects dict-of-dicts.
    #   Tradelist.json : {trade_id: record}   (member trade_id reused —
    #                    globally unique, and traceable back to its member)
    #   Equity.json    : {date: {equityValue, dailyDrawdown,
    #                            dayEndUtilityValue, dayEndUtility}}
    tl = combined_df.copy()
    # Patch 138: member trade-ids are only unique WITHIN a member's run —
    # prefix with the source system id to guarantee global uniqueness
    # (e.g. "S21_ANF_1783355865340_1") while keeping traceability.
    if "trade_id" in tl.columns and "system_source" in tl.columns:
        tl.index = "S" + tl["system_source"].astype(str) + "_" + tl["trade_id"].astype(str)
        tl = tl.drop(columns=["trade_id"])
    elif "trade_id" in tl.columns:
        tl = tl.set_index("trade_id")
    tl.index = tl.index.astype(str)
    if not tl.index.is_unique:
        dupes = tl.index[tl.index.duplicated()].unique()[:5].tolist()
        raise MemberOutputError(
            f"Combined trade ids still not unique after prefixing — "
            f"first duplicates: {dupes}")
    with open(os.path.join(out_dir, "Tradelist.json"), "w") as f:
        json.dump(tl.to_dict(orient="index"), f, default=str)

    gate_df.to_csv(os.path.join(out_dir, "gate_audit.csv"), index=False)

    daily = combined_df.groupby("exitDate")["profit"].sum().sort_index()
    equity = (cfg.capital + daily.cumsum()).round(2)
    peak = equity.cummax()
    # Patch 141 (verified against the member equity chart axes):
    #   dailyDrawdown     = DOLLARS below the running peak (chart negates)
    #   dayEndUtilityValue = capital deployed that day (sum of qty x entry)
    #   dayEndUtility      = positions taken that day (slot count)
    drawdown = (peak - equity).round(2)
    util_val = combined_df.groupby("entryDate")["capital"].sum().round(2)
    util_cnt = combined_df.groupby("entryDate")["symbol"].count()
    eq = {}
    for d, v, dd in zip(equity.index, equity.values, drawdown.values):
        key = str(d)[:10]
        eq[key] = {
            "equityValue": float(v),
            "dailyDrawdown": float(dd),
            "dayEndUtilityValue": float(util_val.get(d, 0.0)),
            "dayEndUtility": float(util_cnt.get(d, 0)),
        }
    with open(os.path.join(out_dir, "Equity.json"), "w") as f:
        json.dump(eq, f)

    return {"trades": int(len(combined_df)),
            "gated_days": int((~gate_df["trade_open"]).sum()),
            "days": int(len(gate_df))}