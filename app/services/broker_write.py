"""
broker_write.py — D2 (morning broker basket emission)

Reads PENDING_FILL rows for a given intended_trade_date and writes
M_Combined_{YYYYMMDD}.xlsx in IBKR Basket Trader format.

Sequence:
  1. Auto-promote any remaining PROPOSED → PENDING_FILL (implicit "kept by
     trader" — they didn't elide/substitute them in the overlay step).
  2. Read all PENDING_FILL rows for the date (TRADED ledger only — SYSTEM
     audit shadows are excluded from the broker basket).
  3. Map each row to an IBKR Basket Trader row.
  4. Write XLSX to <backtestPath>/broker_output/{YYYYMMDD}/.

Phase 1 simplifications:
  - No OCA grouping (Phase 1 strategies emit single-leg entries; STP/TP
    brackets land in Phase 2 with sector-cap-aware bracket templates)
  - No AuxPrice (no STP orders emitted at entry — engine-side stops fire
    on the next bar after entry, not as bracket attachments)
  - TimeInForce derived from regime.entry_timing:
       'open'  → OPG
       'close' → DAY

IBKR Basket Trader columns (standard, in order):
  Action, Quantity, Symbol, SecType, Exchange, Currency, OrderType,
  LmtPrice, AuxPrice, TimeInForce, OutsideRth, OrderRef, OCAGroup, OCAType
"""

from __future__ import annotations
import traceback
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session
from openpyxl import Workbook
from openpyxl.styles import Font

from app.models.tradelist import Tradelist
from app.models.market_regime import MarketRegime
from app.models.strategy_bucket import StrategyBucket
from app.models.eod_run_log import EodRunLog
from app.constants.PricePath import PricePath

# Legacy IBKR Basket Trader format — matches the columns used in the
# combiner script that produces M_Combined_*.xlsx today. Phase 1 leaves
# Account/Rth as constants and ParentOrderId/AuxPrice/OCAGroup empty;
# Percentage carries the stoploss percent for the trader's downstream
# bracket logic (or 0 when stoploss_pct=0, per RT correction).
IBKR_BASKET_HEADERS = [
    'Account', 'BasketTag', 'OrderId', 'ParentOrderId',
    'Action', 'Quantity', 'Symbol', 'SecType', 'Exchange', 'Currency',
    'TimeInForce', 'OrderType', 'LmtPrice', 'AuxPrice',
    'OCAGroup', 'Rth', 'OrderRef', 'Percentage', 'Rank',
]

# IBKR account — Phase 1 default. Override via env var IBKR_ACCOUNT if needed.
import os
IBKR_ACCOUNT = os.getenv('IBKR_ACCOUNT', 'U14642225')


