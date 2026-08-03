"""run_revert.py — Patch 112: journal capture + revert of a Position Manager run.

Design (approved):
  - runner.py calls journal_pre_run_state() right after creating its
    eod_run_log row (inside the work transaction), and
    journal_created_rows() right after Step D.
  - POST /pm/revert-execution restores the journaled state:
      1. Locate the LATEST SUCCESS execution_step run for (strategy, run_date).
      2. Guard: refuse when a LATER successful run exists for the strategy —
         only the top of the stack is revertible (no mid-history rewrites).
      3. Restore every PRE snapshot verbatim onto its tradelist row.
      4. Delete every CREATED tradelist row (that run's PROPOSED / POOL).
      5. Mark the eod_run_log row status='REVERTED'.
  - Out of scope by design: exec_data parquets (regenerable), basket XLSX
    (regenerate after re-run), morning-overlay changes (separate workflow),
    live_equity snapshots (recompute-equity flag on re-run covers it).

Loud-fail everywhere: a revert either fully lands or raises with the exact
row that diverged.
"""
from __future__ import annotations
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.models.tradelist import Tradelist
from app.models.eod_run_log import EodRunLog
from app.models.tradelist_run_journal import TradelistRunJournal

# Every field a Position Manager run can mutate on a tradelist row.
# (Morning overlay fields like source_tag/ledger are included defensively —
# restoring them to their pre-run value is always correct because the runner
# never changes them mid-run, so PRE == post-run for those.)
SNAPSHOT_FIELDS = (
    'status', 'ledger', 'source_tag',
    'entry_date', 'entry_price', 'entry_timing',
    'filled_qty', 'avg_fill_price', 'fill_status',
    'exit_date', 'exit_price', 'exit_reason',
    'profit', 'profit_pct', 'day_count',
    'current_stop_price', 'current_tp_price', 'stop_overridden',
)

# Statuses a run may touch (journal scope). PROPOSED/POOL are journaled too:
# the run DELETES and reinserts them, so their pre-run state must be
# restorable when the reinserted generation is removed on revert.
JOURNALED_STATUSES = (
    'PENDING_FILL', 'EXIT_SUBMITTED', 'LIVE', 'PENDING_EXIT',
    'PROPOSED', 'SUBSTITUTE_POOL',
)

# Full-row fields needed to RECREATE a deleted PROPOSED/POOL row on revert.
RECREATE_FIELDS = SNAPSHOT_FIELDS + (
    'strategy_id', 'symbol', 'direction', 'intended_trade_date',
    'intended_qty', 'intended_capital', 'limit_price',
    'initial_stop_price', 'initial_tp_price',
    'ranking_rank', 'ranking_value', 'entered_regime_id',
    'proposal_date', 'substitute_link_id',
)


def _to_jsonable(v):
    if isinstance(v, Decimal):
        return {'__dec__': str(v)}
    if isinstance(v, (date, datetime)):
        return {'__date__': v.isoformat()}
    return v


def _from_jsonable(v):
    if isinstance(v, dict) and '__dec__' in v:
        return Decimal(v['__dec__'])
    if isinstance(v, dict) and '__date__' in v:
        return date.fromisoformat(v['__date__'][:10])
    return v


def _snapshot_row(row: Tradelist, fields) -> str:
    return json.dumps(
        {f: _to_jsonable(getattr(row, f, None)) for f in fields}
    )


def journal_pre_run_state(db: Session, log_row_id: int,
                          strategy_id: int, run_date: date) -> int:
    """Journal the pre-run state of every row this run may touch.

    Called from runner.py inside the work transaction, before any step.
    PROPOSED/POOL rows get FULL snapshots (RECREATE_FIELDS) because Step D
    deletes them; all other statuses get the mutable-field snapshot.
    """
    rows = (
        db.query(Tradelist)
        .filter(
            Tradelist.strategy_id == strategy_id,
            Tradelist.status.in_(JOURNALED_STATUSES),
        )
        .all()
    )
    for row in rows:
        full = row.status in ('PROPOSED', 'SUBSTITUTE_POOL')
        db.add(TradelistRunJournal(
            eod_run_log_id=log_row_id,
            strategy_id=strategy_id,
            run_date=run_date,
            tradelist_id=row.id,
            kind='PRE_FULL' if full else 'PRE',
            snapshot_json=_snapshot_row(
                row, RECREATE_FIELDS if full else SNAPSHOT_FIELDS),
        ))
    db.flush()
    print(f'[run_revert] journaled pre-run state of {len(rows)} row(s) '
          f'(log_id={log_row_id})')
    return len(rows)


