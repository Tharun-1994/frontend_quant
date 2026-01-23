from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
from typing import Dict, Any

from app.models import MarketRegime
from fastapi.encoders import jsonable_encoder

class Rule(BaseModel):
    indicator: str
    lookback: int
    operator: str
    value: float
    connector: str
    label: Optional[str] = None
    value_type :Optional[str] = None
    value_indicator :Optional[str] = None
    value_lookback :Optional[int] = None
    params: Optional[Dict[str, Any]] = None
    def to_dict(self):
        return jsonable_encoder(self)


RuleTree = Dict[str, Any]

class MarketRegimeBase(BaseModel):
    id: Optional[int] = None
    strategy_id: int
    regime_type: str
    regime_ticker: str
    market_trend_type: Optional[str] = None
    market_trend_rules: Optional[List[Rule]] = None
    volatility_rules: Optional[List[Rule]] = None

    entry_rules: List[Rule]
    exit_rules: List[Rule]

    entry_timing: Optional[str] = None
    exit_timing: Optional[str] = None

    stoploss_type: Optional[str] = None
    takeprofit_type: Optional[str] = None
    stoploss_pct: Optional[float] = None
    takeprofit_pct: Optional[float] = None
    stoploss_timing: Optional[str] = None
    takeprofit_timing: Optional[str] = None
    atr_lookback_stp: Optional[int] = None
    atr_lookback_tp: Optional[int] = None

    ranking: Optional[str] = None
    ranking_lookback: Optional[int] = None
    ranking_order: Optional[str] = None

    order_type: Optional[str] = None
    limit_pct: Optional[float] = None
    atr_limit_lookback: Optional[int] = None

    universe: Optional[str] = None
    capital: Optional[float] = None
    slots: Optional[int] = None

    created_at: Optional[datetime] = None
    max_time:Optional[int] = None

    banned_months: Optional[List[int]] = []  # parsed as list in Python
    market_trend_rules_labels : Optional[str] = None  # JSON string or comma list of labels
    volatility_rules_labels : Optional[str] = None
    entry_rules_labels : Optional[str] = None
    exit_rules_labels : Optional[str] = None

    market_trend_rules_tree: Optional[RuleTree] = None
    volatility_rules_tree: Optional[RuleTree] = None
    entry_rules_tree: Optional[RuleTree] = None
    exit_rules_tree: Optional[RuleTree] = None



    def to_dict(self):
        return jsonable_encoder(self)


class GlobalFilter:
    condition: List[Rule]
    action : str

class StrategyRequest(BaseModel):
    id: int
    name: str
    rebalance: str

    start_date: str
    end_date: str

    min_quantity:int
    min_price:float
    system_type:str

    market_regime_type: Optional[str] = None

    regimes: List[MarketRegimeBase] = []

    # global_filter : List[GlobalFilter]= []


    def to_dict(self):
        return jsonable_encoder(self)