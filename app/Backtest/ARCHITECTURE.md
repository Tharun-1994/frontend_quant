# ETF Backtest Engine – Architecture

## File Structure

```
app/engine/etf/
├── __init__.py                  # Package exports
├── price_data_etf.py            # Loads parquets, provides daily/minute access
├── rule_evaluator.py            # Evaluates nested AND/OR rule trees
├── portfolio_etf.py             # Trade management, P&L, equity tracking
├── backtest_engine_etf.py       # Main loop orchestrator
└── integration_example.py       # Wiring into your existing codebase
```

## Data Flow

```
StrategyRequest (JSON from API)
        │
        ▼
┌──────────────────────┐
│  BacktestEngine      │  ← Your existing class
│  .backtest_tradestation()
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│  ETFBacktestEngine   │  ← New engine
│  .run()              │
└──────────┬───────────┘
           │
     ┌─────┼──────────────────┐
     │     │                  │
     ▼     ▼                  ▼
┌────────┐ ┌──────────────┐ ┌─────────────┐
│ETFPrice│ │RuleEvaluator │ │ETFPortfolio │
│Data    │ │              │ │             │
│        │ │evaluate_tree()│ │enter_trade()│
│load()  │ │              │ │close_trade()│
│minute  │ │              │ │mark_to_mkt()│
│bars    │ │              │ │             │
└────────┘ └──────────────┘ └─────────────┘
```

## Day-Loop Sequence

```
For each day d in all_dates:
  ┌─ START OF DAY ──────────────────────────────────────┐
  │                                                      │
  │  1. PENDING EXIT at OPEN                             │
  │     If scheduled (max_time, next-bar-open TP/SL)     │
  │     → close_trade(open_price)                        │
  │                                                      │
  │  2. INTRADAY SL/TP SCAN (minute bars)                │
  │     For each minute bar:                             │
  │       - Check SL: Low ≤ sl_price → exit              │
  │       - Check TP: High ≥ tp_price → exit             │
  │                                                      │
  │  3. CLOSE SL/TP CHECK                                │
  │     For CLOSE or NEXT_BAR_OPEN timing                │
  │                                                      │
  ├─ END OF DAY ────────────────────────────────────────┤
  │                                                      │
  │  4. MARK TO MARKET (close price)                     │
  │     - Update equity curve                            │
  │     - Track drawdown                                 │
  │     - Increment bars_since_entry                     │
  │                                                      │
  │  ── Only on REBALANCE dates ──                       │
  │                                                      │
  │  5. MAX TIME CHECK                                   │
  │     If bars_since_entry ≥ max_time                   │
  │     → schedule exit for next open                    │
  │                                                      │
  │  6. DETERMINE ACTIVE REGIME                          │
  │     Evaluate market_trend_rules_tree for each regime │
  │     First match wins                                 │
  │                                                      │
  │  7. ENTRY CHECK                                      │
  │     Evaluate entry_rules_tree                        │
  │     → enter_trade(close_price)                       │
  │                                                      │
  └──────────────────────────────────────────────────────┘
```

## Regime Switching Behavior

When a trade is open and the ATR regime flips:
- The **new** regime's SL/TP/max_time parameters apply immediately
- `bars_since_entry` continues counting from original entry
- Example: Enter under Regime 1 (max_time=11, TP=$2000), regime flips at bar 5 →
  now Regime 2's rules apply (max_time=24, SL=$6375, TP=$3750)

## SL/TP Price Calculation (LONG positions)

```
DOLLAR_BASED:
  sl_price = entry_price − (stoploss_dollar / shares)
  tp_price = entry_price + (takeprofit_dollar / shares)

PCT_BASED:
  sl_price = entry_price × (1 − stoploss_pct / 100)
  tp_price = entry_price × (1 + takeprofit_pct / 100)
```

## Minute-Bar SL/TP Logic

```
For each minute bar:
  Stop-Loss:
    if bar.Open ≤ sl_price    → exit at bar.Open (gap below)
    elif bar.Low ≤ sl_price   → exit at sl_price (hit intrabar)

  Take-Profit:
    if bar.Open ≥ tp_price    → exit at bar.Open (gap above)
    elif bar.High ≥ tp_price  → exit at tp_price (hit intrabar)
```

## Timing Modes Supported

| `stoploss_timing` / `takeprofit_timing` | Behavior |
|----------------------------------------|----------|
| `INTRADAY` | Scan minute bars, exit at trigger price |
| `CLOSE` | Check at daily close, exit at close price |
| `NEXT_BAR_OPEN` | Check at daily close, schedule exit for next open |
| `MAX_PROFIT_NEXT_OPEN` | Track peak profit, exit next open when threshold hit |

## Output Files

Written to `{backtest_data_path}/{strategy_name}/output/`:

| File | Content |
|------|---------|
| `trade_list.json` | Array of all trades with entry/exit details |
| `equity_curve.json` | `{date: equity_value}` dictionary |
| `summary.json` | Key stats (P&L, win rate, max DD, etc.) |
| `backtest_result.json` | Everything combined |

## Position Sizing

```python
shares = floor(capital / slots / entry_price)
# Example: floor(37500 / 1 / 148.25) = 252 shares
```

## Rule Tree Evaluation

The `RuleEvaluator` recursively walks the tree:
1. **Group nodes** combine children with AND/OR logic
2. **Rule nodes** resolve LHS (indicator) and RHS (value or indicator) then compare

LHS resolution:
- `close`, `open`, `high`, `low` → daily price
- `atr`, `sma`, `rsi`, etc. → lookup `{indicator}_{lookback}` in parquets

RHS resolution:
- `value_type: "indicator_price"` → lookup another indicator parquet
- `range_close` → lookup `range_close_{percent}` parquet
- Otherwise → static float value
