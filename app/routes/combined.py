# Patch 126: combined-system endpoints. Configs are normalized on every read
# (normalize_combined_config), so old bull/bear configs auto-convert to the
# labelled v2 shape and the UI only ever sees v2. gate-today returns the
# active label.
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import StrategyBucket, UniverseTickerExclusion
from app.models.combined_system import CombinedMember, CombinedConfig
from app.models.market_regime import MarketRegime   # Patch 149
from app.schemas.Combined import (CombinedSaveRequest, MemberOverrides,
                                  normalize_combined_config)
from app.services.combined.simulate import simulate_combined
from app.Settings import settings

router = APIRouter(prefix="/api/combined", tags=["combined"])


_EMPTY_TREE_JSON = '{"type":"group","id":"root","logic":"AND","children":[]}'


def _upsert_combined_regime(db: Session, combined_id: int, cfg,
                            member_ids: list[int]) -> dict:
    """Patch 149: Save owns the combined's marketregime row.

    Why the row exists at all: proposed_inserter resolves slots /
    substitute_pool_size from it, the orchestrator's Patch-89 funding gate
    checks its production_capital, get_equity baselines from its capital,
    exec_data_refresh derives universes from it, and broker_basket_builder
    reads its exit/stop semantics for strategy-34 bracket children.

    Instead of a manual SQL insert per combined, Save DERIVES it:
      - sizing/capital fields  <- the combined config on this page
        (slots = cfg.max_slots: proposed_inserter truncates PROPOSED to
        free_slots = slots - LIVE, and the combined can allocate up to
        members x per-member slots per day — never let it truncate)
      - bracket semantics      <- the MEMBER regimes, with LOUD failure if
        members disagree (a combined mixing e.g. eod_close and overnight
        members cannot share one bracket template)
      - calendars              <- EMPTY on purpose: members enforce their
        own tdom/banned-month rules during candidate generation
        (SingleBarEvaluatorImpl Patch 148); the combined must not
        double-apply them.

    Upsert is UPDATE-in-place when a row exists — tradelist rows FK its id
    (entered_regime_id), so the row is never deleted/recreated. On update,
    substitute_pool_size is preserved (manually tunable until the
    REPLACE-depth item lands).
    """
    # Patch 168: order_type removed from strict derivation — see the
    # family check below the derive loop.
    derive_fields = ("entry_timing", "exit_timing", "stoploss_type",
                     "stoploss_pct", "stoploss_timing",
                     "universe")
    member_regimes: dict[int, MarketRegime] = {}
    for mid in member_ids:
        r = db.query(MarketRegime).filter_by(strategy_id=mid).first()
        if r is None:
            raise HTTPException(400, (
                f"member strategy {mid} has no marketregime row — the "
                f"combined derives its bracket semantics from members, so "
                f"every member must be a fully configured strategy first."))
        member_regimes[mid] = r

    def _norm(field, val):
        if field == "stoploss_pct":
            return round(float(val or 0), 4)
        return (str(val or "")).strip()

    derived = {}
    for f in derive_fields:
        seen = {mid: _norm(f, getattr(r, f)) for mid, r in member_regimes.items()}
        if len(set(seen.values())) > 1:
            raise HTTPException(400, (
                f"members disagree on regime.{f}: "
                + ", ".join(f"strategy {mid}={v!r}" for mid, v in seen.items())
                + " — a combined book needs one bracket template; align the "
                  "members or split the combined."))
        derived[f] = next(iter(seen.values()))

    # Patch 168: order_type is NOT required to be identical across members.
    # The bracket template's SHAPE comes from entry/exit/stop timing (the
    # fields above); order_type only selects how each member computes its
    # own limit price, and legacy freely mixes flat-pct and HV-scaled
    # limits in one basket (M_SDEQ_52: 1.75% / 2.5% / HV-clamped). What
    # must still agree is the FAMILY: a NORMAL (market-order) member
    # cannot share a bracket with limit members — the parent row type
    # would differ.
    _LIMIT_FAMILY = {"LIMIT", "LIMIT_ATR", "LIMIT_HV"}
    ot_seen = {mid: (str(r.order_type or "")).strip().upper()
               for mid, r in member_regimes.items()}
    fam = {mid: ("LIMIT_FAMILY" if v in _LIMIT_FAMILY else v)
           for mid, v in ot_seen.items()}
    if len(set(fam.values())) > 1:
        raise HTTPException(400, (
            "members disagree on order-type FAMILY: "
            + ", ".join(f"strategy {mid}={v!r}" for mid, v in ot_seen.items())
            + " — market-order (NORMAL) members cannot share a bracket "
              "with limit members; align the members or split the combined."))
    # Canonical marker for the derived combined row: the shared value when
    # identical, else LIMIT (the parent row is LMT either way; members
    # price themselves).
    derived["order_type"] = (next(iter(set(ot_seen.values())))
                             if len(set(ot_seen.values())) == 1 else "LIMIT")

    rows = db.query(MarketRegime).filter_by(strategy_id=combined_id).all()
    if len(rows) > 1:
        raise HTTPException(400, (
            f"combined {combined_id} has {len(rows)} marketregime rows — "
            f"exactly one is expected. Remove the extras before saving."))

    created = False
    if rows:
        row = rows[0]
    else:
        created = True
        row = MarketRegime(
            strategy_id=combined_id,
            regime_type="Combined",
            regime_ticker="",
            freeze_timing="open",
            resume_timing="open",
            safety_net_type="simple",
            takeprofit_type="", takeprofit_pct=0, takeprofit_dollar=0,
            takeprofit_timing="", stoploss_dollar=0,
            atr_lookback_stp=0, atr_lookback_tp=0,
            ranking="", ranking_lookback=0, ranking_order="",
            limit_pct=0, atr_limit_lookback=0,
            max_time=0, gap_filter_pct=0,
            substitute_pool_size=20,   # placeholder; derived just below (Patch 162)
            banned_months="[]",
            tdom_filters_json="[]",
            market_trend_rules_tree_json=_EMPTY_TREE_JSON,
            entry_rules_tree_json=_EMPTY_TREE_JSON,
            exit_rules_tree_json=_EMPTY_TREE_JSON,
            freeze_rules_tree_json=_EMPTY_TREE_JSON,
            resume_rules_tree_json=_EMPTY_TREE_JSON,
        )
        db.add(row)

    # projection fields — refreshed on EVERY save so the regime row can
    # never drift from the page (kills the two-homes mismatch class;
    # execute.py's cross-check stays as a belt-and-braces invariant)
    row.regime_type = "Combined"
    row.universe = derived["universe"]
    row.entry_timing = derived["entry_timing"]
    row.exit_timing = derived["exit_timing"]
    row.stoploss_type = derived["stoploss_type"]
    row.stoploss_pct = derived["stoploss_pct"]
    row.stoploss_timing = derived["stoploss_timing"]
    row.order_type = derived["order_type"]
    row.capital = float(cfg.capital)
    row.production_capital = float(cfg.production_capital)
    row.slots = int(cfg.max_slots)
    # Patch 162 (supersedes the preserve-if-set rule): the combined's pool
    # cap = SUM of its members' substitute_pool_size — each subsystem
    # contributes its own pool ("use the same substitute pool size of the
    # subsystem"), and proposed_inserter caps the explicit substitute
    # channel by THIS value, so the sum guarantees no member's pool is
    # truncated. Derived on every Save, same as the other projections.
    row.substitute_pool_size = sum(
        int(r.substitute_pool_size or 0)
        for r in member_regimes.values()) or 20

    db.flush()
    return {"regime_id": row.id, "created": created,
            "universe": row.universe, "slots": row.slots,
            "production_capital": float(row.production_capital)}


