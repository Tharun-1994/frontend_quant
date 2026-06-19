"""
eod.py
======
End-of-day execution endpoints. Exposes the Position Manager and related
nightly services via HTTP so an orchestrator (or operator) can fire them
without running Python scripts.

Endpoints
---------
POST /api/eod/execution-step/{strategy_id}   — run PM for one strategy

The orchestrator (C5, future) calls this once per execution_enabled
strategy after exec_data_refresh completes. A failure on one strategy
does NOT block the orchestrator from continuing with others (per Gap 6
decision: log + continue).
"""

from __future__ import annotations
import logging
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.strategy_bucket import StrategyBucket
from app.services.position_manager import run_position_manager
from app.services.overlay_apply import apply_overlay
from app.services.broker_write import write_broker_basket
from app.constants.PricePath import PricePath

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/eod", tags=["eod"])


# ── Request / response models ─────────────────────────────────────────────────


class ExecutionStepRequest(BaseModel):
    """Body for POST /api/eod/execution-step/{strategy_id}.

    Both fields optional. Defaults:
      - run_date → today (date.today())
      - data_root → <backtestPath>/exec_data/{YYYYMMDD where YYYYMMDD = run_date}
    """
    run_date: Optional[date] = Field(
        None,
        description="The data date — the bar that just closed. Defaults to today.",
    )
    data_root: Optional[str] = Field(
        None,
        description="Absolute path to exec_data/{YYYYMMDD}/ folder. "
                    "Defaults to <backtestPath>/exec_data/{YYYYMMDD}.",
    )


class ExecutionStepResponse(BaseModel):
    """Mirrors the runner's summary dict + adds explicit HTTP status."""
    eod_run_log_id: int
    strategy_id: int
    run_date: str
    fills_resolved: int
    fills_cancelled: int
    exits_applied: int
    proposed_inserted: int
    substitute_pool_inserted: int
    proposed_deleted: int
    active_regime_id: Optional[int] = None


# ── Endpoint ──────────────────────────────────────────────────────────────────


@router.post(
    "/execution-step/{strategy_id}",
    response_model=ExecutionStepResponse,
    summary="Run nightly Position Manager for one strategy",
)
def trigger_execution_step(
    strategy_id: int,
    request: ExecutionStepRequest = Body(default_factory=ExecutionStepRequest),
    db: Session = Depends(get_db),
):
    """
    Run the Position Manager nightly pipeline for one strategy:

      Step A — Resolve PENDING_FILL rows (PENDING → LIVE | CANCELLED)
                  using today's Norgate parquets in `data_root`.
      Step B — Build seedHoldings + payload, POST to engine
                  /api/execution/step/single.
      Step C — Apply engine's tradeLogger exit decisions to SQL
                  (LIVE → EXITED for rows with non-null exitDate).
      Step D — Insert tomorrow's PROPOSED + SUBSTITUTE_POOL rows
                  from engine's proposedOrders response.

    All four steps run in a single SQL transaction. Any failure rolls back
    every write, marks eod_run_log row as FAILED with the traceback, and
    surfaces as HTTP 500.

    Errors:
      404 — strategy not found
      409 — strategy has execution_enabled=False (admin must enable first)
      400 — data_root folder doesn't exist (exec_data_refresh hasn't run)
      500 — sub-step failure; details in response.detail and eod_run_log
    """
    # Validate strategy exists + is execution_enabled
    strategy = db.query(StrategyBucket).filter_by(id=strategy_id).first()
    if strategy is None:
        raise HTTPException(404, detail=f"Strategy id={strategy_id} not found")
    if not strategy.execution_enabled:
        raise HTTPException(
            409,
            detail=(f"Strategy id={strategy_id} ({strategy.name}) has "
                    f"execution_enabled=False. Enable via the strategy editor first."),
        )

    # Resolve run_date and data_root with sensible defaults
    run_date = request.run_date or date.today()
    if request.data_root:
        data_root = request.data_root
    else:
        data_root = str(
            Path(PricePath.backtestPath)
            / "exec_data"
            / run_date.strftime("%Y%m%d")
        )

    # Validate data_root exists — quick fail-fast vs the engine returning 500
    if not Path(data_root).exists():
        raise HTTPException(
            400,
            detail=(f"data_root folder not found: {data_root}. "
                    f"Run exec_data_refresh for this date first, or pass an "
                    f"explicit data_root in the request body."),
        )

    logger.info(
        f"[eod] execution-step strategy_id={strategy_id} ({strategy.name}) "
        f"run_date={run_date} data_root={data_root}"
    )

    # Fire PM. Runner owns its transaction; we just translate exceptions
    # to HTTP error responses. The runner has already written FAILED to
    # eod_run_log and rolled back SQL state on exception.
    try:
        result = run_position_manager(
            db=db,
            strategy_id=strategy_id,
            run_date=run_date,
            data_root=data_root,
        )
        return ExecutionStepResponse(**result)
    except HTTPException:
        raise
    except Exception as e:
        # Already logged + persisted in eod_run_log by runner. Surface as 500.
        logger.exception("[eod] execution-step failed")
        raise HTTPException(
            500,
            detail=f"{type(e).__name__}: {e}",
        )



