"""
equity_recompute.py
===================
Reconstruct a strategy's live equity curve mark-to-market from the TRADELIST,
day by day, from first entry to the latest available close. Replaces reliance on
the sparse / stale live_equity_snapshot rows on the Live Performance page.

WHY
---
The nightly snapshot writer computes
    equity = (prod_cap - Σ intended_capital_of_LIVE) + Σ(qty*close_of_LIVE)
i.e. it values OPEN positions but returns a closed position's capital to the pool
AT COST — so realized P&L is discarded and a churning strategy flatlines near
production capital. It also only has rows for the handful of nights it ran.

This recompute mirrors the engine's PortfolioServiceImplV2.markToMarket, where
equity = cash + Σ(qty*close) and `cash` carries realized P&L (exit proceeds flow
back in). Equivalently, per day D:

    equity(D)     = production_capital + realized(D) + unrealized(D)
    realized(D)   = Σ profit            for EXITED rows with exit_date ≤ D
    unrealized(D) = Σ qty*(close(D) - entry_price)   for rows OPEN on D   [LONG]
                    Σ qty*(entry_price - close(D))   for rows OPEN on D   [SHORT]
    open on D     = entry_date ≤ D and (exit_date is null or exit_date > D)

This is correct (realized + unrealized), consistent with the tradelist, and
spans the full trading history.

ADJUSTED vs UNADJUSTED
----------------------
Live positions are currently seeded from the BACKTEST, whose prices are
TOTALRETURN-adjusted. So we value opens against the ADJUSTED closes
(daily_closes.csv) for an apples-to-apples, backtest-comparable equity — exactly
what the test-vs-backtest phase needs. When real IBKR fills replace the seeds
(unadjusted), flip `adjusted=False` to read daily_unadjusted.csv.

The pure math lives in compute_equity_curve() — no DB / Norgate / file IO — so it
is unit-testable (see __main__). recompute_live_equity() is the thin loader that
pulls the tradelist + closes and calls it.
"""

from __future__ import annotations

import datetime as dt
from typing import List, Dict, Optional

import numpy as np
import pandas as pd


