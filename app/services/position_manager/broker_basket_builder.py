"""
broker_basket_builder.py — Builds the 18-column IBKR Basket Trader format
from PROPOSED tradelist rows. Single source of truth for the CSV/XLSX
shape, reusable by:
  - The F2 "Download M_Combined.csv" endpoint (live now, Patch 38)
  - The nightly broker_write step (planned — writes M_Combined_{D}.xlsx)

Format reference: M_Combined20260605.xlsx supplied by RT. 18 cols:
  Account, BasketTag, OrderId, ParentOrderId, Action, Quantity, Symbol,
  SecType, Exchange, Currency, TimeInForce, OrderType, LmtPrice, AuxPrice,
  OCAGroup, Rth, OrderRef, Percentage

Phase 1 conventions:
  - SecType: STK
  - Exchange: SMART/AMEX
  - Currency: USD
  - TimeInForce: DAY
  - OrderType: MKT when limit_price is null/0, else LMT
- OrderRef: strategy.system_code (Patch 81; full name — Vas-style short codes can come later)
  - Percentage: regime.stoploss_pct * 100, blank when stoploss disabled
  - Account: from IBKR_ACCOUNT env var, default 'U14642225'
"""

from __future__ import annotations
import hashlib   # Patch 46: legacy SHA256-based OrderId scheme
import os
from datetime import date
from decimal import Decimal
from typing import Any
from sqlalchemy import case   # Patch 41: SQL Server has no NULLS LAST
from sqlalchemy.orm import Session

from app.models.tradelist import Tradelist
from app.models.strategy_bucket import StrategyBucket
from app.models.market_regime import MarketRegime


# 18-column header in the exact order IBKR Basket Trader expects.
BASKET_COLUMNS = [
    "Account", "BasketTag", "OrderId", "ParentOrderId", "Action", "Quantity",
    "Symbol", "SecType", "Exchange", "Currency", "TimeInForce", "OrderType",
    "LmtPrice", "AuxPrice", "OCAGroup", "Rth", "OrderRef", "Percentage", "Rank",
]


def build_entry_rows(
    *,
    account: str,
    tl: Tradelist,
    strat: StrategyBucket,
    regime: MarketRegime,
    order_id: int,
) -> list[dict[str, Any]]:
    """Patch 80: single source of truth for an entry row + its bracket child.

    Returns [entry] when no stoploss, or [entry, child_stop] when
    stoploss_pct > 0. Shared by build_combined_basket (CSV), build_substitute_basket
    (SUB CSV) AND broker_write.write_broker_basket (xlsx) so OrderId hashing and
    the entry+bracket structure are defined ONCE and cannot drift between the
    three generators.

    Entry order type (mirrors legacy Daily_Orders):
      NORMAL + open  -> OPG MKT      NORMAL + close -> DAY MOC
      LIMIT  + open  -> DAY LMT      LIMIT  + close -> DAY LOC   (LIMIT_ATR == LIMIT)
    Child stop (only when stoploss_pct > 0), via _build_child_stop_row:
      INTRADAY -> DAY STP            EOD -> DAY STPMOC
      AuxPrice: NORMAL = blank (IBKR derives stop from Percentage x fill price);
                LIMIT / LIMIT_ATR = engine initial_stop_price (handles PCT and ATR).
      ParentOrderId on the child = this entry order_id (bracket linkage).
    """
    direction         = (tl.direction or "").upper()
    basket_tag        = "long" if direction == "LONG" else "short"
    action            = "BUY" if direction == "LONG" else "SELL"
    regime_order_type = (regime.order_type or "NORMAL").upper()
    entry_timing      = (regime.entry_timing or "open").lower()
    limit_price_val   = _decimal_to_float(tl.limit_price)

    if regime_order_type == "NORMAL":
        if entry_timing == "open":
            time_in_force, ibkr_order_type = "OPG", "MKT"
        else:  # close
            time_in_force, ibkr_order_type = "DAY", "MOC"
        lmt_price = ""
    elif regime_order_type in ("LIMIT", "LIMIT_ATR"):
        time_in_force   = "DAY"
        ibkr_order_type = "LMT" if entry_timing == "open" else "LOC"
        lmt_price = (
            round(limit_price_val, 4)
            if limit_price_val and limit_price_val > 0 else ""
        )
    else:
        print(f"[broker_basket_builder] WARN unknown order_type="
              f"{regime_order_type!r} for strategy={strat.name}; "
              f"falling back to OPG/MKT")
        time_in_force, ibkr_order_type, lmt_price = "OPG", "MKT", ""

    # Percentage = stoploss_pct (DB stores 20 for 20%). Applies to all order types.
    stoploss_pct = _decimal_to_float(regime.stoploss_pct)
    percentage = round(stoploss_pct, 4) if stoploss_pct and stoploss_pct > 0 else ""

    # Entry AuxPrice = engine stop price for LIMIT/LIMIT_ATR (known at proposal),
    # blank for NORMAL (fill price unknown until the open).
    initial_stop_val = _decimal_to_float(tl.initial_stop_price)
    aux_price = (
        round(initial_stop_val, 4)
        if regime_order_type in ("LIMIT", "LIMIT_ATR")
           and initial_stop_val and initial_stop_val > 0
        else ""
    )

    rows: list[dict[str, Any]] = [{
        "Account": account,
        "BasketTag": basket_tag,
        "OrderId": order_id,
        "ParentOrderId": "",
        "Action": action,
        "Quantity": int(tl.intended_qty or 0),
        "Symbol": tl.symbol,
        "SecType": "STK",
        "Exchange": "SMART/AMEX",
        "Currency": "USD",
        "TimeInForce": time_in_force,
        "OrderType": ibkr_order_type,
        "LmtPrice": lmt_price,
        "AuxPrice": aux_price,
        "OCAGroup": "",
        "Rth": "=FALSE()",
        "OrderRef": strat.system_code or strat.name,  # Patch 81: system_code (Vas short code), not name
        "Percentage": percentage,
        "Rank": int(tl.ranking_rank) if tl.ranking_rank is not None else "",
    }]

    # Child bracket stop — only when a stoploss is configured.
    if stoploss_pct and stoploss_pct > 0:
        stoploss_timing = (regime.stoploss_timing or "EOD").upper()
        child_action    = "SELL" if action == "BUY" else "BUY"
        rows.append(_build_child_stop_row(
            account=account,
            basket_tag=basket_tag,
            action=child_action,
            quantity=int(tl.intended_qty or 0),
            symbol=tl.symbol,
            stoploss_timing=stoploss_timing,
            regime_order_type=regime_order_type,
            stop_price=initial_stop_val,
            stoploss_pct=stoploss_pct,
            parent_order_id=order_id,
            strategy_name=strat.system_code or strat.name,  # Patch 81: system_code
        ))

    return rows


