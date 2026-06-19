"""
test_runner.py — Test-mode PM runner (no DB writes).

Calls exec_data_refresh + engine for a historical run_date, then writes
the resulting entries/exits as a CSV to:

  <backtestPath>/testing/{strategy_name}/m_combined_{YYYYMMDD}.csv

Nothing is written to the tradelist, eod_run_log, or any other table.
Use this to validate engine signals against the backtest tradelist.
"""
from __future__ import annotations
import csv
import io
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any
import pandas as pd
import pandas_market_calendars as mcal
import requests
from sqlalchemy.orm import Session

from app.models.strategy_bucket import StrategyBucket
from app.models.market_regime import MarketRegime
from app.services.position_manager.live_seed_builder import build_live_holdings_seed
from app.services.position_manager.payload_builder import build_execution_step_payload
from app.services.position_manager.broker_basket_builder import (
    _strategy_orderid_base,
)
from app.constants.PricePath import PricePath
from app.Settings import settings

ENGINE_HTTP_TIMEOUT_SEC = 300
TEST_BASKET_COLUMNS = [
    "Account", "BasketTag", "OrderId", "ParentOrderId", "Action", "Quantity",
    "Symbol", "SecType", "Exchange", "Currency", "TimeInForce", "OrderType",
    "LmtPrice", "AuxPrice", "OCAGroup", "Rth", "OrderRef", "Percentage", "Rank",
]


def build_mock_live_holdings_from_csv(
    csv_text: str,
    run_date: date,
    production_capital: float,
    slots: int,
) -> list[dict]:
    """Parse a backtest tradelist CSV and extract positions open on run_date,
    rescaled to production capital sizing.

    A position is open on run_date when:
        entryDate <= run_date < exitDate

    Rescale formula (per user spec):
        per_slot  = production_capital / slots
        quantity  = floor(per_slot / entryPrice)
        capital   = int(per_slot)

    entryPrice stays as the historical price from the CSV — correct because the
    engine uses it for stop computation (pct * entryPrice), not for slot sizing.

    Args:
        csv_text:           Raw CSV string uploaded by the user (in-memory).
        run_date:           The test close date. Positions open on this date
                            are the ones that should be seeded.
        production_capital: regime.production_capital from DB.
        slots:              regime.slots from DB.

    Returns:
        List of dicts matching LiveHoldingsSeedDto field names exactly.
        Empty list if no positions are open on run_date.
    """
    import io
    import math

    if not csv_text or not csv_text.strip():
        return []

    df = pd.read_csv(io.StringIO(csv_text), index_col=0)

    required = {'symbol', 'direction', 'entryDate', 'entryPrice',
                'quantity', 'entryTiming'}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f'Tradelist CSV missing required columns: {sorted(missing)}. '
            f'Found: {sorted(df.columns)}'
        )

    df['entryDate'] = pd.to_datetime(df['entryDate'])
    df['exitDate']  = pd.to_datetime(df['exitDate'])
    run_dt          = pd.Timestamp(run_date)

    # Positions open on run_date: entered on or before, not yet exited
    open_pos = df[(df['entryDate'] <= run_dt) & (df['exitDate'] > run_dt)].copy()

    if open_pos.empty:
        print(f'[test_runner] no open positions on {run_date} in uploaded CSV')
        return []

    print(f'[test_runner] {len(open_pos)} open positions on {run_date} '
          f'(production_capital={production_capital}, slots={slots})')

    per_slot = production_capital / max(slots, 1)
    seed: list[dict] = []

    for trade_id, row in open_pos.iterrows():
        entry_price = float(row['entryPrice'])
        if entry_price <= 0:
            print(f'[test_runner] WARNING: skipping {row["symbol"]} — entryPrice={entry_price}')
            continue

        mock_qty     = row['quantity']
        mock_capital = int(per_slot)

        # currentStopPrice: use CSV value if present and not NaN, else None
        # (engine recomputes from stoploss_pct when None)
        stop_price_raw = row.get('currentStopPrice', None)
        current_stop   = (
            float(stop_price_raw)
            if stop_price_raw is not None and not (isinstance(stop_price_raw, float) and math.isnan(stop_price_raw))
            else None
        )

        seed.append({
            'tradeId':          str(trade_id),           # CSV index: SYMBOL_millis_n
            'symbol':           str(row['symbol']),
            'direction':        str(row['direction']).upper(),
            'entryDate':        pd.Timestamp(row['entryDate']).date().isoformat(),
            'entryprice':       entry_price,              # lowercase 'p' — Java field name
            'quantity':         mock_qty,
            'capital':          mock_capital,
            'entryTiming':      str(row.get('entryTiming', 'open') or 'open'),
            'entryReason':      'Entries',
            'pairId':           None,
            'currentStopPrice': current_stop,
        })

    print(f'[test_runner] built {len(seed)} mock holdings '
          f'(per_slot={per_slot:.0f}, skipped={len(open_pos) - len(seed)})')
    return seed


