from sqlalchemy import (
    Column, Integer, String, Numeric, Date, DateTime,
    ForeignKey, Text, Boolean
)
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.database import Base


class Tradelist(Base):
    """
    Central execution ledger. One row per (strategy, symbol, intended_trade_date).

    Holds the full lifecycle: PROPOSED / SUBSTITUTE_POOL → PENDING_FILL →
    LIVE → EXITED (with CANCELLED / ELIDED / UNUSED branches).

    Two ledgers coexist via the `ledger` column:
      TRADED  — rows matching what IBKR actually holds
      SYSTEM  — shadow rows (ELIDED + SUBSTITUTE originals) for worthiness audit

    Substitution pairs are linked via `substitute_link_id` (self-FK):
      TRADED-side row holds the substitute symbol Vas traded
      SYSTEM-side row holds the original symbol the system picked

    pair_id is reserved for the future LONGSHORT execution path (LRA pairs).
    Stays NULL for all Phase 1 single-direction rows.
    """
    __tablename__ = "tradelist"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # ── Foreign keys ──────────────────────────────────────────────────────────
    strategy_id = Column(Integer, ForeignKey("strategies_bucket.id"),
                         nullable=False)
    entered_regime_id = Column(Integer, ForeignKey("marketregime.id"),
                               nullable=False)
    substitute_link_id = Column(Integer, ForeignKey("tradelist.id"),
                                nullable=True)
    pair_id = Column(Integer, nullable=True)

    # ── Ledger discriminator (the two-ledger model) ───────────────────────────
    ledger = Column(String(10), nullable=False)
    # 'TRADED' | 'SYSTEM'
    source_tag = Column(String(20), nullable=False)
    # 'SYSTEM' | 'SUBSTITUTE' | 'ADJUSTED' | 'ELIDED'

    # ── Identity ──────────────────────────────────────────────────────────────
    symbol = Column(String(20), nullable=False)
    direction = Column(String(10), nullable=False)
    # 'LONG' | 'SHORT' — Phase 1.
    status = Column(String(20), nullable=False)
    # 'PROPOSED' | 'SUBSTITUTE_POOL' | 'PENDING_FILL' | 'LIVE' | 'EXITED'
    # | 'CANCELLED' | 'ELIDED' | 'UNUSED'

    # ── Proposal data (populated at PROPOSED / SUBSTITUTE_POOL) ───────────────
    proposal_date = Column(Date, nullable=False)
    # The night the signal was generated (D-1 ET).
    intended_trade_date = Column(Date, nullable=False)
    # The market day the order targets (D).
    limit_price = Column(Numeric(12, 4), nullable=False)
    intended_qty = Column(Integer, nullable=False)
    intended_capital = Column(Numeric(18, 2), nullable=False)
    # Slot capital used to size this row, snapshot at proposal time.
    # Changes to strategies_bucket.production_capital do NOT retro-resize.
    initial_stop_price = Column(Numeric(12, 4), nullable=True)
    initial_tp_price = Column(Numeric(12, 4), nullable=True)
    ranking_rank = Column(Integer, nullable=True)
    # Where this candidate ranked among the day's signals (1 = best).
    ranking_value = Column(Numeric(18, 4), nullable=True)
    # Patch 162/163: which SUBSYSTEM of a combined book produced this row —
    # the member strategy's system_code VERBATIM (legacy scheme:
    # M_LDEQ_54A / _54B / _54C; REQUIRED on combined members, enforced
    # loud in combined/execute.py). It becomes the basket OrderRef as-is
    # (Patch 163) on PROPOSED and SUBSTITUTE_POOL rows alike. NULL for
    # every non-combined strategy — their OrderRef stays exactly Patch 81.
    subsystem_ref = Column(String(10), nullable=True)
    # The indicator value used for ranking (audit).

    # ── Stop adjustment (trader-editable on Holdings page) ────────────────────
    # Engine reads current_stop_price for exit checks, NOT initial_stop_price.
    # Webapp-only — manual IBKR sync is the trader's responsibility.
    current_stop_price = Column(Numeric(12, 4), nullable=True)
    # Patch 108: daily-maintained take-profit price (engine newTpPrice).
    # broker_write emits a SELL LMT DAY row OCA-paired with the stop.
    current_tp_price = Column(Numeric(12, 4), nullable=True)
    # Patch 108: TRUE only when the trader explicitly set the stop via the
    # F2 UI. Fixes the echo-freeze bug: stop_updater writes the nightly
    # recompute into current_stop_price, so a non-null value alone CANNOT
    # mean 'trader override' — that froze every stop at its first value
    # with source=trader_override forever. Seed sends currentStopPrice to
    # the engine ONLY when this flag is set; otherwise the engine
    # recomputes from today's ATR (legacy daily behaviour).
    stop_overridden = Column(Boolean, nullable=False, default=False,
                             server_default='0')

    # ── Fill data (populated when status → LIVE) ──────────────────────────────
    entry_date = Column(Date, nullable=True)
    entry_price = Column(Numeric(12, 4), nullable=True)
    entry_timing = Column(String(20), nullable=True)
    # 'open' | 'intraday' | 'close' — mirrors TradeLog.entryTiming
    filled_qty = Column(Integer, nullable=True)
    avg_fill_price = Column(Numeric(12, 4), nullable=True)
    fill_status = Column(String(20), nullable=True)
    # 'FULL' | 'PARTIAL' | 'REJECTED' — Phase 2 reconciliation only.
    # Phase 1 always 'FULL' under modeled fills.

    # ── Exit data (populated when status → EXITED) ────────────────────────────
    exit_date = Column(Date, nullable=True)
    exit_price = Column(Numeric(12, 4), nullable=True)
    exit_reason = Column(String(30), nullable=True)
    # 'limit_exit' | 'stop' | 'take_profit' | 'max_time' |
    # 'regime_shift' | 'safety_net'

    # ── P&L (computed at EXITED) ──────────────────────────────────────────────
    profit = Column(Numeric(18, 4), nullable=True)
    profit_pct = Column(Numeric(10, 4), nullable=True)
    day_count = Column(Integer, nullable=True)

    # ── Free-form per-position notes (trader-editable on Holdings page) ───────
    trader_notes = Column(Text, nullable=True)

    # ── Audit ─────────────────────────────────────────────────────────────────
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(),
                        onupdate=func.now(), nullable=False)

    # ── ORM nav ───────────────────────────────────────────────────────────────
    # Self-ref for substitution pairs. remote_side tells SQLAlchemy this is a
    # many-to-one self-reference.
    substitute_partner = relationship(
        "Tradelist",
        remote_side="Tradelist.id",
        foreign_keys=[substitute_link_id],
        uselist=False,
    )

    def __repr__(self):
        return (f"<Tradelist(id={self.id}, strategy_id={self.strategy_id}, "
                f"symbol='{self.symbol}', ledger='{self.ledger}', "
                f"status='{self.status}')>")