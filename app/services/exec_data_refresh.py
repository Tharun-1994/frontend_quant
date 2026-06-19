"""
exec_data_refresh.py

C1 Patch 4: production indicator refresh.

Runs nightly AFTER universe_pipeline.py has refreshed Folder A. Iterates
every execution-enabled strategy + regime, calls
GeneratePricesIndicators.generate in production mode. Indicator parquets
are written to a universe-shared, date-stamped folder:

  <DATA_ROOT>/exec_data/{YYYYMMDD}/{universe}/*.parquet

Engine reads from this path via Patch 14: middleware sends the full
{YYYYMMDD}-stamped path as `data_root` in ExecutionStepRequestDto,
BacktestContext.executionDataRoot is set from it, and
BacktestContext.inputPath() resolves {data_root}/{universe}/ instead of
the legacy backtest_data/{strategy}/input/{universe}.

Failure mode: fail fast on any exception. The orchestrator (C5) catches
the ExecDataRefreshError, writes a FAILED row to eod_run_log with the
error message, and the frontend (Phase F) shows a retry button against
that row. Retry triggers /eod/retry-step which re-invokes this service.
Partial writes are never acceptable — partial signals mean wrong trades.
"""

from __future__ import annotations
import datetime as dt
import traceback
from collections import defaultdict
from typing import Optional

import pandas_market_calendars as mcal
from sqlalchemy.orm import Session

from app.models.eod_run_log import EodRunLog

from app.models.strategy_bucket import StrategyBucket
from app.loader.GeneratePricesIndicators import GeneratePricesIndicators
from app.services.position_manager.payload_builder import EXECUTION_START_DATE
# C1-fix-E (2026-06-12): GeneratePricesIndicators.generate() expects the
# Pydantic MarketRegimeBase shape (parsed trees, vol_filter object, etc.),
# NOT the SQLAlchemy ORM (raw JSON strings in *_json columns). Reuse the
# same converter the /marketregime GET route uses.
from app.routes.backtest import db_to_pydantic


class ExecDataRefreshError(RuntimeError):
    """Raised on any failure during exec_data refresh. Fail-fast contract."""


# ── Shared Norgate-post-hour resolver ────────────────────────────────────────
#
# Promoted from ExecDataRefreshService._resolve_run_date so the orchestrator
# can call the SAME logic. Without sharing, the orchestrator was passing
# today's date to the PM step while exec_data_refresh was rolling back to
# the previous trading day — engine then looked for an exec_data folder that
# didn't exist and failed with HTTP 500.

def resolve_data_date(run_date: Optional[dt.date] = None,
                      norgate_post_hour: int = 22) -> dt.date:
    """Return the data date the nightly chain should use.

    Norgate posts EOD at ~22:30. If the run fires before that hour for a
    date >= today, rolls back to the prior NYSE trading day so all downstream
    steps land on the date Norgate actually has data for.
    """
    today = dt.date.today()
    if run_date is None:
        run_date = today
    if run_date >= today and dt.datetime.now().hour < norgate_post_hour:
        resolved = _previous_trading_day(today)
        print(f'[resolve_data_date] before Norgate post hour '
              f'(< {norgate_post_hour}:00) -> {run_date} → {resolved}')
        return resolved
    return run_date


def _previous_trading_day(ref: dt.date) -> dt.date:
    """NYSE-calendar-aware previous trading day."""
    nyse = mcal.get_calendar('NYSE')
    valid = nyse.valid_days(ref - dt.timedelta(days=10), ref).tz_localize(None)
    prior = [d.date() for d in valid if d.date() < ref]
    return prior[-1] if prior else ref - dt.timedelta(days=1)


