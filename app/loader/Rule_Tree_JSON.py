import json
from typing import Any, Dict, Optional

RuleTree = Dict[str, Any]
BOOLEAN_INDICATORS = {"n_week_high_recent", "n_week_low_recent"}  # adjust names
def dumps_tree(tree) -> str:
    if tree is None:
        return None
    if hasattr(tree, "dict"):  # Pydantic model
        tree = tree.dict(exclude_none=True)
    return json.dumps(tree, ensure_ascii=False)

def loads_tree(raw: Optional[str]) -> Optional[RuleTree]:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def normalize_rule(rule: Dict[str, Any]) -> Dict[str, Any]:
    ind = rule.get("indicator")

    if ind in BOOLEAN_INDICATORS:
        # ✅ boolean rules must rely on params, not comparisons
        rule["operator"] = "IS_TRUE"
        rule["value_type"] = "value"
        rule["value"] = 1

        # ✅ clear stale compare fields that cause "unadjusted_close" leak
        rule["value_indicator"] = ""
        rule["value_lookback"] = 0

        # ✅ ensure params exists with defaults (update keys to your meta)
        p = rule.get("params") or {}
        p.setdefault("n_week_days", 252)
        p.setdefault("within_days", 20)
        rule["params"] = p

    return rule


def normalize_rules_tree(node: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handles both:
      - leaf: { "type": "rule", "rule": {...} }
      - group:{ "type": "group", "logic": "AND", "children": [...] }
    """
    if node and node.get("type") == "rule":
        node["rule"] = normalize_rule(node.get("rule") or {})
        return node

    if  node and node.get("type") == "group":
        kids = node.get("children") or []
        node["children"] = [normalize_rules_tree(k) for k in kids]
        return node

    return node

def rule_to_expr(r: dict) -> str:
    ind = r.get("indicator")
    if not ind:
        return ""

    # ✅ boolean indicator uses params
    if ind in BOOLEAN_INDICATORS:
        p = r.get("params") or {}
        window_days = p.get("window_days", 252)
        occurred_within_days = p.get("occurred_within_days", 20)
        return f"{ind}({window_days},{occurred_within_days})"