def write_broker_basket(
    db: Session,
    trade_date: date,
    output_dir: Optional[str] = None,
) -> dict:
    """Generate M_Combined_{trade_date}.xlsx and write to disk.

    Args:
        db: SQLAlchemy session.
        trade_date: the intended_trade_date to filter PENDING_FILL rows on.
        output_dir: where to write the file. Default
                    <backtestPath>/broker_output/{YYYYMMDD}/.

    Returns:
        Summary dict with file_path, orders_written count, eod_run_log id.

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
        'eod_run_log_id': log_row.id,
        'trade_date': trade_date.isoformat(),
        'file_path': str(file_path),
        'promoted_proposed': 0,
        'exits_written': 0,
        'orders_written': 0,
    }

    print(f'[broker_write] trade_date={trade_date} output={file_path}')

    try:
        # Step 1: Auto-promote remaining PROPOSED → PENDING_FILL
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
            # source_tag stays 'SYSTEM' — never touched by trader overlay
        summary['promoted_proposed'] = len(proposed_rows)
        db.flush()

        if proposed_rows:
            print(f'[broker_write] auto-promoted {len(proposed_rows)} PROPOSED → PENDING_FILL '
                  f'(implicit "kept by trader")')

        # Step 2: Read all PENDING_FILL rows for the date
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
            print(f'[broker_write] no PENDING_FILL rows for {trade_date} — '
                  f'writing empty workbook')

        # Step 1.5: Read PENDING_EXIT rows for trade_date and mark EXIT_SUBMITTED.
        # These become SELL rows in the basket ahead of entry rows so the trader
        # sees exits first. exit_timing comes from the row's entered regime:
        #   open  → OPG / MKT, OCAGroup = oca_base+i per ticker
        #   close → DAY / MOC, OCAGroup = empty
        # Quantity: filled_qty (actual shares held), not intended_qty.
        # OrderId: blank — legacy exits carry no OrderId (matches Daily_Orders).
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

        # Step 3+4: Build XLSX
        wb = Workbook()
        ws = wb.active
        ws.title = f'Basket_{trade_date.strftime("%Y%m%d")}'

        ws.append(IBKR_BASKET_HEADERS)
        for cell in ws[1]:
            cell.font = Font(bold=True)

            # Cache regime + strategy lookups to avoid N+1 queries
            regime_cache: dict[int, MarketRegime] = {}
            strategy_cache: dict[int, StrategyBucket] = {}

            # Write exit SELL rows first — trader sees what is closing before new entries.
            # OCAGroup is per-ticker (oca_base+i) for open exits, blank for close exits.
            # OrderId is blank — legacy Daily_Orders exits carry no OrderId.
            oca_counters: dict[str, int] = {}  # strategy_name → current oca counter
            order_id_counter = 0

            for row in exit_rows:
                if row.entered_regime_id not in regime_cache:
                    regime_cache[row.entered_regime_id] = (
                        db.query(MarketRegime).filter_by(id=row.entered_regime_id).first()
                    )
                regime = regime_cache[row.entered_regime_id]

                if row.strategy_id not in strategy_cache:
                    strategy_cache[row.strategy_id] = (
                        db.query(StrategyBucket).filter_by(id=row.strategy_id).first()
                    )
                strategy = strategy_cache[row.strategy_id]

                strat_name = strategy.name if strategy else f'sid{row.strategy_id}'
                if strat_name not in oca_counters:
                    oca_counters[strat_name] = _oca_base(strat_name)

                ws.append(_build_exit_ibkr_row(
                    row, regime, strategy,
                    oca_counters=oca_counters,
                    account=IBKR_ACCOUNT,
                ))

            # Write entry BUY rows after exits.
            for row in pending_rows:
                if row.entered_regime_id not in regime_cache:
                    regime_cache[row.entered_regime_id] = (
                        db.query(MarketRegime).filter_by(id=row.entered_regime_id).first()
                    )
                regime = regime_cache[row.entered_regime_id]

                if row.strategy_id not in strategy_cache:
                    strategy_cache[row.strategy_id] = (
                        db.query(StrategyBucket).filter_by(id=row.strategy_id).first()
                    )
                strategy = strategy_cache[row.strategy_id]

                order_id_counter += 1
                ws.append(_build_ibkr_row(
                    row, regime, strategy,
                    order_id=order_id_counter,
                    account=IBKR_ACCOUNT,
                ))
                summary['orders_written'] += 1

        # Auto-size columns
        for col in ws.columns:
            max_len = max((len(str(c.value or '')) for c in col), default=10)
            ws.column_dimensions[col[0].column_letter].width = max(10, min(40, max_len + 2))

        wb.save(file_path)

        db.commit()

        log_row.status        = 'SUCCESS'
        log_row.rows_affected = summary['orders_written']
        log_row.finished_at   = datetime.utcnow()
        db.commit()

        print(f'[broker_write] === SUCCESS wrote {summary["orders_written"]} entr'
              f'y order(s) + {summary["exits_written"]} exit order(s) '
              f'(promoted {summary["promoted_proposed"]}) → {file_path} ===')

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
    """Build one IBKR Basket Trader row matching the legacy XLSX format.

    Column order (must match IBKR_BASKET_HEADERS):
        Account, BasketTag, OrderId, ParentOrderId, Action, Quantity,
        Symbol, SecType, Exchange, Currency, TimeInForce, OrderType,
        LmtPrice, AuxPrice, OCAGroup, Rth, OrderRef, Percentage

    Mapping rules:
      Action      — 'BUY' for LONG, 'SELL' for SHORT (from row.direction)
      BasketTag   — 'long' / 'short' lowercase, derived from direction
      OrderId     — caller-provided sequential counter starting at 1
      ParentOrderId — empty (Phase 1: no child orders / brackets)
      TimeInForce — 'OPG' if regime.entry_timing='open' else 'DAY'
      OrderType   — 'MKT'    for NORMAL (PullBack_X3_Sp500 path)
                  — 'STPMOC' for LIMIT / LIMIT_ATR (matches legacy convention;
                    carries the stoploss bracket via Percentage)
      LmtPrice    — limit_price when > 0, else empty (MKT path)
      AuxPrice    — empty (Phase 1: no STP attached at entry)
      OCAGroup    — empty (Phase 1: single-leg)
      Rth         — False (legacy convention — fill only during RTH)
      OrderRef    — strategy name (e.g., 'PullBack_X3_Sp500')
      Percentage  — stoploss_pct × 100 (e.g., 0.05 → 5)
                  — When stoploss_pct=0: emit 0, NOT a price. Per RT
                    correction — close-price-as-stop confuses the trader.
    """
    # Direction → Action + BasketTag
    direction_upper = (row.direction or 'LONG').upper()
    ib_action  = 'BUY'  if direction_upper == 'LONG' else 'SELL'
    basket_tag = direction_upper.lower()

    # OrderType + LmtPrice — match the legacy convention
    regime_order_type = (regime.order_type if regime else 'NORMAL').upper()
    limit_price_dec   = row.limit_price or Decimal('0')

    if regime_order_type == 'NORMAL':
        ib_order_type   = 'MKT'
        lmt_price_value = ''            # no limit for MKT
    else:
        # LIMIT or LIMIT_ATR — both ride the STPMOC composite type per legacy.
        # The stop bracket (when stoploss_pct>0) attaches via the Percentage
        # column rather than as a separate row. When stoploss_pct=0, IBKR-side
        # logic sees Percentage=0 and skips the stop bracket.
        ib_order_type   = 'STPMOC'
        lmt_price_value = float(limit_price_dec) if limit_price_dec > 0 else ''

    # TimeInForce from regime.entry_timing
    entry_timing = (regime.entry_timing or 'open').lower() if regime else 'open'
    ib_tif       = 'OPG' if entry_timing == 'open' else 'DAY'

    # Percentage — stoploss_pct as a percentage value (5 for 5%), 0 when disabled
    stoploss_pct      = float(regime.stoploss_pct or 0) if regime else 0.0
    percentage_value  = round(stoploss_pct * 100, 2)

    strategy_name = strategy.name if strategy else f'sid{row.strategy_id}'

    return [
        account,                            # Account
        basket_tag,                         # BasketTag
        order_id,                           # OrderId
        '',                                 # ParentOrderId
        ib_action,                          # Action
        int(row.intended_qty or 0),         # Quantity
        row.symbol,                         # Symbol
        'STK',                              # SecType
        'SMART/AMEX',                       # Exchange  (per legacy)
        'USD',                              # Currency
        ib_tif,                             # TimeInForce
        ib_order_type,                      # OrderType
        lmt_price_value,                    # LmtPrice
        '',                                 # AuxPrice
        '',                                 # OCAGroup
        False,                              # Rth (legacy convention)
        strategy_name,  # OrderRef
        percentage_value,  # Percentage
        int(row.ranking_rank) if row.ranking_rank is not None else '',  # Rank
    ]

def _build_exit_ibkr_row(
    row: Tradelist,
    regime: Optional[MarketRegime],
    strategy: Optional[StrategyBucket],
    oca_counters: dict,
    account: str,
) -> list:
    """Build one IBKR Basket Trader exit SELL row.

    Mirrors legacy Daily_Orders.close_open_positions() and
    close_open_positionsonclose() exactly:
      exit_timing='open'  → OPG / MKT, OCAGroup = oca_base+i per ticker
      exit_timing='close' → DAY / MOC, OCAGroup = empty
      OrderId = blank     — legacy exits carry no OrderId
      Quantity = filled_qty (actual shares held, not intended_qty)
    """
    direction_upper = (row.direction or 'LONG').upper()
    # Exit action is opposite of entry direction
    exit_action = 'SELL' if direction_upper == 'LONG' else 'BUY'
    basket_tag  = direction_upper.lower()

    exit_timing = (regime.exit_timing or 'open').lower() if regime else 'open'

    if exit_timing == 'open':
        ib_tif        = 'OPG'
        ib_order_type = 'MKT'
        strat_name    = strategy.name if strategy else f'sid{row.strategy_id}'
        oca_counters[strat_name] += 1
        oca_group = oca_counters[strat_name]
    else:
        ib_tif        = 'DAY'
        ib_order_type = 'MOC'
        oca_group     = ''

    strategy_name = strategy.name if strategy else f'sid{row.strategy_id}'

    return [
        account,                             # Account
        basket_tag,                          # BasketTag
        '',                                  # OrderId — blank for exits
        '',                                  # ParentOrderId
        exit_action,                         # Action
        int(row.filled_qty or 0),            # Quantity — actual held shares
        row.symbol,                          # Symbol
        'STK',                               # SecType
        'SMART/AMEX',                        # Exchange
        'USD',                               # Currency
        ib_tif,                              # TimeInForce
        ib_order_type,                       # OrderType
        '',                                  # LmtPrice
        '',                                  # AuxPrice
        oca_group,                           # OCAGroup
        False,                               # Rth
        strategy_name,                       # OrderRef
        '',                                  # Percentage — exits never carry stop pct
        '',                                  # Rank — exits have no rank
    ]


def _oca_base(strategy_name: str) -> int:
    """SHA256-derived 4-digit OCA group base. Mirrors legacy Daily_Orders.__init__:
        unique_ids_oca = int(str(sha256(name+'D'))[:4])
    Each exiting ticker gets oca_base+1, oca_base+2, etc.
    4-digit range keeps it distinct from the 5-digit OrderId range.
    """
    import hashlib as _hashlib
    unique_number = int(_hashlib.sha256((strategy_name + 'D').encode()).hexdigest(), 16)
    return int(str(unique_number)[:4])