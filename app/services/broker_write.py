"""
broker_write.py — D2 (morning broker basket emission)

Reads PENDING_FILL rows for a given intended_trade_date and writes
M_Combined_{YYYYMMDD}.xlsx in IBKR Basket Trader format.

Sequence:
  0. Write STP DAY rows for all LIVE positions that have current_stop_price set.
     Each LIVE position gets its own unique OCA group number so that a STP and
     a future LOC/LMT take-profit for the same symbol share one OCA group.
  1. Auto-promote any remaining PROPOSED → PENDING_FILL.
  1.5 Read PENDING_EXIT rows → EXIT_SUBMITTED, write OPG MKT SELL rows.
      OPG exits get their own separate OCA group (different from STP OCA).
  2. Read all PENDING_FILL rows for the date (TRADED ledger only).
  3. Map each row to an IBKR Basket Trader row.
  4. Write XLSX to <backtestPath>/broker_output/{YYYYMMDD}/.

IBKR Basket Trader columns (19, in order):
  Account, BasketTag, OrderId, ParentOrderId, Action, Quantity, Symbol,
  SecType, Exchange, Currency, TimeInForce, OrderType, LmtPrice, AuxPrice,
  OCAGroup, Rth, OrderRef, Percentage, Rank

Production conventions (verified from M_Combined20260224.xlsx):
  - Rth:         '=FALSE()' string (Excel formula), not Python bool
  - STP rows:    TimeInForce='DAY' (re-submitted each morning via basket)
  - OCAGroup:    Each LIVE position gets its own unique OCA number always.
                 STP and LOC/LMT for same symbol share that OCA.
                 OPG MKT exits get a SEPARATE OCA (different number).
  - Percentage:  Appears on STP child rows and STPMOC rows only.
                 Standalone STP stop rows leave Percentage blank.
"""

from __future__ import annotations
import os
import traceback
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional, Union

from sqlalchemy.orm import Session
from openpyxl import Workbook
from openpyxl.styles import Font

from app.models.tradelist import Tradelist
from app.models.market_regime import MarketRegime
from app.models.strategy_bucket import StrategyBucket
from app.models.eod_run_log import EodRunLog
from app.constants.PricePath import PricePath
from app.services.position_manager.broker_basket_builder import (
    build_entry_rows,
    _strategy_orderid_base,
)

IBKR_BASKET_HEADERS = [
    'Account', 'BasketTag', 'OrderId', 'ParentOrderId',
    'Action', 'Quantity', 'Symbol', 'SecType', 'Exchange', 'Currency',
    'TimeInForce', 'OrderType', 'LmtPrice', 'AuxPrice',
    'OCAGroup', 'Rth', 'OrderRef', 'Percentage', 'Rank',
]

# Rth value matches production — Excel formula string, not Python bool
RTH = '=FALSE()'

IBKR_ACCOUNT = os.getenv('IBKR_ACCOUNT', 'U14642225')


