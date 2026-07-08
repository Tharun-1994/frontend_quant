"""
overlay_apply.py — D1 (morning overlay step)

Parses the trader's substitution CSV and applies overrides to PROPOSED rows
for one strategy on a given intended_trade_date.

CSV format (header required, no other columns):
    original_symbol,action,substitute_symbol,adjusted_capital
    AAPL,elide,,
    MSFT,substitute,GOOG,
    NVDA,adjust_capital,,75000
    TSLA,half_size,,

Action semantics:
  elide          — original PROPOSED row's ledger flips to SYSTEM, status=ELIDED,
                   source_tag=ELIDED. No TRADED-side row.
  substitute     — original PROPOSED row flips to SYSTEM ledger, status=ELIDED,
                   source_tag=SUBSTITUTE. The substitute symbol's
                   SUBSTITUTE_POOL row promotes to PENDING_FILL with
                   substitute_link_id pointing back at the SYSTEM shadow.
  adjust_capital — Patch 101: legacy vas_helper math. REDUCE-ONLY: no-op
                   when qty×ref <= target. Otherwise qty = ceil(qty ×
                   target/(qty×ref)), ref = limit_price (LIMIT/LIMIT_ATR)
                   or inferred capital/qty (NORMAL). capital = new_qty×ref.
                   PROPOSED → PENDING_FILL, source_tag=ADJUSTED.
  half_size      — Patch 101: qty = ceil(qty/2) (legacy np.ceil — odd
                   rounds UP), capital scaled proportionally.
                   PROPOSED → PENDING_FILL, source_tag=ADJUSTED.

Untouched PROPOSED rows stay PROPOSED. Broker-write (D2) auto-promotes
them on its own run (implicit "kept by trader").

Idempotency / re-upload semantics:
  Each call writes new SubstitutionOverride rows with version=max+1 — full
  audit history preserved. However, the tradelist transitions are NOT
  reverted on re-upload — once a row is SYSTEM/ELIDED or ADJUSTED, the next
  CSV can only act on rows still in PROPOSED state. If a trader uploads a
  WRONG v1 and then a fix v2, the v2 will only correct rows it can still
  reach. Recovering from a bad upload requires manual SQL today.

  This trade-off is acceptable for Phase 1 — proper reversible overlay is
  Phase 2 work and needs new columns (original_intended_capital etc.) on
  tradelist to remember pre-adjustment values.

Phase 1 contract:
  - One CSV per (strategy_id, override_date) — no cross-strategy CSV
  - The endpoint takes strategy_id as path parameter
  - Override actions all apply to the same intended_trade_date
  - Substitute symbols must exist in SUBSTITUTE_POOL for the same date
"""

from __future__ import annotations
import csv
import math
import traceback
from datetime import date, datetime
from decimal import Decimal
from io import StringIO
from typing import Optional

from sqlalchemy.orm import Session

from app.models.tradelist import Tradelist
from app.models.substitution_override import SubstitutionOverride
from app.models.strategy_bucket import StrategyBucket
from app.models.eod_run_log import EodRunLog


VALID_ACTIONS = {'elide', 'substitute', 'adjust_capital', 'half_size'}
REQUIRED_CSV_COLUMNS = {'original_symbol', 'action'}

# Vas's real column names → internal names
_VAS_COLUMN_MAP = {
    'original':   'original_symbol',
    'substitute': 'substitute_symbol',
    'capital':    'adjusted_capital',
    # 'action', 'reason_for_action', 'Date', 'System' pass through as-is
}


