"""
scheduler.py — APScheduler integration for nightly and morning jobs.

Two jobs:
  22:30 UK (Europe/London) → eod_orchestrator  (exec_data_refresh + PM per strategy)
  07:30 UK (Europe/London) → morning_basket    (overlay-apply + broker-write → XLSX)

Scheduler starts inside the FastAPI lifespan (main.py) so it shares the
process with the API server. Jobs run in a ThreadPoolExecutor — they call
the same functions the scripts call directly, with no subprocess overhead.

On Windows (your Spyder environment): times follow the machine's local clock
when timezone is set to Europe/London in Windows settings. Set the machine
timezone correctly before running.

Manual override: the UI trigger buttons on EodRunHistoryPage still work —
those call the API routes which invoke the same functions. No conflict.
"""

from __future__ import annotations
import logging
import subprocess
import sys
from datetime import date
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

# Patch 107: single source of truth for the nightly fire time. 22:50 gives
# Norgate's ~22:30 UK EOD post a 20-minute margin — verify NDU has ingested
# by then (see the [price_provider] freshness lines) and adjust if needed.
NIGHTLY_HOUR   = 22
NIGHTLY_MINUTE = 50


def _is_us_trading_day(d) -> bool:
    """Patch 110: True when the NYSE has a session on date d.

    The cron already restricts to mon-fri, but US market holidays fall on
    weekdays too (e.g. Fri 2026-07-03, Independence Day observed). Running
    the chain on a holiday would resolve to the PREVIOUS trading day and
    duplicate that session's processing (re-proposals, re-fills).

    FAIL-OPEN by design: if the calendar library errors, we return True —
    a wrongly-run holiday is an idempotent nuisance, a wrongly-SKIPPED
    trading day is a missed execution. The warning makes the fallback loud.
    """
    try:
        import pandas_market_calendars as mcal
        nyse = mcal.get_calendar('NYSE')
        return len(nyse.valid_days(d, d)) > 0
    except Exception as e:
        logger.warning('[scheduler] trading-day check FAILED (%s: %s) — '
                       'assuming trading day (fail-open)', type(e).__name__, e)
        return True


def _run_eod() -> None:
    """Nightly job — Patch 107 + 110.

    Patch 110: skip non-trading days (weekends handled by the cron;
    US market holidays checked against the NYSE calendar here).

    Patch 107: spawn the FULL chain, detached. Pre-107 this called
    eod_orchestrator.main([]) IN-PROCESS, which (a) skipped Step 1
    entirely — live universes/prices were never rebuilt on scheduled
    nights, only on Trigger Nightly button presses (the frozen-folder
    failures) — and (b) died mid-run whenever uvicorn --reload restarted
    the server. Now it calls the SAME _spawn_detached the button calls:
    same interpreter, same project-root cwd, full run_nightly chain,
    detached from the server process. Scheduler ≡ button by construction.
    """
    import datetime as _dt
    today = _dt.date.today()
    if today.weekday() >= 5 or not _is_us_trading_day(today):
        logger.info('[scheduler] %s is not a US trading day (weekend or '
                    'NYSE holiday) — skipping nightly chain', today)
        return

    logger.info('[scheduler] ── EOD job: spawning run_nightly (full chain, '
                'detached — identical to Trigger Nightly button) ──')
    try:
        # Lazy import to avoid any import cycle at app startup.
        from app.routes.eod import _spawn_detached
        pid = _spawn_detached('app.scripts.run_nightly')
        logger.info('[scheduler] EOD chain spawned, pid=%d — progress in the '
                    'run_nightly log under <backtestPath>/logs/', pid)
    except Exception:
        logger.exception('[scheduler] EOD spawn FAILED')


def _run_morning() -> None:
    """Morning job — Patch 107 + 110: trading-day guard + detached spawn."""
    import datetime as _dt
    today = _dt.date.today()
    if today.weekday() >= 5 or not _is_us_trading_day(today):
        logger.info('[scheduler] %s is not a US trading day — skipping '
                    'morning basket', today)
        return
    logger.info('[scheduler] ── Morning job: spawning run_morning (detached, '
                'identical to Trigger Morning button) ──')
    try:
        from app.routes.eod import _spawn_detached   # Patch 107
        pid = _spawn_detached('app.scripts.run_morning')
        logger.info('[scheduler] Morning chain spawned, pid=%d', pid)
    except Exception:
        logger.exception('[scheduler] Morning spawn FAILED')


def create_scheduler() -> BackgroundScheduler:
    """Build and return a configured scheduler (not yet started).

    Called from main.py lifespan so the scheduler lifecycle is tied to
    the FastAPI app — starts on app startup, shuts down on app shutdown.

    Timezone: Europe/London (handles BST/GMT automatically).
    Jobs:
      22:30 → EOD nightly (exec_data_refresh + PM)
      07:30 → Morning basket (overlay + XLSX)
    """
    scheduler = BackgroundScheduler(timezone='Europe/London')

    scheduler.add_job(
        _run_eod,
        # Patch 107/110: weekdays only (NYSE); weekday HOLIDAYS are skipped
        # inside _run_eod via the NYSE calendar. Time driven by the
        # constants above — was left at a test value.
        trigger=CronTrigger(day_of_week='mon-fri', hour=NIGHTLY_HOUR,
                            minute=NIGHTLY_MINUTE, timezone='Europe/London'),
        id='eod_nightly',
        name='EOD nightly — exec_data_refresh + position manager',
        replace_existing=True,
        misfire_grace_time=600,   # fire up to 10 min late if server was restarting
        coalesce=True,            # if missed multiple fires, run only once
    )

    scheduler.add_job(
        _run_morning,
        trigger=CronTrigger(hour=13, minute=40, timezone='Europe/London'),
        id='morning_basket',
        name='Morning basket — overlay-apply + broker-write',
        replace_existing=True,
        misfire_grace_time=600,
        coalesce=True,
    )
    print("Scheduler started")
    logger.info('[scheduler] Jobs registered: eod_nightly=%02d:%02d UK '
                '(mon-fri, NYSE-holiday-aware, full run_nightly chain, '
                'detached), morning job disabled',
                NIGHTLY_HOUR, NIGHTLY_MINUTE)
    return scheduler