def build_combined_basket(
    db: Session,
    trade_date: date,
) -> list[dict[str, Any]]:
    """Return basket rows (one per PROPOSED tradelist row) in IBKR format.

    Pulls every PROPOSED row on TRADED ledger where intended_trade_date
    matches AND the row belongs to an execution_enabled strategy.

    SUBSTITUTE_POOL rows are NOT included — those are backups, not basket.

    Args:
        db: SQLAlchemy session.
        trade_date: intended_trade_date filter.

    Returns:
        List of dicts, one per row, keyed by BASKET_COLUMNS. Empty list
        when no PROPOSED rows exist for this date.
    """
    account = os.environ.get("IBKR_ACCOUNT", "U14642225")

    # Pull PROPOSED rows joined with their strategy + regime (for OrderRef
    # name and Percentage's stoploss_pct).
    rows = (
        db.query(Tradelist, StrategyBucket, MarketRegime)
        .join(StrategyBucket, Tradelist.strategy_id == StrategyBucket.id)
        .join(MarketRegime, Tradelist.entered_regime_id == MarketRegime.id)
        .filter(
            Tradelist.intended_trade_date == trade_date,
            Tradelist.ledger == "TRADED",
            Tradelist.status == "PROPOSED",
            StrategyBucket.execution_enabled == True,   # Patch 42: SQL Server emits "= 1"; .is_(True) emits invalid "IS 1"
        )
        .order_by(
            StrategyBucket.name.asc(),
            # Patch 41: portable NULLS LAST for SQL Server compatibility.
            case((Tradelist.ranking_rank.is_(None), 1), else_=0).asc(),
            Tradelist.ranking_rank.asc(),
        )
        .all()
    )

    basket: list[dict[str, Any]] = []
    # Patch 46: per-strategy OrderId counter seeded from SHA256(strategy_name+'D').
    # First row of a strategy → base+1, second → base+2, etc. Matches legacy
    # Daily_Orders.enter_entry_signals counter semantics.
    order_id_counters: dict[str, int] = {}

    for tl, strat, regime in rows:
        # Initialize this strategy's counter on first encounter, then bump.
        if strat.name not in order_id_counters:
            order_id_counters[strat.name] = _strategy_orderid_base(strat.name)
        order_id_counters[strat.name] += 1
        order_id = order_id_counters[strat.name]

        # Patch 80: entry + bracket child built by the shared builder.
        basket.extend(build_entry_rows(
            account=account, tl=tl, strat=strat, regime=regime, order_id=order_id,
        ))

    return basket


def basket_to_csv_string(basket: list[dict[str, Any]]) -> str:
    """Render a basket as a CSV string with the 18 columns in fixed order."""
    import io
    import csv
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=BASKET_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in basket:
        writer.writerow(row)
    return buf.getvalue()


