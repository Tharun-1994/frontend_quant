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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.strategy_bucket import StrategyBucket
from app.services.position_manager import run_position_manager
from app.services.overlay_apply import apply_overlay
from app.services.broker_write import write_broker_basket
from app.constants.PricePath import PricePath
from app.utiliy.universeGenerations.universe_today_refresh import refresh_all_today

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



class ReplayRequest(BaseModel):
    """Body for POST /api/eod/replay/{strategy_id} — manual date-targeted LIVE replay.

    Runs the SAME LIVE pipeline the nightly scheduler runs (writes the DB), but for a
    date you choose, on demand. Lets an operator step a missed session forward or replay
    a date while debugging a backtest/execution mismatch.
    """
    run_date: date = Field(..., description="Close date to replay (YYYY-MM-DD)")
    refresh_data: bool = Field(
        True, description="Run exec_data_refresh for this date first (regenerate parquets)."
    )
    recompute_equity: bool = Field(
        True, description="Recompute live_equity_snapshot after the PM run."
    )
    promote_and_write: bool = Field(
        True, description="Patch 83: run broker_write(run_date) FIRST — promote this "
                          "date's PROPOSED->PENDING_FILL / PENDING_EXIT->EXIT_SUBMITTED "
                          "so the PM can resolve fills, and emit M_Combined_{run_date}.xlsx."
    )