def write_broker_basket(
    db: Session,
    trade_date: date,
    output_dir: Optional[str] = None,
) -> dict:
    """Generate M_Combined_{trade_date}.xlsx and write to disk.

    Args:
        db:          SQLAlchemy session.
        trade_date:  the intended_trade_date to filter PENDING_FILL rows on.
        output_dir:  where to write the file. Default
                     <backtestPath>/broker_output/{YYYYMMDD}/.

    Returns:
        Summary dict with file_path, orders_written, stop_rows_written,
        exits_written, promoted_proposed counts and eod_run_log id.

    Raises:
        Re-raises any error after rollback + FAILED log writeback.
    """
    if output_dir is None:
        output_dir = str(
            Path(PricePath.backtestPath)
            / 'broker_output'
            / trade_date.strftime('%Y%m%d')
        )
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    file_path = Path(output_dir) / f'M_Combined_{trade_date.strftime("%Y%m%d")}.xlsx'

    # eod_run_log row committed independently so it survives a rollback
    log_row = EodRunLog(
        run_date=trade_date,
        step='broker_write',
        strategy_id=None,
        status='RUNNING',
    )
    db.add(log_row)
    db.commit()

    summary = {
        'eod_run_log_id':    log_row.id,
        'trade_date':        trade_date.isoformat(),
        'file_path':         str(file_path),
        'promoted_proposed': 0,
        'exits_written':     0,
        'stop_rows_written': 0,
        'orders_written':    0,
    }

    print(f'[broker_write] trade_date={trade_date} output={file_path}')

    try:
        # ── Step 1: Auto-promote remaining PROPOSED → PENDING_FILL ───────────
        proposed_rows = (
            db.query(Tradelist)
            .filter(
                Tradelist.intended_trade_date == trade_date,
                Tradelist.ledger == 'TRADED',
                Tradelist.status == 'PROPOSED',
            )
            .all()
        )
        for r in proposed_rows:
            r.status = 'PENDING_FILL'
        summary['promoted_proposed'] = len(proposed_rows)
        db.flush()
        if proposed_rows:
            print(f'[broker_write] auto-promoted {len(proposed_rows)} PROPOSED → PENDING_FILL '
                  f'(implicit "kept by trader")')

        # ── Step 1.5: PENDING_EXIT → EXIT_SUBMITTED ───────────────────────────
        exit_rows = (
            db.query(Tradelist)
            .filter(
                Tradelist.exit_date == trade_date,
                Tradelist.ledger == 'TRADED',
                Tradelist.status == 'PENDING_EXIT',
            )
            .order_by(Tradelist.strategy_id, Tradelist.id)
            .all()
        )
        for r in exit_rows:
            r.status = 'EXIT_SUBMITTED'
        summary['exits_written'] = len(exit_rows)
        db.flush()
        if exit_rows:
            print(f'[broker_write] {len(exit_rows)} PENDING_EXIT row(s) → EXIT_SUBMITTED')

        # ── Step 2: Read PENDING_FILL rows ────────────────────────────────────
        pending_rows = (
            db.query(Tradelist)
            .filter(
                Tradelist.intended_trade_date == trade_date,
                Tradelist.ledger == 'TRADED',
                Tradelist.status == 'PENDING_FILL',
            )
            .order_by(Tradelist.strategy_id, Tradelist.ranking_rank, Tradelist.id)
            .all()
        )
        if not pending_rows:
            print(f'[broker_write] no PENDING_FILL rows for {trade_date}')

        # ── Step 2.5: Read LIVE rows for stop-monitoring STP orders ──────────
        live_rows = (
            db.query(Tradelist)
            .filter(
                Tradelist.ledger == 'TRADED',
                Tradelist.status == 'LIVE',
                Tradelist.current_stop_price.isnot(None),
            )
            .order_by(Tradelist.strategy_id, Tradelist.id)
            .all()
        )
        print(f'[broker_write] {len(live_rows)} LIVE row(s) with stop price for monitoring')

        # ── Step 3: Build XLSX ────────────────────────────────────────────────
        wb = Workbook()
        ws = wb.active
        ws.title = f'Basket_{trade_date.strftime("%Y%m%d")}'

        ws.append(IBKR_BASKET_HEADERS)
        for cell in ws[1]:
            cell.font = Font(bold=True)

        # ── Lookup caches + helpers ───────────────────────────────────────────
        regime_cache:   dict = {}
        strategy_cache: dict = {}

        def _get_regime(regime_id: int) -> Optional[MarketRegime]:
            if regime_id not in regime_cache:
                regime_cache[regime_id] = (
                    db.query(MarketRegime).filter_by(id=regime_id).first()
                )
            return regime_cache[regime_id]

        def _get_strategy(strategy_id: int) -> Optional[StrategyBucket]:
            if strategy_id not in strategy_cache:
                strategy_cache[strategy_id] = (
                    db.query(StrategyBucket).filter_by(id=strategy_id).first()
                )
            return strategy_cache[strategy_id]

        # ── OCA group assignment ──────────────────────────────────────────────
        # Production rule (verified from M_Combined20260224.xlsx):
        #
        #   LIVE position STP rows  → each symbol gets its OWN unique OCA.
        #     STP OCA=2775  ← LIVE stop for GPC
        #     LOC OCA=2775  ← take-profit for GPC (same OCA as STP)
        #
        #   OPG MKT exit rows → separate OCA pool (completely different numbers).
        #     MKT OCA=1103  ← exit signal for GPC (different from STP OCA)
        #
        # This means: OPG MKT exits are NOT in the same OCA as the STP.
        # IBKR handles this correctly — the exit fills at open which is before
        # the STP can trigger intraday. Trader cancels STP manually after fill.
        #
        # OCA counter shared across both pools to ensure uniqueness.

        oca_counters: dict = {}  # strategy_name → current oca base
        oca_idx = 0              # global increment across all symbols

        # Pool 1: LIVE position OCA (for STP + future LOC/LMT pairs)
        live_oca: dict = {}      # symbol → OCA number
        for row in live_rows:
            strategy   = _get_strategy(row.strategy_id)
            strat_name = strategy.name if strategy else f'sid{row.strategy_id}'
            if strat_name not in oca_counters:
                oca_counters[strat_name] = _oca_base(strat_name)
            oca_idx += 1
            live_oca[row.symbol] = oca_counters[strat_name] + oca_idx

        # Pool 2: OPG MKT exit OCA (separate numbers from live_oca)
        exit_oca: dict = {}      # symbol → OCA number
        for row in exit_rows:
            strategy   = _get_strategy(row.strategy_id)
            strat_name = strategy.name if strategy else f'sid{row.strategy_id}'
            if strat_name not in oca_counters:
                oca_counters[strat_name] = _oca_base(strat_name)
            oca_idx += 1
            exit_oca[row.symbol] = oca_counters[strat_name] + oca_idx

        # ── Row order: STP stops → OPG exits → BUY entries ───────────────────
        # Patch 80: hashed per-strategy OrderId for entries (was sequential 1,2,3..).
        order_id_counters: dict[str, int] = {}

        # 3a. Stop-monitoring STP DAY rows (existing LIVE positions)
        #     TimeInForce = DAY — re-submitted each morning via basket.
        #     OCAGroup    = unique per symbol (always set, even with no exit today).
        #     Percentage  = blank (standalone STP — not a bracket child row).
        for row in live_rows:
            strategy        = _get_strategy(row.strategy_id)
            stop_val        = round(float(row.current_stop_price), 4)
            direction_upper = (row.direction or 'LONG').upper()
            stop_action     = 'SELL' if direction_upper == 'LONG' else 'BUY'
            oca_group       = live_oca.get(row.symbol, '')
            ws.append([
                IBKR_ACCOUNT,                                              # Account
                direction_upper.lower(),                                   # BasketTag
                '',                                                        # OrderId
                '',                                                        # ParentOrderId
                stop_action,                                               # Action
                int(row.filled_qty or 0),                                  # Quantity
                row.symbol,                                                # Symbol
                'STK',                                                     # SecType
                'SMART/AMEX',                                              # Exchange
                'USD',                                                     # Currency
                'DAY',                                                     # TimeInForce (not GTC)
                'STP',                                                     # OrderType
                '',                                                        # LmtPrice
                stop_val,                                                  # AuxPrice = stop price
                oca_group,                                                 # OCAGroup (unique per symbol)
                RTH,                                                       # Rth = '=FALSE()'
                (strategy.system_code or strategy.name) if strategy else f'sid{row.strategy_id}',   # OrderRef
                '',                                                        # Percentage (blank — standalone)
                '',                                                        # Rank
            ])
            summary['stop_rows_written'] += 1

        # 3b. Exit OPG MKT SELL rows (positions engine wants to close)
        #     OCAGroup = exit_oca[symbol] — separate from STP OCA pool.
        for row in exit_rows:
            regime    = _get_regime(row.entered_regime_id)
            strategy  = _get_strategy(row.strategy_id)
            oca_group = exit_oca.get(row.symbol, '')
            ws.append(_build_exit_ibkr_row(
                row, regime, strategy,
                oca_group=oca_group,
                account=IBKR_ACCOUNT,
            ))

        # 3c. Entry BUY rows (+ bracket child stop) for new positions.
        #     Patch 80: built via the shared broker_basket_builder.build_entry_rows
        #     so OrderId hashing and the NORMAL/LIMIT child-stop bracket match the
        #     test-execution CSV path exactly. dict rows -> list for openpyxl.
        for row in pending_rows:
            regime     = _get_regime(row.entered_regime_id)
            strategy   = _get_strategy(row.strategy_id)
            strat_name = strategy.name if strategy else f'sid{row.strategy_id}'
            if strat_name not in order_id_counters:
                order_id_counters[strat_name] = _strategy_orderid_base(strat_name)
            order_id_counters[strat_name] += 1
            order_id = order_id_counters[strat_name]
            for entry_dict in build_entry_rows(
                account=IBKR_ACCOUNT, tl=row, strat=strategy,
                regime=regime, order_id=order_id,
            ):
                ws.append([entry_dict[col] for col in IBKR_BASKET_HEADERS])
            summary['orders_written'] += 1

        # Auto-size columns
        for col in ws.columns:
            max_len = max((len(str(c.value or '')) for c in col), default=10)
            ws.column_dimensions[col[0].column_letter].width = max(10, min(40, max_len + 2))

        wb.save(file_path)
        db.commit()

        log_row.status        = 'SUCCESS'
        log_row.rows_affected = (
            summary['stop_rows_written']
            + summary['exits_written']
            + summary['orders_written']
        )
        log_row.finished_at = datetime.utcnow()
        db.commit()

        print(
            f'[broker_write] === SUCCESS '
            f'stop={summary["stop_rows_written"]} '
            f'exits={summary["exits_written"]} '
            f'entries={summary["orders_written"]} '
            f'(promoted {summary["promoted_proposed"]}) → {file_path} ==='
        )

        return summary

    except Exception as e:
        db.rollback()
        log_row.status      = 'FAILED'
        log_row.error_msg   = f'{type(e).__name__}: {e}\n{traceback.format_exc()}'
        log_row.finished_at = datetime.utcnow()
        db.commit()
        print(f'[broker_write] === FAILED — {type(e).__name__}: {e} ===')
        raise


