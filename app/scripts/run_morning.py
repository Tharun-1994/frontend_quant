"""run_morning.py — D-day morning entry point for Task Scheduler.

Wraps morning_basket with date-stamped file logging. morning_basket itself
imports overlay_apply + broker_write services directly (no HTTP), so this
script works without the FastAPI server running.

Task Scheduler entry:
    Program     : <python.exe in the frontend_quant conda env>
    Arguments   : -m app.scripts.run_morning
    Start in    : <project root>

Exit codes (propagated from morning_basket):
    0   — broker-write succeeded (all overlays + final XLSX OK)
    1   — broker-write failed
    2   — one or more overlay-apply failed (broker-write still attempted)
    3   — unexpected error
"""

from __future__ import annotations
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.constants.PricePath import PricePath


def main(argv: Optional[list[str]] = None) -> int:
    log_path = _open_log()
    _log(log_path, '=' * 60)
    _log(log_path, f'Morning basket START  {datetime.now().isoformat(timespec="seconds")}')
    _log(log_path, f'log file: {log_path}')
    _log(log_path, '=' * 60)

    rc = _run_chained(
        [sys.executable, '-m', 'app.scripts.morning_basket'] + (argv or []),
        log_path,
    )

    _log(log_path, '')
    _log(log_path, '=' * 60)
    _log(log_path, f'Morning basket FINISHED  exit_code={rc}')
    _log(log_path, '=' * 60)
    return rc


def _open_log() -> Path:
    log_dir = Path(PricePath.backtestPath) / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M')
    return log_dir / f'morning_basket_{stamp}.log'


def _log(log_path: Path, msg: str) -> None:
    print(msg)
    with log_path.open('a', encoding='utf-8') as f:
        f.write(msg + '\n')


def _run_chained(cmd: list[str], log_path: Path) -> int:
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
            print(line)
            f.write(line + '\n')
            f.flush()
    proc.wait()
    return proc.returncode


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))