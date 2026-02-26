"""
Rule Tree Evaluator (Optimized)
================================
Evaluates nested AND/OR rule trees. All indicator lookups are O(1) dict-based
via the optimized ETFPriceData.
"""

from datetime import date
from typing import Optional, Dict, Any

# Operator lookup table (avoids if/elif chain)
_OP_MAP = {
    '>':  lambda a, b: a > b,
    '<':  lambda a, b: a < b,
    '>=': lambda a, b: a >= b,
    '<=': lambda a, b: a <= b,
    '==': lambda a, b: a == b,
    '=':  lambda a, b: a == b,
    '!=': lambda a, b: a != b,
    'crosses_above': lambda a, b: a > b,
    'crosses_below': lambda a, b: a < b,
}


class RuleEvaluator:

    @staticmethod
    def evaluate_tree(tree: Optional[Dict[str, Any]],
                      d: date,
                      price_data,
                      ticker: str = 'spy') -> bool:
        if not tree:
            return True
        children = tree.get('children')
        if not children:
            return True
        return _eval_node(tree, d, price_data, ticker)


# ======================================================================
#  Module-level functions (avoid method dispatch overhead in hot loop)
# ======================================================================

def _eval_node(node: dict, d: date, pd_, ticker: str) -> bool:
    ntype = node.get('type', '')

    if ntype == 'rule':
        return _eval_rule(node.get('rule', {}), d, pd_, ticker)

    if ntype == 'group':
        children = node.get('children')
        if not children:
            return True
        logic = (node.get('logic') or 'AND')
        if logic == 'AND':
            for c in children:
                if not _eval_node(c, d, pd_, ticker):
                    return False
            return True
        else:  # OR
            for c in children:
                if _eval_node(c, d, pd_, ticker):
                    return True
            return False

    return True


def _eval_rule(rule: dict, d: date, pd_, ticker: str) -> bool:
    if not rule:
        return True

    # --- LHS ---
    indicator = rule.get('indicator', '')
    lookback = rule.get('lookback') or 0

    if indicator == 'close':
        lhs = pd_.get_daily_close(d)
    elif indicator == 'open':
        lhs = pd_.get_daily_open(d)
    elif indicator == 'high':
        lhs = pd_.get_daily_high(d)
    elif indicator == 'low':
        lhs = pd_.get_daily_low(d)
    else:
        lhs = pd_.get_indicator_value(f'{indicator}_{lookback}', d)

    if lhs is None:
        return False

    # --- RHS ---
    value_type = rule.get('value_type', '')

    if value_type == 'indicator_price':
        vi = rule.get('value_indicator', '')
        vl = rule.get('value_lookback') or 0
        vrp = rule.get('value_range_percent') or 0

        if vi == 'range_close':
            rhs = pd_.get_indicator_value(f'range_close_{vrp}', d)
        elif vi == 'close':
            rhs = pd_.get_daily_close(d)
        elif vi == 'open':
            rhs = pd_.get_daily_open(d)
        elif vi == 'high':
            rhs = pd_.get_daily_high(d)
        elif vi == 'low':
            rhs = pd_.get_daily_low(d)
        else:
            rhs = pd_.get_indicator_value(f'{vi}_{vl}', d)
    else:
        try:
            rhs = float(rule.get('value', 0))
        except (TypeError, ValueError):
            rhs = 0.0

    if rhs is None:
        return False

    # --- Compare ---
    op_fn = _OP_MAP.get(rule.get('operator', ''))
    if op_fn is None:
        return False
    return op_fn(lhs, rhs)