def run_test_position_manager(
    db: Session,
    strategy_id: int,
    run_date: date,
    data_root: str,
    mock_holdings_csv: str | None = None,   # raw CSV text; None = cold start (no holdings)
) -> dict[str, Any]:
    """Run engine for a historical date and write CSV. No DB writes.

    Args:
        db:                SQLAlchemy session (read-only — no writes at all).
        strategy_id:       which strategy to evaluate.
        run_date:          historical close date (prices as of this date).
        data_root:         path to exec_data/{YYYYMMDD}/ folder for this date.
        mock_holdings_csv: raw text of the backtest tradelist CSV. When supplied,
                           positions open on run_date are rescaled to
                           regime.production_capital / regime.slots and sent to
                           the engine as live holdings. None = cold start.

    Returns:
        Summary dict with csv_path, entries_count, exits_count, holdings_seeded.
    """
    strategy = db.query(StrategyBucket).filter_by(id=strategy_id).first()
    if strategy is None:
        raise ValueError(f'Strategy id={strategy_id} not found')

    first_regime = (
        db.query(MarketRegime).filter_by(strategy_id=strategy_id)
        .order_by(MarketRegime.id.asc()).first()
    )
    if first_regime is None:
        raise ValueError(f'Strategy id={strategy_id} has no regimes')

    print(f'[test_runner] strategy={strategy.name} run_date={run_date} '
          f'data_root={data_root} '
          f'mock_csv={"yes" if mock_holdings_csv else "no (cold start)"}')

    # Build mock live holdings from CSV if supplied.
    # production_capital and slots come from the first regime — same values
    # the engine will use for new entry sizing, so slot accounting is consistent.
    live_holdings: list[dict] = []
    if mock_holdings_csv:
        prod_cap = float(first_regime.capital or 0)
        slots    = int(first_regime.slots or 1)
        if prod_cap <= 0:
            raise ValueError(
                f'Strategy id={strategy_id} regime id={first_regime.id} has no '
                f'production_capital set. Set it before running a mock holdings test.'
            )
        live_holdings = build_mock_live_holdings_from_csv(
            csv_text=mock_holdings_csv,
            run_date=run_date,
            production_capital=prod_cap,
            slots=slots,
        )

    payload = build_execution_step_payload(
        db,
        strategy_id=strategy_id,
        run_date=run_date,
        live_holdings=live_holdings,
        data_root=data_root,
        test_start_date=run_date - timedelta(days=650),
        execution_mode=False,   # test mode: regime.capital (backtest scale), not production_capital
    )

    engine_response = _call_engine(payload)

    trade_date = _next_trading_day(run_date)
    proposed_entries = engine_response.get('proposedEntries') or []
    proposed_exits   = engine_response.get('proposedExits')   or []
    active_regime    = engine_response.get('activeRegimeOnLastBar', '')

    main_basket, sub_basket = _build_test_basket(
        strategy_name=strategy.name,
        first_regime=first_regime,
        proposed_entries=proposed_entries,
        proposed_exits=proposed_exits,
        live_holdings=live_holdings,
        proposed_exits_count=len(proposed_exits),
    )

    csv_path = _write_test_csv(
        strategy_name=strategy.name,
        run_date=run_date,
        basket=main_basket,
        suffix='',
    )
    sub_path = _write_test_csv(
        strategy_name=strategy.name,
        run_date=run_date,
        basket=sub_basket,
        suffix='_SUB',
    )

    print(f'[test_runner] main basket → {csv_path} ({len(main_basket)} rows)')
    print(f'[test_runner] sub file   → {sub_path} ({len(sub_basket)} rows)')

    return {
        'strategy_id': strategy_id,
        'strategy_name': strategy.name,
        'run_date': run_date.isoformat(),
        'trade_date': trade_date.isoformat(),
        'active_regime': active_regime,
        'entries_count': len(proposed_entries),
        'exits_count': len(proposed_exits),
        'holdings_seeded': len(live_holdings),
        'proposed_in_basket': sum(1 for r in main_basket if r['Action'] == 'BUY'),
        'subs_in_file': len(sub_basket),
        'csv_path': str(csv_path),
        'sub_csv_path': str(sub_path),
    }


def _oca_base(strategy_name: str) -> int:
    """SHA256-derived 4-digit OCA group base. Mirrors legacy Daily_Orders.__init__:
        unique_ids_oca = int(str(sha256(name+'D'))[:4])
    One increment per exiting ticker. Always distinct from OrderId (5-digit).
    """
    import hashlib as _hashlib
    unique_number = int(_hashlib.sha256((strategy_name + 'D').encode()).hexdigest(), 16)
    return int(str(unique_number)[:4])


