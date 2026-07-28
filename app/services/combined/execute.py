# Patch 147: combined EXECUTION leg (evening D1 for the combined book).
#
# What this is:
#   simulate.py answers "what WOULD this combined have traded historically"
#   by feeding member BACKTEST tradelists (fills) through run_allocator.
#   execute.py answers "what SHOULD strategy {combined_id} order TOMORROW"
#   by feeding member EXEC-STEP candidate lists (tomorrow's ranked orders,
#   not fills) through the SAME parity-validated primitives:
#     - _resolve_multiplier   (allocator.py — the ladder, verbatim import)
#     - build_gate_series / gate_for_entry_day (market_gate.py — IBS gate)
#     - prepare_combined_inputs (prepare_inputs.py — mtime-fresh SPY data)
#   and writes the result as PROPOSED (+SUBSTITUTE_POOL) rows for the
#   combined strategy via proposed_inserter — after which the EXISTING
#   morning machinery (broker_write → basket) needs nothing new.
#
# Why members are NOT execution_enabled:
#   payload_builder/eod both hard-fail on execution_enabled=False, because
#   an enabled strategy emits its OWN basket rows. Members are scouts: the
#   combined re-sizes every trade from its own pot and must be the only
#   emitter. So this module calls build_execution_step_payload with
#   allow_disabled=True (Patch 147 param, default False — no behavior
#   change for every existing caller) and execution_mode=False so member
#   candidate lists are computed at regime capital (backtest scale),
#   keeping list composition identical to the parity-validated backtests.
#
# Sizing profile:
#   Uses cfg.production_capital / cfg.production_min_to_enter (Patch 146)
#   — NOT cfg.capital/min_to_enter, which stay the research profile that
#   Simulate runs. Skip rule mirrors legacy: qty <= min.
#
# curr_hold semantics in exec mode (documented divergence-by-design):
#   The backtest walk increments curr_hold per FILL row because its inputs
#   are fills. Live legacy (vas_helper) walks member ORDER lists, i.e.
#   occurrence counting — and the 2026-07-09 parity diff proved legacy
#   counts CANDIDATE occurrences (count_basis=candidates, 8-row proof).
#   This walk therefore increments curr_hold per processed candidate,
#   accepted or skipped. The nightly legacy-vs-webapp order CSV diffs are
#   the empirical check on this choice.
#
# Loud failure everywhere: every error names the exact path/value tried
# and, where known, the fix.

from __future__ import annotations

import json
import logging
import math
import os
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import requests
from sqlalchemy.orm import Session

from app.Settings import settings
from app.constants.PricePath import PricePath
from app.models import StrategyBucket
from app.models.market_regime import MarketRegime
from app.models.combined_system import CombinedMember, CombinedConfig
from app.schemas.Combined import (CombinedAllocationConfig, MemberOverrides,
                                  normalize_combined_config)
from app.services.combined.allocator import _resolve_multiplier
# Patch 152: GateDataError propagates from build_gate_series to the route's
# own except clause — no local reference, import removed (pyflakes-clean).
from app.services.combined.market_gate import (build_gate_series,
                                               gate_for_entry_day)
# Patch 148: exec reads gate/closes from exec_data/{ymd}/{universe} (fresh by
# construction after Step 1) — prepare_combined_inputs stays Simulate-only.
from app.services.position_manager.payload_builder import (
    build_execution_step_payload, _next_trading_day)
from app.services.position_manager.live_seed_builder import (
    build_live_holdings_seed)
from app.services.position_manager.proposed_inserter import (
    insert_proposed_rows)
from app.services.position_manager.runner import ENGINE_HTTP_TIMEOUT_SEC

logger = logging.getLogger(__name__)


class CombinedExecError(RuntimeError):
    """Loud, operator-actionable failure in the combined execution leg."""


# ---------------------------------------------------------------------------
# Small loud helpers
# ---------------------------------------------------------------------------

def _strategy_dir(name: str) -> str:
    return f"{settings.BACKTEST_DATA_PATH}/{name}"


