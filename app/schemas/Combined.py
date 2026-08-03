# Patch 142: combined-system schemas v2 — labeled market conditions.
# The hardcoded bull/bear branch becomes a configurable list of LABELS, each
# defined by a rule on the condition ticker (same rule shape as entry-rule
# leaves, for future unification with the rule-tree editor). Each label owns
# its repeat-buy sizing and its gate threshold. Old bull/bear configs are
# auto-converted by normalize_combined_config() — nothing re-entered.
from typing import Dict, List, Literal, Optional
from pydantic import BaseModel


class ConditionRule(BaseModel):
    # Mirrors the entry-rule leaf shape (subset the Python evaluator supports).
    indicator: str = "close"          # close | sma | roc | ibs
    lookback: int = 0
    operator: str = ">"               # > < >= <=
    value: float = 0.0
    value_type: Literal["value", "indicator_price"] = "value"
    value_indicator: str = ""         # e.g. sma
    value_lookback: int = 0


def _leaf_tree(rule: dict) -> dict:
    """Wrap a single leaf rule into the standard tree JSON shape."""
    return {"type": "group", "id": "root", "logic": "AND",
            "children": [{"type": "rule", "id": "r1", "rule": rule}]}


class MarketConditionLabel(BaseModel):
    label: str
    # Patch 142: rules are TREES — the same {type:'group', logic, children}
    # JSON as entry/exit rule trees, so the frontend reuses RulesTreeEditor
    # and future multi-rule conditions need no schema change.
    # rule_tree=None (or an empty group) => matches all remaining days.
    rule_tree: Optional[dict] = None


class MarketConditions(BaseModel):
    ticker: str = "spy"
    labels: List[MarketConditionLabel] = [
        MarketConditionLabel(label="all_days", rule_tree=None),
    ]


class LabelSizing(BaseModel):
    second_buy: float = 1.5           # multiplier when curr_hold == 1
    third_buy: float = 0.5            # multiplier when curr_hold == 2


def _default_gate_tree() -> dict:
    return _leaf_tree({"indicator": "ibs", "lookback": 0, "operator": "<",
                       "value": 0.95, "value_type": "value",
                       "value_indicator": "", "value_lookback": 0})


class LabelGate(BaseModel):
    # Patch 142: the gate is a RULE TREE (AND/OR groups), evaluated on the
    # condition ticker's T-1 bar. Trade allowed when the tree passes.
    # Empty/None tree => always trade under this label.
    rule_tree: Optional[dict] = None


class GateConfig(BaseModel):
    # v2: only the master switch lives here; per-label thresholds are in
    # gate_by_label; the condition ticker is in market_conditions.
    enabled: bool = True
    ticker: str = "spy"               # retained for back-compat reads


class LadderConfig(BaseModel):
    # label-independent parts of the verified ladder
    # (Portfolio_.py:2720-2825): base for the 1st buy, seed_count for
    # tickers seen in seed-source systems' lists.
    base: float = 2.0
    seed_count: Dict[str, float] = {"1": 1.5, "2": 0.5}


class MemberOverrides(BaseModel):
    curr_hold_1: Optional[float] = None
    seed_1: Optional[float] = None
    seed_other: Optional[float] = None


class CombinedMemberIn(BaseModel):
    member_strategy_id: int
    priority: int
    is_active: bool = True
    seed_source_ids: List[int] = []
    overrides: Optional[MemberOverrides] = None


class CombinedAllocationConfig(BaseModel):
    capital: float = 25000
    base_slots: int = 8
    slot_divisor: int = 3
    max_slots: int = 32
    max_per_ticker: int = 3
    min_to_enter: int = 2             # legacy: skip when qty <= this
    count_basis: Literal["fills", "candidates"] = "candidates"
    # Patch 146: production (live execution) profile — consumed ONLY by the
    # execution path. Simulate/backtest keep reading capital/min_to_enter
    # above (the research profile), so parity runs stay comparable to the
    # legacy reference. Legacy values: research = $100k / min 5; production
    # M-book = $25k / min 2. Skip rule is qty <= min in BOTH profiles.
    production_capital: float = 25000
    production_min_to_enter: int = 2
    # Patch 134: the combined book materializes its own inputs into
    # {combined}/input/gate/ (see prepare_inputs.py). The only path config
    # needed is where the FIRST member keeps its universe input files.
    member_input_relpath: str = "input/russell3000"
    gate: GateConfig = GateConfig()
    ladder: LadderConfig = LadderConfig()
    market_conditions: MarketConditions = MarketConditions()
    ladder_by_label: Dict[str, LabelSizing] = {
        "all_days": LabelSizing(second_buy=1.0, third_buy=1.0),
    }
    gate_by_label: Dict[str, LabelGate] = {
        "all_days": LabelGate(rule_tree=_default_gate_tree()),
    }


