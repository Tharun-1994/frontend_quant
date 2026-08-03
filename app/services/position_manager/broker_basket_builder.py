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

    # Patch 98: Percentage semantics split by order type (legacy M_Combined format).
    #   NORMAL          -> parent Percentage = stoploss_pct (IBKR derives the stop
    #                      from Percentage x fill price; entry price unknown here).
    #   LIMIT/LIMIT_ATR -> parent Percentage = blank. Absolute prices are known,
    #                      so the children carry absolute OFFSETS in Percentage:
    #                      stop child = limit - stop, TP child = tp - limit.
    stoploss_pct = _decimal_to_float(regime.stoploss_pct)
    is_priced    = regime_order_type in ("LIMIT", "LIMIT_ATR")
    if is_priced:
        percentage = ""
    else:
        percentage = round(stoploss_pct, 4) if stoploss_pct and stoploss_pct > 0 else ""

    # Patch 98: parent AuxPrice is ALWAYS blank (legacy format). The stop price
    # lives on the child STP row's AuxPrice, never on the entry row.
    initial_stop_val = _decimal_to_float(tl.initial_stop_price)
    initial_tp_val   = _decimal_to_float(getattr(tl, "initial_tp_price", None))
    aux_price = ""

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
        "OrderRef": _order_ref_for(strat, tl),  # Patch 81 base + Patch 162 subsystem suffix
        "Percentage": percentage,
        "Rank": int(tl.ranking_rank) if tl.ranking_rank is not None else "",
    }]

    # Patch 163: unconditional END-OF-DAY close for day-trade books.
    # exit_timing='eod_close' with an INTRADAY stop yields a plain STP
    # child (fires intraday) — nothing closed the position at the bell.
    # The legacy M_LDEQ_54 basket does it with an explicit SELL MOC
    # sibling (legacy layout kept: parent LMT, MOC, STP). Guards against
    # double-closing: a close-timed entry (already MOC/LOC) or an
    # EOD-timed stop (child becomes STPMOC, which IS the close) skip it —
    # every pre-existing book is byte-identical.
    stoploss_timing = (regime.stoploss_timing or "EOD").upper()
    exit_timing_lc  = (regime.exit_timing or "").lower()
    add_moc_close = (
        exit_timing_lc == "eod_close"
        and entry_timing != "close"
        and stoploss_timing != "EOD"
    )
    if add_moc_close:
        moc_action = "SELL" if action == "BUY" else "BUY"
        rows.append(_build_child_moc_row(
            account=account,
            basket_tag=basket_tag,
            action=moc_action,
            quantity=int(tl.intended_qty or 0),
            symbol=tl.symbol,
            parent_order_id=order_id,
            order_ref=_order_ref_for(strat, tl),
        ))

    # Child bracket stop — only when a stoploss is configured.
    if stoploss_pct and stoploss_pct > 0:
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
            strategy_name=_order_ref_for(strat, tl),  # Patch 162: children share the suffixed ref
            limit_price=limit_price_val,  # Patch 98: for Percentage = |limit - stop| offset
            # Patch 163: the legacy-54 3-row bracket carries the stoploss
            # PERCENTAGE (e.g. 7) on the STP child; 2-row books keep the
            # Patch-98 offset format.
            percentage_override=(stoploss_pct if add_moc_close else None),
        ))

    # Patch 98: child take-profit LMT row — only for LIMIT/LIMIT_ATR entries
    # where the engine computed a TP at proposal time (initial_tp_price > 0).
    # Legacy format: SELL LMT DAY, LmtPrice = TP price, Percentage = |tp - limit|.
    # NORMAL orders never get a TP child (tpPrice is null — entry price unknown).
    if is_priced and initial_tp_val and initial_tp_val > 0:
        child_action = "SELL" if action == "BUY" else "BUY"
        rows.append(_build_child_tp_row(
            account=account,
            basket_tag=basket_tag,
            action=child_action,
            quantity=int(tl.intended_qty or 0),
            symbol=tl.symbol,
            tp_price=initial_tp_val,
            limit_price=limit_price_val,
            parent_order_id=order_id,
            strategy_name=_order_ref_for(strat, tl),  # Patch 162
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


def _order_ref_for(strat, tl) -> str:
    """Patch 162: OrderRef = the strategy's base ref (system_code or name —
    Patch 81) plus, for combined-book rows, the subsystem's system_code
    carried on the tradelist row (subsystem_ref: '1'/'5'/'6' for the CRDT
    members): M_LDEQ_54_1 / _5 / _6 — on PROPOSED and SUBSTITUTE rows
    alike, so every broker line names its subsystem. Non-combined rows
    have subsystem_ref NULL and keep the exact Patch-81 ref. Children
    share the parent's ref (grouping)."""
    # Patch 163 (corrects 162's composition): the legacy M_LDEQ_54 basket
    # shows the subsystem ref VERBATIM as the whole OrderRef (M_LDEQ_54A —
    # not base+"_"+code, which 162 produced as M_LDEQ_54_M_LDEQ_54A). The
    # member's system_code, carried per row as subsystem_ref, IS the ref.
    sub = getattr(tl, 'subsystem_ref', None)
    if sub:
        return str(sub)
    return strat.system_code or strat.name


def _build_child_moc_row(
    account: str,
    basket_tag: str,
    action: str,
    quantity: int,
    symbol: str,
    parent_order_id: int,
    order_ref: str,
) -> dict:
    """Patch 163: unconditional end-of-day close sibling (SELL MOC DAY) for
    eod_close day-trade books — matches the legacy M_LDEQ_54 layout exactly
    (all price fields blank; the closing auction sets the price)."""
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
        'OrderType':     'MOC',
        'LmtPrice':      '',
        'AuxPrice':      '',
        'OCAGroup':      '',
        'Rth':           '=FALSE()',
        'OrderRef':      order_ref,
        'Percentage':    '',
        'Rank':          '',
    }


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
    limit_price: float | None = None,  # Patch 98: entry limit for offset computation
    percentage_override: float | None = None,  # Patch 163: pct-on-child (legacy 54)
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

    # Patch 98: Percentage on the stop child.
    #   LIMIT/LIMIT_ATR with both prices known -> absolute offset |limit - stop|
    #     (legacy M_Combined format, e.g. entry 56.50 / stop 45.20 -> 11.30).
    #     abs() keeps this direction-agnostic for SHORT (stop above limit).
    #   NORMAL (or missing prices)             -> stoploss_pct (IBKR needs it).
    if percentage_override is not None and percentage_override > 0:
        # Patch 163: the eod_close 3-row bracket (legacy M_LDEQ_54) carries
        # the stoploss PERCENTAGE here (legacy sample: 7). The offset
        # format below stays for every existing 2-row LIMIT book —
        # verified against the production M_Combined xlsx (Patch 98).
        percentage = round(percentage_override, 2)
    elif (order_type_up in ('LIMIT', 'LIMIT_ATR')
            and limit_price and limit_price > 0
            and stop_price and stop_price > 0):
        percentage = round(abs(limit_price - stop_price), 2)
    else:
        percentage = round(stoploss_pct, 2) if stoploss_pct else ''

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
        'Percentage':    percentage,   # Patch 98: offset for LIMIT/LIMIT_ATR, pct for NORMAL
        'Rank':          '',
    }