def _resolve_data_root(run_date: date, data_root: Optional[str]) -> str:
    """Mirror routes/eod.py: default exec_data/{YYYYMMDD}, fail fast."""
    root = data_root or str(
        Path(PricePath.backtestPath) / "exec_data" / run_date.strftime("%Y%m%d"))
    if not Path(root).exists():
        raise CombinedExecError(
            f"data_root folder not found: {root}. Run exec_data_refresh for "
            f"{run_date} first, or pass an explicit data_root.")
    return root


def _today_close(closes: pd.DataFrame, symbol: str, run_date: date) -> float:
    """Sizing close for exec: the RUN-DATE bar (which is prev-close relative
    to the intended trade day). Mirrors allocator._prev_close semantics —
    'sizing on prev close' (Portfolio_.py: daily_closes.iloc[index_today-1])
    — but anchored on run_date because tomorrow's bar does not exist yet."""
    if symbol not in closes.columns:
        raise CombinedExecError(
            f"Sizing close missing: symbol {symbol} not in DAILY_closes "
            f"columns (have {len(closes.columns)} symbols)")
    ser = closes[symbol].loc[:pd.Timestamp(run_date)].dropna()
    if ser.empty:
        raise CombinedExecError(
            f"Sizing close missing: no bar <= {run_date} for {symbol}")
    ts, val = ser.index[-1], float(ser.iloc[-1])
    if ts.date() != run_date:
        raise CombinedExecError(
            f"Sizing close stale for {symbol}: last bar {ts.date()} != "
            f"run_date {run_date}. Refresh the member universe parquets "
            f"(nightly chain) before combined execute.")
    if val <= 0 or pd.isna(val):
        raise CombinedExecError(
            f"Bad sizing close for {symbol} at {run_date}: {val}")
    return val


def _ensure_gate_ohlc(gate_dir: str, data_root: str, ticker: str,
                      run_date: date) -> None:
    """Patch 154: materialize the gate ticker's dedicated OHLC parquets
    (DAILY_{closes,highs,lows}_{ticker}.parquet) into the exec universe
    folder, because market_gate's loader is dedicated-or-die.

    Source order, freshest first:
      1. already present AND covering run_date -> done (idempotent)
      2. this universe's wide DAILY_{field}.parquet carries a {ticker}
         column -> extract
      3. the gate ticker's own exec universe folder
         {data_root}/{ticker}/DAILY_{field}.parquet — generated by the
         Patch-154 fan-in in exec_data_refresh
    Anything else -> CombinedExecError listing every path tried.
    """
    fields = ("closes", "highs", "lows")
    ts = pd.Timestamp(run_date)

    def _covers(path: str) -> bool:
        if not os.path.exists(path):
            return False
        idx = pd.to_datetime(pd.read_parquet(path).index)
        return len(idx) > 0 and idx.max() >= ts

    if all(_covers(os.path.join(gate_dir, f"DAILY_{f}_{ticker}.parquet"))
           for f in fields):
        return

    for f in fields:
        dst = os.path.join(gate_dir, f"DAILY_{f}_{ticker}.parquet")
        tried: List[str] = []
        frame = None
        wide = os.path.join(gate_dir, f"DAILY_{f}.parquet")
        if os.path.exists(wide):
            wdf = pd.read_parquet(wide)
            col = next((c for c in wdf.columns
                        if str(c).lower() == ticker.lower()), None)
            if col is not None:
                frame = wdf[[col]].rename(columns={col: ticker})
            else:
                tried.append(f"{wide} (no '{ticker}' column)")
        else:
            tried.append(wide)
        if frame is None:
            tuni = os.path.join(data_root, ticker, f"DAILY_{f}.parquet")
            if os.path.exists(tuni):
                tdf = pd.read_parquet(tuni)
                col = next((c for c in tdf.columns
                            if str(c).lower() == ticker.lower()),
                           tdf.columns[0])
                frame = tdf[[col]].rename(columns={col: ticker})
            else:
                tried.append(tuni)
        if frame is None:
            raise CombinedExecError(
                f"Gate ticker OHLC unavailable for '{ticker}' (field "
                f"'{f}'). Tried: " + "; ".join(tried) + ". Fix: "
                f"exec_data_refresh (Patch 154) generates "
                f"{os.path.join(data_root, ticker)} whenever the combined "
                f"is execution_enabled — re-run the refresh for this "
                f"date, then retry.")
        frame.index = pd.to_datetime(frame.index)
        frame.to_parquet(dst)


