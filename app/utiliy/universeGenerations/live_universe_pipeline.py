"""
live_universe_pipeline.py — nightly Norgate refresh for EXECUTION universes only.

Mirrors universe_pipeline.py exactly except:
  - reads LIVE_REGISTRY instead of REGISTRY
  - writes to backtest_data/live_universes/ instead of backtest_data/universes/
  - pulls only ~5 years of data (2021 → today) per spec.start_date

The static backtest universes (backtest_data/universes/) are NEVER touched
by this script. That preserves the frozen tradelists used for manager demos.
"""

from __future__ import annotations
import sys
from pathlib import Path

# Make app.* imports resolve when run as `python -m`
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from app.utiliy.universeGenerations.universe_pipeline import UniversePipeline
from app.utiliy.universeGenerations.storage import CsvDataStore
from app.utiliy.universeGenerations.live_universe_registry import LIVE_REGISTRY
from app.utiliy.synthetic_ticker_processor import (
    load_synthetics_from_db, SyntheticTickerProcessor,
)
from app.constants.PricePath import PricePath

# Patch 106: scheduler-parity rebuild. Freshness audit + step rows.
import os
import getpass
import datetime as dt
import pandas as pd
import pandas_market_calendars as mcal


def _write_step_row(run_date, status, error_msg=None, rows=None):
    """Patch 106: write a 'live_universe_step' row to eod_run_log so Step 1
    outcomes (incl. a skipped/stale universe) are visible in the EOD Run
    History UI, not only in the text log. Isolated: a DB hiccup here must
    never take down the price build itself.
    """
    try:
        from app.database import SessionLocal
        from app.models.eod_run_log import EodRunLog
        _db = SessionLocal()
        try:
            row = EodRunLog(
                run_date=run_date,
                step='live_universe_step',
                strategy_id=None,
                status=status,
                rows_affected=rows,
                error_msg=error_msg,
                finished_at=dt.datetime.now(),
            )
            _db.add(row)
            _db.commit()
        finally:
            _db.close()
    except Exception as e:
        print(f'[live_pipeline] WARN could not write eod_run_log step row: '
              f'{type(e).__name__}: {e}')


def _last_csv_date(csv_path):
    """Last Date-index value of a daily CSV, or None if missing/unreadable."""
    try:
        _df = pd.read_csv(csv_path, usecols=['Date'])
        return pd.to_datetime(_df['Date']).max().date()
    except Exception:
        return None


def _audit_liquid500_freshness(resolved):
    """Patch 106: pre-flight audit — print the latest available date in
    BOTH liquid500 files (source-of-truth membership + live closes) against
    the resolved run date, and list exactly which NYSE trading dates are
    missing from the membership. extend_liquid500_membership() then
    catches up ALL of those dates in one call (it walks every trading day
    in (last_stored, end_date], recomputing month-starts, ffilling rest).
    """
    mem_last = _last_csv_date(LIQUID_500_CSV)
    closes_last = _last_csv_date(
        PricePath.close(PricePath.liquid500_live_base_path))
    print(f'[live_pipeline][audit] liquid500 membership file: '
          f'{LIQUID_500_CSV} -> last date = {mem_last}')
    print(f'[live_pipeline][audit] liquid500 live closes:     '
          f'{PricePath.close(PricePath.liquid500_live_base_path)} '
          f'-> last date = {closes_last}')
    print(f'[live_pipeline][audit] resolved run date:         {resolved}')
    if mem_last is not None and mem_last < resolved:
        _nyse = mcal.get_calendar('NYSE')
        _missing = [d.date() for d in _nyse.valid_days(
            mem_last + dt.timedelta(days=1), resolved).tz_localize(None)]
        print(f'[live_pipeline][audit] membership MISSING '
              f'{len(_missing)} trading day(s): {_missing} '
              f'-> pre-step will catch up all of them now')
    else:
        print(f'[live_pipeline][audit] membership is current '
              f'(>= {resolved}) -> pre-step will NOOP')