class OverlayApplyRequest(BaseModel):
    """Body for POST /api/eod/overlay-apply/{strategy_id}."""
    override_date: date = Field(
        ...,
        description="The intended_trade_date the overlay targets",
    )
    csv_text: str = Field(
        ...,
        description="Full CSV body (header + data rows). Required columns: "
                    "original_symbol, action. Optional: substitute_symbol, "
                    "adjusted_capital.",
    )
    csv_source_path: Optional[str] = Field(
        None, description="Filesystem path of the CSV (for audit, optional)"
    )
    uploaded_by: Optional[str] = Field(
        None, description="Who uploaded (for audit, optional)"
    )

class OverlayApplyResponse(BaseModel):
    eod_run_log_id: int
    strategy_id: int
    override_date: str
    version: int
    overrides_recorded: int
    elided: int
    substituted: int
    adjusted_capital: int
    half_sized: int
    skipped_no_match: int

@router.post(
    "/overlay-apply/{strategy_id}",
    response_model=OverlayApplyResponse,
    summary="Apply trader's substitution CSV overlay to PROPOSED rows",
)
def trigger_overlay_apply(
        strategy_id: int,
        request: OverlayApplyRequest,
        db: Session = Depends(get_db),
):
    """
    Apply trader's substitution CSV to PROPOSED rows for one strategy on
    a given intended_trade_date. CSV actions: elide / substitute /
    adjust_capital / half_size. Untouched PROPOSED rows stay PROPOSED
    (broker-write auto-promotes them on its run).

    Errors:
      404 — strategy not found
      400 — malformed CSV or invalid action
      409 — substitute symbol not in SUBSTITUTE_POOL
      500 — DB exception
    """
    strategy = db.query(StrategyBucket).filter_by(id=strategy_id).first()
    if strategy is None:
        raise HTTPException(404, detail=f"Strategy id={strategy_id} not found")

    logger.info(
        f"[eod] overlay-apply strategy_id={strategy_id} ({strategy.name}) "
        f"override_date={request.override_date} csv_bytes={len(request.csv_text)}"
    )

    try:
        result = apply_overlay(
            db=db,
            strategy_id=strategy_id,
            override_date=request.override_date,
            csv_text=request.csv_text,
            uploaded_by=request.uploaded_by,
            csv_source_path=request.csv_source_path,
        )
        return OverlayApplyResponse(**result)
    except HTTPException:
        raise
    except ValueError as e:
        # CSV parse / validation errors → 400. Substitute target missing → 409.
        msg = str(e)
        status = 409 if 'SUBSTITUTE_POOL' in msg else 400
        logger.warning(f"[eod] overlay-apply rejected: {e}")
        raise HTTPException(status, detail=msg)
    except Exception as e:
        logger.exception("[eod] overlay-apply failed")
        raise HTTPException(500, detail=f"{type(e).__name__}: {e}")