def build_substitute_basket(
    db: Session,
    trade_date: date,
) -> list[dict[str, Any]]:
    """Return SUBSTITUTE_POOL rows in IBKR basket format for trade_date.

    Same 19-column layout as the main basket (BASKET_COLUMNS).
    OrderId, ParentOrderId, OCAGroup all blank — subs are reference-only,
    not loaded into IBKR directly. Vas reads this file to pick
    substitute_symbol when filling the morning substitution CSV.
    """
    account = os.environ.get("IBKR_ACCOUNT", "U14642225")

    rows = (
        db.query(Tradelist, StrategyBucket, MarketRegime)
        .join(StrategyBucket, Tradelist.strategy_id == StrategyBucket.id)
        .join(MarketRegime, Tradelist.entered_regime_id == MarketRegime.id)
        .filter(
            Tradelist.intended_trade_date == trade_date,
            Tradelist.ledger == "TRADED",
            Tradelist.status == "SUBSTITUTE_POOL",
            StrategyBucket.execution_enabled == True,
        )
        .order_by(
            StrategyBucket.name.asc(),
            case((Tradelist.ranking_rank.is_(None), 1), else_=0).asc(),
            Tradelist.ranking_rank.asc(),
        )
        .all()
    )

    basket: list[dict[str, Any]] = []
    # Patch 80: hashed per-strategy OrderId (was ranking_rank) so the SUB file
    # matches the main/test basket scheme — base = SHA256(name+"D")[:5], +1/row.
    order_id_counters: dict[str, int] = {}
    for tl, strat, regime in rows:
        if strat.name not in order_id_counters:
            order_id_counters[strat.name] = _strategy_orderid_base(strat.name)
        order_id_counters[strat.name] += 1
        order_id = order_id_counters[strat.name]
        basket.extend(build_entry_rows(
            account=account, tl=tl, strat=strat, regime=regime, order_id=order_id,
        ))

    return basket


def substitute_to_csv_string(basket: list[dict[str, Any]]) -> str:
    """Render substitute basket as CSV string using same BASKET_COLUMNS layout."""
    import io
    import csv
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=BASKET_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in basket:
        writer.writerow(row)
    return buf.getvalue()


def _build_child_stop_row(
    account: str,
    basket_tag: str,
    action: str,
    quantity: int,
    symbol: str,
    stoploss_timing: str,
    regime_order_type: str,  # 'NORMAL' | 'LIMIT' | 'LIMIT_ATR'
    stop_price: float | None,
    stoploss_pct: float,
    parent_order_id: int,
    strategy_name: str,
) -> dict:
    """Build the child bracket stop row linked to a parent entry order.

    stoploss_timing:
      INTRADAY → STP    DAY  (fires intraday)
      EOD      → STPMOC DAY  (fires at end of day)

    AuxPrice logic:
      NORMAL order: entry price unknown at proposal time (MKT fills at open).
        AuxPrice = blank. IBKR computes the actual stop using Percentage
        field applied to the fill price. e.g. Percentage=20 → IBKR sets
        stop at fill_price * 0.80 automatically.

      LIMIT / LIMIT_ATR order: engine computed stop_price at proposal time.
        AuxPrice = initial_stop_price (absolute price). IBKR uses this
        exact price as the stop trigger.
    """
    timing          = (stoploss_timing or 'EOD').upper()
    order_type_up   = (regime_order_type or 'NORMAL').upper()
    ibkr_order_type = 'STP' if timing == 'INTRADAY' else 'STPMOC'

    # AuxPrice: only for LIMIT orders where stop price is known at proposal
    if order_type_up in ('LIMIT', 'LIMIT_ATR') and stop_price and stop_price > 0:
        aux_price = round(stop_price, 4)
    else:
        aux_price = ''   # NORMAL: IBKR derives stop from Percentage × fill_price

    return {
        'Account':       account,
        'BasketTag':     basket_tag,
        'OrderId':       '',
        'ParentOrderId': parent_order_id,
        'Action':        action,
        'Quantity':      quantity,
        'Symbol':        symbol,
        'SecType':       'STK',
        'Exchange':      'SMART/AMEX',
        'Currency':      'USD',
        'TimeInForce':   'DAY',
        'OrderType':     ibkr_order_type,
        'LmtPrice':      '',
        'AuxPrice':      aux_price,
        'OCAGroup':      '',
        'Rth':           '=FALSE()',
        'OrderRef':      strategy_name,
        'Percentage':    round(stoploss_pct, 2) if stoploss_pct else '',
        'Rank':          '',
    }


def _decimal_to_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _strategy_orderid_base(strategy_name: str) -> int:
    """SHA256-derived 5-digit OrderId base for a strategy.

    Patch 46: matches the legacy Daily_Orders.__init__ scheme exactly:
        c = strategy_name + "D"
        unique_number = int(sha256(c).hexdigest(), 16)
        base = int(str(unique_number)[:5])
    Then the basket builder increments the base by 1 for each row of
    that strategy. This preserves the OrderId range Vas is used to
    seeing in M_Combined files (5-digit numbers like 35588, 35589, ...).

    Collision risk: with ~10⁵ possible bases and few strategies, distinct
    bases are almost certain. Brackets within one strategy stay clustered
    in their own number range, mirroring legacy behavior.
    """
    c = strategy_name + "D"
    unique_number = int(hashlib.sha256(c.encode()).hexdigest(), 16)
    return int(str(unique_number)[:5])