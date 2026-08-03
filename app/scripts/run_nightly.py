"""run_nightly.py — nightly EOD entry point for Task Scheduler.

Chains universe_pipeline + eod_orchestrator inside one process, captures
both stdout streams to a date-stamped log file under <backtestPath>/logs/.

Task Scheduler entry:
    Program     : <python.exe in the frontend_quant conda env>
    Arguments   : -m app.scripts.run_nightly
    Start in    : <project root>

Exit codes:
    0   — universe + orchestrator both succeeded
    1   — eod_orchestrator: exec_data_refresh failed
    2   — eod_orchestrator: one or more PM runs failed
    3   — eod_orchestrator: unexpected error
    10  — universe_pipeline failed (no orchestrator run attempted)

universe_pipeline runs as a subprocess so its existing __main__ setup
(synthetics loader, CsvDataStore, processor) is unchanged. The orchestrator
also runs as a subprocess for consistency — both get identical log capture.
"""

from __future__ import annotations
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, TextIO

from app.constants.PricePath import PricePath


def main(argv: Optional[list[str]] = None) -> int:
    log_path = _open_log()
    _log(log_path, '=' * 60)
    _log(log_path, f'Nightly EOD START  {datetime.now().isoformat(timespec="seconds")}')
    _log(log_path, f'log file: {log_path}')
    _log(log_path, '=' * 60)

    # ── Step 1: live_universe_pipeline (EQUITY universes for execution) ──
    # Refreshes backtest_data/live_universes/sp500, spy, ... with ~5 years
    # of fresh Norgate data. Backtest universes folder is NEVER touched.
    _log(log_path, '')
    _log(log_path, '--- Step 1: live_universe_pipeline (equity, ~5yr) ---')
    rc1_live = _run_chained(
        [sys.executable, '-m', 'app.utiliy.universeGenerations.live_universe_pipeline'],
        log_path,
    )
    if rc1_live != 0:
        # Patch 96: do NOT abort. The live pipeline now isolates per-universe (a
        # failed universe is dropped, healthy ones still build), so proceed to
        # Step 2 — the orchestrator runs each enabled strategy independently and
        # any whose universe didn't build fails on its own (the rest continue).
        _log(log_path, f'Step 1 returned exit code {rc1_live} — continuing '
                       f'(per-universe isolation; Step 2 handles the rest)')

    # ── Step 1b: universe_pipeline (legacy / lra_14 etc.) ────────────────
    # Static REGISTRY currently only includes lra_14, which IS execution-
    # facing and needs the same nightly refresh treatment.
    _log(log_path, '')
    _log(log_path, '--- Step 1b: universe_pipeline (static registry, lra_14 etc.) ---')
    rc1 = _run_chained(
        [sys.executable, '-m', 'app.utiliy.universeGenerations.universe_pipeline'],
        log_path,
    )
    if rc1 != 0:
        # Patch 96: same isolation policy as Step 1 — log and continue.
        _log(log_path, f'Step 1b returned exit code {rc1} — continuing')
    _log(log_path, 'Step 1 complete (see per-step results above)')

    # ── Step 2: eod_orchestrator ───────────────────────────────────────────
    _log(log_path, '')
    _log(log_path, '--- Step 2: eod_orchestrator ---')
    rc2 = _run_chained(
        [sys.executable, '-m', 'app.scripts.eod_orchestrator'],
        log_path,
    )

    _log(log_path, '')
    _log(log_path, '=' * 60)
    _log(log_path, f'Nightly EOD FINISHED  exit_code={rc2}')
    _log(log_path, '=' * 60)
    return rc2


def _open_log() -> Path:
    log_dir = Path(PricePath.backtestPath) / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M')
    return log_dir / f'nightly_eod_{stamp}.log'


def _log(log_path: Path, msg: str) -> None:
    """Tee one line to both console and the log file."""
    print(msg)
    with log_path.open('a', encoding='utf-8') as f:
        f.write(msg + '\n')


def _run_chained(cmd: list[str], log_path: Path) -> int:
    """Run a subprocess; tee its stdout (and merged stderr) to console + log file.
    Returns the subprocess exit code.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    with log_path.open('a', encoding='utf-8') as f:
        for line in proc.stdout:
            line = line.rstrip()
            # Echo to console live so Task Scheduler / a watching terminal sees progress
            print(line)
            f.write(line + '\n')
            f.flush()
    proc.wait()
    return proc.returncode


if __name__ == '__main__':
    sys.exit(main())