def apply_overlay(
    db: Session,
    strategy_id: int,
    override_date: date,
    csv_text: str,
    uploaded_by: Optional[str] = None,
    csv_source_path: Optional[str] = None,
) -> dict:
    """Apply trader's substitution CSV to PROPOSED rows for one strategy.

    Args:
        db: SQLAlchemy session.
        strategy_id: which strategy this CSV applies to.
        override_date: intended_trade_date the CSV targets (D in the docs).
        csv_text: full CSV body (header + data rows).
        uploaded_by: who uploaded (optional, for audit).
        csv_source_path: filesystem path of the CSV (optional, for audit).

    Returns:
        Summary dict with per-action counts and eod_run_log id.

    Raises:
        ValueError on malformed CSV, unknown actions, or substitute-without-target.
        Re-raises any DB exception after rollback + FAILED log writeback.
    """
    # Validate strategy exists
    strategy = db.query(StrategyBucket).filter_by(id=strategy_id).first()
    if strategy is None:
        raise ValueError(f'Strategy id={strategy_id} not found')

    # 1. Parse + validate CSV up-front (fail fast before any DB writes)
    actions = _parse_csv(csv_text)

    # 2. Compute next version
    max_v_row = (
        db.query(SubstitutionOverride.version)
        .filter(
            SubstitutionOverride.strategy_id == strategy_id,
            SubstitutionOverride.override_date == override_date,
        )
        .order_by(SubstitutionOverride.version.desc())
        .first()
    )
    version = (max_v_row[0] + 1) if max_v_row else 1

    # 3. Open audit row (committed independently so it survives rollback)
    log_row = EodRunLog(
        run_date=override_date,
        step='overlay_apply',
        strategy_id=strategy_id,
        status='RUNNING',
    )
    db.add(log_row)
    db.commit()

    summary = {
        'eod_run_log_id': log_row.id,
        'strategy_id': strategy_id,
        'override_date': override_date.isoformat(),
        'version': version,
        'overrides_recorded': 0,
        'elided': 0, 'substituted': 0,
        'adjusted_capital': 0, 'half_sized': 0,
        'adjust_reduce_only_noop': 0,   # Patch 101: legacy reduce-only skips
        'skipped_no_match': 0,
    }

    print(f'[overlay_apply] strategy_id={strategy_id} ({strategy.name}) '
          f'override_date={override_date} version={version} actions={len(actions)}')

    try:
        # 4. Record audit rows (one per CSV row)
        for a in actions:
            db.add(SubstitutionOverride(
                strategy_id=strategy_id,
                override_date=override_date,
                version=version,
                original_symbol=a['original_symbol'],
                action=a['action'],
                substitute_symbol=a.get('substitute_symbol'),
                adjusted_capital=a.get('adjusted_capital'),
                reason_for_action=a.get('reason_for_action'),
                csv_source_path=csv_source_path,
                uploaded_by=uploaded_by,
            ))
            summary['overrides_recorded'] += 1

        # 5. Apply each action
        for a in actions:
            outcome = _apply_one_action(db, strategy_id, override_date, a)
            if outcome == 'elide':           summary['elided']            += 1
            elif outcome == 'substitute':    summary['substituted']       += 1
            elif outcome == 'adjust_capital':summary['adjusted_capital']  += 1
            elif outcome == 'adjust_noop':   summary['adjust_reduce_only_noop'] += 1
            elif outcome == 'half_size':     summary['half_sized']        += 1
            elif outcome == 'skipped':       summary['skipped_no_match']  += 1

        db.commit()

        log_row.status = 'SUCCESS'
        log_row.rows_affected = (summary['elided'] + summary['substituted']
                                 + summary['adjusted_capital']
                                 + summary['half_sized'])
        log_row.finished_at = datetime.utcnow()
        db.commit()

        print(f'[overlay_apply] === SUCCESS strategy_id={strategy_id} '
              f'elide={summary["elided"]} sub={summary["substituted"]} '
              f'adj={summary["adjusted_capital"]} half={summary["half_sized"]} '
              f'skipped={summary["skipped_no_match"]} ===')

        return summary

    except Exception as e:
        db.rollback()
        log_row.status = 'FAILED'
        log_row.error_msg = f'{type(e).__name__}: {e}\n{traceback.format_exc()}'
        log_row.finished_at = datetime.utcnow()
        db.commit()
        print(f'[overlay_apply] === FAILED strategy_id={strategy_id} — '
              f'{type(e).__name__}: {e} ===')
        raise


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_csv(csv_text: str) -> list[dict]:
    """Parse CSV text → list of action dicts. Handles both formats:

    Old format (one file per strategy):
        original_symbol, action, substitute_symbol, adjusted_capital

    Vas's combined format (one file, all strategies):
        Date, System, original, substitute, action, capital, reason_for_action

    Auto-detected by presence of 'original' column (Vas) vs 'original_symbol' (old).
    Column names are normalised before processing.
    Action values are case-normalised ('ADJUST_CAPITAL' → 'adjust_capital').

    Returns list of action dicts with keys:
        original_symbol, action, substitute_symbol, adjusted_capital,
        reason_for_action (optional), system_code (optional), override_date (optional)
    """
    reader = csv.DictReader(StringIO(csv_text.strip()))
    if not reader.fieldnames:
        raise ValueError('CSV is empty or has no header row')

    # Normalise header: strip whitespace, apply column name mapping
    raw_fields = [f.strip() for f in reader.fieldnames]
    is_vas_format = 'original' in raw_fields  # Vas uses 'original', old uses 'original_symbol'

    actions = []
    for i, raw_row in enumerate(reader, start=2):
        # Strip whitespace from all values and remap column names
        row: dict = {}
        extras: list = []
        for k, v in raw_row.items():
            # Patch 103: a data row with MORE fields than the header (an
            # UNQUOTED comma in free text, e.g. reason 'concentr 50Y, L1',
            # or capital typed as '2,000') makes csv.DictReader park the
            # spillover under key None as a LIST — which used to crash
            # '.strip()' with a 500. Collect it instead and rejoin below.
            if k is None:
                extras = [str(x).strip() for x in (v or [])]
                continue
            if isinstance(v, list):   # duplicate header names — defensive
                v = ','.join(str(x) for x in v)
            # lstrip BOM: Excel's UTF-8 CSVs prefix the first header cell
            # with \ufeff, silently breaking the 'Date' column match.
            key = (k or '').strip().lstrip('\ufeff')
            key = _VAS_COLUMN_MAP.get(key, key)   # remap Vas's names → internal
            row[key] = (v or '').strip()

        if extras:
            # Spillover from an unquoted comma. Safe to fold into the
            # free-text reason ONLY for rows that carry no capital — for
            # adjust_capital rows we cannot distinguish a comma in the
            # reason from capital typed as '2,000' (which would silently
            # shift columns and size the trade to $2). Hard-fail those
            # with a row-numbered message instead of guessing.
            if (row.get('action', '').strip().lower() == 'adjust_capital'):
                raise ValueError(
                    f'CSV row {i}: unquoted comma detected on an '
                    f'adjust_capital row — ambiguous (comma in reason, or '
                    f'capital typed as "2,000"?). Quote the reason text '
                    f'and write capital without thousands separators.'
                )
            tail = ', '.join(x for x in extras if x)
            base = row.get('reason_for_action', '')
            row['reason_for_action'] = f'{base}, {tail}' if base else tail
            print(f'[overlay_apply] WARN csv row {i}: {len(extras)} unquoted '
                  f'extra field(s) folded into reason_for_action — quote '
                  f'free-text fields containing commas')

        original = row.get('original_symbol', '').upper()
        action   = row.get('action', '').lower()    # case-normalise
        reason   = row.get('reason_for_action') or None
        sys_code = row.get('System') or None

        if not original:
            raise ValueError(f'CSV row {i}: original symbol is empty')
        if action not in VALID_ACTIONS:
            raise ValueError(
                f'CSV row {i}: invalid action {action!r}. '
                f'Must be one of {sorted(VALID_ACTIONS)}'
            )

        sub     = row.get('substitute_symbol', '').upper() or None
        cap_str = row.get('adjusted_capital', '')
        cap     = float(cap_str) if cap_str else None

        # Parse override_date from Date column if present (DD/MM/YYYY UK format)
        override_date_str = row.get('Date', '')
        override_date_parsed = None
        if override_date_str:
            try:
                from datetime import datetime as _dt
                override_date_parsed = _dt.strptime(override_date_str, '%d/%m/%Y').date()
            except ValueError:
                pass   # caller-supplied override_date takes precedence

        if action == 'substitute' and not sub:
            raise ValueError(
                f'CSV row {i}: action=substitute requires substitute_symbol')
        if action == 'adjust_capital' and cap is None:
            raise ValueError(
                f'CSV row {i}: action=adjust_capital requires adjusted_capital')

        actions.append({
            'original_symbol':   original,
            'action':            action,
            'substitute_symbol': sub,
            'adjusted_capital':  cap,
            'reason_for_action': reason,
            'system_code':       sys_code,
            'override_date':     override_date_parsed,
        })

    return actions