def _build_ibkr_row(
    row: Tradelist,
    regime: Optional[MarketRegime],
    strategy: Optional[StrategyBucket],
    order_id: int,
    account: str,
) -> list:
    """Build one IBKR Basket Trader BUY/SELL entry row (19 columns)."""
    direction_upper   = (row.direction or 'LONG').upper()
    ib_action         = 'BUY' if direction_upper == 'LONG' else 'SELL'
    basket_tag        = direction_upper.lower()
    regime_order_type = (regime.order_type if regime else 'NORMAL').upper()
    limit_price_dec   = row.limit_price or Decimal('0')

    if regime_order_type == 'NORMAL':
        ib_order_type   = 'MKT'
        lmt_price_value = ''
        aux_price_value = ''
    else:
        # LIMIT or LIMIT_ATR — STPMOC with limit entry price + initial stop
        ib_order_type   = 'STPMOC'
        lmt_price_value = float(limit_price_dec) if limit_price_dec > 0 else ''
        initial_stop    = float(row.initial_stop_price) if row.initial_stop_price else 0.0
        aux_price_value = round(initial_stop, 4) if initial_stop > 0 else ''

    entry_timing     = (regime.entry_timing or 'open').lower() if regime else 'open'
    ib_tif           = 'OPG' if entry_timing == 'open' else 'DAY'
    stoploss_pct     = float(regime.stoploss_pct or 0) if regime else 0.0
    percentage_value = round(stoploss_pct, 2) if stoploss_pct > 0 else ''
    strategy_name    = (strategy.system_code or strategy.name) if strategy else f'sid{row.strategy_id}'

    return [
        account,                                                           # Account
        basket_tag,                                                        # BasketTag
        order_id,                                                          # OrderId
        '',                                                                # ParentOrderId
        ib_action,                                                         # Action
        int(row.intended_qty or 0),                                        # Quantity
        row.symbol,                                                        # Symbol
        'STK',                                                             # SecType
        'SMART/AMEX',                                                      # Exchange
        'USD',                                                             # Currency
        ib_tif,                                                            # TimeInForce
        ib_order_type,                                                     # OrderType
        lmt_price_value,                                                   # LmtPrice
        aux_price_value,                                                   # AuxPrice
        '',                                                                # OCAGroup
        RTH,                                                               # Rth = '=FALSE()'
        strategy_name,                                                     # OrderRef
        percentage_value,                                                  # Percentage
        int(row.ranking_rank) if row.ranking_rank is not None else '',     # Rank
    ]