@router.post("/{combined_id}/save")
def save_combined(combined_id: int, req: CombinedSaveRequest,
                  db: Session = Depends(get_db)):
    db.query(CombinedMember).filter(
        CombinedMember.combined_strategy_id == combined_id).delete()
    for m in req.members:
        db.add(CombinedMember(
            combined_strategy_id=combined_id,
            member_strategy_id=m.member_strategy_id,
            priority=m.priority,
            is_active=m.is_active,
            seed_sources_json=json.dumps(m.seed_source_ids),
            overrides_json=json.dumps(m.overrides.dict()) if m.overrides else "null",
        ))
    cfg_row = db.get(CombinedConfig, combined_id)
    if cfg_row is None:
        cfg_row = CombinedConfig(combined_strategy_id=combined_id, config_json="")
        db.add(cfg_row)
    cfg_row.config_json = json.dumps(req.config.dict())
    # Patch 149: Save owns the marketregime row (see _upsert_combined_regime).
    active_ids = [m.member_strategy_id for m in req.members if m.is_active]
    regime_info = None
    if active_ids:
        regime_info = _upsert_combined_regime(db, combined_id, req.config,
                                              active_ids)
    db.commit()
    return {"ok": True, "members": len(req.members), "regime": regime_info}


def _load_cfg(db: Session, combined_id: int):
    cfg_row = db.get(CombinedConfig, combined_id)
    if cfg_row is None:
        return None
    return normalize_combined_config(json.loads(cfg_row.config_json))