if __name__ == '__main__':
    LIVE_FOLDER = PricePath.live_universes_root   # backtest_data/live_universes

    # Patch 106: context fingerprint — the scheduler and Trigger Nightly run
    # THIS SAME script; when one works and the other doesn't, these three
    # lines name the difference (interpreter, cwd, user) immediately.
    print(f'[live_pipeline] context: python={sys.executable}')
    print(f'[live_pipeline] context: cwd={os.getcwd()} '
          f'user={getpass.getuser()} pid={os.getpid()} '
          f'started={dt.datetime.now():%Y-%m-%d %H:%M:%S}')

    # Same synthetics loading as the static pipeline — LIVE_REGISTRY doesn't
    # currently declare any synthetics, but build the processor so future
    # additions don't blow up.
    processor = None
    try:
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            synthetics = load_synthetics_from_db(db)
        finally:
            db.close()
        if synthetics:
            processor = SyntheticTickerProcessor(synthetics)
            print(f'[live_pipeline] loaded {len(synthetics)} synthetic(s) from DB')
        else:
            print('[live_pipeline] synthetic_tickers table empty -> no processor needed')
    except Exception as e:
        print(f'[live_pipeline] synthetics not loaded ({type(e).__name__}: {e})')

    pipeline = UniversePipeline(
        store=CsvDataStore(LIVE_FOLDER),
        registry=LIVE_REGISTRY,  # ← uses live registry, not static REGISTRY
        num_of_cpus=10,
        synthetic_processor=processor,
    )

    # Patch 96: SCOPING — build only the universes that execution-enabled
    # strategies actually trade (+ spy, a shared market-trend dependency many
    # regimes reference via SPY rules). Disable a strategy and its universe
    # drops out of the whole nightly; if no strategy is enabled on liquid500,
    # its expensive month-start recompute + deps refresh are skipped entirely.
    _active_univ = {'spy'}
    try:
        from app.database import SessionLocal as _SessionLocal
        from app.models.strategy_bucket import StrategyBucket as _StrategyBucket
        from app.models.market_regime import MarketRegime as _MarketRegime

        _sdb = _SessionLocal()
        try:
            for _r in (
                    _sdb.query(_MarketRegime)
                            .join(_StrategyBucket, _MarketRegime.strategy_id == _StrategyBucket.id)
                            .filter(_StrategyBucket.execution_enabled == True)  # noqa: E712
                            .all()
            ):
                if _r.universe:
                    _active_univ.add(_r.universe.lower())
        finally:
            _sdb.close()
    except Exception as _e:
        print(f'[live_pipeline] could not resolve active universes '
              f'({type(_e).__name__}: {_e}) — building all LIVE_REGISTRY universes')
        _active_univ = {s.slug.lower() for s in LIVE_REGISTRY}
    active_slugs = {s.slug for s in LIVE_REGISTRY if s.slug.lower() in _active_univ}
    liquid500_active = 'liquid500' in {s.lower() for s in active_slugs}
    print(f'[live_pipeline] active universes: {sorted(active_slugs)} '
          f'(liquid500_active={liquid500_active})')

    # Patch 92: liquid500 membership pre-step. Extend the source-of-truth
    # universes/liquid500/liquid500.csv to today's NYSE trading day BEFORE
    # pipeline.run() reads it for the live rebuild. Without this, the
    # live_universes/liquid500/ folder would lag by one day every nightly.
    # ffill on non-month-start days; full legacy recompute on month-starts.
    # Loud-fails if Norgate is unreachable on a needed month-start — the
    # nightly chain aborts rather than emit execution data against a
    # stale universe.
    try:
        from app.utiliy.universeGenerations.liquid500_membership import (
            extend_liquid500_membership,
        )
        from app.utiliy.universeGenerations.universe_registry import (
            LIQUID_500_CSV,
        )
        from app.services.exec_data_refresh import resolve_data_date
        from app.utiliy.universeGenerations.universe_today_refresh import (
            refresh_all_today,
        )

        if not liquid500_active:
            # Patch 96: no enabled strategy on liquid500 — skip its (expensive,
            # month-start) recompute + deps refresh entirely.
            print('[live_pipeline] liquid500 not on any enabled strategy — '
                  'skipping its membership pre-step')
        else:
            resolved = resolve_data_date(None)  # None -> today, post-hour rollback

            # Patch 95: extend sp1500 + russell3000 (the OTC-restriction deps)
            # BEFORE the liquid500 month-start recompute reads them. Same
            # REGISTRY loop the button uses, scoped to the two deps, ordered
            # sp1500 -> russell3000.
            dep_result = refresh_all_today(
                only_universes={'sp1500', 'russell3000'}, end_date=resolved,
            )
            print(f'[live_pipeline] sp1500/russell3000 deps extended: {dep_result}')

            # Patch 106: audit BOTH files before touching anything, so the
            # log states exactly which dates are missing and from where.
            _audit_liquid500_freshness(resolved)
            info = extend_liquid500_membership(LIQUID_500_CSV, end_date=resolved)
            print(f'[live_pipeline] liquid500 membership pre-step: {info}')
            # Patch 106: explicit post-check — membership must now reach the
            # resolved date; anything else is a loud failure, not a shrug.
            _mem_after = _last_csv_date(LIQUID_500_CSV)
            if _mem_after is None or _mem_after < resolved:
                raise RuntimeError(
                    f'liquid500 membership still ends {_mem_after} after '
                    f'pre-step; needed {resolved}'
                )
            print(f'[live_pipeline][audit] membership post-step: last date = '
                  f'{_mem_after} (OK, matches resolved {resolved})')
    except Exception as e:
        # Patch 96: ISOLATION — a liquid500 pre-step failure drops liquid500 from
        # THIS nightly (skipped in the build below + no CRSI) but does NOT abort;
        # sp500 and every other active universe still build and run.
        # Patch 106: the skip is no longer invisible — a FAILED
        # 'live_universe_step' row lands in eod_run_log so the EOD Run
        # History page shows WHY liquid500 froze, next to the PM failures
        # it will cause.
        import traceback as _tb
        _write_step_row(
            run_date=resolved if 'resolved' in dir() else dt.date.today(),
            status='FAILED',
            error_msg=(f'liquid500 membership pre-step failed — liquid500 '
                       f'SKIPPED this night (live folder frozen): '
                       f'{type(e).__name__}: {e}\n{_tb.format_exc()}'),
        )
        print(f'[live_pipeline] liquid500 pre-step FAILED — SKIPPING liquid500, '
              f'other universes continue: {type(e).__name__}: {e}')
        liquid500_active = False
        active_slugs.discard('liquid500')

    pipeline.run(only=active_slugs)  # Patch 96: build only active universes

    # Patch 106: post-build verification — confirm each active universe's
    # live closes actually reached the resolved date, and write one
    # 'live_universe_step' row per universe to eod_run_log (SUCCESS or
    # FAILED-with-reason). A stale build is now a visible red row in the
    # Run History page instead of three downstream KeyErrors.
    from app.services.exec_data_refresh import resolve_data_date as _rdd  # Patch 106: local import — the branch import above may not have run
    _verify_resolved = _rdd(None)
    for _slug in sorted(active_slugs):
        _base = str(Path(LIVE_FOLDER) / _slug)
        _last = _last_csv_date(PricePath.close(_base))
        if _last is None and _slug.lower() == 'spy':
            _last = _last_csv_date(PricePath.spy_daily_prices(_base))
        if _last is None:
            print(f'[live_pipeline][verify] {_slug}: could not read live '
                  f'closes to verify — treating as FAILED')
            _write_step_row(_verify_resolved, 'FAILED',
                            error_msg=f'{_slug}: live closes unreadable '
                                      f'after build ({_base})')
        elif _last < _verify_resolved:
            print(f'[live_pipeline][verify] {_slug}: STALE — live closes end '
                  f'{_last}, needed {_verify_resolved}. Local Norgate DB has '
                  f'not ingested the {_verify_resolved} close (check NDU '
                  f'schedule) OR the build failed above.')
            _write_step_row(_verify_resolved, 'FAILED',
                            error_msg=f'{_slug}: live closes end {_last}, '
                                      f'needed {_verify_resolved} — stale '
                                      f'source (NDU?) or skipped build')
        else:
            print(f'[live_pipeline][verify] {_slug}: OK — live closes end '
                  f'{_last} (>= resolved {_verify_resolved})')
            _write_step_row(_verify_resolved, 'SUCCESS',
                            error_msg=None, rows=None)

    # Patch 106: universes that were SKIPPED by isolation never reach the
    # loop above (they were removed from active_slugs) — their FAILED row
    # was already written at the skip site.

    # Patch 94: generate CRSI IN THE LIVE FOLDER
    # built. Supersedes Patch 93's "read static CRSI in live" — the live folder
    # is now self-contained (prices + CRSI), so execution no longer depends on
    # the manual button having refreshed the static CRSI. extend_universe_crsi
    # is the SAME universe-agnostic service the button uses (identical math), so
    # live and backtest CRSI stay in lockstep; it reads
    # live_universes/liquid500/daily_closes.csv (present after pipeline.run())
    # and appends today's row incrementally. Loud-fail aborts the chain rather
    # than emit execution signals against missing CRSI.
    if liquid500_active:
        try:
            from app.utiliy.universeGenerations.universe_crsi import (
                sweep_universe_crsi_variants,
            )

            # Patch 100: sweep EVERY existing CRSI variant in the live
            # folder (was: single default-variant extend). A variant the
            # live folder doesn't have yet is created on demand by the
            # execution reader (GeneratePricesIndicators) from the live
            # daily_closes — sync, full history — and swept nightly from
            # then on.
            crsi_info = sweep_universe_crsi_variants(
                universe_slug='liquid500',
                base_path=PricePath.liquid500_live_base_path,
                end_date=resolved,
            )
            print(f'[live_pipeline] liquid500 live CRSI sweep: {crsi_info}')
        except Exception as e:
            # Patch 96: ISOLATION — a CRSI failure is loud but not fatal to the
            # rest of the nightly; liquid500 execution would read stale CRSI.
            print(f'[live_pipeline] liquid500 live CRSI FAILED — liquid500 may '
                  f'read stale CRSI: {type(e).__name__}: {e}')