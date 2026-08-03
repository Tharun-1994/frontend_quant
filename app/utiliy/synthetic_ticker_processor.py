"""
synthetic_ticker_processor.py

LRA Patch 13: arithmetic-only synthetic ticker computation for the universe pipeline.

Given specs from the synthetic_tickers DB table (symbol, formula, fields), compute
new ticker columns and add them to a UniverseDataset's price DataFrames. Optionally
drop source ticker columns afterwards.

Formulas are restricted to arithmetic via an AST node whitelist: BinOp, UnaryOp,
Constant, Name (looked up as a column in the field DataFrame). No sympy, no eval,
no function calls. Reading rows from synthetic_tickers cannot inject code.

Allowed nodes: Module, Expression, Expr, BinOp, UnaryOp, Constant, Num, Name, Load,
Add, Sub, Mult, Div, Pow, USub, UAdd.

Public surface:
    SyntheticSpec(symbol, formula, fields)         dataclass
    SyntheticTickerProcessor(synthetics)           main class
        .apply(dataset, symbols=None, drop_sources=None)
    load_synthetics_from_db(session)               helper for callers with a session
    FormulaError                                   raised on bad formula or missing source
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import List, Optional


_ALLOWED_NODES = (
    ast.Module, ast.Expression, ast.Expr,
    ast.BinOp, ast.UnaryOp,
    ast.Constant, ast.Num,                  # Num for 3.7 back-compat; harmless on 3.8+
    ast.Name, ast.Load,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow,
    ast.USub, ast.UAdd,
)


class FormulaError(Exception):
    """Bad formula, missing source ticker, or disallowed AST node."""


@dataclass
class SyntheticSpec:
    symbol: str                  # e.g. 'CHFAUD'
    formula: str                 # e.g. 'CHFUSD * USDAUD'
    fields: List[str]            # e.g. ['Open', 'High', 'Low', 'Close']

    def source_symbols(self) -> List[str]:
        """Tickers referenced by the formula, parsed from AST Name nodes."""
        tree = ast.parse(self.formula, mode='eval')
        return sorted({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)})


def _validate_ast(node):
    for n in ast.walk(node):
        if not isinstance(n, _ALLOWED_NODES):
            raise FormulaError(
                f"disallowed node {type(n).__name__} in formula"
            )


def _eval_node(node, df):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, df)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Num):                 # deprecated path, kept for safety
        return node.n
    if isinstance(node, ast.Name):
        if node.id not in df.columns:
            raise FormulaError(f"source ticker '{node.id}' not in dataset columns")
        return df[node.id]
    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, df)
        if isinstance(node.op, ast.USub): return -operand
        if isinstance(node.op, ast.UAdd): return +operand
        raise FormulaError(f"unsupported unary op {type(node.op).__name__}")
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, df)
        right = _eval_node(node.right, df)
        if isinstance(node.op, ast.Add):  return left + right
        if isinstance(node.op, ast.Sub):  return left - right
        if isinstance(node.op, ast.Mult): return left * right
        if isinstance(node.op, ast.Div):  return left / right
        if isinstance(node.op, ast.Pow):  return left ** right
        raise FormulaError(f"unsupported binary op {type(node.op).__name__}")
    raise FormulaError(f"unsupported node {type(node).__name__}")


class SyntheticTickerProcessor:
    """
    Construct once with the list of SyntheticSpec rows from DB. All formulas are
    parsed and validated at construction so a bad row in synthetic_tickers fails
    fast, not mid-pipeline.

    Then call apply(dataset) for each universe dataset that needs synthetics.
    """

    def __init__(self, synthetics: List[SyntheticSpec]):
        self.synthetics = synthetics
        self._asts = {}
        for spec in synthetics:
            try:
                tree = ast.parse(spec.formula, mode='eval')
            except SyntaxError as e:
                raise FormulaError(f"{spec.symbol}: parse error: {e}")
            _validate_ast(tree)
            self._asts[spec.symbol] = tree

    def apply(self, dataset,
              symbols: Optional[List[str]] = None,
              drop_sources: Optional[List[str]] = None) -> None:
        """
        Mutate dataset.fields in place.

        symbols       which synthetic symbols to compute. None = all known.
        drop_sources  source-ticker columns to remove from every field after compute.
                      None or [] = keep all columns.

        Silently skips a field if the requested field name (e.g. 'Volume') is not
        present in dataset.fields — different universes may carry different fields.
        """
        symbols = symbols if symbols is not None else [s.symbol for s in self.synthetics]
        drop_sources = drop_sources or []

        spec_by_symbol = {s.symbol: s for s in self.synthetics}

        for symbol in symbols:
            spec = spec_by_symbol.get(symbol)
            if spec is None:
                raise FormulaError(f"synthetic '{symbol}' not declared")
            tree = self._asts[symbol]

            for field_name in spec.fields:
                if field_name not in dataset.fields:
                    continue
                df = dataset.fields[field_name]
                try:
                    series = _eval_node(tree, df)
                except FormulaError:
                    raise
                except Exception as e:
                    raise FormulaError(
                        f"{symbol} on field {field_name}: formula "
                        f"'{spec.formula}' raised {type(e).__name__}: {e}"
                    )
                df[symbol] = series

        if drop_sources:
            for field_name, df in dataset.fields.items():
                cols_to_drop = [c for c in drop_sources if c in df.columns]
                if cols_to_drop:
                    df.drop(columns=cols_to_drop, inplace=True)


def load_synthetics_from_db(session) -> List[SyntheticSpec]:
    """
    Pull every row from the synthetic_tickers table.

    Caller supplies an active SQLAlchemy session. Returns [] if the table is empty.
    Raises if the table is missing (caller should treat that as a config error).
    """
    from sqlalchemy import text
    rows = session.execute(text(
        "SELECT symbol, formula, fields FROM synthetic_tickers"
    )).fetchall()
    return [
        SyntheticSpec(
            symbol=row.symbol,
            formula=row.formula,
            fields=[f.strip() for f in row.fields.split(',')],
        )
        for row in rows
    ]


# ── Self-test ────────────────────────────────────────────────────────────────
# Verify in isolation without DB or pipeline. Run:
#   cd fastapi/app/utiliy/universeGenerations && python synthetic_ticker_processor.py
if __name__ == '__main__':
    import pandas as pd

    # Fake dataset shaped like UniverseDataset.fields
    class _FakeDataset:
        def __init__(self, fields):
            self.fields = fields

    idx = pd.date_range('2024-01-02', periods=3, freq='B')
    chfusd = pd.DataFrame({'CHFUSD': [1.10, 1.11, 1.12], 'OTHER': [9, 9, 9]}, index=idx)
    usdaud = pd.DataFrame({'USDAUD': [1.50, 1.51, 1.52], 'OTHER': [9, 9, 9]}, index=idx)
    # Same OHLC field has both columns side-by-side
    close = pd.DataFrame({
        'CHFUSD': [1.10, 1.11, 1.12],
        'USDAUD': [1.50, 1.51, 1.52],
        'SPY':    [470.0, 471.0, 472.0],
    }, index=idx)
    dataset = _FakeDataset({'Close': close.copy()})

    specs = [SyntheticSpec(
        symbol='CHFAUD',
        formula='CHFUSD * USDAUD',
        fields=['Open', 'High', 'Low', 'Close'],   # Volume etc. would be skipped
    )]

    processor = SyntheticTickerProcessor(specs)
    processor.apply(dataset, drop_sources=['CHFUSD', 'USDAUD'])

    print(dataset.fields['Close'])
    # Expected: SPY column intact, CHFAUD computed (1.65, 1.6761, 1.7024),
    # CHFUSD and USDAUD dropped.

    # Bad formula example — uncomment to confirm rejection
    # SyntheticTickerProcessor([SyntheticSpec('X', '__import__("os").system("ls")', ['Close'])])