def _build_test_basket(
    strategy_name: str,
    first_regime: MarketRegime,
    proposed_entries: list[dict],
    proposed_exits: list[dict],
    live_holdings: list[dict],
    proposed_exits_count: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build IBKR basket rows from engine response. No DB reads.

    Returns (main_basket, sub_basket):
      main_basket — firm entries (top free_slots) + exits. Written to main CSV.
      sub_basket  — substitute pool rows. Written to _SUB CSV.

    Both lists use TEST_BASKET_COLUMNS layout. Rank is the last column.

    Slot split mirrors proposed_inserter exactly:
      remaining_live = len(live_holdings) - proposed_exits_count
      free_slots     = regime.slots - remaining_live
      proposed_slice = proposed_entries[:free_slots]          → main_basket BUY rows
      sub_slice      = proposed_entries[free_slots:free_slots + pool_size]
                       → sub_basket rows (OrderId/ParentOrderId blank)

    OCA / OrderId rules (from legacy Daily_Orders):
      Entries:  OrderId = order_base+i, ParentOrderId = blank, OCAGroup = blank
      Exits:    OrderId = blank, ParentOrderId = blank,
                OCAGroup = oca_base+i for open exits, blank for close exits
      Subs:     OrderId = blank, ParentOrderId = blank, OCAGroup = blank
    """
    account = os.environ.get('IBKR_ACCOUNT', 'U14642225')

    regime_order_type = (first_regime.order_type or 'NORMAL').upper()
    entry_timing      = (first_regime.entry_timing or 'open').lower()
    stoploss_pct_raw  = first_regime.stoploss_pct
    stoploss_pct      = float(stoploss_pct_raw) if stoploss_pct_raw else 0.0

    if regime_order_type == 'NORMAL':
        entry_tif   = 'OPG' if entry_timing == 'open' else 'DAY'
        entry_otype = 'MKT' if entry_timing == 'open' else 'MOC'
    elif regime_order_type in ('LIMIT', 'LIMIT_ATR'):
        entry_tif   = 'DAY'
        entry_otype = 'LMT' if entry_timing == 'open' else 'LOC'
    else:
        entry_tif   = 'OPG'
        entry_otype = 'MKT'

    percentage = round(stoploss_pct * 100.0, 4) if stoploss_pct > 0 else ''

    holdings_qty: dict[str, int] = {
        h['symbol']: int(h['quantity'])
        for h in (live_holdings or [])
        if h.get('symbol') and h.get('quantity')
    }

    # Slot split — mirrors proposed_inserter (no DB needed in test mode)
    slots          = int(first_regime.slots or 7)
    pool_size      = int(first_regime.substitute_pool_size or 0)
    remaining_live = max(0, len(live_holdings) - proposed_exits_count)
    free_slots     = max(0, slots - remaining_live)
    proposed_slice = proposed_entries[:free_slots]
    sub_slice      = proposed_entries[free_slots:free_slots + pool_size]

    print(f'[test_basket] slots={slots} seeded={len(live_holdings)} '
          f'exiting={proposed_exits_count} remaining={remaining_live} '
          f'free_slots={free_slots} proposed={len(proposed_slice)} '
          f'subs={len(sub_slice)}')

    order_id_counter = _strategy_orderid_base(strategy_name)
    oca_counter      = _oca_base(strategy_name)

    main_basket: list[dict[str, Any]] = []

    # ── Firm entries (PROPOSED slice) ────────────────────────────────────────
    for entry in proposed_slice:
        order_id_counter += 1
        parent_id = order_id_counter

        direction = (entry.get('direction') or 'LONG').upper()
        lmt_price = (
            round(float(entry.get('limitPrice') or 0), 4)
            if entry_otype in ('LMT', 'LOC') and entry.get('limitPrice')
            else ''
        )

        main_basket.append({
            'Account':       account,
            'BasketTag':     'long' if direction == 'LONG' else 'short',
            'OrderId':       order_id_counter,
            'ParentOrderId': '',
            'Action':        'BUY' if direction == 'LONG' else 'SELL',
            'Quantity':      int(entry.get('quantity') or 0),
            'Symbol':        entry.get('symbol', ''),
            'SecType':       'STK',
            'Exchange':      'SMART/AMEX',
            'Currency':      'USD',
            'TimeInForce':   entry_tif,
            'OrderType':     entry_otype,
            'LmtPrice':      lmt_price,
            'AuxPrice':      '',
            'OCAGroup':      '',
            'Rth': 'False',
            'OrderRef': strategy_name,
            'Percentage': percentage,
            'Rank': int(entry.get('rank') or 0),
        })

        if stoploss_pct > 0:
            order_id_counter += 1
            exit_action = 'SELL' if direction == 'LONG' else 'BUY'
            main_basket.append({
                'Account':       account,
                'BasketTag':     'long' if direction == 'LONG' else 'short',
                'OrderId':       order_id_counter,
                'ParentOrderId': parent_id,
                'Action':        exit_action,
                'Quantity':      int(entry.get('quantity') or 0),
                'Symbol':        entry.get('symbol', ''),
                'SecType':       'STK',
                'Exchange':      'SMART/AMEX',
                'Currency':      'USD',
                'TimeInForce':   'DAY',
                'OrderType':     'STPMOC',
                'LmtPrice':      '',
                'AuxPrice':      '',
                'OCAGroup':      '',
                'Rth': 'False',
                'OrderRef': strategy_name,
                'Percentage': percentage,
                'Rank': '',
            })

    # ── Exits ────────────────────────────────────────────────────────────────
    for ex in proposed_exits:
        exit_timing = (ex.get('exitTiming') or 'open').lower()
        symbol      = ex.get('symbol', '')
        direction   = (ex.get('direction') or 'LONG').upper()

        if exit_timing == 'open':
            oca_counter += 1
            exit_tif   = 'OPG'
            exit_otype = 'MKT'
            oca_group  = oca_counter
        else:
            exit_tif   = 'DAY'
            exit_otype = 'MOC'
            oca_group  = ''

        main_basket.append({
            'Account':       account,
            'BasketTag':     'long' if direction == 'LONG' else 'short',
            'OrderId':       '',
            'ParentOrderId': '',
            'Action':        'SELL' if direction == 'LONG' else 'BUY',
            'Quantity':      holdings_qty.get(symbol, 0),
            'Symbol':        symbol,
            'SecType':       'STK',
            'Exchange':      'SMART/AMEX',
            'Currency':      'USD',
            'TimeInForce':   exit_tif,
            'OrderType':     exit_otype,
            'LmtPrice':      '',
            'AuxPrice':      '',
            'OCAGroup': oca_group,
            'Rth': 'False',
            'OrderRef': strategy_name,
            'Percentage': '',
            'Rank': '',
        })

    # ── Substitute pool — same columns, all IDs blank ─────────────────────
    sub_basket: list[dict[str, Any]] = []
    for entry in sub_slice:
        direction = (entry.get('direction') or 'LONG').upper()
        lmt_price = (
            round(float(entry.get('limitPrice') or 0), 4)
            if entry_otype in ('LMT', 'LOC') and entry.get('limitPrice')
            else ''
        )
        sub_basket.append({
            'Account':       account,
            'BasketTag':     'long' if direction == 'LONG' else 'short',
            'OrderId':       '',
            'ParentOrderId': '',
            'Action':        'BUY' if direction == 'LONG' else 'SELL',
            'Quantity':      int(entry.get('quantity') or 0),
            'Symbol':        entry.get('symbol', ''),
            'SecType':       'STK',
            'Exchange':      'SMART/AMEX',
            'Currency':      'USD',
            'TimeInForce':   entry_tif,
            'OrderType':     entry_otype,
            'LmtPrice':      lmt_price,
            'AuxPrice':      '',
            'OCAGroup': '',
            'Rth': 'False',
            'OrderRef': strategy_name,
            'Percentage': '',
            'Rank': int(entry.get('rank') or 0),
        })

    return main_basket, sub_basket


def _write_test_csv(
    strategy_name: str,
    run_date: date,
    basket: list[dict[str, Any]],
    suffix: str = '',
) -> Path:
    """Write basket to testing/{strategy_name}/m_combined_{YYYYMMDD}{suffix}.csv.

    suffix=''     → main basket (firm entries + exits)
    suffix='_SUB' → substitute reference file
    Both use TEST_BASKET_COLUMNS (same layout, Rank as last column).
    """
    out_dir = Path(PricePath.backtestPath) / 'testing' / strategy_name
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f'm_combined_{run_date.strftime("%Y%m%d")}{suffix}.csv'

    with csv_path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=TEST_BASKET_COLUMNS)
        writer.writeheader()
        for row in basket:
            writer.writerow(row)

    return csv_path


def _call_engine(payload: dict[str, Any]) -> dict[str, Any]:
    url = f'{settings.BACKTEST_JAVA_URL}/api/execution/signals/last-bar'
    print(f'[test_runner] POST {url}')
    response = requests.post(url, json=payload, timeout=ENGINE_HTTP_TIMEOUT_SEC)
    if response.status_code != 200:
        raise RuntimeError(
            f'Engine returned HTTP {response.status_code}: {response.text[:500]}'
        )
    return response.json()


def _next_trading_day(ref: date) -> date:
    nyse = mcal.get_calendar('NYSE')
    valid = nyse.valid_days(ref, ref + timedelta(days=10)).tz_localize(None)
    forward = [d.date() for d in valid if d.date() > ref]
    return forward[0] if forward else ref + timedelta(days=1)