def _apply_one_action(
    db: Session,
    strategy_id: int,
    override_date: date,
    action_row: dict,
) -> str:
    """Apply one CSV action. Returns the action name applied, or 'skipped'.

    Raises ValueError when an action's preconditions are violated (e.g.,
    substitute target not in SUBSTITUTE_POOL).
    """
    original = action_row['original_symbol']
    action = action_row['action']

    # Find PROPOSED row for original symbol
    original_row = (
        db.query(Tradelist)
        .filter(
            Tradelist.strategy_id == strategy_id,
            Tradelist.intended_trade_date == override_date,
            Tradelist.ledger == 'TRADED',
            Tradelist.status == 'PROPOSED',
            Tradelist.symbol == original,
        )
        .first()
    )

    if original_row is None:
        # No matching PROPOSED row — silently skip. Trader CSV may name a
        # symbol that's already been actioned (e.g., re-upload).
        print(f'[overlay_apply]   {original} action={action} → SKIPPED '
              f'(no PROPOSED row for this symbol on {override_date})')
        return 'skipped'

    if action == 'elide':
        original_row.ledger     = 'SYSTEM'
        original_row.status     = 'ELIDED'
        original_row.source_tag = 'ELIDED'
        print(f'[overlay_apply]   {original} → ELIDED (ledger flipped to SYSTEM)')
        return 'elide'

    if action == 'substitute':
        sub_symbol = action_row['substitute_symbol']
        sub_row = (
            db.query(Tradelist)
            .filter(
                Tradelist.strategy_id == strategy_id,
                Tradelist.intended_trade_date == override_date,
                Tradelist.ledger == 'TRADED',
                Tradelist.status == 'SUBSTITUTE_POOL',
                Tradelist.symbol == sub_symbol,
            )
            .first()
        )
        if sub_row is None:
            raise ValueError(
                f"Action substitute for {original} → {sub_symbol}: target symbol "
                f"{sub_symbol!r} not in SUBSTITUTE_POOL for strategy_id="
                f"{strategy_id} on {override_date}. Trader must pick from the "
                f"system's substitute pool only."
            )

        # Flip original to SYSTEM ledger
        original_row.ledger     = 'SYSTEM'
        original_row.status     = 'ELIDED'
        original_row.source_tag = 'SUBSTITUTE'
        db.flush()   # need original_row.id committed for FK link

        # Promote substitute → PENDING_FILL with link
        sub_row.status              = 'PENDING_FILL'
        sub_row.source_tag          = 'SUBSTITUTE'
        sub_row.substitute_link_id  = original_row.id

        print(f'[overlay_apply]   {original} → SUBSTITUTE → {sub_symbol} '
              f'(original→SYSTEM/ELIDED, sub→PENDING_FILL, link_id={original_row.id})')
        return 'substitute'

    if action == 'adjust_capital':
        # Patch 101: exact legacy vas_helper math.
        #   ref      = LmtPrice when the row has one (LIMIT/LIMIT_ATR);
        #              inferred capital/qty for NORMAL rows (no limit stored).
        #   total    = qty × ref            (current committed capital)
        #   REDUCE-ONLY: if total <= target → NO-OP (legacy only shrinks;
        #              upsizing was never a trader capability).
        #   factor   = target / total
        #   new_qty  = ceil(qty × factor)   (legacy np.ceil)
        #   capital  = new_qty × ref        (true exposure — matches broker
        #              file, so next-day FILLED holdings reconcile with IB).
        target = Decimal(str(action_row['adjusted_capital']))
        old_qty = int(original_row.intended_qty or 0)
        limit_p = original_row.limit_price
        if limit_p and Decimal(limit_p) > 0:
            ref_price = Decimal(limit_p)
        elif old_qty > 0 and original_row.intended_capital:
            ref_price = Decimal(original_row.intended_capital) / Decimal(old_qty)
        else:
            ref_price = Decimal(0)

        total = ref_price * old_qty
        if total <= 0 or old_qty <= 0:
            print(f'[overlay_apply]   {original} → ADJUST_CAPITAL SKIPPED '
                  f'(qty={old_qty}, ref={ref_price} — nothing to size)')
            return 'skipped'
        if total <= target:
            # Legacy reduce-only: leave the row untouched (stays PROPOSED;
            # broker_write auto-promotes it unchanged). Audit row already
            # recorded above.
            print(f'[overlay_apply]   {original} → ADJUST_CAPITAL NO-OP '
                  f'(current {total:.2f} <= target {target:.2f}; reduce-only, '
                  f'matching legacy vas_helper)')
            return 'adjust_noop'

        factor  = target / total
        new_qty = int(math.ceil(old_qty * float(factor)))   # legacy np.ceil
        new_capital = (ref_price * new_qty).quantize(Decimal('0.01'))

        original_row.intended_capital = new_capital
        original_row.intended_qty     = new_qty
        original_row.status           = 'PENDING_FILL'
        original_row.source_tag       = 'ADJUSTED'
        print(f'[overlay_apply]   {original} → ADJUST_CAPITAL target={target} '
              f'ref={ref_price:.4f} qty {old_qty}→{new_qty} '
              f'capital→{new_capital} → PENDING_FILL')
        return 'adjust_capital'

    if action == 'half_size':
        # Patch 101: legacy np.ceil(qty / 2) — odd quantities round UP
        # (37 → 19), matching every M_Combined Vas has ever produced.
        # Capital scales proportionally to the actual new qty so intended
        # exposure stays consistent with the broker file and next-day
        # FILLED holdings reconcile with IB.
        old_qty = int(original_row.intended_qty or 0)
        new_qty = int(math.ceil(old_qty / 2)) if old_qty > 0 else 0
        if old_qty > 0 and original_row.intended_capital:
            original_row.intended_capital = (
                Decimal(original_row.intended_capital) * new_qty / old_qty
            ).quantize(Decimal('0.01'))
        original_row.intended_qty     = new_qty
        original_row.status           = 'PENDING_FILL'
        original_row.source_tag       = 'ADJUSTED'
        print(f'[overlay_apply]   {original} → HALF_SIZE qty {old_qty}→{new_qty} '
              f'(legacy ceil) → PENDING_FILL')
        return 'half_size'

    return 'skipped'  # unreachable