class CombinedSaveRequest(BaseModel):
    members: List[CombinedMemberIn]
    config: CombinedAllocationConfig


def normalize_combined_config(raw: dict) -> CombinedAllocationConfig:
    """Accept v1 (hardcoded bull/bear) or v2 (labelled) config dicts.

    v1 markers: ladder.curr_hold_above / gate.above_ma_threshold.
    Conversion: bull = close > sma(gate.sma_lookback) on gate.ticker,
    bear = default — byte-equivalent behaviour to the old branch logic.
    """
    raw = dict(raw or {})
    ladder = dict(raw.get("ladder") or {})
    gate = dict(raw.get("gate") or {})
    is_v1 = "curr_hold_above" in ladder or "above_ma_threshold" in gate

    if is_v1:
        sma_lb = int(gate.get("sma_lookback", 200))
        ticker = gate.get("ticker", "spy")
        raw["market_conditions"] = {
            "ticker": ticker,
            "labels": [
                {"label": "bull", "rule_tree": _leaf_tree({
                    "indicator": "close", "lookback": 0, "operator": ">",
                    "value": 0.0, "value_type": "indicator_price",
                    "value_indicator": "sma", "value_lookback": sma_lb})},
                {"label": "bear", "rule_tree": None},
            ],
        }
        cha = ladder.get("curr_hold_above", {}) or {}
        chb = ladder.get("curr_hold_below", {}) or {}
        raw["ladder_by_label"] = {
            "bull": {"second_buy": float(cha.get("1", 1.5)),
                     "third_buy": float(cha.get("2", 0.5))},
            "bear": {"second_buy": float(chb.get("1", 0.5)),
                     "third_buy": float(chb.get("2", 0.5))},
        }
        raw["gate_by_label"] = {
            "bull": {"rule_tree": _leaf_tree({
                "indicator": "ibs", "lookback": 0, "operator": "<",
                "value": float(gate.get("above_ma_threshold", 0.98)),
                "value_type": "value", "value_indicator": "",
                "value_lookback": 0})},
            "bear": {"rule_tree": _leaf_tree({
                "indicator": "ibs", "lookback": 0, "operator": "<",
                "value": float(gate.get("below_ma_threshold", 0.95)),
                "value_type": "value", "value_indicator": "",
                "value_lookback": 0})},
        }
        raw["ladder"] = {"base": float(ladder.get("base", 2.0)),
                         "seed_count": ladder.get("seed_count", {"1": 1.5, "2": 0.5})}
        raw["gate"] = {"enabled": bool(gate.get("enabled", True)), "ticker": ticker}

    # Patch 134: drop superseded path fields from older saved configs
    raw.pop("closes_parquet_relpath", None)
    raw.pop("gate_data_relpath", None)
    # Patch 140/142: migrate older gate shapes -> rule TREES
    gbl = raw.get("gate_by_label") or {}
    for label, g in list(gbl.items()):
        if not isinstance(g, dict):
            continue
        if "ibs_below" in g and "rule_tree" not in g and "rule" not in g:
            gbl[label] = {"rule_tree": _leaf_tree({
                "indicator": "ibs", "lookback": 0, "operator": "<",
                "value": float(g["ibs_below"]), "value_type": "value",
                "value_indicator": "", "value_lookback": 0})}
        elif "rule" in g and "rule_tree" not in g:
            gbl[label] = {"rule_tree": _leaf_tree(g["rule"]) if g["rule"] else None}
    # Patch 142: migrate single-rule condition labels -> trees
    mc = raw.get("market_conditions") or {}
    for item in mc.get("labels", []):
        if isinstance(item, dict) and "rule" in item and "rule_tree" not in item:
            item["rule_tree"] = _leaf_tree(item["rule"]) if item["rule"] else None
            item.pop("rule", None)
    return CombinedAllocationConfig(**raw)