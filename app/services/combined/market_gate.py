# Patch 142: labelled market conditions.
# Model change vs 126: there is no special "default" label any more.
# Every label may carry a rule (rule=None means "matches all remaining
# days"). Labels are evaluated IN ORDER, first match wins, and any day
# matched by nothing falls into the LAST label — mirroring the legacy
# if/else exactly (bull test, else bear), including SMA warm-up days.
import os
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from app.schemas.Combined import CombinedAllocationConfig, GateConfig


class GateDataError(RuntimeError):
    pass


@dataclass
class MarketGate:
    trade_open: bool
    label: str
    ibs: float
    ma: float = 0.0

    @property
    def branch(self) -> str:          # back-compat alias
        return self.label


def _load_ohlc_f64(spy_dir: str, ticker: str) -> pd.DataFrame:
    out = {}
    for field in ("closes", "highs", "lows"):
        p = os.path.join(spy_dir, f"DAILY_{field}_{ticker}.parquet")
        if not os.path.exists(p):
            raise GateDataError(f"Condition ticker parquet missing: {p}")
        df = pd.read_parquet(p)
        out[field] = df[df.columns[0]].astype("float64")
    frame = pd.DataFrame(out)
    frame.index = pd.to_datetime(frame.index)
    return frame


def _indicator_series(name: str, lookback: int, ohlc: pd.DataFrame) -> pd.Series:
    n = (name or "").lower()
    if n == "close":
        return ohlc["closes"]
    if n == "sma":
        if lookback <= 0:
            raise GateDataError("sma requires lookback > 0")
        return ohlc["closes"].rolling(lookback).mean()
    if n == "roc":
        if lookback <= 0:
            raise GateDataError("roc requires lookback > 0")
        return ohlc["closes"].pct_change(lookback) * 100
    if n == "ibs":
        rng = ohlc["highs"] - ohlc["lows"]
        s = (ohlc["closes"] - ohlc["lows"]) / rng
        s[rng <= 0] = np.nan
        return s
    raise GateDataError(
        f"Condition indicator '{name}' not supported by the label evaluator "
        f"(supported: close, sma, roc, ibs)")


_OPS = {
    ">":  lambda a, b: a > b,
    "<":  lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
}


def _rule_mask(rule: dict, ohlc: pd.DataFrame) -> pd.Series:
    left = _indicator_series(rule.get("indicator"), int(rule.get("lookback") or 0), ohlc)
    if rule.get("value_type") == "indicator_price":
        right = _indicator_series(rule.get("value_indicator"),
                                  int(rule.get("value_lookback") or 0), ohlc)
    else:
        right = float(rule.get("value") or 0.0)
    op = _OPS.get(rule.get("operator"))
    if op is None:
        raise GateDataError(f"Unsupported operator '{rule.get('operator')}'")
    return op(left, right)


def _tree_mask(node: Optional[dict], ohlc: pd.DataFrame) -> pd.Series:
    """Patch 142: evaluate a standard rule TREE ({type:'group'|'rule'}).
    None or an empty group == always True (matches everything / always
    trade). NaN handling: a leaf's NaN stays NaN so warm-up is detectable;
    groups combine with AND/OR on filled values but track validity via
    notna of all children (conservative)."""
    if node is None:
        return pd.Series(True, index=ohlc.index)
    ntype = node.get("type")
    if ntype == "rule":
        return _rule_mask(node.get("rule") or {}, ohlc)
    if ntype == "group":
        children = node.get("children") or []
        if not children:
            return pd.Series(True, index=ohlc.index)
        masks = [_tree_mask(c, ohlc) for c in children]
        logic = (node.get("logic") or "AND").upper()
        any_nan = None
        for m in masks:
            nn = m.isna() if m.dtype == object or m.isna().any() else None
            if nn is not None:
                any_nan = nn if any_nan is None else (any_nan | nn)
        if logic == "AND":
            out = masks[0].fillna(False)
            for m in masks[1:]:
                out = out & m.fillna(False)
        elif logic == "OR":
            out = masks[0].fillna(False)
            for m in masks[1:]:
                out = out | m.fillna(False)
        else:
            raise GateDataError(f"Unsupported group logic '{node.get('logic')}'")
        out = out.astype(object)
        if any_nan is not None:
            out[any_nan] = float("nan")
        return out
    raise GateDataError(f"Unsupported tree node type '{ntype}'")


def build_gate_series(spy_dir: str, cfg) -> pd.DataFrame:
    """Full CombinedAllocationConfig in, per-date frame out:
    label, ibs, trade_open, valid."""
    if isinstance(cfg, GateConfig):
        raise GateDataError(
            "build_gate_series takes the full config — pass "
            "CombinedAllocationConfig, not GateConfig")
    mc = cfg.market_conditions
    if not mc.labels:
        raise GateDataError("market_conditions has no labels configured")
    ohlc = _load_ohlc_f64(spy_dir, mc.ticker)

    rng = ohlc["highs"] - ohlc["lows"]
    ibs = (ohlc["closes"] - ohlc["lows"]) / rng
    ibs[rng <= 0] = float("nan")

    labels = pd.Series(index=ohlc.index, dtype="object")
    for item in mc.labels:
        if item.rule_tree is None:
            take = labels.isna()                     # matches all remaining days
        else:
            mask = _tree_mask(item.rule_tree, ohlc)
            take = pd.Series(mask).fillna(False).astype(bool) & labels.isna()
        labels[take] = item.label
    # Fallback: unmatched days (incl. rule warm-up NaNs) go to the LAST label —
    # exactly the legacy else-branch behaviour.
    fallback = mc.labels[-1].label
    labels = labels.where(labels.notna(), fallback)

    missing = sorted(set(labels.unique()) - set(cfg.gate_by_label.keys()))
    if missing:
        raise GateDataError(f"gate_by_label missing entries for labels: {missing}")

    # Patch 140: the gate is a per-label RULE (any supported indicator),
    # evaluated vectorised per label, then selected by the active label.
    trade_open = pd.Series(False, index=ohlc.index)
    gate_valid = pd.Series(False, index=ohlc.index)
    for L in labels.unique():
        tree = cfg.gate_by_label[L].rule_tree
        mask = pd.Series(_tree_mask(tree, ohlc))
        sel = labels == L
        trade_open[sel] = mask[sel].fillna(False).astype(bool)
        gate_valid[sel] = mask[sel].notna()

    return pd.DataFrame({
        "ibs": ibs,
        "label": labels,
        "trade_open": trade_open,
        "valid": ibs.notna() & gate_valid,
    }, index=ohlc.index)


def gate_for_entry_day(gates: pd.DataFrame, entry_day) -> MarketGate:
    """T-1 discipline: gate from the last bar strictly BEFORE entry_day."""
    ts = pd.Timestamp(entry_day)
    prior = gates.index[gates.index < ts]
    if len(prior) == 0:
        raise GateDataError(f"No gate bar before {entry_day}")
    row = gates.loc[prior[-1]]
    if not bool(row["valid"]):
        raise GateDataError(
            f"Gate not computable for {prior[-1].date()} (degenerate bar)")
    return MarketGate(bool(row["trade_open"]), str(row["label"]), float(row["ibs"]))