# ── D2: Broker-write endpoint ─────────────────────────────────────────────────

class BrokerWriteRequest(BaseModel):
    """Body for POST /api/eod/broker-write."""
    trade_date: date = Field(
        ...,
        description="The intended_trade_date to emit a basket for"
    )
    output_dir: Optional[str] = Field(
        None,
        description="Where to write the file. Default "
                    "<backtestPath>/broker_output/{YYYYMMDD}/."
    )

class BrokerWriteResponse(BaseModel):
    eod_run_log_id: int
    trade_date: str
    file_path: str
    promoted_proposed: int
    orders_written: int

@router.post(
    "/broker-write",
    response_model=BrokerWriteResponse,
    summary="Emit M_Combined_{D}.xlsx from PENDING_FILL rows for IBKR upload",
)
def trigger_broker_write(
        request: BrokerWriteRequest,
        db: Session = Depends(get_db),
):
    """
    Auto-promote any remaining PROPOSED → PENDING_FILL (implicit "kept by
    trader"), then write M_Combined_{trade_date}.xlsx in IBKR Basket
    Trader format from all PENDING_FILL rows across strategies.

    Errors:
      500 — DB or filesystem exception
    """
    logger.info(f"[eod] broker-write trade_date={request.trade_date}")

    try:
        result = write_broker_basket(
            db=db,
            trade_date=request.trade_date,
            output_dir=request.output_dir,
        )
        return BrokerWriteResponse(**result)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[eod] broker-write failed")
        raise HTTPException(500, detail=f"{type(e).__name__}: {e}")

# ── F4: EOD run-log read + retry ─────────────────────────────────────────────

class EodRunLogRow(BaseModel):
    id: int
    run_date: str
    step: str
    strategy_id: Optional[int]
    strategy_name: Optional[str]
    status: str
    rows_affected: Optional[int]
    started_at: Optional[str]
    finished_at: Optional[str]
    error_msg: Optional[str]

@router.get(
    "/run-log",
    response_model=list[EodRunLogRow],
    summary="EOD run log (F4)",
)
def list_eod_run_log(
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        status_filter: Optional[str] = None,
        step_filter: Optional[str] = None,
        strategy_id: Optional[int] = None,
        limit: int = 200,
        db: Session = Depends(get_db),
):
    from app.models.eod_run_log import EodRunLog
    from app.models.strategy_bucket import StrategyBucket

    q = db.query(EodRunLog)
    if from_date:
        q = q.filter(EodRunLog.run_date >= from_date)
    if to_date:
        q = q.filter(EodRunLog.run_date <= to_date)
    if status_filter:
        q = q.filter(EodRunLog.status == status_filter.upper())
    if step_filter:
        q = q.filter(EodRunLog.step == step_filter)
    if strategy_id:
        q = q.filter(EodRunLog.strategy_id == strategy_id)

    rows = q.order_by(EodRunLog.id.desc()).limit(min(limit, 1000)).all()

    # Bulk-resolve strategy names
    sids = {r.strategy_id for r in rows if r.strategy_id is not None}
    name_map = {
        s.id: s.name
        for s in db.query(StrategyBucket).filter(StrategyBucket.id.in_(sids)).all()
    } if sids else {}

    return [
        EodRunLogRow(
            id=r.id,
            run_date=r.run_date.isoformat(),
            step=r.step,
            strategy_id=r.strategy_id,
            strategy_name=name_map.get(r.strategy_id) if r.strategy_id else None,
            status=r.status,
            rows_affected=r.rows_affected,
            started_at=r.started_at.isoformat() if r.started_at else None,
            finished_at=r.finished_at.isoformat() if r.finished_at else None,
            error_msg=r.error_msg,
        )
        for r in rows
    ]

class RetryResponse(BaseModel):
    eod_run_log_id: int
    original_id: int
    step: str
    status: str
    detail: str