def _build_child_tp_row(
    account: str,
    basket_tag: str,
    action: str,
    quantity: int,
    symbol: str,
    tp_price: float,
    limit_price: float | None,
    parent_order_id: int,
    strategy_name: str,
) -> dict:
    """Patch 98: child take-profit LMT row linked to a parent entry order.

    Legacy M_Combined format (verified against Vas's file, e.g. AXTI):
      OrderType  = LMT, TimeInForce = DAY
      LmtPrice   = absolute TP price (entry 56.50 -> TP LmtPrice 73.55)
      AuxPrice   = blank (LMT orders use LmtPrice; AuxPrice is STP-only)
      Percentage = absolute offset |tp - limit| (73.55 - 56.50 = 17.05)
      ParentOrderId = entry order id; IBKR OCA-links same-parent children,
      so the stop and TP cancel each other on fill. OCAGroup stays blank,
      matching the legacy file.

    Only called for LIMIT/LIMIT_ATR entries with engine-computed tp_price.
    """
    if limit_price and limit_price > 0:
        percentage = round(abs(tp_price - limit_price), 2)
    else:
        percentage = ''

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
        'OrderType':     'LMT',
        'LmtPrice':      round(tp_price, 4),
        'AuxPrice':      '',
        'OCAGroup':      '',
        'Rth':           '=FALSE()',
        'OrderRef':      strategy_name,
        'Percentage':    percentage,
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