# ──────────────────────────────────────────────────────────────────────────
#  Pure core — positions + closes → daily equity curve (no IO)
# ──────────────────────────────────────────────────────────────────────────
def compute_equity_curve(positions: List[Dict],
                         closes: pd.DataFrame,
                         prod_cap: float) -> Dict:
    """Mark-to-market daily equity curve from a position list.

    Args:
        positions: list of dicts, each:
            symbol (str), direction ('LONG'|'SHORT'), qty (int),
            entry_price (float), entry_date (date),
            exit_date (date|None), realized_profit (float|None for open)
        closes: DataFrame, DatetimeIndex (trading days), columns = symbols.
            MUST be forward-filled (no gaps for held tickers) — the loader
            ffills before calling.
        prod_cap: production capital (the equity baseline).

    Returns a dict of equal-length lists (one per trading day from the first
    entry through the last close), plus per-position contributions. Empty
    structure when there are no positions or no usable close dates.
    """
    empty = {
        'dates': [], 'equity': [], 'equity_offset': [], 'realized': [],
        'unrealized': [], 'deployed': [], 'unused': [], 'max_equity': [],
        'max_equity_offset': [], 'drawdown': [], 'drawdown_offset': [],
        'drawdown_pct': [], 'open_count': [], 'position_contributions': [],
        'as_of': None,
    }
    if not positions or closes is None or closes.empty:
        return empty

    first_entry = min(p['entry_date'] for p in positions)
    idx = closes.index[closes.index >= pd.Timestamp(first_entry)].sort_values()
    if len(idx) == 0:
        return empty

    dates, equity, realized_l, unrealized_l = [], [], [], []
    deployed_l, unused_l, open_count_l = [], [], []
    max_equity_l, drawdown_l, drawdown_pct_l = [], [], []
    running_max = prod_cap

    def close_on(symbol: str, ts: pd.Timestamp):
        if symbol not in closes.columns:
            return None
        v = closes.at[ts, symbol]
        if isinstance(v, pd.Series):     # dup column guard
            v = v.iloc[0]
        return None if pd.isna(v) else float(v)

    for ts in idx:
        d = ts.date()
        realized = 0.0
        unreal = 0.0
        cost_open = 0.0
        open_count = 0
        for p in positions:
            ed = p['entry_date']
            xd = p.get('exit_date')
            if xd is not None and xd <= d:
                realized += float(p.get('realized_profit') or 0.0)
                continue
            # open on d?
            if ed <= d and (xd is None or xd > d):
                c = close_on(p['symbol'], ts)
                if c is None:
                    continue
                qty = int(p['qty'])
                entry = float(p['entry_price'])
                if str(p.get('direction', 'LONG')).upper() == 'SHORT':
                    unreal += qty * (entry - c)
                else:
                    unreal += qty * (c - entry)
                cost_open += qty * entry
                open_count += 1

        eq = prod_cap + realized + unreal
        running_max = max(running_max, eq)
        dd = eq - running_max

        dates.append(d.isoformat())
        equity.append(round(eq, 2))
        realized_l.append(round(realized, 2))
        unrealized_l.append(round(unreal, 2))
        deployed_l.append(round(cost_open, 2))
        unused_l.append(round(prod_cap + realized - cost_open, 2))
        open_count_l.append(open_count)
        max_equity_l.append(round(running_max, 2))
        drawdown_l.append(round(dd, 2))
        drawdown_pct_l.append(round(dd / running_max, 6) if running_max else 0.0)

    # Per-position contribution: realized for closed, latest MTM for open.
    last_ts = idx[-1]
    contributions = []
    for p in positions:
        xd = p.get('exit_date')
        if xd is not None:
            pnl = float(p.get('realized_profit') or 0.0)
            status = 'EXITED'
        else:
            c = close_on(p['symbol'], last_ts)
            qty = int(p['qty']); entry = float(p['entry_price'])
            if c is None:
                pnl = 0.0
            elif str(p.get('direction', 'LONG')).upper() == 'SHORT':
                pnl = qty * (entry - c)
            else:
                pnl = qty * (c - entry)
            status = 'LIVE'
        contributions.append({
            'symbol': p['symbol'],
            'status': status,
            'entry_date': p['entry_date'].isoformat(),
            'exit_date': xd.isoformat() if xd else None,
            'pnl': round(pnl, 2),
            'pnl_pct_of_cap': round(pnl / prod_cap * 100, 2) if prod_cap else 0.0,
        })
    contributions.sort(key=lambda x: x['pnl'])  # worst first (drives drawdown)

    return {
        'dates': dates,
        'equity': equity,
        'equity_offset': [round(e - prod_cap, 2) for e in equity],
        'realized': realized_l,
        'unrealized': unrealized_l,
        'deployed': deployed_l,
        'unused': unused_l,
        'max_equity': max_equity_l,
        'max_equity_offset': [round(m - prod_cap, 2) for m in max_equity_l],
        'drawdown': drawdown_l,
        'drawdown_offset': drawdown_l,   # already equity - max_equity
        'drawdown_pct': drawdown_pct_l,
        'open_count': open_count_l,
        'position_contributions': contributions,
        'as_of': dates[-1] if dates else None,
    }


# ──────────────────────────────────────────────────────────────────────────
#  Loader — pull tradelist + closes, call the pure core
# ──────────────────────────────────────────────────────────────────────────
def _closes_path_for_universe(universe: str, adjusted: bool = True) -> str:
    """Resolve the universe's matrix-CSV path. Adjusted = daily_closes.csv
    (TOTALRETURN); unadjusted = daily_unadjusted.csv."""
    from app.constants.PricePath import PricePath  # lazy
    base_map = {
        'sp500':       PricePath.sp500base_path,
        'liquid500':   PricePath.liquid500base_path,
        'russell3000': PricePath.russell3000base_path,
        'sp100':       PricePath.sp100base_path,
        'nasdaq100':   PricePath.nasdaq100base_path,
        'spy':         PricePath.spy_path,
    }
    base = base_map.get((universe or '').lower())
    if base is None:
        raise ValueError(f"No closes path configured for universe '{universe}'")
    return PricePath.unadjustedCloses(base) if not adjusted else PricePath.close(base)