def journal_created_rows(db: Session, log_row_id: int,
                         strategy_id: int, run_date: date) -> int:
    """Journal the ids of rows this run created (post-Step-D generation).

    The inserter deletes all prior PROPOSED/POOL and inserts fresh, so the
    current PROPOSED/POOL set for the strategy IS exactly this run's output.
    """
    rows = (
        db.query(Tradelist.id)
        .filter(
            Tradelist.strategy_id == strategy_id,
            Tradelist.status.in_(('PROPOSED', 'SUBSTITUTE_POOL')),
        )
        .all()
    )
    for (rid,) in rows:
        db.add(TradelistRunJournal(
            eod_run_log_id=log_row_id,
            strategy_id=strategy_id,
            run_date=run_date,
            tradelist_id=rid,
            kind='CREATED',
            snapshot_json=None,
        ))
    db.flush()
    print(f'[run_revert] journaled {len(rows)} CREATED row id(s) '
          f'(log_id={log_row_id})')
    return len(rows)


def revert_execution(db: Session, strategy_id: int, run_date: date) -> dict:
    """Revert the latest SUCCESS execution_step run for (strategy, run_date)."""
    log_row: Optional[EodRunLog] = (
        db.query(EodRunLog)
        .filter(
            EodRunLog.step == 'execution_step',
            EodRunLog.strategy_id == strategy_id,
            EodRunLog.run_date == run_date,
            EodRunLog.status == 'SUCCESS',
        )
        .order_by(EodRunLog.id.desc())
        .first()
    )
    if log_row is None:
        raise ValueError(
            f'No SUCCESS execution_step run found for strategy_id='
            f'{strategy_id} run_date={run_date} — nothing to revert.')

    # Guard: only the LATEST successful run for this strategy is revertible.
    later = (
        db.query(EodRunLog)
        .filter(
            EodRunLog.step == 'execution_step',
            EodRunLog.strategy_id == strategy_id,
            EodRunLog.status == 'SUCCESS',
            EodRunLog.id > log_row.id,
        )
        .order_by(EodRunLog.id.asc())
        .first()
    )
    if later is not None:
        raise ValueError(
            f'A later successful run exists (log_id={later.id}, '
            f'run_date={later.run_date}). Revert that one first — only the '
            f'top of the stack is revertible.')

    journal = (
        db.query(TradelistRunJournal)
        .filter(TradelistRunJournal.eod_run_log_id == log_row.id)
        .all()
    )
    if not journal:
        raise ValueError(
            f'Run log_id={log_row.id} has NO journal rows — it predates '
            f'Patch 112 and cannot be reverted automatically.')

    pre_rows     = [j for j in journal if j.kind in ('PRE', 'PRE_FULL')]
    created_ids  = {j.tradelist_id for j in journal if j.kind == 'CREATED'}
    pre_ids      = {j.tradelist_id for j in pre_rows}

    # 1) Delete this run's created generation (skip any id that is also in
    #    PRE — cannot happen by construction, but belt-and-braces).
    deleted = 0
    for rid in sorted(created_ids - pre_ids):
        row = db.query(Tradelist).filter_by(id=rid).first()
        if row is not None:
            db.delete(row)
            deleted += 1

    # 2) Restore every PRE snapshot. PRE_FULL rows that the run deleted
    #    (previous PROPOSED/POOL generation) are RECREATED.
    restored, recreated = 0, 0
    for j in pre_rows:
        snap = {k: _from_jsonable(v)
                for k, v in json.loads(j.snapshot_json).items()}
        row = db.query(Tradelist).filter_by(id=j.tradelist_id).first()
        if row is None:
            if j.kind != 'PRE_FULL':
                raise ValueError(
                    f'Journaled tradelist_id={j.tradelist_id} (kind={j.kind}) '
                    f'no longer exists and cannot be recreated — state '
                    f'diverged outside the runner. Aborting revert.')
            row = Tradelist(**{k: v for k, v in snap.items()})
            db.add(row)
            recreated += 1
        else:
            for k, v in snap.items():
                setattr(row, k, v)
            restored += 1

    log_row.status = 'REVERTED'
    log_row.error_msg = (
        f'REVERTED at {datetime.now():%Y-%m-%d %H:%M:%S}: '
        f'{restored} restored, {recreated} recreated, {deleted} deleted.')
    db.commit()

    result = {
        'log_id': log_row.id,
        'strategy_id': strategy_id,
        'run_date': str(run_date),
        'rows_restored': restored,
        'rows_recreated': recreated,
        'rows_deleted': deleted,
        'note': ('Re-run via "Run execution for a date" after fixing. '
                 'Basket XLSX for the next day, if already generated, must '
                 'be regenerated after the re-run.'),
    }
    print(f'[run_revert] REVERTED log_id={log_row.id}: {result}')
    return result