@router.get("/{combined_id}")
def get_combined(combined_id: int, db: Session = Depends(get_db)):
    members = db.query(CombinedMember).filter(
        CombinedMember.combined_strategy_id == combined_id,
        CombinedMember.is_active == True).order_by(CombinedMember.priority).all()
    cfg = _load_cfg(db, combined_id)
    out_members = []
    for m in members:
        s = db.get(StrategyBucket, m.member_strategy_id)
        ov = json.loads(m.overrides_json or "null")
        out_members.append({
            "member_strategy_id": m.member_strategy_id,
            "strategy_name": s.name if s else f"#{m.member_strategy_id}",
            "priority": m.priority,
            "seed_source_ids": json.loads(m.seed_sources_json or "[]"),
            "overrides": ov,
        })
    return {"members": out_members,
            "config": cfg.dict() if cfg else None}


@router.post("/{combined_id}/simulate")
def simulate(combined_id: int, db: Session = Depends(get_db)):
    combined = db.get(StrategyBucket, combined_id)
    if combined is None:
        raise HTTPException(404, "combined strategy not found")
    cfg = _load_cfg(db, combined_id)
    if cfg is None:
        raise HTTPException(400, "combined has no allocation config — save first")
    rows = db.query(CombinedMember).filter(
        CombinedMember.combined_strategy_id == combined_id,
        CombinedMember.is_active == True).all()
    if not rows:
        raise HTTPException(400, "combined has no members")

    members = []
    for m in rows:
        s = db.get(StrategyBucket, m.member_strategy_id)
        if s is None:
            raise HTTPException(400, f"member {m.member_strategy_id} missing")
        ov = json.loads(m.overrides_json or "null")
        members.append({
            "strategy_id": s.id,
            "strategy_name": s.name,
            "priority": m.priority,
            "seed_source_ids": json.loads(m.seed_sources_json or "[]"),
            "overrides": MemberOverrides(**ov) if ov else None,
        })

    # Patch 134: simulate materializes its own inputs; no spy_dir needed here
    spy_dir = ""
    # Patch 169: universe-scoped exclusions for the allocator intake --
    # same rows the generators use; scoped to this combined's universe.
    _c_regime = db.query(MarketRegime).filter_by(strategy_id=combined_id).first()
    _c_uni = ((_c_regime.universe if _c_regime else '') or '').lower()
    # getattr: tolerate a not-yet-migrated model (rows behave as GLOBAL
    # until the Patch-169 model + SQL land) instead of 500ing.
    _excl = {r.ticker for r in db.query(UniverseTickerExclusion)
             .filter(UniverseTickerExclusion.active == True).all()
             if getattr(r, 'universe', None) is None
             or (getattr(r, 'universe', '') or '').lower() == _c_uni}
    try:
        result = simulate_combined(combined.name, members, cfg, spy_dir,
                                   excluded_tickers=_excl,          # Patch 169
                                   system_type=combined.system_type)  # Patch 172
    except Exception as e:
        raise HTTPException(500, f"combined simulate failed: {e}")
    return result


