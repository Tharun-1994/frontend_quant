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
    regime_ticker: Optional[str] = None
    label: Optional[str] = None
    value_type :Optional[str] = None
    value_indicator :Optional[str] = None
    value_lookback :Optional[int] = None

    value_range_percent :Optional[int] = None
    params: Optional[Dict[str, Any]] = None
    def to_dict(self):
        return jsonable_encoder(self)


RuleTree = Dict[str, Any]


class TdomFilter(BaseModel):
    """
    One TDOM (Trading Day of Month) calendar filter rule.
    Either tdom-based (blocks on a specific 0-indexed trading day position)
    or weekday-based (blocks on a specific Python weekday: 0=Mon, 4=Fri).
    Matches Java TdomFilterDto exactly.
    """
    tdom: Optional[int] = None        # 0 = 1st trading day of month, 1 = 2nd, etc.
    weekday: Optional[int] = None     # 0 = Monday … 4 = Friday
    banned_months: List[int] = []     # 1=Jan … 12=Dec

class VolFilter(BaseModel):
    """
    Dynamic vol/turnover filter config.
    When enabled=False the engine skips all vol/turnover threshold logic.
    Matches Python crdt_strat_1 vol_threshold_pct_* / turnover_threshold_pct_* params.
    """
    enabled: bool = False
    spy_ticker: str = "spy"          # parquet key: DAILY_closes_{spy_ticker}
    vol_pct_bull: float = 0.20       # bottom 20% excluded when SPY > SMA200
    vol_pct_bear: float = 0.45       # bottom 45% excluded when SPY <= SMA200
    turnover_pct_bull: float = 0.35  # bottom 35% excluded when SPY > SMA200
    turnover_pct_bear: float = 0.05  # bottom 5%  excluded when SPY <= SMA200
    # Patch 116: configurable regime-SMA lookback and annual recalc trigger.
    # Defaults preserve legacy behavior: SMA(200), first trading day of January.
    spy_sma_lookback: int = 200      # SPY SMA lookback for bull/bear detection
    trigger_month: int = 1           # 1=Jan .. 12=Dec — annual recalc month
    trigger_tdom: int = 0            # 0-indexed trading day of trigger_month
    # Patch 116: rolling lookback for avg_volume/avg_turnover parquets.
    # Legacy uses 21 (vol_avg_lookback/turnover_avg_lookback in
    # application_phase_1.properties); GeneratePricesIndicators previously
    # hardcoded 200 — parity bug. Consumed in Patch 117.
    avg_lookback: int = 21

class SafetyNetItem(BaseModel):
    """One stateful safety net policy.

    `type` selects the policy class on the engine side.
    `params` is a free-form blob; each policy validates its own shape.

    Examples
    --------
    Simple freeze/resume:
        { "type": "simple",
          "params": {
            "freeze_rules_tree": {...},
            "resume_rules_tree": {...},
            "freeze_timing": "open",
            "resume_timing": "open"
          } }

    SPY volatility (Stage 3c):
        { "type": "spy_volatility",
          "params": {
            "vol_ticker": "SPY", "vol_lookback": 5, "vol_threshold": 0.025,
            "timeout_days": 20, "selloff_pct": 0.20,
            "peak_drop_pct": 0.80, "rearm_pct": 0.80
          } }
    """
    type: str
    params: Dict[str, Any] = {}

