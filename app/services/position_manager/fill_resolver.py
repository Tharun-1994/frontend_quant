"""
fill_resolver.py — C2.3 (Step A of Position Manager)

Pure computation: given a strategy + run_date + exec_data path, returns
fill outcomes for every PENDING_FILL row on the TRADED ledger. No SQL
writes — caller (runner.py / C2.7) applies the outcomes inside its
transaction so observability stays under one controller.

Phase 1 fill modeling (matches legacy run_exe_instr_final.py semantics):

  LIMIT-style (limit_price > 0):
      LONG  fills if day's low  ≤ limit_price → entry_price = limit_price
      SHORT fills if day's high ≥ limit_price → entry_price = limit_price
      Otherwise CANCELLED.

  MKT-style (limit_price == 0):
      Always fills. entry_price comes from the bar's open or close
      depending on the regime's entry_timing config.

Stop modeling: Phase 1 always 'FULL' fills (no partial / no rejection).
Phase 2 reconciliation will compare modeled fills against IBKR statements.
See design doc §10 known-flaws #7 (calibration study pre-live).
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd
from sqlalchemy.orm import Session

from app.models.tradelist import Tradelist
from app.models.market_regime import MarketRegime


@dataclass
class FillOutcome:
    """Per-row resolution outcome. The runner applies these via UPDATE.

    Filled rows: all entry_* and fill_status fields populated.
    Cancelled rows: only row_id + filled=False populated; UPDATE flips
    status='CANCELLED' and leaves entry_* columns NULL.
    """
    row_id: int
    filled: bool
    entry_price: Optional[float] = None
    entry_date: Optional[date] = None
    entry_timing: Optional[str] = None
    fill_status: Optional[str] = None  # 'FULL' when filled, None when not


def resolve_fills(
    db: Session,
    strategy_id: int,
    run_date: date,
    data_root: str,
    universe: str,
    rebalance: str,
) -> list[FillOutcome]:
    """Resolve all PENDING_FILL rows for one strategy against today's bar.

    Args:
        db: SQLAlchemy session (read-only here; runner owns the transaction).
        strategy_id: PM is per-strategy; this call processes one strategy's rows.
        run_date: data date. PM resolves PENDING_FILL rows whose
                  intended_trade_date == run_date (the bar that just closed).
        data_root: full absolute path to date-stamped exec_data folder,
                   e.g. 'C:/Tharun/Projects/backtest_data/exec_data/20260616'.
                   Same string PM sends to engine as `dataRoot` (Patch 14).
        universe: regime's universe slug (e.g. 'sp500'). Folder under data_root.
        rebalance: strategy.rebalance value (e.g. 'daily', 'weekly', 'monthly').
                   Used to build the parquet filename prefix — must match the
                   engine's PriceLoader.java:105-110 convention exactly:
                       daily   → 'DAILY_'  (uppercase, historical)
                       weekly  → 'weekly_' (lowercase)
                       monthly → 'monthly_' (lowercase)

    Returns:
        List of FillOutcome — one per PENDING_FILL row, in DB-id order.
        Empty list if no PENDING_FILL rows for this (strategy, run_date).

    Raises:
        FileNotFoundError if expected parquets don't exist at data_root/universe/
        KeyError if run_date not present in the parquet's index
    """
    # 1. Query PENDING_FILL rows for (strategy, run_date) on TRADED ledger only.
    #    SYSTEM ledger rows are audit shadows — never resolved against fills.
    rows = (
        db.query(Tradelist)
        .filter(
            Tradelist.strategy_id == strategy_id,
            Tradelist.ledger == 'TRADED',
            Tradelist.status == 'PENDING_FILL',
            Tradelist.intended_trade_date == run_date,
        )
        .order_by(Tradelist.id.asc())
        .all()
    )

    if not rows:
        print(f'[fill_resolver] strategy_id={strategy_id} run_date={run_date}: '
              f'0 PENDING_FILL rows, nothing to resolve')
        return []

    print(f'[fill_resolver] strategy_id={strategy_id} run_date={run_date}: '
          f'{len(rows)} PENDING_FILL row(s) to resolve')

    # 2. Look up the regime for each unique entered_regime_id.
    #    We need regime.entry_timing for the MKT-fill case (limit_price == 0).
    regime_ids = {row.entered_regime_id for row in rows}
    regimes_by_id = {
        r.id: r for r in
        db.query(MarketRegime).filter(MarketRegime.id.in_(regime_ids)).all()
    }
    if len(regimes_by_id) != len(regime_ids):
        missing = regime_ids - set(regimes_by_id.keys())
        raise ValueError(
            f'PENDING_FILL rows reference regime IDs {missing} that no longer '
            f'exist in marketregime. Refusing to proceed — data integrity issue.'
        )

    # 3. Load parquets ONCE for the day. PM may have 10-30 rows; reading the
    #    parquet per-row would be 30+ reads. One read, then .loc lookup per ticker.
    parquet_dir = Path(data_root) / universe
    prefix = _prefix_for_rebalance(rebalance)
    day_lows = _read_day_series(parquet_dir / f'{prefix}lows.parquet', run_date)
    day_highs = _read_day_series(parquet_dir / f'{prefix}highs.parquet', run_date)
    day_opens = _read_day_series(parquet_dir / f'{prefix}opens.parquet', run_date)
    day_closes = _read_day_series(parquet_dir / f'{prefix}closes.parquet', run_date)

    # 4. Resolve each row.
    outcomes = []
    for row in rows:
        regime = regimes_by_id[row.entered_regime_id]
        outcome = _resolve_one_row(row, regime, run_date,
                                   day_lows, day_highs, day_opens, day_closes)
        outcomes.append(outcome)

    n_filled    = sum(1 for o in outcomes if o.filled)
    n_cancelled = sum(1 for o in outcomes if not o.filled)
    print(f'[fill_resolver] resolved: {n_filled} filled, {n_cancelled} cancelled')

    return outcomes


def _resolve_one_row(
    row: Tradelist,
    regime: MarketRegime,
    run_date: date,
    day_lows: pd.Series,
    day_highs: pd.Series,
    day_opens: pd.Series,
    day_closes: pd.Series,
) -> FillOutcome:
    """Decide LIVE vs CANCELLED for one PENDING_FILL row.

    Discriminator: limit_price == 0 → MKT-style; else LIMIT-style.
    This avoids a regime.order_type lookup — limit_price carries the same
    information at the row level and was set authoritatively at PROPOSED time.
    """
    limit_price = float(row.limit_price) if row.limit_price is not None else 0.0

    # MKT-style fill (NORMAL order_type → limit_price = 0 by Patch 16 contract;
    # Patch 16 not yet shipped, so this branch is dormant until then).
    if limit_price == 0.0:
        entry_timing = (regime.entry_timing or 'open').lower()
        if entry_timing == 'close':
            entry_price = _safe_lookup(day_closes, row.symbol, row.id, 'close')
        else:  # 'open' default
            entry_price = _safe_lookup(day_opens, row.symbol, row.id, 'open')
        return FillOutcome(
            row_id=row.id,
            filled=True,
            entry_price=entry_price,
            entry_date=run_date,
            entry_timing=entry_timing,
            fill_status='FULL',
        )

    # LIMIT-style fill. Direction determines which OHLC leg matters.
    if row.direction == 'LONG':
        day_low = _safe_lookup(day_lows, row.symbol, row.id, 'low')
        filled = (day_low <= limit_price)
    elif row.direction == 'SHORT':
        day_high = _safe_lookup(day_highs, row.symbol, row.id, 'high')
        filled = (day_high >= limit_price)
    else:
        raise ValueError(
            f'Tradelist row id={row.id} has unexpected direction={row.direction!r}'
        )

    if filled:
        # Phase 1: modeled fill at limit_price exactly. Matches legacy
        # run_exe_instr_final convention. Real IBKR fill may differ — Phase 2
        # calibration study compares against statements.
        return FillOutcome(
            row_id=row.id,
            filled=True,
            entry_price=limit_price,
            entry_date=run_date,
            entry_timing='intraday',   # LIMIT fills are always intraday
            fill_status='FULL',
        )
    else:
        return FillOutcome(row_id=row.id, filled=False)


def _read_day_series(path: Path, run_date: date) -> pd.Series:
    """Read one parquet file and return the row at run_date as a Series
    indexed by ticker. Raises if file missing or run_date not in index.
    """
    if not path.exists():
        raise FileNotFoundError(
            f'Expected parquet missing: {path}. Was exec_data_refresh run for '
            f'this date + universe?'
        )
    df = pd.read_parquet(path)
    # Parquets are written with dates as index (DatetimeIndex or DateIndex).
    # Normalize to date-only for the .loc lookup.
    try:
        # Try direct .loc — works if index is already date-typed
        row = df.loc[run_date]
    except KeyError:
        # Try with explicit Timestamp coercion
        ts = pd.Timestamp(run_date)
        if ts in df.index:
            row = df.loc[ts]
        else:
            available = df.index.min(), df.index.max()
            raise KeyError(
                f'run_date={run_date} not in {path.name}. '
                f'Parquet date range: {available[0]} .. {available[1]}'
            )
    return row


def _prefix_for_rebalance(rebalance: str) -> str:
    """Mirror engine's PriceLoader.java:105-110 prefix convention.

    Daily-rebalance strategies use 'DAILY_' (uppercase, historical artifact);
    weekly/monthly use lowercase. PM must use the SAME prefix the engine
    uses so it reads the same files the engine reads.
    """
    rb = (rebalance or '').lower()
    if rb == 'daily':
        return 'DAILY_'
    if rb in ('weekly', 'monthly'):
        return f'{rb}_'
    raise ValueError(
        f'Unknown rebalance {rebalance!r}. Expected daily / weekly / monthly. '
        f'See PriceLoader.java:105-110.'
    )


@dataclass
class ExitFillOutcome:
    """Per-row exit resolution outcome. Runner applies these via UPDATE.

    exit_price: the price at which the position closed (open or close bar price
                depending on regime.exit_timing).
    profit:     (exit_price - entry_price) * filled_qty for LONG,
                (entry_price - exit_price) * filled_qty for SHORT.
    profit_pct: profit / (entry_price * filled_qty).
    day_count:  calendar days from entry_date to exit_date inclusive.
    """
    row_id:     int
    exit_price: float
    profit:     float
    profit_pct: float
    day_count:  int


def resolve_exit_fills(
    db: Session,
    strategy_id: int,
    run_date: date,
    data_root: str,
    universe: str,
    rebalance: str,
) -> list[ExitFillOutcome]:
    """Resolve EXIT_SUBMITTED rows against today's bar price.

    Called from runner.py Step A.5 (after entry fill resolution).
    Reads EXIT_SUBMITTED rows whose exit_date == run_date, looks up the
    exit price from the parquet (open or close per regime.exit_timing),
    computes profit + profit_pct + day_count. Returns outcomes — runner
    applies them via UPDATE inside its transaction.

    Args:
        db:          SQLAlchemy session (read-only; runner owns transaction).
        strategy_id: process one strategy's rows only.
        run_date:    data date — the bar whose open/close is the fill price.
        data_root:   path to exec_data/{YYYYMMDD}/ folder.
        universe:    regime's universe slug (e.g. 'sp500').
        rebalance:   strategy.rebalance (e.g. 'daily').

    Returns:
        List of ExitFillOutcome, one per EXIT_SUBMITTED row with
        exit_date == run_date. Empty if no exits today.
    """
    rows = (
        db.query(Tradelist)
        .filter(
            Tradelist.strategy_id == strategy_id,
            Tradelist.ledger == 'TRADED',
            Tradelist.status == 'EXIT_SUBMITTED',
            Tradelist.exit_date == run_date,
        )
        .order_by(Tradelist.id.asc())
        .all()
    )

    if not rows:
        print(f'[fill_resolver] strategy_id={strategy_id} run_date={run_date}: '
              f'0 EXIT_SUBMITTED rows, nothing to resolve')
        return []

    print(f'[fill_resolver] strategy_id={strategy_id} run_date={run_date}: '
          f'{len(rows)} EXIT_SUBMITTED row(s) to resolve')

    # Look up regimes for exit_timing
    regime_ids = {row.entered_regime_id for row in rows}
    regimes_by_id = {
        r.id: r for r in
        db.query(MarketRegime).filter(MarketRegime.id.in_(regime_ids)).all()
    }

    # Load parquets once — same pattern as entry fill resolution
    parquet_dir = Path(data_root) / universe
    prefix      = _prefix_for_rebalance(rebalance)
    day_opens   = _read_day_series(parquet_dir / f'{prefix}opens.parquet',  run_date)
    day_closes  = _read_day_series(parquet_dir / f'{prefix}closes.parquet', run_date)

    outcomes: list[ExitFillOutcome] = []
    for row in rows:
        regime       = regimes_by_id.get(row.entered_regime_id)
        exit_timing  = (regime.exit_timing or 'open').lower() if regime else 'open'

        # Exit price: open of exit_date for 'open' timing, close for 'close'
        if exit_timing == 'close':
            exit_price = _safe_lookup(day_closes, row.symbol, row.id, 'close')
        else:
            exit_price = _safe_lookup(day_opens, row.symbol, row.id, 'open')

        entry_price = float(row.entry_price or 0)
        filled_qty  = int(row.filled_qty or 0)
        direction   = (row.direction or 'LONG').upper()

        # Profit calculation — mirrors backtest tradelist convention
        if direction == 'LONG':
            profit = (exit_price - entry_price) * filled_qty
        else:
            profit = (entry_price - exit_price) * filled_qty

        cost       = entry_price * filled_qty
        profit_pct = (profit / cost) if cost != 0 else 0.0

        # day_count: inclusive calendar days from entry_date to exit_date
        entry_date = row.entry_date
        day_count  = (run_date - entry_date).days + 1 if entry_date else 0

        outcomes.append(ExitFillOutcome(
            row_id=row.id,
            exit_price=exit_price,
            profit=profit,
            profit_pct=profit_pct,
            day_count=day_count,
        ))

        print(f'[fill_resolver]   exit tradeId={row.id} {row.symbol}: '
              f'exit_price={exit_price:.4f} profit={profit:.2f} '
              f'profit_pct={profit_pct:.4f} day_count={day_count}')

    print(f'[fill_resolver] exit fills resolved: {len(outcomes)}')
    return outcomes


@dataclass
class HypotheticalFillOutcome:
    """P&L outcome for a SYSTEM-ledger row (original ticker never traded).

    entry_price: open price on intended_trade_date for the original ticker.
    exit_price:  open/close price on exit_date — same date as the linked
                 TRADED row's exit (so comparison is apples-to-apples).
    profit:      hypothetical P&L if the original had been held and sold
                 on the same day as the substitute.
    profit_pct:  profit / (entry_price * intended_qty).
    day_count:   calendar days from intended_trade_date to exit_date inclusive.
    """
    row_id:     int
    entry_price: float
    exit_price:  float
    profit:      float
    profit_pct:  float
    day_count:   int


def resolve_hypothetical_fills(
    db: Session,
    run_date: date,
    data_root: str,
    universe: str,
    rebalance: str,
) -> list[HypotheticalFillOutcome]:
    """Compute hypothetical entry + exit prices for SYSTEM-ledger rows.

    SYSTEM rows are created by overlay_apply when Vas elides or substitutes
    a ticker. They carry the original symbol the engine picked but were never
    executed. This function populates their entry_price, exit_price, profit,
    profit_pct, and day_count from parquet data so future analysis can compare:
      actual substitute P&L  vs  hypothetical original P&L.

    Called from runner.py Step A.6 after exit fill resolution. Runs across ALL
    strategies (not per-strategy) since SYSTEM rows span strategies and the
    parquets loaded here are universe-wide.

    Two sub-cases handled per nightly run:

    Case 1 — entry price not yet set:
        SYSTEM row has intended_trade_date == run_date and entry_price IS NULL.
        Look up the open price for the original ticker on run_date.
        Write entry_price and entry_date.

    Case 2 — exit price not yet set:
        SYSTEM row has exit_date == run_date and exit_price IS NULL
        AND entry_price IS NOT NULL (already set).
        Look up the open/close price on run_date (exit_timing from linked
        TRADED row's regime).
        Write exit_price, profit, profit_pct, day_count, status='EXITED'.

    Args:
        db:          SQLAlchemy session (read-only; runner owns transaction).
        run_date:    data date — the bar that just closed.
        data_root:   path to exec_data/{YYYYMMDD}/ folder.
        universe:    universe slug (e.g. 'sp500').
        rebalance:   strategy.rebalance (e.g. 'daily').

    Returns:
        List of HypotheticalFillOutcome for rows whose exit was resolved today.
        Entry-only updates (Case 1) are applied directly to the DB rows and
        not included in the returned list (no profit to record yet).
    """
    # ── Case 1 — entry price fill ─────────────────────────────────────────────
    entry_rows = (
        db.query(Tradelist)
        .filter(
            Tradelist.ledger == 'SYSTEM',
            Tradelist.intended_trade_date == run_date,
            Tradelist.entry_price.is_(None),
        )
        .all()
    )

    # ── Case 2 — exit price fill ──────────────────────────────────────────────
    exit_rows = (
        db.query(Tradelist)
        .filter(
            Tradelist.ledger == 'SYSTEM',
            Tradelist.exit_date == run_date,
            Tradelist.entry_price.isnot(None),
            Tradelist.exit_price.is_(None),
        )
        .all()
    )

    if not entry_rows and not exit_rows:
        return []

    print(
        f'[fill_resolver] hypothetical fills: '
        f'{len(entry_rows)} entry row(s), {len(exit_rows)} exit row(s) on {run_date}'
    )

    # Load parquets once for both cases
    parquet_dir = Path(data_root) / universe
    prefix      = _prefix_for_rebalance(rebalance)

    try:
        day_opens  = _read_day_series(parquet_dir / f'{prefix}opens.parquet',  run_date)
        day_closes = _read_day_series(parquet_dir / f'{prefix}closes.parquet', run_date)
    except (FileNotFoundError, KeyError) as e:
        print(f'[fill_resolver] WARNING: hypothetical fills skipped — '
              f'could not load parquet: {e}')
        return []

    # Case 1 — write entry_price for SYSTEM rows entering today
    for row in entry_rows:
        price = day_opens.get(row.symbol)
        if price is None or price <= 0:
            print(f'[fill_resolver]   hypothetical entry: {row.symbol} '
                  f'not in opens parquet for {run_date} — skipping')
            continue
        row.entry_price = float(price)
        row.entry_date  = run_date
        row.entry_timing = 'open'
        print(f'[fill_resolver]   hypothetical entry id={row.id} '
              f'{row.symbol} entry_price={price:.4f}')

    # Case 2 — write exit_price + profit for SYSTEM rows exiting today
    # Resolve exit_timing from the linked TRADED row's regime
    outcomes: list[HypotheticalFillOutcome] = []
    regime_cache: dict[int, MarketRegime] = {}

    for row in exit_rows:
        # Get exit_timing from the linked TRADED row (substitute_link_id points
        # to the SYSTEM row FROM the TRADED row — so query the other direction)
        linked_traded = (
            db.query(Tradelist)
            .filter(
                Tradelist.substitute_link_id == row.id,
                Tradelist.ledger == 'TRADED',
            )
            .first()
        )

        exit_timing = 'open'   # default — matches PullBack open exits
        if linked_traded and linked_traded.entered_regime_id:
            regime_id = linked_traded.entered_regime_id
            if regime_id not in regime_cache:
                regime_cache[regime_id] = (
                    db.query(MarketRegime).filter_by(id=regime_id).first()
                )
            regime = regime_cache.get(regime_id)
            if regime:
                exit_timing = (regime.exit_timing or 'open').lower()

        exit_price = (
            day_closes.get(row.symbol)
            if exit_timing == 'close'
            else day_opens.get(row.symbol)
        )

        if exit_price is None or exit_price <= 0:
            print(f'[fill_resolver]   hypothetical exit: {row.symbol} '
                  f'not in parquet for {run_date} — skipping')
            continue

        exit_price    = float(exit_price)
        entry_price   = float(row.entry_price)
        intended_qty  = int(row.intended_qty or 0)
        direction     = (row.direction or 'LONG').upper()

        if direction == 'LONG':
            profit = (exit_price - entry_price) * intended_qty
        else:
            profit = (entry_price - exit_price) * intended_qty

        cost       = entry_price * intended_qty
        profit_pct = (profit / cost) if cost != 0 else 0.0
        day_count  = (run_date - row.intended_trade_date).days + 1 if row.intended_trade_date else 0

        outcomes.append(HypotheticalFillOutcome(
            row_id=row.id,
            entry_price=entry_price,
            exit_price=exit_price,
            profit=profit,
            profit_pct=profit_pct,
            day_count=day_count,
        ))

        print(
            f'[fill_resolver]   hypothetical exit id={row.id} {row.symbol}: '
            f'entry={entry_price:.4f} exit={exit_price:.4f} '
            f'profit={profit:.2f} profit_pct={profit_pct:.4f}'
        )

    print(f'[fill_resolver] hypothetical exits resolved: {len(outcomes)}')
    return outcomes


def _safe_lookup(series: pd.Series, ticker: str, row_id: int, column_name: str) -> float:
    """Read one ticker's value from a day's series. Raises explicitly if
    the ticker is missing (e.g. delisted before today's bar) rather than
    returning NaN — that would silently mark fills as resolved with bogus prices.
    """
    if ticker not in series.index:
        raise KeyError(
            f'Ticker {ticker!r} not found in daily_{column_name} parquet for '
            f'tradelist row id={row_id}. Possibly delisted? Cannot resolve fill.'
        )
    val = series[ticker]
    if pd.isna(val):
        raise ValueError(
            f'NaN value for ticker {ticker!r} in daily_{column_name} for '
            f'tradelist row id={row_id}. Cannot resolve fill.'
        )
    return float(val)



if __name__ == '__main__':
    from app.database import SessionLocal
    from app.services.position_manager.fill_resolver import resolve_fills
    from datetime import date
    db = SessionLocal()
    try:
        # PullBack_X3_Sp500 strategy_id = 27 per your test
        # data_root and universe need real values:
        result = resolve_fills(
            db, strategy_id=27,
            run_date=date(2026, 6, 11),  # whatever your latest exec_data folder is
            data_root=r'C:\Tharun\Projects\backtest_data\exec_data\20260611',
            universe='sp500',
            rebalance='daily',
        )
        print(f'Got {len(result)} outcomes')
        for o in result:
            print(o)
    finally:
        db.close()

if __name__ == '__main__':
    from app.database import SessionLocal
    from app.services.position_manager.fill_resolver import resolve_fills
    from datetime import date
    db = SessionLocal()
    try:
        result = resolve_fills(
            db, strategy_id=27,
            run_date=date(2026, 6, 8),
            data_root=r'C:\Tharun\Projects\backtest_data\exec_data\20260611',
            universe='sp500',
            rebalance='daily',
        )
        print(f'Got {len(result)} outcomes')
        for o in result:
            print(o)
    finally:
        db.close()