"""
regime_label.py — C2.1

Mirrors the engine's regime-label algorithm so middleware can resolve the
string in `response.activeRegimeOnLastBar` back to a MarketRegime ID and
read its `substitute_pool_size`.

Engine source (must stay in sync):
  - BacktestEngineController.java:864-872 (StringBuilder + "_" + trim trailing)
  - MarketTrendServiceV2Impl.buildLabelFromTree (recursive variant for trees)

Algorithm: in-order traversal joining non-blank `rule.label` values with "_".
Same for flat rule lists (the engine treats them identically — see the
fallback branch in MarketTrendServiceV2Impl.generateMarketSignals:315-318).
"""

from __future__ import annotations
from typing import Any, Iterable


def compute_regime_label_from_flat_rules(rules: Iterable[Any]) -> str:
    """Build label from a flat rule list (legacy non-tree path).

    Mirrors BacktestEngineController:864-872:
        StringBuilder marketRuleBuilder = new StringBuilder();
        for (RuleDto rd : conds) {
            marketRuleBuilder.append(rd.getLabel()).append("_");
        }
        if (marketRuleBuilder.length() > 0)
            marketRule = marketRule.substring(0, marketRule.length() - 1);

    Args:
        rules: iterable of objects/dicts with a `.label` (or `['label']`) field.

    Returns:
        Underscore-joined non-blank labels. Empty string if no labels.
    """
    parts = []
    for rule in rules:
        label = _extract_label(rule)
        if label:  # treats None and "" the same — both skipped
            parts.append(label)
    return '_'.join(parts)


def compute_regime_label_from_tree(tree_node: Any) -> str:
    """Build label from a rule tree (current path).

    Mirrors MarketTrendServiceV2Impl.buildLabelFromTree:113-134:
        - leaf node: return its rule's label (or "" if null/blank)
        - group node: in-order traversal of children, join non-blank with "_"

    Args:
        tree_node: dict with shape {"type": "rule"|"group", ...} as
                   produced by the frontend RuleTree builder.

    Returns:
        Underscore-joined labels. Empty string if tree is None/empty.
    """
    if tree_node is None:
        return ''
    return _walk_tree(tree_node)


def _walk_tree(node: Any) -> str:
    """Recursive in-order traversal. Internal."""
    if not isinstance(node, dict):
        return ''

    node_type = node.get('type')

    # Leaf node — extract rule.label
    if node_type == 'rule':
        rule = node.get('rule') or {}
        label = rule.get('label')
        return label if label else ''

    # LRA-shaped leaf (Patch 44 — indicator field directly on node)
    if 'indicator' in node and node_type != 'group':
        label = node.get('label')
        return label if label else ''

    # Group node — join children
    if node_type == 'group':
        children = node.get('children') or []
        parts = []
        for child in children:
            s = _walk_tree(child)
            if s:  # skip blanks
                parts.append(s)
        return '_'.join(parts)

    return ''


def _extract_label(rule: Any) -> str:
    """Pull `label` off a Rule object or dict. Returns "" if missing."""
    if rule is None:
        return ''
    if isinstance(rule, dict):
        return rule.get('label') or ''
    return getattr(rule, 'label', '') or ''