class MarketRegimeBase(BaseModel):
    id: Optional[int] = None
    strategy_id: int
    regime_type: str
    regime_ticker: Optional[str] = ""
    market_trend_type: Optional[str] = None
    market_trend_rules: Optional[List[Rule]] = None
    volatility_rules: Optional[List[Rule]] = None

    # entry_rules: List[Rule]
    # exit_rules: List[Rule]

    entry_timing: Optional[str] = None
    exit_timing: Optional[str] = None

    freeze_timing: Optional[str] = "open"  # "open" | "close"
    resume_timing: Optional[str] = "open"  # "open" | "close"
    safety_net_type: Optional[str] = "none"  # "none" | "simple" | "spy_volatility"
    # New list-based contract. Each item is a stateful policy with its own
    # config blob. The engine iterates them per day; any item saying "freeze"
    # stops trading. See SAFETY_NET_TYPES in the frontend for valid types.
    safety_nets: Optional[List[SafetyNetItem]] = None

    stoploss_type: Optional[str] = None
    takeprofit_type: Optional[str] = None
    stoploss_pct: Optional[float] = None
    # Patch 99: cap on ATR stop offset, as % of anchor price. None/0 = disabled.
    stoploss_max_pct: Optional[float] = None

    stoploss_dollar: Optional[float] = None

    takeprofit_pct: Optional[float] = None

    takeprofit_dollar: Optional[float] = None

    stoploss_timing: Optional[str] = None
    takeprofit_timing: Optional[str] = None
    # Patch 72c: PORTFOLIO drawdown anchor — required when stoploss_type==PORTFOLIO.
    # Defaults to PEAK at the save layer; null when stoploss_type != PORTFOLIO.
    portfolio_stoploss_anchor: Optional[str] = None
    atr_lookback_stp: Optional[int] = None
    atr_lookback_tp: Optional[int] = None

    ranking: Optional[str] = None
    ranking_lookback: Optional[int] = None
    ranking_order: Optional[str] = None

    order_type: Optional[str] = None
    limit_pct: Optional[float] = None
    atr_limit_lookback: Optional[int] = None
    limit_params: Optional[Dict[str, float]] = None   # Patch 167 v2 (LIMIT_HV et al.)

    universe: Optional[str] = None
    capital: Optional[float] = None
    slots: Optional[int] = None

    # Patch 49: per-regime live sizing. Backfilled from strategy.production_capital
    # by migrate_production_capital_to_regime. payload_builder overrides
    # regime.capital with this value for execution; backtest ignores it.
    production_capital: Optional[float] = None

    # Spec A3: How many extra ranked candidates (beyond `slots`) to persist
    # as the substitute pool each night. Default 20. 0 disables substitution
    # on this regime. SignalEngine reads this when building the pool the
    # trader can promote to active via overlay-apply.
    substitute_pool_size: Optional[int] = 20

    created_at: Optional[datetime] = None
    max_time: Optional[int] = None

    banned_months: Optional[List[int]] = []  # parsed as list in Python
    market_trend_rules_labels : Optional[str] = None  # JSON string or comma list of labels
    volatility_rules_labels : Optional[str] = None
    entry_rules_labels : Optional[str] = None
    exit_rules_labels : Optional[str] = None

    market_trend_rules_tree: Optional[RuleTree] = None
    volatility_rules_tree: Optional[RuleTree] = None
    entry_rules_tree: Optional[RuleTree] = None
    exit_rules_tree: Optional[RuleTree] = None
    freeze_rules_tree: Optional[RuleTree] = None
    resume_rules_tree: Optional[RuleTree] = None
    is_look_inside_bar: Optional[bool] = False
    close_positions_on_regime_exit: Optional[bool] = False
    sector_level: Optional[int] = None
    sector_limit: Optional[int] = None

    gap_filter_pct: Optional[float] = None
    max_duplicates: Optional[int] = None
    max_duplicate_sets: Optional[int] = None

    tdom_filters: Optional[List[TdomFilter]] = []  # dynamic TDOM calendar filter rules
    vol_filter: Optional[VolFilter] = None          # dynamic vol/turnover filter

    # LRA Patch 12: LONGSHORT system_type fields. All optional, default None.
    # Structured sub-types may be added in later patches; for now accept any dict.
    ticker_classification: Optional[Dict[str, Any]] = None
    pairing_entry_rules:   Optional[Dict[str, Any]] = None
    pairing_exit_rules:    Optional[Dict[str, Any]] = None
    sizing_policy:         Optional[Dict[str, Any]] = None
    pair_exit_policy:      Optional[Dict[str, Any]] = None

    # LRA Patch 34: per-leg entry rule trees consumed by the engine's
    # runBacktestLongShortSimpleV2 evaluator (Patch 28). Free-form dicts —
    # the engine validates the tree shape at evaluation time.
    entry_rules_tree_long:  Optional[Dict[str, Any]] = None
    entry_rules_tree_short: Optional[Dict[str, Any]] = None

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

    # Spec A3: live execution config. Both optional so existing
    # backtest-only strategies validate without disturbance.
    # production_capital is the live sizing knob; NULL = strategy not yet live.
    # execution_enabled is the per-strategy kill switch; default FALSE.
    # Flipping execution_enabled=TRUE requires production_capital > 0
    # (validated frontend and again in the route).
    production_capital: Optional[float] = None
    execution_enabled: Optional[bool] = False
    system_code: Optional[str] = None
    regimes: List[MarketRegimeBase] = []

    # global_filter : List[GlobalFilter]= []


    def to_dict(self):
        return jsonable_encoder(self)