class ExecDataRefreshService:
    """Orchestrates the nightly indicator parquet refresh for execution_enabled
    strategies. Reuses GeneratePricesIndicators.generate (the same indicator
    code path used on strategy save) with production=True to redirect output
    to the exec_data folder. No new indicator math — purely a path redirect
    plus a fan-out loop over execution-enabled strategies.
    """

    # Matches universe_pipeline._resolve_end_date. Norgate posts EOD at ~22:30
    # ET; if this fires before that, the data won't include today, so we roll
    # run_date back to the previous trading day to keep the folder name
    # consistent with the data vintage actually inside it.
    NORGATE_POST_HOUR = 22

    def __init__(self, db: Session):
        self.db = db

    def run(self, run_date: Optional[dt.date] = None,
            universe_filter: Optional[set] = None,
            start_date: Optional[dt.date] = None) -> dict:
        """Refresh exec_data parquets for all execution-enabled strategies.

        Args:
            run_date: data date for the exec_data folder. Defaults to today.
                If today and current hour < Norgate post hour, rolls back to
                previous trading day (same guard as universe_pipeline).
            universe_filter: optional set of universe slugs (case-insensitive
                comparison). When None, processes all universes touched by
                execution-enabled strategies. When set, only those universes
                are processed. Useful for targeted testing — pass
                {'sp500'} to refresh just SP500.

        Returns:
            dict[universe_slug -> 'SUCCESS'] for every universe processed.
            Empty dict if no execution-enabled strategies exist or all are
            filtered out. Universe keys are lower-cased.

        Raises:
            ExecDataRefreshError on any failure. Fail-fast contract — partial
            writes mean partial signals which mean wrong trades. Orchestrator
            (C5) catches and writes FAILED row to eod_run_log; frontend shows
            retry button.
        """
        run_date = self._resolve_run_date(run_date)
        print(f'[exec_data_refresh] starting for run_date={run_date}')

        strategies = (self.db.query(StrategyBucket)
                      .filter(StrategyBucket.execution_enabled == True)
                      .all())

        if not strategies:
            print('[exec_data_refresh] no execution_enabled strategies found, no-op')
            return {}

        # Group (strategy, regime) pairs by universe (case-insensitive key).
        # Same universe can host multiple strategies; we iterate them all and
        # the last write wins per parquet file. Indicator parquets are
        # deterministic per-universe (RSI(14) on AAPL is the same regardless
        # of which strategy requested it), so overwrites are content-identical.
        pairs_by_universe = defaultdict(list)
        filter_lc = {u.lower() for u in universe_filter} if universe_filter else None
        for strategy in strategies:
            # ORM relationship is `regimes` (not `market_regimes`) — see
            # StrategyBucket.regimes back_populates MarketRegime.strategy.
            for regime in strategy.regimes:
                univ = (regime.universe or '').lower()
                if not univ:
                    continue
                if filter_lc is not None and univ not in filter_lc:
                    continue
                pairs_by_universe[univ].append((strategy, regime))

        if not pairs_by_universe:
            print(f'[exec_data_refresh] no (strategy, regime) pairs after '
                  f'filtering (filter={universe_filter}), no-op')
            return {}

        # Process each universe sequentially. Within a universe, process its
        # (strategy, regime) pairs in DB insertion order. Sequential is fine
        # for Phase 1 (Sp500 only, few strategies). Parallelism is an
        # optimisation for later — would need to ensure write-collisions on
        # the same parquet file don't corrupt the file (pyarrow's to_parquet
        # is not atomic at the OS level).
        results = {}
        for universe, pairs in pairs_by_universe.items():
            print(f'[exec_data_refresh] universe={universe}, '
                  f'{len(pairs)} (strategy, regime) pair(s) to compute')
            try:
                for strategy, regime in pairs:
                    regime_label = (regime.market_trend_type
                                    or regime.regime_type
                                    or f'regime_id={regime.id}')
                    print(f'[exec_data_refresh]   strategy={strategy.name}, '
                          f'regime={regime_label}')
                    # ORM → Pydantic conversion: generate() reads parsed-tree
                    # attributes (market_trend_rules_tree, entry_rules_tree, ...)
                    # not the ORM's *_json string columns. Same converter used
                    # by /marketregime GET — reusing it keeps parsing semantics
                    # identical to the strategy-save backtest path.
                    regime_schema = db_to_pydantic(regime)
                    effective_start = start_date if start_date is not None else EXECUTION_START_DATE
                    is_test = start_date is not None  # dynamic start_date = test mode
                    GeneratePricesIndicators.generate(
                        marketRegime=regime_schema,
                        strategy=strategy,
                        production=True,
                        run_date=run_date,
                        start_date=effective_start,
                        lookback_buffer_days=650,
                        test_mode=is_test,
                    )
                results[universe] = 'SUCCESS'
                print(f'[exec_data_refresh] universe={universe} done')
            except Exception as e:
                # Fail fast — re-raise wrapped. Orchestrator catches and logs.
                # The strategy/regime that broke is in the traceback; no need
                # to dump them again here.
                raise ExecDataRefreshError(
                    f'Failed to refresh exec_data for universe={universe}: '
                    f'{type(e).__name__}: {e}'
                ) from e

        print(f'[exec_data_refresh] complete: {results}')
        return results

    def _resolve_run_date(self, run_date: Optional[dt.date]) -> dt.date:
        """Delegates to the module-level resolve_data_date so the
        orchestrator and this service always agree on the rollback decision.
        """
        return resolve_data_date(run_date, self.NORGATE_POST_HOUR)


def run_exec_data_refresh(db: Session, run_date: Optional[dt.date] = None,
                          universe_filter: Optional[set] = None,
                          write_eod_log: bool = True,
                          start_date: Optional[dt.date] = None) -> dict:
    """Refresh exec_data parquets for execution_enabled strategies.

    When write_eod_log=True (default), this function writes a
    RUNNING → SUCCESS/FAILED row to eod_run_log around the service call.
    Callers that already write their own row (legacy orchestrator path)
    should pass write_eod_log=False to avoid double-logging.

    Args:
        db: SQLAlchemy session.
        run_date: data date (defaults to today; rolled back if before
            Norgate post hour).
        universe_filter: only refresh these universes (default: all).
        write_eod_log: gate the audit-row writes (default True).
    """
    service = ExecDataRefreshService(db)

    if not write_eod_log:
        return service.run(run_date=run_date, universe_filter=universe_filter,
                           start_date=start_date)

    # Resolve the run_date up front so the log row carries the same date
    # the service will actually process.
    resolved = service._resolve_run_date(run_date)
    log_row = EodRunLog(
        run_date=resolved,
        step='exec_data_refresh',
        strategy_id=None,
        status='RUNNING',
    )
    db.add(log_row)
    db.commit()

    try:
        result = service.run(run_date=run_date, universe_filter=universe_filter,
                             start_date=start_date)
        log_row.status        = 'SUCCESS'
        log_row.rows_affected = len(result) if result else 0
        log_row.finished_at   = dt.datetime.utcnow()
        db.commit()
        return result
    except Exception as e:
        log_row.status      = 'FAILED'
        log_row.error_msg   = f'{type(e).__name__}: {e}\n{traceback.format_exc()}'
        log_row.finished_at = dt.datetime.utcnow()
        db.commit()
        raise





if __name__ == '__main__':
    from app.database import SessionLocal
    from app.services.exec_data_refresh import run_exec_data_refresh
    from datetime import date

    db = SessionLocal()
    try:
        result = run_exec_data_refresh(db, run_date=date(2026, 6, 11), universe_filter={'sp500'})
        print(result)
    finally:
        db.close()