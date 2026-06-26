"""
universe_exclusions.py — CRUD endpoints for universe ticker exclusions.

GET  /api/universe/exclusions        — list all active + inactive
POST /api/universe/exclusions        — add a ticker
DELETE /api/universe/exclusions/{id} — soft-delete (sets active=False)
PUT  /api/universe/exclusions/{id}/restore — re-activate
"""
from __future__ import annotations
import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.universe_ticker_exclusion import UniverseTickerExclusion

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/api/universe', tags=['universe_exclusions'])


class ExclusionCreate(BaseModel):
    ticker:   str           = Field(..., min_length=1, max_length=20)
    reason:   Optional[str] = Field(None, max_length=200)
    added_by: Optional[str] = Field(None, max_length=100)


class ExclusionOut(BaseModel):
    id:       int
    ticker:   str
    reason:   Optional[str]
    added_by: Optional[str]
    added_at: str
    active:   bool

    class Config:
        from_attributes = True


@router.get('/exclusions', response_model=list[ExclusionOut])
def list_exclusions(db: Session = Depends(get_db)):
    rows = (
        db.query(UniverseTickerExclusion)
        .order_by(UniverseTickerExclusion.ticker)
        .all()
    )
    return [ExclusionOut(
        id=r.id, ticker=r.ticker, reason=r.reason,
        added_by=r.added_by,
        added_at=r.added_at.isoformat() if r.added_at else '',
        active=bool(r.active),
    ) for r in rows]


@router.post('/exclusions', response_model=ExclusionOut, status_code=201)
def add_exclusion(body: ExclusionCreate, db: Session = Depends(get_db)):
    ticker = body.ticker.strip().upper()
    existing = db.query(UniverseTickerExclusion).filter_by(ticker=ticker).first()
    if existing:
        if existing.active:
            raise HTTPException(409, detail=f'{ticker} is already excluded')
        # Re-activate soft-deleted entry
        existing.active   = True
        existing.reason   = body.reason or existing.reason
        existing.added_by = body.added_by or existing.added_by
        db.commit()
        return ExclusionOut(
            id=existing.id, ticker=existing.ticker, reason=existing.reason,
            added_by=existing.added_by,
            added_at=existing.added_at.isoformat() if existing.added_at else '',
            active=True,
        )
    row = UniverseTickerExclusion(
        ticker=ticker, reason=body.reason, added_by=body.added_by or 'ui',
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info('[exclusions] Added %s — %s', ticker, body.reason)
    return ExclusionOut(
        id=row.id, ticker=row.ticker, reason=row.reason,
        added_by=row.added_by,
        added_at=row.added_at.isoformat() if row.added_at else '',
        active=True,
    )


@router.delete('/exclusions/{exclusion_id}', status_code=200)
def remove_exclusion(exclusion_id: int, db: Session = Depends(get_db)):
    row = db.query(UniverseTickerExclusion).filter_by(id=exclusion_id).first()
    if not row:
        raise HTTPException(404, detail=f'Exclusion id={exclusion_id} not found')
    row.active = False
    db.commit()
    logger.info('[exclusions] Deactivated %s', row.ticker)
    return {'id': row.id, 'ticker': row.ticker, 'active': False}


@router.put('/exclusions/{exclusion_id}/restore', status_code=200)
def restore_exclusion(exclusion_id: int, db: Session = Depends(get_db)):
    row = db.query(UniverseTickerExclusion).filter_by(id=exclusion_id).first()
    if not row:
        raise HTTPException(404, detail=f'Exclusion id={exclusion_id} not found')
    row.active = True
    db.commit()
    return {'id': row.id, 'ticker': row.ticker, 'active': True}