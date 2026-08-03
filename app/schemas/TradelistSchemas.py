from pydantic import BaseModel
from typing import Optional
from datetime import datetime, date
from fastapi.encoders import jsonable_encoder


class TradelistOut(BaseModel):
    """
    Full tradelist row. Used by Holdings page, Tradelist page, Basket Review.
    Filter by `ledger`, `status`, `intended_trade_date`, `strategy_id` at the
    route layer to produce per-page views.
    """
    id: int

    # Foreign keys
    strategy_id: int
    entered_regime_id: int
    substitute_link_id: Optional[int] = None
    pair_id: Optional[int] = None

    # Ledger discriminator
    ledger: str
    source_tag: str

    # Identity
    symbol: str
    direction: str
    status: str

    # Proposal data
    proposal_date: date
    intended_trade_date: date
    limit_price: float
    intended_qty: int
    intended_capital: float
    initial_stop_price: Optional[float] = None
    initial_tp_price: Optional[float] = None
    ranking_rank: Optional[int] = None
    ranking_value: Optional[float] = None

    # Stop adjustment (trader-editable on Holdings page)
    current_stop_price: Optional[float] = None

    # Fill data
    entry_date: Optional[date] = None
    entry_price: Optional[float] = None
    entry_timing: Optional[str] = None
    filled_qty: Optional[int] = None
    avg_fill_price: Optional[float] = None
    fill_status: Optional[str] = None

    # Exit data
    exit_date: Optional[date] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None

    # P&L
    profit: Optional[float] = None
    profit_pct: Optional[float] = None
    day_count: Optional[int] = None

    # Trader notes (per-position)
    trader_notes: Optional[str] = None

    # Audit
    created_at: datetime
    updated_at: datetime

    def to_dict(self):
        return jsonable_encoder(self)

    class Config:
        from_attributes = True


class StopUpdateRequest(BaseModel):
    """
    Body for PATCH /api/tradelist/{id}/stop.
    Updates tradelist.current_stop_price and inserts a tradelist_stop_history row.
    Engine reads current_stop_price on next nightly run; webapp does NOT push to IBKR.
    """
    new_stop_price: float
    reason: Optional[str] = None
    changed_by: Optional[str] = None


class TraderNotesUpdateRequest(BaseModel):
    """
    Body for PATCH /api/tradelist/{id}/notes.
    Overwrites tradelist.trader_notes. No history table — last-write-wins.
    """
    trader_notes: Optional[str] = None
    changed_by: Optional[str] = None


class TradelistStopHistoryOut(BaseModel):
    """
    One stop-adjustment audit row. Used by the Holdings-page stop history panel.
    """
    id: int
    tradelist_id: int
    old_stop_price: Optional[float] = None
    new_stop_price: float
    changed_at: datetime
    changed_by: Optional[str] = None
    reason: Optional[str] = None

    class Config:
        from_attributes = True