def _call_member_step(payload: dict[str, Any]) -> dict[str, Any]:
    """POST one member payload to the engine's single-bar endpoint
    (signals/last-bar — Patch 160 cutover from the deprecated step/single)."""
    # Patch 160: cutover to the Phase-B single-bar endpoint — the engine's
    # own Patch-31 comment marks step/single as the deprecated day-loop
    # path kept only "until middleware cuts over". signals/last-bar is what
    # the per-strategy PM (runner.py) already posts nightly, evaluates ONLY
    # the last bar (~200ms vs ~30s re-simulation), and is where the
    # Patch-148 intended-date tdom ban and Patch-160 vol seeding live.
    url = f"{settings.BACKTEST_JAVA_URL}/api/execution/signals/last-bar"
    logger.info(f"[combined-exec] POST {url} strategy="
                f"{payload.get('strategy', {}).get('id', '?')}")
    resp = requests.post(url, json=payload, timeout=ENGINE_HTTP_TIMEOUT_SEC)
    if resp.status_code != 200:
        raise CombinedExecError(
            f"engine signals/last-bar returned {resp.status_code} for "
            f"member payload: {resp.text[:500]}")
    try:
        return resp.json()
    except ValueError as e:
        raise CombinedExecError(
            f"engine signals/last-bar returned non-JSON body: {e}: "
            f"{resp.text[:300]}")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def execute_combined(db: Session, combined_id: int,
                     run_date: Optional[date] = None,
                     data_root: Optional[str] = None) -> dict[str, Any]:
    """Evening execution step for one combined strategy.

    Sequence:
      1. Preflight: combined row, ONE regime row (proposed_inserter needs
         regime.slots / regime.substitute_pool_size), active members, config.
      2. Per member (priority order): engine signals/last-bar (Patch 160) via
         build_execution_step_payload(allow_disabled=True,
         execution_mode=False) → tomorrow's ranked candidates.
      3. Gate for the intended trade day (IBS on the run-date bar) with a
         freshness assertion.
      4. Allocation walk at the PRODUCTION profile — same ladder resolution
         as Simulate (_resolve_multiplier import).
      5. insert_proposed_rows for the combined (idempotent per date; called
         even when gated so a re-run clears stale rows), commit.

    Returns an audit dict; raises CombinedExecError / GateDataError with
    operator-actionable messages on any inconsistency.
    """
    run_date = run_date or date.today()

    # ── 1. Preflight ─────────────────────────────────────────────────────
    combined = db.get(StrategyBucket, combined_id)
    if combined is None:
        raise CombinedExecError(f"No StrategyBucket with id={combined_id}")

    regimes = (db.query(MarketRegime)

               .filter_by(strategy_id=combined_id).all())

    # ── Step 0.5 Patch 182 — resolution leg for the combined book ──────
    # The Patch-148/150 divert sent combined books straight to this
    # decision leg; Steps A/A.5/A.7/A.8 never ran for them, so their
    # PENDING_FILL rows could NEVER resolve (strategy 34's zombie rows,
    # strategy 35's frozen basket). Resolve TODAY'S fills / exit fills /
    # stop hits / eod_close completions before deciding tomorrow.
    from app.services.position_manager.runner import run_resolution_steps
    _r0 = regimes[0] if regimes else None
    if _r0 is not None:
        resolution_summary = run_resolution_steps(
            db, strategy_id=combined_id, run_date=run_date,
            data_root=data_root, universe=_r0.universe,
            rebalance='DAILY')
    else:
        resolution_summary = {}
    if not regimes:
        raise CombinedExecError(
            f"Combined strategy {combined_id} ({combined.name}) has NO "
            f"marketregime row. proposed_inserter resolves slots and "
            f"substitute_pool_size from it. Insert one minimal row: "
            f"strategy_id={combined_id}, capital=<production capital>, "
            f"slots=<base_slots>, substitute_pool_size=<pool depth>.")

    # Patch 152: config load moved ABOVE the Patch-148 preflight — the
    # production-capital cross-check below reads cfg, which was previously
    # assigned only after it (UnboundLocalError on the first replay run).
    cfg_row = db.query(CombinedConfig).filter(
        CombinedConfig.combined_strategy_id == combined_id).first()
    if cfg_row is None:
        raise CombinedExecError(
            f"Combined {combined_id} has no saved config — Save on the "
            f"Combined System tab first.")
    cfg: CombinedAllocationConfig = normalize_combined_config(
        json.loads(cfg_row.config_json or "{}"))

    if len(regimes) > 1:
        raise CombinedExecError(
            f"Combined strategy {combined_id} has {len(regimes)} regime rows — "
            f"exactly one is expected (it carries slots/pool/exit semantics "
            f"for the basket). Remove the extras.")
    reg = regimes[0]
    universe = (reg.universe or "").strip()
    if not universe:
        raise CombinedExecError(
            f"Combined regime row (id={reg.id}) has no universe — set "
            f"universe='russell3000' so exec_data_refresh builds its parquets "
            f"and the gate can read SPY from them.")
    # Patch 148: two production-capital homes exist by design — the regime
    # column feeds the orchestrator funding gate + basket scale; the
    # combined_config field (Patch 146) feeds THIS allocator. They must agree.
    if reg.production_capital is not None and abs(
            float(reg.production_capital) - float(cfg.production_capital)) > 0.01:
        raise CombinedExecError(
            f"production_capital mismatch: marketregime.production_capital="
            f"{float(reg.production_capital)} but combined_config."
            f"production_capital={float(cfg.production_capital)} — set them "
            f"equal (regime row + Combined System tab).")

    member_rows: List[CombinedMember] = (
        db.query(CombinedMember)
        .filter(CombinedMember.combined_strategy_id == combined_id,
                CombinedMember.is_active == True)  # noqa: E712 — SQL Server (Patch 42)
        .order_by(CombinedMember.priority).all())
    if not member_rows:
        raise CombinedExecError(
            f"Combined {combined_id} has no active members — nothing to "
            f"execute. Add subsystems on the Combined System tab.")

    root = _resolve_data_root(run_date, data_root)
    intended = _next_trading_day(run_date)

    members: List[dict] = []
    for m in member_rows:
        srow = db.get(StrategyBucket, m.member_strategy_id)
        if srow is None:
            raise CombinedExecError(
                f"Member strategy id={m.member_strategy_id} of combined "
                f"{combined_id} not found in strategies_bucket.")
        ov = json.loads(m.overrides_json) if m.overrides_json else None
        # Patch 162: subsystem reference — REQUIRED, from the member's
        # system_code only (design decision: "this can be taken from the
        # system code of each subsystem"). Loud when missing; no name
        # parsing, no fallbacks.
        _m_ref = (srow.system_code or "").strip()
        if not _m_ref:
            raise CombinedExecError(
                f"Member strategy {srow.id} ({srow.name}) has no "
                f"system_code — the combined's OrderRef suffix "
                f"(e.g. M_LDEQ_54_1) and substitute-pool tagging derive "
                f"from it. Set system_code ('1'/'5'/'6' for the CRDT "
                f"members) on the member strategy, then re-run.")
        # Patch 164: per-member slot + pool sizes from the MEMBER's own
        # regime row. The engine returns the FULL ranked list (SBE header:
        # "full ranked list — middleware splits"), so the split is OURS:
        # the first `slots` ACCEPTED candidates are the member's mains,
        # the next `substitute_pool_size` its SUB block — "A should have
        # 8 main and the rest as subs, same for the other two systems".
        m_regime = (db.query(MarketRegime)
                    .filter_by(strategy_id=m.member_strategy_id).first())
        if m_regime is None:
            raise CombinedExecError(
                f"Member strategy {srow.id} ({srow.name}) has no "
                f"marketregime row — cannot derive its slots / "
                f"substitute_pool_size for the combined split.")
        members.append({
            "strategy_id": m.member_strategy_id,
            "strategy_name": srow.name,
            "subsystem_ref": _m_ref,
            "slots": int(m_regime.slots or cfg.base_slots),
            "pool_size": int(m_regime.substitute_pool_size or 0),
            "priority": m.priority,
            "seed_source_ids": json.loads(m.seed_sources_json or "[]"),
            "overrides": MemberOverrides(**ov) if ov else None,
        })

    # ── 2. Member candidates from the engine ─────────────────────────────
    candidates: Dict[int, List[dict]] = {}
    for m in members:
        sid = m["strategy_id"]
        seed = build_live_holdings_seed(db, sid)   # exec-disabled ⇒ [] today
        payload = build_execution_step_payload(
            db=db, strategy_id=sid, run_date=run_date,
            live_holdings=seed, data_root=root,
            execution_mode=False,      # regime capital: list == backtest list
            allow_disabled=True,       # Patch 147: members are scouts
        )
        resp = _call_member_step(payload)
        ents = resp.get("proposedEntries") or []
        ents.sort(key=lambda c: c.get("rank") if c.get("rank") is not None
                  else 10_000)
        candidates[sid] = ents
        # Patch 164 (corrects 162's assumption): SBE's proposedEntries IS
        # the full ranked list; its substitutePool is not the split. The
        # per-member SUB block is derived below from the list tail. Engine
        # pool size logged for visibility only.
        logger.info(f"[combined-exec] member {sid} ({m['strategy_name']}): "
                    f"{len(ents)} ranked candidates "
                    f"(engine substitutePool={len(resp.get('substitutePool') or [])}, unused)")

    # ── 3. Gate for the intended trade day ───────────────────────────────
    # Patch 148/154: read gate data from the exec_data universe folder —
    # refreshed by Step 1, fresh by construction. Patch 154 correction:
    # market_gate._load_ohlc_f64 requires DEDICATED
    # DAILY_{field}_{ticker}.parquet files and has NO wide-column fallback
    # (the Patch-148 comment claiming one was wrong — Simulate only works
    # because prepare_combined_inputs materializes these). We materialize
    # them here from the freshest available source.
    gate_dir = os.path.join(root, universe)
    if not os.path.isdir(gate_dir):
        raise CombinedExecError(
            f"exec universe folder missing: {gate_dir}. exec_data_refresh "
            f"builds it for universes touched by execution-enabled "
            f"strategies — is the combined's regime.universe set and did "
            f"Step 1 run for {run_date}?")
    _ensure_gate_ohlc(gate_dir, root,
                      (cfg.market_conditions.ticker or "spy").lower(),
                      run_date)
    gates = build_gate_series(gate_dir, cfg)
    valid = gates[gates["valid"]]
    if valid.empty:
        raise CombinedExecError(
            "No valid gate bars (condition-label rules not warm?) in "
            f"{gate_dir}")
    last_bar = valid.index[-1].date()
    if last_bar != run_date:
        raise CombinedExecError(
            f"SPY gate data ends {last_bar} but run_date is {run_date} — "
            f"the T-1 gate bar for {intended} is missing. Refresh the "
            f"member universe parquets (nightly chain), then re-run.")
    g = gate_for_entry_day(gates, pd.Timestamp(intended))
    gate_open = (not cfg.gate.enabled) or bool(g.trade_open)

    # ── 4. Allocation walk (production profile) ──────────────────────────
    # Mirrors allocator.run_allocator's day body (allocator.py:72-115):
    # same _resolve_multiplier, same sizing formula, same <= min skip —
    # with production_capital/production_min_to_enter (Patch 146) and
    # candidate-occurrence curr_hold (see module docstring).
    orders: List[dict] = []
    if gate_open:
        closes = pd.read_parquet(os.path.join(gate_dir,
                                              "DAILY_closes.parquet"))
        closes.index = pd.to_datetime(closes.index)
        slot_cash = cfg.production_capital / cfg.base_slots
        curr_hold: Dict[str, int] = {}
        slots_used = 0
        day_symbols: Dict[int, List[str]] = {
            m["strategy_id"]: [c["symbol"] for c in
                               candidates[m["strategy_id"]]]
            for m in members}

        for m in members:
            sid = m["strategy_id"]
            ov = m["overrides"]
            seed_ct: Dict[str, int] = {}
            for src in m["seed_source_ids"]:
                for sym in day_symbols.get(src, []):
                    seed_ct[sym] = seed_ct.get(sym, 0) + 1

            # Patch 164: this member contributes at most m["slots"] MAINS
            # (accepted rows — min-skips do not consume a slot, the next
            # ranked candidate steps up, exactly like slot-filling in the
            # backtest). Where the mains stop, the SUB block begins.
            accepted_for_member = 0
            m["_pool_start"] = len(candidates[sid])
            for _ci, c in enumerate(candidates[sid]):
                if accepted_for_member >= m["slots"]:
                    m["_pool_start"] = _ci
                    break
                if slots_used >= cfg.max_slots:
                    m["_pool_start"] = _ci
                    break
                sym = c["symbol"]
                held = curr_hold.get(sym, 0)
                if held >= cfg.max_per_ticker:
                    continue
                mult = _resolve_multiplier(cfg, g.label, held,
                                           seed_ct.get(sym, 0), ov)
                # occurrence counting — see module docstring
                curr_hold[sym] = held + 1
                size = (slot_cash / cfg.slot_divisor) * mult
                pc = _today_close(closes, sym, run_date)
                qty = math.floor(size / pc)
                if qty <= cfg.production_min_to_enter:   # legacy: <=
                    continue
                if c.get("limitPrice") is None:
                    # Patch 175: message rewritten — the old text ("must be
                    # LIMIT/LIMIT_ATR") predated LIMIT_HV and sent debugging
                    # toward order types when the real cause is DATA: the
                    # engine prices every limit-family candidate it has
                    # inputs for; a null here means the pricing input was
                    # missing from THIS run_date's exec folder.
                    raise CombinedExecError(
                        f"Member {sid} candidate {sym} has no limitPrice "
                        f"(order type {c.get('orderType')}) — "
                        f"Tradelist.limit_price is NOT NULL. For LIMIT_HV "
                        f"this means hv_limit.parquet is missing/stale in "
                        f"exec_data/<run_date>/<universe>/ (regenerate the "
                        f"exec refresh FOR THIS run_date); for LIMIT/"
                        f"LIMIT_ATR check closes/atr inputs the same way.")
                slots_used += 1
                accepted_for_member += 1
                orders.append({
                    "symbol": sym,
                    "direction": c.get("direction", "LONG"),
                    "orderType": c.get("orderType"),
                    "entryDate": str(intended),
                    "entryTiming": c.get("entryTiming"),
                    "entryReason": (f"Combined m{sid} "
                                    f"({m['strategy_name']}): "
                                    f"{c.get('entryReason') or 'candidate'}"
                                    f" ×{mult}"),
                    "quantity": qty,
                    "capital": round(qty * pc, 2),
                    "sector": c.get("sector"),
                    # Patch 162: rank is the SUBSYSTEM'S OWN ranking (the
                    # engine's per-member rank), not a global sequence —
                    # three rank-1 rows (one per member) is the intended
                    # legacy semantics.
                    "rank": c.get("rank"),
                    "subsystemRef": m["subsystem_ref"],
                    "score": c.get("score"),
                    "limitPrice": c.get("limitPrice"),
                    "stopPrice": c.get("stopPrice"),
                    "tpPrice": c.get("tpPrice"),
                })

    # ── 4b. Per-subsystem substitute pool (Patch 162/164) ────────────────
    # Each member's SUB block = the ranked-list tail AFTER its accepted
    # mains, capped by THAT member's substitute_pool_size (20). Tagged with
    # its system_code, carrying its OWN engine rank. Sized at the
    # production profile with the BASE multiplier — a substitute steps into
    # a failed first-touch slot. Min-skips do not consume a pool slot.
    # Gate closed ⇒ no pool either (the insert below still clears stale
    # rows).
    pool_orders: List[dict] = []
    if gate_open:
        for m in members:
            sid = m["strategy_id"]
            pool_added = 0
            for c in candidates[sid][m.get("_pool_start",
                                           len(candidates[sid])):]:
                if pool_added >= m["pool_size"]:
                    break
                sym = c["symbol"]
                if c.get("limitPrice") is None:
                    raise CombinedExecError(
                        f"Member {sid} substitute {sym} has no limitPrice "
                        f"— Tradelist.limit_price is NOT NULL.")
                pc = _today_close(closes, sym, run_date)
                size = (slot_cash / cfg.slot_divisor) * cfg.ladder.base
                qty = math.floor(size / pc)
                if qty <= cfg.production_min_to_enter:
                    continue
                pool_added += 1
                pool_orders.append({
                    "symbol": sym,
                    "direction": c.get("direction", "LONG"),
                    "orderType": c.get("orderType"),
                    "entryDate": str(intended),
                    "entryTiming": c.get("entryTiming"),
                    "entryReason": (f"Combined m{sid} "
                                    f"({m['strategy_name']}) substitute: "
                                    f"{c.get('entryReason') or 'pool'}"),
                    "quantity": qty,
                    "capital": round(qty * pc, 2),
                    "sector": c.get("sector"),
                    "rank": c.get("rank"),
                    "subsystemRef": m["subsystem_ref"],
                    "score": c.get("score"),
                    "limitPrice": c.get("limitPrice"),
                    "stopPrice": c.get("stopPrice"),
                    "tpPrice": c.get("tpPrice"),
                })

    # ── 5. Insert (idempotent — also clears stale rows on gated days) ────
    try:
        res = insert_proposed_rows(
            db=db, strategy_id=combined_id,
            intended_trade_date=intended, proposed_orders=orders,
            active_regime_label=None, proposal_date=run_date,
            substitute_orders=pool_orders)   # Patch 162: explicit channel
        db.commit()
    except Exception:
        db.rollback()
        raise

    audit = {
        "combined_id": combined_id,
        "combined_name": combined.name,
        "run_date": str(run_date),
        "intended_trade_date": str(intended),
        "gate": {"open": gate_open, "label": str(g.label),
                 "ibs": round(float(g.ibs), 4),
                 "enabled": bool(cfg.gate.enabled)},
        "production_capital": cfg.production_capital,
        "production_min_to_enter": cfg.production_min_to_enter,
        "member_candidates": {m["strategy_id"]:
                              len(candidates[m["strategy_id"]])
                              for m in members},
        "member_mains": {m["subsystem_ref"]: sum(
            1 for o in orders
            if o.get("subsystemRef") == m["subsystem_ref"])
            for m in members},                # Patch 164
        "member_pools": {m["subsystem_ref"]: sum(
            1 for o in pool_orders
            if o.get("subsystemRef") == m["subsystem_ref"])
            for m in members},                # Patch 164: derived SUB blocks
        "orders_allocated": len(orders),
        **res,
        **resolution_summary,   # Patch 182: resolution counts in the banner
    }
    logger.info(f"[combined-exec] {json.dumps(audit)}")
    return audit