@router.get("/{combined_id}/gate-today")
def gate_today(combined_id: int, db: Session = Depends(get_db)):
    """Latest computable gate verdict with its active condition label."""
    from app.services.combined.market_gate import build_gate_series, GateDataError

    cfg = _load_cfg(db, combined_id)
    if cfg is None:
        raise HTTPException(404, "combined has no config saved yet")
    rows = db.query(CombinedMember).filter(
        CombinedMember.combined_strategy_id == combined_id,
        CombinedMember.is_active == True).order_by(CombinedMember.priority).all()
    if not rows:
        raise HTTPException(400, "combined has no members")
    first = db.get(StrategyBucket, rows[0].member_strategy_id)
    # Patch 134: gate-today reads the combined's own materialized inputs,
    # preparing them if needed (same step simulate uses).
    from app.services.combined.prepare_inputs import prepare_combined_inputs
    combined_row = db.get(StrategyBucket, combined_id)
    spy_dir = prepare_combined_inputs(
        combined_dir=f"{settings.BACKTEST_DATA_PATH}/{combined_row.name}",
        member_dir=f"{settings.BACKTEST_DATA_PATH}/{first.name}",
        member_universe_relpath=cfg.member_input_relpath,
        ticker=cfg.market_conditions.ticker)

    try:
        gates = build_gate_series(spy_dir, cfg)
    except GateDataError as e:
        raise HTTPException(400, f"gate data unavailable: {e}")

    valid = gates[gates["valid"]]
    if valid.empty:
        raise HTTPException(400, "no valid gate bars (label rules not warm?)")
    last = valid.iloc[-1]
    return {"date": str(valid.index[-1].date()),
            "trade_open": bool(last["trade_open"]),
            "label": str(last["label"]),
            "branch": str(last["label"]),   # back-compat for older UI
            "ibs": float(round(last["ibs"], 4))}

# Patch 147: evening execution step for the combined book. Members are
# stepped as scouts (allow_disabled) and the parity-validated allocation
# primitives size tomorrow's orders at the PRODUCTION profile, landing as
# PROPOSED rows that the existing broker_write morning flow picks up.
from datetime import date as _date
from typing import Optional as _Optional
from pydantic import BaseModel as _BaseModel


class CombinedExecuteRequest(_BaseModel):
    run_date: _Optional[_date] = None    # default: today
    data_root: _Optional[str] = None     # default: exec_data/{YYYYMMDD}


@router.post("/{combined_id}/execute")
def execute_combined_route(combined_id: int,
                           req: CombinedExecuteRequest = None,
                           db: Session = Depends(get_db)):
    """Run the combined's evening execution step (candidates → allocation
    → PROPOSED rows). Idempotent per intended trade date."""
    from app.services.combined.execute import (execute_combined,
                                               CombinedExecError)
    from app.services.combined.market_gate import GateDataError
    req = req or CombinedExecuteRequest()
    try:
        return execute_combined(db, combined_id,
                                run_date=req.run_date,
                                data_root=req.data_root)
    except (CombinedExecError, GateDataError) as e:
        raise HTTPException(400, f"combined execute failed: {e}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"combined execute failed: "
                                 f"{type(e).__name__}: {e}")