@router.post("/run-log/{log_id}/retry",response_model=RetryResponse,summary="Retry a FAILED eod_run_log step (F4 retry button)",)
def retry_eod_run_log_step(log_id: int,db: Session = Depends(get_db),):
    """Re-fire the step that the given eod_run_log row recorded.

    Supported steps:
      execution_step    → re-fire run_position_manager(strategy_id, run_date)
      exec_data_refresh → re-fire run_exec_data_refresh(run_date)
      broker_write      → re-fire write_broker_basket(trade_date=run_date)

    Not supported (returns 409):
      overlay_apply     → CSV inputs are not stored, cannot replay deterministically.
                          Trader must re-upload the CSV via /overlay-apply directly.
    """
    from app.models.eod_run_log import EodRunLog
    from app.services.position_manager import run_position_manager
    from app.services.exec_data_refresh import run_exec_data_refresh, resolve_data_date
    from app.services.broker_write import write_broker_basket
    from pathlib import Path as _Path

    original = db.query(EodRunLog).filter_by(id=log_id).first()
    if original is None:
        raise HTTPException(404, detail=f"eod_run_log id={log_id} not found")

    step = original.step
    # For DATA-DATE-based steps (execution_step, exec_data_refresh) — re-resolve
    # in case the original row was written with a pre-fix (or pre-Norgate-post)
    # date that no longer matches reality. resolve_data_date is a no-op when
    # the input is already a past trading day.
    # For TRADE-DATE-based steps (broker_write), keep original.run_date as-is —
    # the value there is the day orders fire, not a data date.
    original_run_date = original.run_date
    if step in ("execution_step", "exec_data_refresh"):
        run_date = resolve_data_date(original_run_date)
        if run_date != original_run_date:
            logger.info(
                "[eod] retry log_id=%s step=%s re-resolved run_date %s -> %s",
                log_id, step, original_run_date, run_date,
            )
    else:
        run_date = original_run_date

    try:
        if step == "execution_step":
            if original.strategy_id is None:
                raise HTTPException(
                    400,
                    detail=f"execution_step row id={log_id} has no strategy_id — "
                           f"cannot retry",
                )
            # Resolve default data_root from the (possibly re-resolved) run_date
            data_root = str(
                _Path(PricePath.backtestPath)
                / "exec_data"
                / run_date.strftime("%Y%m%d")
            )
            result = run_position_manager(
                db=db,
                strategy_id=original.strategy_id,
                run_date=run_date,
                data_root=data_root,
            )
            return RetryResponse(
                eod_run_log_id=result["eod_run_log_id"],
                original_id=log_id,
                step=step,
                status="SUCCESS",
                detail=f"Re-fired execution_step for strategy_id="
                       f"{original.strategy_id} on {run_date} "
                       f"(original row had run_date={original_run_date}).",
            )

        if step == "exec_data_refresh":
            result = run_exec_data_refresh(db=db, run_date=run_date)
            return RetryResponse(
                eod_run_log_id=log_id,  # exec_data_refresh runs may not return a new id
                original_id=log_id,
                step=step,
                status="SUCCESS",
                detail=f"Re-fired exec_data_refresh for {run_date}: "
                       f"{len(result) if result else 0} universe(s) "
                       f"(original row had run_date={original_run_date}).",
            )

        if step == "broker_write":
            result = write_broker_basket(db=db, trade_date=run_date)
            return RetryResponse(
                eod_run_log_id=result["eod_run_log_id"],
                original_id=log_id,
                step=step,
                status="SUCCESS",
                detail=f"Re-fired broker_write for {run_date}: "
                       f"wrote {result['orders_written']} order(s) to "
                       f"{result['file_path']}",
            )

        if step == "overlay_apply":
            raise HTTPException(
                409,
                detail=f"step=overlay_apply cannot be retried automatically — "
                       f"the trader's CSV input is not persisted. Re-upload "
                       f"the CSV via POST /api/eod/overlay-apply/"
                       f"{original.strategy_id}.",
            )

        raise HTTPException(
            400, detail=f"Unknown step {step!r} — cannot retry"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[eod] retry failed")
        raise HTTPException(500, detail=f"{type(e).__name__}: {e}")



# ── F4: manual trigger endpoints (kick run_nightly / run_morning from UI) ────


class TriggerResponse(BaseModel):
    status: str
    pid: int
    message: str


def _spawn_detached(module: str, extra_args: list[str] | None = None) -> int:
    """Spawn `python -m <module>` as a detached subprocess.

    Returns the child PID. Child's stdout+stderr are redirected to a
    date-stamped file under <backtestPath>/logs/spawn_<module>_<ts>.log
    so any startup failure (ImportError, missing env var, etc.) is
    visible rather than silently swallowed.
    """
    import subprocess
    import sys
    from datetime import datetime as _dt
    from pathlib import Path as _Path

    from app.constants.PricePath import PricePath

    project_root = _Path(__file__).resolve().parents[2]
    cmd = [sys.executable, "-m", module] + (extra_args or [])

    # Spawn-log: separate from the script's own log file. Captures things
    # the script never gets to write — ImportError before main() runs,
    # missing module, wrong cwd, etc. Each spawn gets a fresh file.
    log_dir = _Path(PricePath.backtestPath) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.now().strftime("%Y%m%d_%H%M%S")
    safe_module = module.replace(".", "_")
    spawn_log = log_dir / f"spawn_{safe_module}_{stamp}.log"

    logger.info(
        "[eod] spawning child: cmd=%s cwd=%s spawn_log=%s",
        cmd, project_root, spawn_log,
    )

    log_fh = open(spawn_log, "w", encoding="utf-8")
    log_fh.write(
        f"=== spawn_detached === module={module} args={extra_args}\n"
        f"cmd={cmd}\ncwd={project_root}\npython={sys.executable}\n"
        f"started={_dt.now().isoformat(timespec='seconds')}\n"
        f"=== child output below ===\n"
    )
    log_fh.flush()

    proc = subprocess.Popen(
        cmd,
        cwd=str(project_root),
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    return proc.pid


@router.post(
    "/trigger-nightly",
    response_model=TriggerResponse,
    summary="Start run_nightly.py as a background process (UI trigger)",
)
def trigger_nightly():
    """Spawn run_nightly.py. The script runs:
      1. universe_pipeline (refresh Folder A from Norgate)
      2. eod_orchestrator (exec_data_refresh + PM per execution_enabled strategy)

    Returns immediately with the child PID. Watch progress on /execution/run-log.
    """
    try:
        pid = _spawn_detached("app.scripts.run_nightly")
        return TriggerResponse(
            status="STARTED",
            pid=pid,
            message="Nightly chain started. Monitor progress on the EOD Run History page.",
        )
    except Exception as e:
        logger.exception("[eod] failed to start nightly")
        raise HTTPException(500, detail=f"{type(e).__name__}: {e}")


@router.post(
    "/trigger-morning",
    response_model=TriggerResponse,
    summary="Start run_morning.py as a background process (UI trigger)",
)
def trigger_morning(trade_date: Optional[date] = None):
    """Spawn run_morning.py. The script runs:
      1. Discover substitution CSVs at backtest_data/substitution_input/{YYYYMMDD}/
      2. apply_overlay per CSV
      3. broker_write → emit M_Combined_{D}.xlsx

    Optional trade_date query overrides the default (today).
    """
    try:
        extra = ["--trade-date", trade_date.isoformat()] if trade_date else []
        pid = _spawn_detached("app.scripts.run_morning", extra)
        return TriggerResponse(
            status="STARTED",
            pid=pid,
            message=(
                f"Morning chain started for "
                f"{trade_date.isoformat() if trade_date else 'today'}. "
                f"Monitor progress on the EOD Run History page."
            ),
        )
    except Exception as e:
        logger.exception("[eod] failed to start morning")
        raise HTTPException(500, detail=f"{type(e).__name__}: {e}")



# ── Test Trigger Nightly ──────────────────────────────────────────────────────
# Separate from /trigger-nightly. Accepts a historical run_date so the tester
# can replay any past date and compare engine signals against the backtest
# tradelist. Does NOT run the universe pipeline (Norgate live-data fetch) —
# that step is today-only. Runs exec_data_refresh then PM, same as the real
# nightly but for a user-supplied date.


class TestTriggerNightlyRequest(BaseModel):
    run_date: date = Field(
        ...,
        description="Historical data date (close date). Engine signals will "
                    "target the next trading day after this date.",
    )
    strategy_id: Optional[int] = Field(
        None,
        description="Run for one execution_enabled strategy only. "
                    "Omit to run all execution_enabled strategies.",
    )
    # When supplied: positions open on run_date are extracted, rescaled to
    # regime.production_capital / regime.slots, and sent to the engine as
    # live holdings. Enables exit and dedup testing. None = cold start.
    mock_holdings_csv: Optional[str] = Field(
        None,
        description="Raw text of backtest tradelist CSV. Positions open on "
                    "run_date are rescaled to production capital and seeded "
                    "as live holdings. Omit for cold-start (no holdings).",
    )


class TestTriggerNightlyResponse(BaseModel):
    status: str
    run_date: str
    trade_date: str
    strategies_run: int
    results: list[dict]


@router.post(
    "/trigger-nightly-test",
    response_model=TestTriggerNightlyResponse,
    summary="Test trigger: run exec_data_refresh + PM for a historical date",
)
def trigger_nightly_test(
    request: TestTriggerNightlyRequest,
    db: Session = Depends(get_db),
):
    """Replay a historical date: exec_data_refresh + engine call per strategy.
    No DB writes. When mock_holdings_csv is supplied, open positions on
    run_date are rescaled to production capital and seeded as live holdings —
    allowing exits, dedup, and sector-cap logic to be tested.

    Runs synchronously (not spawned) so the CSV can be passed in-memory.
    Returns full per-strategy results when complete.
    """
    import datetime as _dt
    from pathlib import Path as _Path
    from app.services.exec_data_refresh import run_exec_data_refresh
    from app.services.position_manager.test_runner import run_test_position_manager
    from app.models.strategy_bucket import StrategyBucket

    run_date = request.run_date

    try:
        # Step 1: exec_data_refresh — always regenerate for the test date.
        logger.info(f'[test_trigger] exec_data_refresh for {run_date}')
        test_start_date = run_date - _dt.timedelta(days=650)
        run_exec_data_refresh(
            db,
            run_date=run_date,
            write_eod_log=False,
            start_date=test_start_date,
        )

        data_root = str(
            _Path(PricePath.backtestPath)
            / 'exec_data'
            / run_date.strftime('%Y%m%d')
        )

        # Step 2: engine call per strategy
        query = db.query(StrategyBucket).filter(
            StrategyBucket.execution_enabled == True
        )
        if request.strategy_id is not None:
            query = query.filter(StrategyBucket.id == request.strategy_id)
        strategies = query.order_by(StrategyBucket.id.asc()).all()

        if not strategies:
            raise HTTPException(404, detail='No execution_enabled strategies found.')

        results = []
        for strategy in strategies:
            try:
                result = run_test_position_manager(
                    db=db,
                    strategy_id=strategy.id,
                    run_date=run_date,
                    data_root=data_root,
                    mock_holdings_csv=request.mock_holdings_csv,
                )
                results.append({'status': 'SUCCESS', **result})
            except Exception as e:
                logger.exception(f'[test_trigger] strategy_id={strategy.id} failed')
                results.append({
                    'status': 'FAILED',
                    'strategy_id': strategy.id,
                    'strategy_name': strategy.name,
                    'error': f'{type(e).__name__}: {e}',
                })

        trade_date = results[0].get('trade_date', '') if results else ''
        return TestTriggerNightlyResponse(
            status='SUCCESS' if all(r['status'] == 'SUCCESS' for r in results) else 'PARTIAL',
            run_date=run_date.isoformat(),
            trade_date=trade_date,
            strategies_run=len(results),
            results=results,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception('[eod] trigger-nightly-test failed')
        raise HTTPException(500, detail=f'{type(e).__name__}: {e}')