def recompute_live_equity(db, strategy_id: int, adjusted: bool = True) -> Dict:
    """Load the strategy's TRADED LIVE+EXITED rows and its universe closes, then
    recompute the full mark-to-market equity curve + summary metrics.

    Returns the same `equity_series` shape the Live Performance page already
    consumes (so the existing chart just renders the full, correct curve), plus
    realized/unrealized series, per-position contributions, and metrics.
    """
    from app.models.market_regime import MarketRegime          # lazy
    from app.models.strategy_bucket import StrategyBucket
    from app.models.tradelist import Tradelist

    regime = (db.query(MarketRegime).filter_by(strategy_id=strategy_id)
              .order_by(MarketRegime.id.asc()).first())
    strategy = db.query(StrategyBucket).filter_by(id=strategy_id).first()

    prod_cap = 0.0
    if regime and regime.production_capital:
        prod_cap = float(regime.production_capital)
    elif strategy and strategy.production_capital:
        prod_cap = float(strategy.production_capital)
    universe = (regime.universe if regime else '') or ''

    rows = (db.query(Tradelist)
            .filter(Tradelist.strategy_id == strategy_id,
                    Tradelist.ledger == 'TRADED',
                    Tradelist.status.in_(['LIVE', 'EXITED']))
            .all())

    positions = []
    for r in rows:
        if r.entry_date is None or r.entry_price is None:
            continue
        positions.append({
            'symbol': r.symbol,
            'direction': r.direction or 'LONG',
            'qty': int(r.filled_qty or r.intended_qty or 0),
            'entry_price': float(r.entry_price),
            'entry_date': r.entry_date,
            'exit_date': r.exit_date,
            'realized_profit': float(r.profit) if r.profit is not None else None,
        })

    if not positions:
        return {'curve': compute_equity_curve([], None, prod_cap),
                'production_capital': prod_cap, 'universe': universe,
                'source': 'recompute', 'note': 'no LIVE/EXITED positions'}

    closes_path = _closes_path_for_universe(universe, adjusted=adjusted)
    closes = pd.read_csv(closes_path, index_col=['Date'], parse_dates=True)
    closes = closes[~closes.index.duplicated(keep='first')].sort_index()
    # ffill so a held ticker never shows a gap (halts, missing prints).
    closes = closes.ffill()

    curve = compute_equity_curve(positions, closes, prod_cap)
    return {'curve': curve, 'production_capital': prod_cap,
            'universe': universe, 'adjusted': adjusted, 'source': 'recompute'}


# ──────────────────────────────────────────────────────────────────────────
#  Self-test — proves the MTM math with synthetic data (pandas only, no IO)
# ──────────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    # 5 trading days. AAA opens d0 @100 (10sh), closes d2 @95 → realized -50.
    # BBB opens d1 @50 (20sh), still open; closes 50→52→54→56 over d1..d4.
    dates = pd.to_datetime(['2026-06-16', '2026-06-17', '2026-06-18',
                            '2026-06-22', '2026-06-23'])
    closes = pd.DataFrame({
        'AAA': [100.0, 98.0, 95.0, np.nan, np.nan],   # delisted after exit; ffill handles
        'BBB': [np.nan, 50.0, 52.0, 54.0, 56.0],
    }, index=dates).ffill()

    positions = [
        {'symbol': 'AAA', 'direction': 'LONG', 'qty': 10, 'entry_price': 100.0,
         'entry_date': dt.date(2026, 6, 16), 'exit_date': dt.date(2026, 6, 18),
         'realized_profit': (95.0 - 100.0) * 10},                       # -50
        {'symbol': 'BBB', 'direction': 'LONG', 'qty': 20, 'entry_price': 50.0,
         'entry_date': dt.date(2026, 6, 17), 'exit_date': None,
         'realized_profit': None},
    ]
    prod = 1000.0
    c = compute_equity_curve(positions, closes, prod)

    # Expected equity per day:
    # d0 (06-16): AAA open, 10*(100-100)=0; realized 0 → 1000
    # d1 (06-17): AAA 10*(98-100)=-20; BBB 20*(50-50)=0 → 980
    # d2 (06-18): AAA exited (realized -50); BBB 20*(52-50)=+40 → 1000-50+40 = 990
    # d3 (06-22): realized -50; BBB 20*(54-50)=+80 → 1030
    # d4 (06-23): realized -50; BBB 20*(56-50)=+120 → 1070
    expected = [1000.0, 980.0, 990.0, 1030.0, 1070.0]
    print('dates   :', c['dates'])
    print('equity  :', c['equity'])
    print('expected:', expected)
    print('realized:', c['realized'])
    print('unreal  :', c['unrealized'])
    print('max_eq  :', c['max_equity'])
    print('drawdown:', c['drawdown'])
    assert c['equity'] == expected, f"equity mismatch: {c['equity']} != {expected}"
    # drawdown: running max=1000 until d3(1030),d4(1070). dd: d1=-20,d2=-10,rest 0/peaks
    assert c['drawdown'][1] == -20.0 and c['drawdown'][2] == -10.0, c['drawdown']
    assert c['drawdown'][0] == 0.0 and c['drawdown'][4] == 0.0, c['drawdown']
    # contributions: AAA -50 (EXITED), BBB +120 (LIVE, latest 56)
    contrib = {x['symbol']: x for x in c['position_contributions']}
    assert contrib['AAA']['pnl'] == -50.0 and contrib['AAA']['status'] == 'EXITED'
    assert contrib['BBB']['pnl'] == 120.0 and contrib['BBB']['status'] == 'LIVE'
    print('\n✅ self-test passed — MTM math (realized + unrealized) verified')


