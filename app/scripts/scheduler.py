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


def _run_eod() -> None:
    """Nightly job: exec_data_refresh + per-strategy PM runner."""
    logger.info('[scheduler] ── EOD job starting ──')
    try:
        from app.scripts.eod_orchestrator import main as eod_main
        exit_code = eod_main([])
        if exit_code == 0:
            logger.info('[scheduler] EOD job finished OK')
        else:
            logger.error('[scheduler] EOD job finished with exit_code=%d', exit_code)
    except Exception:
        logger.exception('[scheduler] EOD job raised an exception')


def _run_morning() -> None:
    """Morning job: overlay-apply + broker-write → M_Combined XLSX."""
    logger.info('[scheduler] ── Morning basket job starting ──')
    try:
        from app.scripts.morning_basket import main as morning_main
        exit_code = morning_main([])
        if exit_code == 0:
            logger.info('[scheduler] Morning basket job finished OK')
        else:
            logger.error('[scheduler] Morning basket job finished with exit_code=%d', exit_code)
    except Exception:
        logger.exception('[scheduler] Morning basket job raised an exception')


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
        trigger=CronTrigger(hour=22, minute=50, timezone='Europe/London'),
        id='eod_nightly',
        name='EOD nightly — exec_data_refresh + position manager',
        replace_existing=True,
        misfire_grace_time=600,   # fire up to 10 min late if server was restarting
        coalesce=True,            # if missed multiple fires, run only once
    )

    scheduler.add_job(
        _run_morning,
        trigger=CronTrigger(hour=7, minute=30, timezone='Europe/London'),
        id='morning_basket',
        name='Morning basket — overlay-apply + broker-write',
        replace_existing=True,
        misfire_grace_time=600,
        coalesce=True,
    )

    logger.info('[scheduler] Jobs registered: eod_nightly=22:30 UK, morning_basket=07:30 UK')
    return scheduler