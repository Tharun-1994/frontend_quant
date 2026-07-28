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
from app.models.combined_system import CombinedMember, CombinedConfig   # Patch 153/154
from app.schemas.Combined import normalize_combined_config   # Patch 154
from app.schemas.strategy import VolFilter                     # Patch 165
import json                                                    # Patch 154
import pandas as pd                                            # Patch 158
from pathlib import Path                                       # Patch 158
from app.constants.PricePath import PricePath                  # Patch 158
import hashlib                                                  # Patch 174
# Patch 176: bump when generation gains NEW OUTPUT KINDS (vol trio,
# hv_limit, ...). A manifest written by an older generator must never
# satisfy a newer one -- config fingerprints alone can't see code.
_P174_GEN_VERSION = 2
import glob as _glob                                            # Patch 174
import os                                                       # Patch 174
# Patch 159: PriceProvider is imported LAZILY inside
# _generate_gate_ticker_ohlc — price_provider imports norgatedata at module
# level, and this service is imported by the API server at startup. A
# top-level import here would make uvicorn's boot depend on Norgate tooling
# loading inside the server process. Same isolation pattern eod.py uses
# when importing this very service.

# Patch 158: condition-ticker prices for the combined's market gate are
# generated STRAIGHT FROM NORGATE into exec_data/{ymd}/{ticker}/ — never
# from static folders or any other system's stores (design directive
# 2026-07-09: "if the mechanism needs any ticker configured in the rules,
# generate it from 2021 to the last Norgate date"). Start matches the
# live-registry warm-up convention: ~2yr of SMA-200 warm-up ahead of the
# 2023-01-01 execution floor.
GATE_TICKER_START = dt.date(2021, 1, 1)
from app.loader.GeneratePricesIndicators import GeneratePricesIndicators
from app.models.universe_ticker_exclusion import UniverseTickerExclusion
from app.services.position_manager.payload_builder import EXECUTION_START_DATE
# C1-fix-E (2026-06-12): GeneratePricesIndicators.generate() expects the
# Pydantic MarketRegimeBase shape (parsed trees, vol_filter object, etc.),
# NOT the SQLAlchemy ORM (raw JSON strings in *_json columns). Reuse the
# same converter the /marketregime GET route uses.
from app.routes.backtest import db_to_pydantic
from datetime import datetime   # Patch 187


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
        # Patch 104: every universe processed below prints a
        # [exec_data_refresh][freshness] line with the closes date range of
        # its price source. If any 'closes .. <last>' is BEFORE run_date,
        # that universe's source (live_universes / Norgate local DB) is
        # stale and tonight's fill resolution WILL fail for it.
        print(f'[exec_data_refresh] freshness expectation: every universe '
              f'source must contain closes up to {run_date}')

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
        gate_tickers: set = set()   # Patch 158
        filter_lc = {u.lower() for u in universe_filter} if universe_filter else None
        # Patch 169: exclusions are universe-scoped (NULL row.universe =
        # applies everywhere). Resolved per universe inside the loop below;
        # a global set here would leak the short book's exclusions
        # (NFLX/IBM/...) into the Liquid500 long books.
        _excl_rows = (self.db.query(UniverseTickerExclusion)
                      .filter(UniverseTickerExclusion.active == True)
                      .all())

        def _excl_for(_uni: str) -> set:
            _u = (_uni or '').lower()
            return {r.ticker for r in _excl_rows
                    if getattr(r, 'universe', None) is None
                    or (getattr(r, 'universe', '') or '').lower() == _u}
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

            # Patch 153: combined members are scouts — execution_disabled by
            # design (Patch 147: only the combined emits orders), so the
            # enabled-strategies query above never sees them. But generate()
            # derives the indicator set FROM RULE TREES, and a combined's own
            # trees are empty (Patch 149) — its MEMBERS' trees are what the
            # exec folder must satisfy, because the evening leg steps each
            # member against exec_data/{ymd}/{universe} (combined/execute.py).
            # Without this fan-in the universe folder holds price parquets but
            # none of the indicator parquets (hv_100, rsi_2, sma_200, ...) and
            # the engine 500s on the first member step.
            if (strategy.market_regime_type or '').strip().lower() == 'combined':
                member_rows = (self.db.query(CombinedMember)
                               .filter(CombinedMember.combined_strategy_id
                                       == strategy.id,
                                       CombinedMember.is_active == True)  # noqa: E712
                               .order_by(CombinedMember.priority)
                               .all())
                if not member_rows:
                    raise ExecDataRefreshError(
                        f'Combined strategy {strategy.id} ({strategy.name}) '
                        f'is execution_enabled but has no active members — '
                        f'no rule trees to generate indicators from. Add '
                        f'subsystems on the Combined System tab or disable '
                        f'the strategy.')
                for mr in member_rows:
                    member = self.db.get(StrategyBucket,
                                         mr.member_strategy_id)
                    if member is None:
                        raise ExecDataRefreshError(
                            f'Combined {strategy.id} references missing '
                            f'member strategy id={mr.member_strategy_id}.')
                    for mregime in member.regimes:
                        m_univ = (mregime.universe or '').lower()
                        if not m_univ:
                            continue
                        if filter_lc is not None and m_univ not in filter_lc:
                            continue
                        pairs_by_universe[m_univ].append((member, mregime))
                        print(f'[exec_data_refresh] combined {strategy.id} '
                              f'({strategy.name}) fans in member '
                              f'{member.id} ({member.name}) for '
                              f'universe={m_univ}')
                # Patch 154: the combined's market gate evaluates on the
                # condition ticker's OWN OHLC — market_gate wants dedicated
                # DAILY_{field}_{ticker} files, which combined/execute.py
                # materializes from this ticker's exec universe folder. So
                # generate that folder alongside: reuse the combined's
                # (empty-tree) regime schema with universe overridden —
                # generate()'s spy-universe branch writes
                # DAILY_{closes,highs,lows}.parquet mode-correctly
                # (live_universes/{ticker} on nightly, static path in
                # replay/test_mode). Deliberately EXEMPT from
                # universe_filter: the replay route scopes the filter to the
                # strategy's regime universes, and the gate ticker IS this
                # strategy's data dependency.
                gate_ticker = 'spy'
                cfg_row = self.db.get(CombinedConfig, strategy.id)
                if cfg_row is not None and cfg_row.config_json:
                    try:
                        _cfg = normalize_combined_config(
                            json.loads(cfg_row.config_json))
                        gate_ticker = (_cfg.market_conditions.ticker
                                       or 'spy').lower()
                    except Exception as e:
                        raise ExecDataRefreshError(
                            f'Combined {strategy.id}: could not read the '
                            f'gate ticker from combined_config: '
                            f'{type(e).__name__}: {e}')
                if not strategy.regimes:
                    raise ExecDataRefreshError(
                        f'Combined strategy {strategy.id} ({strategy.name}) '
                        f'is execution_enabled but has no marketregime row '
                        f'— press Save on the Combined System tab (Patch '
                        f'149 creates it).')
                # Patch 158 (supersedes the 154 synthetic-pair route):
                # the gate ticker is pulled STRAIGHT FROM NORGATE below —
                # no detour through universe folders. Collect it here;
                # the pull runs once per ticker, before indicator
                # generation, and is deliberately EXEMPT from
                # universe_filter (it is the combined's own dependency).
                gate_tickers.add(gate_ticker)

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
        # Patch 158: condition-ticker Norgate pulls first — seconds per
        # ticker, fail-fast before any heavy indicator generation.
        for _gt in sorted(gate_tickers):
            self._generate_gate_ticker_ohlc(_gt, run_date)
            results[f'gate_ticker:{_gt}'] = 'SUCCESS'
        for universe, pairs in pairs_by_universe.items():
            print(f'[exec_data_refresh] universe={universe}, '
                  f'{len(pairs)} (strategy, regime) pair(s) to compute')
            # Patch 169: resolve + cache THIS universe's exclusion set
            excluded_tickers = _excl_for(universe)
            GeneratePricesIndicators._excluded_tickers_cache = excluded_tickers
            print(f'[exec_data_refresh] universe={universe}: '
                  f'{len(excluded_tickers)} ticker(s) excluded')

            # Patch 174 -- refresh cost control, two layers:
            #   within-run : load the universe ONCE, share across pairs
            #                (indicator dedup + write-once ride on it);
            #   cross-run  : skip the whole universe when a manifest shows
            #                the SAME run_date, the SAME pair configs, and
            #                UNCHANGED source csv mtimes. Never date-only --
            #                Norgate retro-rescales, so a source re-pull
            #                (mtime change) always forces regeneration.
            # Universe source folders follow PricePath.{univ}base_path
            # (sp500base_path, russell3000base_path, ...); spy is special.
            _p174_cache = {'data': None, 'written': set()}
            _p174_fps = []

            def _p174_source_sig(_u):
                _p = getattr(PricePath, f'{_u.lower()}base_path', None)
                if _p is None and _u.lower() == 'spy':
                    _p = getattr(PricePath, 'spy_path', None)
                if _p is None:
                    return None
                try:
                    return {os.path.basename(f): int(os.path.getmtime(f))
                            for f in sorted(_glob.glob(os.path.join(str(_p), '*.csv')))}
                except Exception:
                    return None

            def _p174_fp(_s, _r):
                _d = {'sid': _s.id, 'rid': _r.id,
                      'cols': {c.name: str(getattr(_r, c.name, None))
                               for c in _r.__table__.columns}}
                return hashlib.md5(json.dumps(
                    _d, sort_keys=True, default=str).encode()).hexdigest()

            _p174_out = PricePath.getExecDataInputPath(universe=universe,
                                                       run_date=run_date)
            _p174_manifest = os.path.join(str(_p174_out),
                                          '_generated_manifest.json')
            _p174_sig = _p174_source_sig(universe)
            _p174_skip = False
            # ── Patch 187: source-staleness sentinel ────────────────────
            # The manifest skip is CORRECT when source csvs are unchanged --
            # but 'unchanged' can also mean the NDU export for this universe
            # silently died (sp500 froze at 2026-07-07 for a week while
            # russell3000 kept updating; strategy 27 executed on stale data
            # the whole time). Read the LAST date of daily_closes.csv and
            # scream when it trails run_date. Warn-only by design: one
            # stale universe must never abort the others.
            try:
                _p187_src = os.path.join(
                    getattr(PricePath, f'{universe}base_path'),
                    'daily_closes.csv')
                with open(_p187_src, 'rb') as _fh:
                    _fh.seek(0, os.SEEK_END)
                    _sz = _fh.tell()
                    _fh.seek(max(0, _sz - 4096))
                    _last = _fh.read().rstrip(b'\r\n').split(b'\n')[-1]
                _p187_last = _last.split(b',')[0].decode('ascii', 'ignore').strip()
                _p187_last_d = datetime.strptime(_p187_last[:10], '%Y-%m-%d').date()
                if _p187_last_d < run_date:
                    _gap = (run_date - _p187_last_d).days
                    print(f'[exec_data_refresh] *** WARNING universe={universe}: '
                          f'source daily_closes.csv ends {_p187_last_d} but '
                          f'run_date={run_date} ({_gap} day(s) behind). The '
                          f'NDU export for this universe looks DEAD/stale -- '
                          f'signals will be computed on old prices. Fix the '
                          f'Norgate export, then rerun (mtime change forces '
                          f'regeneration automatically). ***')
            except Exception as _p187e:
                print(f'[exec_data_refresh] staleness check skipped for '
                      f'{universe}: {_p187e}')

            _p174_pairs_now = {_p174_fp(_s, _r) for _s, _r in pairs}

            # Patch 176: what THIS universe's pairs REQUIRE on disk. A
            # manifest can only skip when every required artifact exists --
            # a folder written by an older/incomplete deploy is stale no
            # matter what its manifest claims.
            _p176_required = set()
            for _s6, _r6 in pairs:
                if getattr(_r6, 'vol_filter_json', None):
                    _p176_required.update(('avg_volume.parquet',
                                           'avg_turnover.parquet',
                                           'closes_spy.parquet'))
                if (str(getattr(_r6, 'order_type', '') or '')
                        .strip().upper() == 'LIMIT_HV'):
                    _p176_required.add('hv_limit.parquet')
            _p176_missing = sorted(
                f for f in _p176_required
                if not os.path.exists(os.path.join(str(_p174_out), f)))

            if os.path.exists(_p174_manifest):
                try:
                    with open(_p174_manifest) as _mf:
                        _m = json.load(_mf)
                    if (_m.get('gen_version') == _P174_GEN_VERSION   # Patch 176
                            and not _p176_missing                    # Patch 176
                            and _p174_sig is not None
                            and _m.get('source_sig') == _p174_sig
                            and _m.get('run_date') == str(run_date)
                            and _p174_pairs_now <= set(_m.get('pairs', []))):
                        _p174_skip = True
                        print(f'[exec_data_refresh] universe={universe} '
                              f'SKIPPED (manifest fresh: same run_date, '
                              f'same configs, source csvs unchanged, '
                              f'all required artifacts present)')
                    elif _m.get('gen_version') != _P174_GEN_VERSION:
                        print(f'[exec_data_refresh] universe={universe}: '
                              f'manifest generator version '
                              f'{_m.get("gen_version")} != '
                              f'{_P174_GEN_VERSION} -- regenerating')
                except Exception as _me:
                    print(f'[exec_data_refresh] manifest unreadable '
                          f'({_me}) -- regenerating {universe}')
            if _p176_missing and not _p174_skip:
                print(f'[exec_data_refresh] universe={universe}: required '
                      f'artifact(s) missing -> regenerating: '
                      f'{_p176_missing}')
            if _p174_sig is None:
                print(f'[exec_data_refresh] universe={universe}: no source '
                      f'signature available -- cross-run skip DISABLED for '
                      f'this universe (within-run sharing still active)')
            if _p174_skip:
                results[universe] = 'SUCCESS (skipped, manifest fresh)'
                continue
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
                    # Patch 165: db_to_pydantic never populates vol_filter
                    # (MarketRegimeBase defaults it to None — strategy.py:183
                    # has the field, the converter has zero mentions of it),
                    # so generate()'s vol block silently skipped for every
                    # exec regime and the engine's vol filter ran INERT
                    # (avgVolume/avgTurnover/spyCloses all null — the exact
                    # Patch-164 diagnostic evidence). Graft it from the ORM
                    # json here, exec-path only; loud on garbage.
                    if getattr(regime, 'vol_filter_json', None):
                        try:
                            regime_schema = regime_schema.copy(update={
                                'vol_filter': VolFilter(
                                    **json.loads(regime.vol_filter_json))})
                        except Exception as e:
                            raise ExecDataRefreshError(
                                f'strategy {strategy.id} regime '
                                f'{regime.id}: unparseable '
                                f'vol_filter_json: {type(e).__name__}: {e}')
                    effective_start = start_date if start_date is not None else EXECUTION_START_DATE
                    is_test = start_date is not None  # dynamic start_date = test mode
                    _p174_ret = GeneratePricesIndicators.generate(
                        marketRegime=regime_schema,
                        strategy=strategy,
                        production=True,
                        run_date=run_date,
                        start_date=effective_start,
                        lookback_buffer_days=650,
                        test_mode=is_test,
                        preloaded_raw=_p174_cache['data'],      # Patch 174
                        already_written=_p174_cache['written'],  # Patch 174
                    )
                    if _p174_ret is not None:
                        _p174_cache['data'] = _p174_ret
                    _p174_fps.append(_p174_fp(strategy, regime))
                results[universe] = 'SUCCESS'
                print(f'[exec_data_refresh] universe={universe} done')
                # Patch 174: record what this run produced
                try:
                    with open(_p174_manifest, 'w') as _mf:
                        json.dump({'gen_version': _P174_GEN_VERSION,   # Patch 176
                                   'run_date': str(run_date),
                                   'source_sig': _p174_sig,
                                   'pairs': _p174_fps,
                                   'keys': sorted(_p174_cache['written'])},
                                  _mf, indent=1)
                except Exception as _we:
                    print(f'[exec_data_refresh] manifest write failed '
                          f'(non-fatal): {_we}')
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

    def _generate_gate_ticker_ohlc(self, ticker: str,
                                   run_date: dt.date) -> None:
        """Patch 158: pull the combined's condition ticker straight from
        Norgate (GATE_TICKER_START -> run_date) and write
        exec_data/{ymd}/{ticker}/DAILY_{closes,highs,lows}.parquet
        (single column = ticker) — the exact shape
        combined/execute._ensure_gate_ohlc consumes as source (3).
        Same PriceProvider settings as every universe pull
        (TOTALRETURN adjust, NONE padding). Loud on any gap."""
        out_dir = (Path(PricePath.backtestPath) / 'exec_data'
                   / run_date.strftime('%Y%m%d') / ticker)
        out_dir.mkdir(parents=True, exist_ok=True)
        symbol = ticker.upper()
        print(f'[exec_data_refresh] gate ticker {symbol}: Norgate pull '
              f'{GATE_TICKER_START} -> {run_date}')
        # Patch 159: lazy import — see the note in the import block.
        from app.utiliy.universeGenerations.price_provider import (
            PriceProvider)
        provider = PriceProvider(num_of_cpus=1)  # defaults: TOTALRETURN/NONE
        per_field = provider.get_prices([symbol], GATE_TICKER_START,
                                        end_date=run_date,
                                        fields=['High', 'Low', 'Close'])
        for field, fname in (('Close', 'closes'), ('High', 'highs'),
                             ('Low', 'lows')):
            df = per_field.get(field)
            if df is None or df.empty or symbol not in df.columns:
                raise ExecDataRefreshError(
                    f'gate ticker {symbol}: Norgate returned no {field} '
                    f'data for {GATE_TICKER_START}..{run_date} — check '
                    f'the symbol and the local Norgate DB (NDU).')
            out = df[[symbol]].rename(columns={symbol: ticker})
            out.index = pd.to_datetime(out.index)
            last = out.index.max().date()
            if last < run_date:
                raise ExecDataRefreshError(
                    f'gate ticker {symbol}: Norgate {field} ends {last} '
                    f'< run_date {run_date} — NDU has not ingested the '
                    f'{run_date} close yet (see the [price_provider] '
                    f'STALE SOURCE WARNING above).')
            out.to_parquet(out_dir / f'DAILY_{fname}.parquet')
        print(f'[exec_data_refresh] gate ticker {symbol}: wrote '
              f'DAILY_closes/highs/lows.parquet -> {out_dir} '
              f'(last bar {run_date})')


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