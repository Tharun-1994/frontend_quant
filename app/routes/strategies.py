"""
strategies.py
=============
All strategy and market-regime CRUD endpoints.

Endpoints
---------
GET    /api/strategies                      — list all strategies
GET    /api/get-strategy/{id}               — fetch one strategy (full)
POST   /api/save-strategy                   — create or update a strategy bucket
DELETE /api/strategies/{strategy_id}        — delete strategy + its regimes
GET    /api/check-username                  — check if a strategy name is taken

GET    /api/marketregime/{strategy_id}      — list all regimes for a strategy
POST   /api/save-marketregime-v2            — create or update a market regime (current)
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.loader.GeneratePricesIndicators import GeneratePricesIndicators
from app.loader.PriceDataLoader import PriceDataLoader
from app.loader.Rule_Tree_JSON import dumps_tree, normalize_rules_tree
from app.models import MarketRegime, StrategyBucket
from app.schemas import StrategyRequest
from app.schemas.strategy import MarketRegimeBase
from app.schemas.StrategyResponse import StrategyResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["strategies"])


# ── Strategy CRUD ─────────────────────────────────────────────────────────────

@router.get("/strategies", response_model=List[StrategyResponse])
def list_strategies(db: Session = Depends(get_db)):
    """Return all strategy buckets."""
    return db.query(StrategyBucket).all()


@router.get("/get-strategy/{strategy_id}", response_model=StrategyRequest)
def get_strategy(strategy_id: int, db: Session = Depends(get_db)):
    """Fetch one strategy by ID."""
    strategy = db.query(StrategyBucket).filter(StrategyBucket.id == strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    return StrategyRequest(
        id=strategy.id,
        name=strategy.name,
        rebalance=strategy.rebalance,
        start_date=strategy.start_date.isoformat() if strategy.start_date else None,
        end_date=strategy.end_date.isoformat() if strategy.end_date else None,
        min_quantity=strategy.min_quantity,
        min_price=strategy.min_price,
        system_type=strategy.system_type,
        market_regime_type=strategy.market_regime_type,
    )


@router.post("/save-strategy")
def save_strategy(strategy_data: StrategyRequest, db: Session = Depends(get_db)):
    """Create or update a strategy bucket."""
    strategy = None
    if getattr(strategy_data, "id", None):
        strategy = db.query(StrategyBucket).filter(
            StrategyBucket.id == strategy_data.id
        ).first()

    if strategy:
        strategy.name               = strategy_data.name
        strategy.start_date         = strategy_data.start_date
        strategy.end_date           = strategy_data.end_date
        strategy.rebalance          = strategy_data.rebalance
        strategy.min_price          = strategy_data.min_price
        strategy.min_quantity       = strategy_data.min_quantity
        strategy.system_type        = strategy_data.system_type
        strategy.market_regime_type = strategy_data.market_regime_type
        action = "updated"
    else:
        strategy = StrategyBucket(
            name               = strategy_data.name,
            start_date         = strategy_data.start_date,
            end_date           = strategy_data.end_date,
            rebalance          = strategy_data.rebalance,
            min_price          = strategy_data.min_price,
            min_quantity       = strategy_data.min_quantity,
            system_type        = strategy_data.system_type,
            market_regime_type = strategy_data.market_regime_type,
        )
        db.add(strategy)
        action = "created"

    db.commit()
    db.refresh(strategy)

    PriceDataLoader.create_strategy_Folder(name=strategy_data.name)

    return {"strategy_id": strategy.id, "status": f"successfully {action}"}


@router.delete("/strategies/{strategy_id}")
def delete_strategy(strategy_id: int, db: Session = Depends(get_db)):
    """
    Delete a strategy and all its market regimes.
    Regimes are deleted first to avoid FK constraint errors.
    """
    strategy = db.query(StrategyBucket).filter(
        StrategyBucket.id == strategy_id
    ).first()
    if not strategy:
        raise HTTPException(
            status_code=404, detail=f"Strategy {strategy_id} not found"
        )

    strategy_name = strategy.name
    try:
        regimes_deleted = (
            db.query(MarketRegime)
            .filter(MarketRegime.strategy_id == strategy_id)
            .delete(synchronize_session=False)
        )
        db.delete(strategy)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete strategy {strategy_id}: {e}",
        )

    return {
        "success": True,
        "id": strategy_id,
        "name": strategy_name,
        "regimes_deleted": regimes_deleted,
        "message": f"Strategy '{strategy_name}' deleted",
    }


@router.get("/check-username")
def check_username(name: str, db: Session = Depends(get_db)):
    """Check whether a strategy name is already taken."""
    count = db.query(func.count(StrategyBucket.name)).filter(
        StrategyBucket.name == name
    ).scalar()
    return {"name": name, "taken": count > 0}


# ── Market Regime CRUD ────────────────────────────────────────────────────────

@router.get("/marketregime/{strategy_id}", response_model=List[MarketRegimeBase])
def get_market_regimes(strategy_id: int, db: Session = Depends(get_db)):
    """List all market regimes for a strategy."""
    from app.routes.backtest import db_to_pydantic  # avoid circular at module level
    db_objs = db.query(MarketRegime).filter(
        MarketRegime.strategy_id == strategy_id
    ).all()
    return [db_to_pydantic(obj) for obj in db_objs]


@router.post("/save-marketregime-v2")
def save_market_regime(marketregime: MarketRegimeBase, db: Session = Depends(get_db)):
    """Create or update a market regime (current v2 implementation)."""
    if marketregime.id:
        db_obj = db.query(MarketRegime).filter(
            MarketRegime.id == marketregime.id
        ).first()
        if not db_obj:
            raise HTTPException(status_code=404, detail="MarketRegime not found")
    else:
        db_obj = MarketRegime(strategy_id=marketregime.strategy_id)
        db.add(db_obj)

    # Scalar fields
    db_obj.regime_type                   = marketregime.regime_type
    db_obj.regime_ticker                 = marketregime.regime_ticker
    db_obj.market_trend_type             = marketregime.market_trend_type
    db_obj.market_trend_rules            = None
    db_obj.market_trend_rules_labels     = None
    db_obj.entry_timing                  = marketregime.entry_timing
    db_obj.exit_timing                   = marketregime.exit_timing
    db_obj.stoploss_type                 = marketregime.stoploss_type
    db_obj.takeprofit_type               = marketregime.takeprofit_type
    db_obj.takeprofit_dollar             = marketregime.takeprofit_dollar
    db_obj.stoploss_dollar               = marketregime.stoploss_dollar
    db_obj.stoploss_pct                  = marketregime.stoploss_pct
    db_obj.takeprofit_pct                = marketregime.takeprofit_pct
    db_obj.stoploss_timing               = marketregime.stoploss_timing
    db_obj.takeprofit_timing             = marketregime.takeprofit_timing
    db_obj.atr_lookback_stp              = marketregime.atr_lookback_stp
    db_obj.atr_lookback_tp               = marketregime.atr_lookback_tp
    db_obj.ranking                       = marketregime.ranking
    db_obj.ranking_lookback              = marketregime.ranking_lookback
    db_obj.ranking_order                 = marketregime.ranking_order
    db_obj.order_type                    = marketregime.order_type
    db_obj.limit_pct                     = marketregime.limit_pct
    db_obj.atr_limit_lookback            = marketregime.atr_limit_lookback
    db_obj.universe                      = marketregime.universe
    db_obj.capital                       = marketregime.capital
    db_obj.slots                         = marketregime.slots
    db_obj.max_time                      = marketregime.max_time
    db_obj.is_look_inside_bar            = marketregime.is_look_inside_bar
    db_obj.close_positions_on_regime_exit = marketregime.close_positions_on_regime_exit
    db_obj.sector_level                  = marketregime.sector_level
    db_obj.sector_limit                  = marketregime.sector_limit
    db_obj.gap_filter_pct                = marketregime.gap_filter_pct
    db_obj.max_duplicates                = marketregime.max_duplicates
    db_obj.max_duplicate_sets            = marketregime.max_duplicate_sets

    # JSON-serialised fields
    db_obj.banned_months      = json.dumps(marketregime.banned_months or [])
    db_obj.tdom_filters_json  = json.dumps(
        [f.dict() for f in (marketregime.tdom_filters or [])]
    )
    db_obj.vol_filter_json    = (
        json.dumps(marketregime.vol_filter.dict()) if marketregime.vol_filter else None
    )

    # Rule trees — normalise then serialise
    marketregime.market_trend_rules_tree = normalize_rules_tree(
        marketregime.market_trend_rules_tree
    )
    marketregime.entry_rules_tree  = normalize_rules_tree(marketregime.entry_rules_tree)
    marketregime.exit_rules_tree   = normalize_rules_tree(marketregime.exit_rules_tree)
    marketregime.freeze_rules_tree = normalize_rules_tree(marketregime.freeze_rules_tree)
    marketregime.resume_rules_tree = normalize_rules_tree(marketregime.resume_rules_tree)

    db_obj.market_trend_rules_tree_json = dumps_tree(marketregime.market_trend_rules_tree)
    db_obj.entry_rules_tree_json        = dumps_tree(marketregime.entry_rules_tree)
    db_obj.exit_rules_tree_json         = dumps_tree(marketregime.exit_rules_tree)
    db_obj.freeze_rules_tree_json       = dumps_tree(marketregime.freeze_rules_tree)
    db_obj.resume_rules_tree_json       = dumps_tree(marketregime.resume_rules_tree)

    db.commit()
    db.refresh(db_obj)

    # Indicator file generation — non-fatal; id is already saved
    generate_warning = None
    try:
        strategy = db.query(StrategyBucket).filter(
            StrategyBucket.id == marketregime.strategy_id
        ).first()
        GeneratePricesIndicators.generate(marketRegime=marketregime, strategy=strategy)
    except Exception as e:
        generate_warning = str(e)
        logger.warning(
            f"Indicator generation failed for regime {db_obj.id}: {e}"
        )

    result = {
        "id":          db_obj.id,
        "strategy_id": db_obj.strategy_id,
        "regime_type": db_obj.regime_type,
    }
    if generate_warning:
        result["warning"] = f"Saved but indicator generation failed: {generate_warning}"

    return result