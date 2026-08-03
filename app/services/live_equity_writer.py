"""
live_equity_writer.py — Step E: write daily live equity snapshot.

Called from runner.py after the main transaction commits so a snapshot
write failure never rolls back the PM work.

Formula:
  deployed_capital = sum(intended_capital) for all LIVE rows of this strategy
  unused_capital   = production_capital - deployed_capital
  market_value     = sum(filled_qty * close_price) for all LIVE rows
  equity           = unused_capital + market_value

  unrealised_pnl   = equity - production_capital
  unrealised_pct   = unrealised_pnl / production_capital

  max_equity       = max(previous max_equity, equity)   [from last snapshot]
  drawdown         = equity - max_equity                 [always ≤ 0]
  drawdown_pct     = drawdown / max_equity
"""
from __future__ import annotations
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from app.models.live_equity_snapshot import LiveEquitySnapshot
from app.models.market_regime import MarketRegime
from app.models.strategy_bucket import StrategyBucket
from app.models.tradelist import Tradelist


def write_live_equity_snapshot(
    db: Session,
    strategy_id: int,
    run_date: date,
    data_root: str,
    universe: str,
    rebalance: str,
) -> dict:
    """Compute and persist one LiveEquitySnapshot row for strategy_id on run_date.

    Args:
        db:          SQLAlchemy session. Caller must commit.
        strategy_id: strategy to snapshot.
        run_date:    the data date (bar that just closed).
        data_root:   path to exec_data/{YYYYMMDD}/ folder.
        universe:    regime's universe slug (e.g. 'sp500').
        rebalance:   strategy.rebalance (e.g. 'daily').

    Returns:
        Dict with equity, unrealised_pnl, open_position_count, drawdown.
    """
    strategy = db.query(StrategyBucket).filter_by(id=strategy_id).first()
    first_regime = (
        db.query(MarketRegime)
        .filter_by(strategy_id=strategy_id)
        .order_by(MarketRegime.id.asc())
        .first()
    )

    # Production capital — fallback chain matches payload_builder
    prod_cap = 0.0
    if first_regime and first_regime.production_capital:
        prod_cap = float(first_regime.production_capital)
    elif strategy and strategy.production_capital:
        prod_cap = float(strategy.production_capital)

    # All LIVE rows for this strategy
    live_rows = (
        db.query(Tradelist)
        .filter(
            Tradelist.strategy_id == strategy_id,
            Tradelist.ledger == 'TRADED',
            Tradelist.status == 'LIVE',
        )
        .all()
    )

    # Load today's closes from exec_data parquet
    from app.services.position_manager.fill_resolver import (
        _read_day_series,
        _prefix_for_rebalance,
    )
    parquet_dir = Path(data_root) / universe
    prefix      = _prefix_for_rebalance(rebalance)
    closes_path = parquet_dir / f'{prefix}closes.parquet'

    day_closes: pd.Series | None = None
    try:
        day_closes = _read_day_series(closes_path, run_date)
    except KeyError:
        # run_date not in parquet — likely a weekend or market holiday.
        # Use the most recent available trading day's closes instead.
        print(f'[live_equity] INFO: {run_date} not in parquet (non-trading day) '
              f'— using most recent available closes')
        try:
            df = pd.read_parquet(closes_path)
            # Find the last trading day before run_date
            available = df.index[df.index <= pd.Timestamp(run_date)]
            if len(available) > 0:
                last_trading_day = available[-1]
                day_closes = df.loc[last_trading_day]
                print(f'[live_equity] Using closes from {last_trading_day.date()} '
                      f'as proxy for {run_date}')
        except Exception as e2:
            print(f'[live_equity] WARNING: fallback close load failed: {e2}')
    except Exception as e:
        print(f'[live_equity] WARNING: could not load closes parquet: {e}')

    # Compute components
    open_count      = len(live_rows)
    deployed_capital = sum(float(r.intended_capital or 0) for r in live_rows)
    unused_capital   = prod_cap - deployed_capital

    market_value = 0.0
    for r in live_rows:
        qty   = int(r.filled_qty or 0)
        close = float(day_closes.get(r.symbol, 0)) if day_closes is not None else 0.0
        market_value += qty * close

    equity           = unused_capital + market_value
    unrealised_pnl   = equity - prod_cap if prod_cap > 0 else 0.0
    unrealised_pct   = unrealised_pnl / prod_cap if prod_cap > 0 else 0.0

    # Running max equity (from the most recent previous snapshot)
    prev = (
        db.query(LiveEquitySnapshot)
        .filter(
            LiveEquitySnapshot.strategy_id == strategy_id,
            LiveEquitySnapshot.snapshot_date < run_date,
        )
        .order_by(LiveEquitySnapshot.snapshot_date.desc())
        .first()
    )
    prev_max = float(prev.max_equity) if prev and prev.max_equity else (prod_cap or equity)
    max_equity  = max(prev_max, equity)
    drawdown     = equity - max_equity
    drawdown_pct = drawdown / max_equity if max_equity > 0 else 0.0

    # Upsert — replace existing snapshot for same date if rerun
    existing = (
        db.query(LiveEquitySnapshot)
        .filter_by(strategy_id=strategy_id, snapshot_date=run_date)
        .first()
    )
    if existing:
        snap = existing
    else:
        snap = LiveEquitySnapshot(strategy_id=strategy_id, snapshot_date=run_date)
        db.add(snap)

    snap.production_capital  = prod_cap or None
    snap.open_position_count = open_count
    snap.deployed_capital    = deployed_capital
    snap.unused_capital      = unused_capital
    snap.market_value        = market_value
    snap.equity              = equity
    snap.unrealised_pnl      = unrealised_pnl
    snap.unrealised_pct      = unrealised_pct
    snap.max_equity          = max_equity
    snap.drawdown            = drawdown
    snap.drawdown_pct        = drawdown_pct

    print(
        f'[live_equity] strategy_id={strategy_id} ({strategy.name if strategy else "?"}) '
        f'date={run_date} equity={equity:.2f} unrealised_pnl={unrealised_pnl:.2f} '
        f'open={open_count} drawdown={drawdown:.2f}'
    )

    return {
        'snapshot_date':      run_date.isoformat(),
        'equity':             equity,
        'unrealised_pnl':     unrealised_pnl,
        'unrealised_pct':     unrealised_pct,
        'open_position_count': open_count,
        'drawdown':           drawdown,
        'drawdown_pct':       drawdown_pct,
    }