def _build_exit_ibkr_row(
    row: Tradelist,
    regime: Optional[MarketRegime],
    strategy: Optional[StrategyBucket],
    oca_group: Union[int, str],
    account: str,
) -> list:
    """Build one IBKR Basket Trader exit SELL row (19 columns).

    exit_timing='open'  → OPG / MKT, OCAGroup from exit_oca pool
    exit_timing='close' → DAY / MOC, OCAGroup blank (standalone)
    OrderId = blank — legacy exits carry no OrderId
    Quantity = filled_qty (actual shares held)
    """
    direction_upper = (row.direction or 'LONG').upper()
    exit_action     = 'SELL' if direction_upper == 'LONG' else 'BUY'
    basket_tag      = direction_upper.lower()
    exit_timing     = (regime.exit_timing or 'open').lower() if regime else 'open'
    strategy_name   = (strategy.system_code or strategy.name) if strategy else f'sid{row.strategy_id}'

    if exit_timing == 'open':
        ib_tif        = 'OPG'
        ib_order_type = 'MKT'
    else:
        ib_tif        = 'DAY'
        ib_order_type = 'MOC'
        oca_group     = ''   # close exits are standalone

    return [
        account,                    # Account
        basket_tag,                 # BasketTag
        '',                         # OrderId
        '',                         # ParentOrderId
        exit_action,                # Action
        int(row.filled_qty or 0),   # Quantity
        row.symbol,                 # Symbol
        'STK',                      # SecType
        'SMART/AMEX',               # Exchange
        'USD',                      # Currency
        ib_tif,                     # TimeInForce
        ib_order_type,              # OrderType
        '',                         # LmtPrice
        '',                         # AuxPrice
        oca_group,                  # OCAGroup (from exit_oca pool)
        RTH,                        # Rth = '=FALSE()'
        strategy_name,              # OrderRef
        '',                         # Percentage
        '',                         # Rank
    ]


def _oca_base(strategy_name: str) -> int:
    """SHA256-derived 4-digit OCA group base.
    Mirrors legacy Daily_Orders: unique_ids_oca = int(str(sha256(name+'D'))[:4])
    Each symbol gets oca_base + incrementing index.
    """
    import hashlib as _hashlib
    unique_number = int(
        _hashlib.sha256((strategy_name + 'D').encode()).hexdigest(), 16
    )
    return int(str(unique_number)[:4])