@router.post(
    "/replay/{strategy_id}",
    summary="Replay LIVE execution for one strategy on a chosen date (writes DB)",
)
def trigger_replay(
    strategy_id: int,
    request: ReplayRequest,
    db: Session = Depends(get_db),
):
    """Manual, date-targeted LIVE execution replay.

    Step 0 - broker_write(run_date) [Patch 83]: promote THIS date's orders
             (PROPOSED->PENDING_FILL, PENDING_EXIT->EXIT_SUBMITTED) — the orders the
             PREVIOUS session decided for today — so Step 2's PM can resolve their fills,
             and emit M_Combined_{run_date}.xlsx. Makes each replay a complete
             promote->resolve->decide session. Skipped when promote_and_write=False.
    Step 1 - exec_data_refresh(run_date): regenerate Norgate parquets for THIS exact
             date. The today-rollback in resolve_data_date is bypassed (same as
             run_nightly_test) because the operator explicitly chose a historical date.
             Skipped when refresh_data=False (parquets already present).
    Step 2 - run_position_manager(run_date): the REAL Position Manager. Resolves fills,
             applies exits, inserts PROPOSED/SUBSTITUTE_POOL - all DB writes. One session.
    Step 3 - recalc_and_store(): rebuild the equity curve so the chart matches the new
             tradelist. Skipped when recompute_equity=False.

    Unlike execution-step this is NOT gated on execution_enabled - it is a deliberate
    operator/debug action and may target a strategy still being set up.

    Errors:
      404 - strategy not found
      400 - exec_data missing for run_date and refresh_data=False
      500 - sub-step failure (already logged + persisted to eod_run_log by the runner)
    """
    # Imported inside the function (matches the retry endpoint pattern, avoids any
    # import-order coupling between eod.py and the exec_data_refresh service).
    from app.services.exec_data_refresh import run_exec_data_refresh

    strategy = db.query(StrategyBucket).filter_by(id=strategy_id).first()
    if strategy is None:
        raise HTTPException(404, detail=f"Strategy id={strategy_id} not found")

    run_date = request.run_date
    logger.info(
        f"[eod] replay strategy_id={strategy_id} ({strategy.name}) run_date={run_date} "
        f"refresh_data={request.refresh_data} recompute_equity={request.recompute_equity} "
        f"promote_and_write={request.promote_and_write}"
    )

    # Step 0 - promote this date's orders + write the basket (Patch 83).
    #   broker_write flips PROPOSED -> PENDING_FILL and PENDING_EXIT -> EXIT_SUBMITTED
    #   for rows whose trade/exit date == run_date (what the PREVIOUS session decided
    #   for today), so the PM below can actually resolve their fills. Also emits
    #   M_Combined_{run_date}.xlsx. broker_write commits internally. Run BEFORE the PM.
    broker = None
    if request.promote_and_write:
        try:
            broker = write_broker_basket(db=db, trade_date=run_date)
        except Exception as e:
            logger.exception("[eod] replay broker_write failed")
            raise HTTPException(
                500, detail=f"broker_write failed: {type(e).__name__}: {e}"
            )

    # Step 1 - regenerate parquets for the chosen date (bypass today-rollback)
    if request.refresh_data:
        try:
            run_exec_data_refresh(
                db,
                run_date=run_date,
                write_eod_log=False,
                start_date=run_date - timedelta(days=650),
            )
        except Exception as e:
            logger.exception("[eod] replay exec_data_refresh failed")
            raise HTTPException(
                500, detail=f"exec_data_refresh failed: {type(e).__name__}: {e}"
            )

    data_root = str(
        Path(PricePath.backtestPath) / "exec_data" / run_date.strftime("%Y%m%d")
    )
    if not Path(data_root).exists():
        raise HTTPException(
            400,
            detail=(f"exec_data folder not found: {data_root}. "
                    f"Set refresh_data=true to regenerate it for {run_date}."),
        )

    # Step 2 - the REAL Position Manager (DB writes)
    try:
        summary = run_position_manager(
            db=db, strategy_id=strategy_id, run_date=run_date, data_root=data_root,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[eod] replay run_position_manager failed")
        raise HTTPException(500, detail=f"{type(e).__name__}: {e}")

    # Step 3 - rebuild the equity curve (non-fatal; PM writes are already committed)
    equity = None
    if request.recompute_equity:
        try:
            from app.services.equity_recompute import recalc_and_store
            equity = recalc_and_store(db, strategy_id, adjusted=True)
            db.commit()
        except Exception as e:
            logger.exception("[eod] replay equity recompute failed (non-fatal)")
            equity = {"error": f"{type(e).__name__}: {e}"}

    return {
        "strategy_id": strategy_id,
        "strategy_name": strategy.name,
        "run_date": run_date.isoformat(),
        "refreshed_data": request.refresh_data,
        "broker": broker,
        "summary": summary,
        "equity": equity,
    }


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
    exits_written: int = 0          # Patch 84: PENDING_EXIT -> EXIT_SUBMITTED count
    stop_rows_written: int = 0      # Patch 84
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

# ── D3: Overlay-and-write combined endpoint ───────────────────────────────────

class OverlayAndWriteRequest(BaseModel):
    """Body for POST /api/eod/overlay-and-write/{strategy_id}.
    Applies substitution CSV overlay then immediately emits M_Combined XLSX.
    """
    override_date: date = Field(
        ...,
        description="The intended_trade_date the overlay targets (= trade date)",
    )
    csv_text: str = Field(
        ...,
        description="Full CSV body. Required columns: original_symbol, action. "
                    "Optional: substitute_symbol, adjusted_capital.",
    )
    uploaded_by: Optional[str] = Field(None)
    output_dir:  Optional[str] = Field(None)


class OverlayAndWriteResponse(BaseModel):
    strategy_id:        int
    strategy_name:      str
    override_date:      str
    # Overlay summary
    overrides_recorded: int
    elided:             int
    substituted:        int
    adjusted_capital:   int
    half_sized:         int
    skipped_no_match:   int
    # Broker-write summary
    orders_written:     int
    exits_written:      int
    file_path:          str


@router.post(
    "/overlay-and-write/{strategy_id}",
    response_model=OverlayAndWriteResponse,
    summary="Apply substitution CSV then emit M_Combined XLSX in one operation",
)
def overlay_and_write(
    strategy_id: int,
    request: OverlayAndWriteRequest,
    db: Session = Depends(get_db),
):
    """
    Combines overlay-apply and broker-write into a single call.
    Designed for the Substitution UI page — Vas uploads CSV, reviews
    the preview, confirms, and gets the final XLSX in one operation.

    Sequence:
      1. apply_overlay  — elide / substitute / adjust_capital / half_size
      2. write_broker_basket — auto-promotes remaining PROPOSED → PENDING_FILL,
                               emits M_Combined_{trade_date}.xlsx

    Errors:
      404 — strategy not found
      400 — malformed CSV or invalid action
      409 — substitute symbol not in SUBSTITUTE_POOL
      500 — DB or filesystem exception
    """
    strategy = db.query(StrategyBucket).filter_by(id=strategy_id).first()
    if strategy is None:
        raise HTTPException(404, detail=f"Strategy id={strategy_id} not found")

    logger.info(
        "[eod] overlay-and-write strategy_id=%d (%s) override_date=%s",
        strategy_id, strategy.name, request.override_date,
    )

    try:
        # Step 1 — overlay
        overlay_result = apply_overlay(
            db=db,
            strategy_id=strategy_id,
            override_date=request.override_date,
            csv_text=request.csv_text,
            uploaded_by=request.uploaded_by or 'ui',
            csv_source_path=None,
        )

        # Step 2 — broker write
        write_result = write_broker_basket(
            db=db,
            trade_date=request.override_date,
            output_dir=request.output_dir,
        )

        return OverlayAndWriteResponse(
            strategy_id=strategy_id,
            strategy_name=strategy.name,
            override_date=request.override_date.isoformat(),
            overrides_recorded=overlay_result.get('overrides_recorded', 0),
            elided=overlay_result.get('elided', 0),
            substituted=overlay_result.get('substituted', 0),
            adjusted_capital=overlay_result.get('adjusted_capital', 0),
            half_sized=overlay_result.get('half_sized', 0),
            skipped_no_match=overlay_result.get('skipped_no_match', 0),
            orders_written=write_result.get('orders_written', 0),
            exits_written=write_result.get('exits_written', 0),
            file_path=write_result.get('file_path', ''),
        )

    except HTTPException:
        raise
    except ValueError as e:
        msg = str(e)
        status = 409 if 'SUBSTITUTE_POOL' in msg else 400
        logger.warning("[eod] overlay-and-write rejected: %s", e)
        raise HTTPException(status, detail=msg)
    except Exception as e:
        logger.exception("[eod] overlay-and-write failed")
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


# ── D4: Overlay-and-write ALL strategies ─────────────────────────────────────

class StrategyOverlayItem(BaseModel):
    """One strategy's substitution CSV in the all-strategies request."""
    strategy_name: str = Field(
        ...,
        description="Must match strategies_bucket.name exactly "
                    "(e.g. 'PullBack_X3_Sp500'). Used to look up strategy_id.",
    )
    csv_text: str = Field(
        ...,
        description="Full CSV body for this strategy. Required columns: "
                    "original_symbol, action. Optional: substitute_symbol, "
                    "adjusted_capital.",
    )


class OverlayAndWriteAllRequest(BaseModel):
    """Body for POST /api/eod/overlay-and-write-all."""
    trade_date: date = Field(
        ...,
        description="The intended_trade_date the overlays target.",
    )
    strategies: List[StrategyOverlayItem] = Field(
        ...,
        description="One item per strategy. Can be empty — broker-write "
                    "still runs and auto-promotes remaining PROPOSED rows.",
    )
    output_dir: Optional[str] = Field(None)


class StrategyOverlayResult(BaseModel):
    """Per-strategy overlay outcome."""
    strategy_name:      str
    strategy_id:        int
    status:             str   # 'ok' | 'skipped' | 'error'
    error:              Optional[str] = None
    overrides_recorded: int = 0
    elided:             int = 0
    substituted:        int = 0
    adjusted_capital:   int = 0
    half_sized:         int = 0
    skipped_no_match:   int = 0


class OverlayAndWriteAllResponse(BaseModel):
    trade_date:       str
    strategies_run:   int
    strategies_ok:    int
    strategies_failed: int
    overlay_results:  List[StrategyOverlayResult]
    orders_written:   int
    exits_written:    int
    file_path:        str


@router.post(
    "/overlay-and-write-all",
    response_model=OverlayAndWriteAllResponse,
    summary="Apply substitution CSVs for all strategies then emit M_Combined XLSX",
)
def overlay_and_write_all(
    request: OverlayAndWriteAllRequest,
    db: Session = Depends(get_db),
):
    """
    Applies substitution CSV overlays for all strategies in one call,
    then writes a single M_Combined XLSX covering all strategies.

    Sequence:
      1. For each item in request.strategies:
           a. Look up strategy by name
           b. Call apply_overlay (elide / substitute / adjust_capital / half_size)
           c. Log result — failure on one strategy does NOT abort others
      2. Call write_broker_basket once across all strategies

    Strategies with no CSV in the request are untouched —
    broker_write auto-promotes their remaining PROPOSED → PENDING_FILL.

    Errors:
      400 — malformed CSV on any strategy
      409 — substitute symbol not in SUBSTITUTE_POOL
      500 — broker-write or DB failure
    """
    logger.info(
        "[eod] overlay-and-write-all trade_date=%s strategies=%d",
        request.trade_date, len(request.strategies),
    )

    overlay_results: List[StrategyOverlayResult] = []
    n_ok = 0
    n_failed = 0

    # Step 1 — apply overlay per strategy
    for item in request.strategies:
        strategy = db.query(StrategyBucket).filter_by(name=item.strategy_name).first()

        if strategy is None:
            logger.warning("[eod] overlay-and-write-all: strategy %r not found", item.strategy_name)
            overlay_results.append(StrategyOverlayResult(
                strategy_name=item.strategy_name,
                strategy_id=0,
                status='skipped',
                error=f"Strategy '{item.strategy_name}' not found in DB",
            ))
            n_failed += 1
            continue

        if not strategy.execution_enabled:
            logger.warning("[eod] overlay-and-write-all: %r not execution_enabled", item.strategy_name)
            overlay_results.append(StrategyOverlayResult(
                strategy_name=item.strategy_name,
                strategy_id=strategy.id,
                status='skipped',
                error=f"Strategy '{item.strategy_name}' has execution_enabled=False",
            ))
            n_failed += 1
            continue

        try:
            result = apply_overlay(
                db=db,
                strategy_id=strategy.id,
                override_date=request.trade_date,
                csv_text=item.csv_text,
                uploaded_by='ui',
                csv_source_path=None,
            )
            overlay_results.append(StrategyOverlayResult(
                strategy_name=item.strategy_name,
                strategy_id=strategy.id,
                status='ok',
                overrides_recorded=result.get('overrides_recorded', 0),
                elided=result.get('elided', 0),
                substituted=result.get('substituted', 0),
                adjusted_capital=result.get('adjusted_capital', 0),
                half_sized=result.get('half_sized', 0),
                skipped_no_match=result.get('skipped_no_match', 0),
            ))
            n_ok += 1
            logger.info(
                "[eod] overlay-and-write-all: %s overlay OK "
                "elide=%d sub=%d adj=%d half=%d",
                item.strategy_name,
                result.get('elided', 0), result.get('substituted', 0),
                result.get('adjusted_capital', 0), result.get('half_sized', 0),
            )
        except (ValueError, HTTPException) as e:
            msg = str(e)
            logger.warning("[eod] overlay-and-write-all: %s overlay FAILED: %s", item.strategy_name, msg)
            overlay_results.append(StrategyOverlayResult(
                strategy_name=item.strategy_name,
                strategy_id=strategy.id,
                status='error',
                error=msg,
            ))
            n_failed += 1

    # Step 2 — broker write (runs even if some overlays failed)
    try:
        write_result = write_broker_basket(
            db=db,
            trade_date=request.trade_date,
            output_dir=request.output_dir,
        )
    except Exception as e:
        logger.exception("[eod] overlay-and-write-all broker-write failed")
        raise HTTPException(500, detail=f"broker-write failed: {type(e).__name__}: {e}")

    return OverlayAndWriteAllResponse(
        trade_date=request.trade_date.isoformat(),
        strategies_run=len(request.strategies),
        strategies_ok=n_ok,
        strategies_failed=n_failed,
        overlay_results=overlay_results,
        orders_written=write_result.get('orders_written', 0),
        exits_written=write_result.get('exits_written', 0),
        file_path=write_result.get('file_path', ''),
    )



# REPLACE
# ── D5: Overlay-and-write COMBINED — one CSV, all strategies ─────────────────

class OverlayAndWriteCombinedRequest(BaseModel):
    """Body for POST /api/eod/overlay-and-write-combined.

    Accepts Vas's single combined CSV with System column.
    Parses system_code from each row, looks up strategy_id,
    groups rows by strategy, applies overlays, writes XLSX.
    """
    csv_text: str = Field(
        ...,
        description="Full combined CSV. Required columns: "
                    "System, original, action. "
                    "Optional: substitute, capital, reason_for_action, Date.",
    )
    output_dir: Optional[str] = Field(None)


@router.post(
    "/overlay-and-write-combined",
    response_model=OverlayAndWriteAllResponse,
    summary="Parse combined substitution CSV by system_code then emit M_Combined XLSX",
)
def overlay_and_write_combined(
    request: OverlayAndWriteCombinedRequest,
    db: Session = Depends(get_db),
):
    """
    Accepts Vas's single combined substitution CSV (one file, all strategies).

    Flow:
      1. Parse CSV — auto-detects Vas format (System, original, action, ...)
      2. Group rows by System column (= system_code)
      3. Look up strategy by system_code — NOT by name
      4. For each strategy group:
           a. Derive override_date from Date column (DD/MM/YYYY) or first valid date
           b. Rebuild per-strategy CSV text and call apply_overlay
      5. write_broker_basket once for all strategies

    system_code must be set on strategies_bucket for routing to work.
    Rows with unknown system_code are logged and skipped.
    """
    logger.info("[eod] overlay-and-write-combined csv_bytes=%d", len(request.csv_text))

    # 1. Parse the combined CSV using the updated _parse_csv
    try:
        from app.services.overlay_apply import _parse_csv
        all_actions = _parse_csv(request.csv_text)
    except ValueError as e:
        raise HTTPException(400, detail=f"CSV parse error: {e}")

    if not all_actions:
        raise HTTPException(400, detail="CSV has no data rows")

    # 2. Group actions by system_code
    from collections import defaultdict
    by_code: dict = defaultdict(list)
    for a in all_actions:
        code = a.get('system_code') or ''
        if not code:
            logger.warning("[eod] overlay-and-write-combined: row missing System column — skipped")
            continue
        by_code[code].append(a)

    if not by_code:
        raise HTTPException(400, detail="No System column values found in CSV")

    # 3. Build system_code → strategy map in one query
    code_list = list(by_code.keys())
    strategies_by_code = {
        s.system_code: s
        for s in db.query(StrategyBucket).filter(
            StrategyBucket.system_code.in_(code_list)
        ).all()
        if s.system_code
    }

    overlay_results: List[StrategyOverlayResult] = []
    n_ok = 0
    n_failed = 0

    # 4. Apply overlay per strategy group
    for code, actions in by_code.items():
        strategy = strategies_by_code.get(code)

        if strategy is None:
            logger.warning(
                "[eod] overlay-and-write-combined: system_code %r not found in DB", code
            )
            overlay_results.append(StrategyOverlayResult(
                strategy_name=code,
                strategy_id=0,
                status='skipped',
                error=f"system_code '{code}' not mapped to any strategy in DB. "
                      f"Set system_code on the strategy via the Strategy editor.",
            ))
            n_failed += 1
            continue

        if not strategy.execution_enabled:
            overlay_results.append(StrategyOverlayResult(
                strategy_name=strategy.name,
                strategy_id=strategy.id,
                status='skipped',
                error=f"Strategy '{strategy.name}' has execution_enabled=False",
            ))
            n_failed += 1
            continue

        # Derive override_date — use Date from first row that has one,
        # fall back to today if none parsed
        override_date = next(
            (a['override_date'] for a in actions if a.get('override_date')),
            date.today(),
        )

        # Rebuild per-strategy CSV text from the parsed action dicts
        # so apply_overlay's existing logic (which expects csv_text) works unchanged
        import io as _io
        buf = _io.StringIO()
        buf.write('original_symbol,action,substitute_symbol,adjusted_capital,reason_for_action\n')
        for a in actions:
            buf.write(
                f"{a['original_symbol']},"
                f"{a['action']},"
                f"{a.get('substitute_symbol') or ''},"
                f"{a.get('adjusted_capital') or ''},"
                f"{a.get('reason_for_action') or ''}\n"
            )
        per_strategy_csv = buf.getvalue()

        try:
            result = apply_overlay(
                db=db,
                strategy_id=strategy.id,
                override_date=override_date,
                csv_text=per_strategy_csv,
                uploaded_by='ui_combined',
                csv_source_path=None,
            )
            overlay_results.append(StrategyOverlayResult(
                strategy_name=strategy.name,
                strategy_id=strategy.id,
                status='ok',
                overrides_recorded=result.get('overrides_recorded', 0),
                elided=result.get('elided', 0),
                substituted=result.get('substituted', 0),
                adjusted_capital=result.get('adjusted_capital', 0),
                half_sized=result.get('half_sized', 0),
                skipped_no_match=result.get('skipped_no_match', 0),
            ))
            n_ok += 1
            logger.info(
                "[eod] overlay-and-write-combined: %s (%s) OK "
                "elide=%d sub=%d adj=%d half=%d date=%s",
                strategy.name, code,
                result.get('elided', 0), result.get('substituted', 0),
                result.get('adjusted_capital', 0), result.get('half_sized', 0),
                override_date,
            )
        except (ValueError, HTTPException) as e:
            msg = str(e)
            logger.warning(
                "[eod] overlay-and-write-combined: %s FAILED: %s", strategy.name, msg
            )
            overlay_results.append(StrategyOverlayResult(
                strategy_name=strategy.name,
                strategy_id=strategy.id,
                status='error',
                error=msg,
            ))
            n_failed += 1

    # 5. broker write — runs even if some overlays failed
    # Use the first valid override_date across all parsed actions
    all_dates = [a['override_date'] for a in all_actions if a.get('override_date')]
    trade_date = min(all_dates) if all_dates else date.today()

    try:
        write_result = write_broker_basket(
            db=db,
            trade_date=trade_date,
            output_dir=request.output_dir,
        )
    except Exception as e:
        logger.exception("[eod] overlay-and-write-combined broker-write failed")
        raise HTTPException(500, detail=f"broker-write failed: {type(e).__name__}: {e}")

    return OverlayAndWriteAllResponse(
        trade_date=trade_date.isoformat(),
        strategies_run=len(by_code),
        strategies_ok=n_ok,
        strategies_failed=n_failed,
        overlay_results=overlay_results,
        orders_written=write_result.get('orders_written', 0),
        exits_written=write_result.get('exits_written', 0),
        file_path=write_result.get('file_path', ''),
    )


# ── F4: EOD run-log read + retry ─────────────────────────────────────────────

# ── Patch 76: manual "bring static backtest universes up to today" ───────────
# Backs the "Update today's prices" button on the strategy dashboard. Calls
# universe_today_refresh.refresh_all_today() — append-only extension of
# backtest_data/universes/ (REGISTRY) + universes/index/ (INDEX_REGISTRY) to the
# latest posted Norgate session. Synchronous (no spawn): the pull is sequential
# and only a few days wide, and it must NOT fork inside uvicorn — see the
# service docstring. Validated history is never re-pulled, so signed-off
# backtest entries cannot shift; per-universe failures surface in has_errors +
# each item's status, a total failure raises 500.

class RefreshUniversesTodayResponse(BaseModel):
    requested_end: str          # the date asked for (today)
    resolved_data_date: str     # after Norgate post-hour rollback
    today_excluded: bool        # True when today's session wasn't posted yet
    universes: List[dict]       # per-universe: slug/status/appended/num_tickers/restated
    index: List[dict]           # per-index: key/status/appended/restated
    restated_any: List[str]     # union of names restated since last update
    has_errors: bool


@router.post(
    "/refresh-universes-today",
    response_model=RefreshUniversesTodayResponse,
    summary="Append today's bar to static backtest universes + index series (Patch 76)",
)
def refresh_universes_today():
    """Extend every active universe (REGISTRY) and index series (INDEX_REGISTRY)
    under backtest_data/universes/ to the latest posted Norgate session.

    Append-only — already-validated backtest entries cannot shift. Runs
    synchronously; the page waits for the summary. A universe/index with no
    base on disk is reported SKIPPED (run universe_pipeline / generate_index_prices
    first). Restated names (div/split since last update) are reported, not
    auto-corrected.
    """
    # Lazy import keeps norgatedata (pulled in transitively via INDEX_REGISTRY)
    # off the eod-router import path until a refresh actually runs.

    try:
        summary = refresh_all_today()
        logger.info("[eod] refresh-universes-today: %s", summary)
        return RefreshUniversesTodayResponse(**summary)
    except Exception as e:
        logger.exception("[eod] refresh-universes-today failed")
        raise HTTPException(500, detail=f"{type(e).__name__}: {e}")