# ──────────────────────────────────────────────────────────────────────────
#  Recalc + store — rebuild live_equity_snapshot from the tradelist on demand
#  (the page's "Recalculate equity" button). Display reads the table as before.
# ──────────────────────────────────────────────────────────────────────────
def _load_positions_and_closes(db, strategy_id: int, adjusted: bool = True):
    from app.models.market_regime import MarketRegime          # lazy
    from app.models.strategy_bucket import StrategyBucket
    from app.models.tradelist import Tradelist

    regime = (db.query(MarketRegime).filter_by(strategy_id=strategy_id)
              .order_by(MarketRegime.id.asc()).first())
    strategy = db.query(StrategyBucket).filter_by(id=strategy_id).first()

    prod_cap = 0.0
    if regime and regime.production_capital:
        prod_cap = float(regime.production_capital)
    elif strategy and strategy.production_capital:
        prod_cap = float(strategy.production_capital)
    universe = (regime.universe if regime else '') or ''

    rows = (db.query(Tradelist)
            .filter(Tradelist.strategy_id == strategy_id,
                    Tradelist.ledger == 'TRADED',
                    Tradelist.status.in_(['LIVE', 'EXITED']))
            .all())
    positions = []
    for r in rows:
        if r.entry_date is None or r.entry_price is None:
            continue
        positions.append({
            'symbol': r.symbol, 'direction': r.direction or 'LONG',
            'qty': int(r.filled_qty or r.intended_qty or 0),
            'entry_price': float(r.entry_price), 'entry_date': r.entry_date,
            'exit_date': r.exit_date,
            'realized_profit': float(r.profit) if r.profit is not None else None,
        })

    closes = None
    if positions:
        path = _closes_path_for_universe(universe, adjusted=adjusted)
        closes = pd.read_csv(path, index_col=['Date'], parse_dates=True)
        closes = closes[~closes.index.duplicated(keep='first')].sort_index().ffill()
    return positions, closes, prod_cap, universe


def recalc_and_store(db, strategy_id: int, adjusted: bool = True) -> Dict:
    """Recompute the full MTM equity curve from the tradelist and UPSERT one
    live_equity_snapshot row per trading day (first entry -> latest close).
    Writes only existing columns (no schema change). Does NOT commit — the
    caller commits so a failure can't leave a half-written table."""
    from datetime import date as _date
    from app.models.live_equity_snapshot import LiveEquitySnapshot   # lazy

    positions, closes, prod_cap, universe = _load_positions_and_closes(
        db, strategy_id, adjusted)
    if not positions:
        return {'ok': True, 'rows_written': 0, 'note': 'no LIVE/EXITED positions',
                'universe': universe, 'production_capital': prod_cap}

    curve = compute_equity_curve(positions, closes, prod_cap)
    dates = curve['dates']
    written = 0
    for i, ds in enumerate(dates):
        d = _date.fromisoformat(ds)
        snap = (db.query(LiveEquitySnapshot)
                .filter_by(strategy_id=strategy_id, snapshot_date=d).first())
        if snap is None:
            snap = LiveEquitySnapshot(strategy_id=strategy_id, snapshot_date=d)
            db.add(snap)
        eq = curve['equity'][i]
        dep = curve['deployed'][i]
        unreal = curve['unrealized'][i]
        snap.production_capital  = prod_cap or None
        snap.open_position_count = curve['open_count'][i]
        snap.deployed_capital    = dep
        snap.unused_capital      = curve['unused'][i]
        snap.market_value        = round(dep + unreal, 4)            # cost + unrealized = mkt value (long)
        snap.equity              = eq
        snap.unrealised_pnl      = round(eq - prod_cap, 4)           # total P&L vs baseline (realized + unrealized)
        snap.unrealised_pct      = round((eq - prod_cap) / prod_cap, 6) if prod_cap else 0.0
        snap.max_equity          = curve['max_equity'][i]
        snap.drawdown            = curve['drawdown'][i]
        snap.drawdown_pct        = curve['drawdown_pct'][i]
        written += 1

    return {'ok': True, 'rows_written': written,
            'first_date': dates[0], 'last_date': dates[-1],
            'latest_equity': round(curve['equity'][-1], 2),
            'latest_drawdown_pct': round(curve['drawdown_pct'][-1] * 100, 2),
            'production_capital': prod_cap, 'universe': universe, 'adjusted': adjusted}