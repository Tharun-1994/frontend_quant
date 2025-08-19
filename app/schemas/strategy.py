from pydantic import BaseModel
from typing import List, Optional

class Rule(BaseModel):
    indicator: str
    lookback: int
    operator: str
    value: float
    connector: Optional[str] = None

class StrategyRequest(BaseModel):
    strategy_name: str
    rebalance: str
    universe: str
    slots: int
    capital: float
    start_date: str
    end_date: str
    stoploss_pct: float
    takeprofit_pct: float
    entry_rules: List[Rule]
    exit_rules: List[Rule]
    ranking: str
    stoploss_timing : str
    takeprofit_timing: str
    entry_timing : str
    exit_timing: str
    ranking_lookback : int
    ranking_order: str

