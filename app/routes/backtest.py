from http.client import HTTPException

from fastapi import APIRouter, Depends
from numpy.testing.print_coercion_tables import print_new_cast_table

from sqlalchemy.orm import Session
import json
from plotly.utils import PlotlyJSONEncoder
from app.constants.PricePath import PricePath
from app.constants.static_config import UNIVERSES, FUNCTION_MAPPER, UNIVERSES_Codes
from app.database import get_db
from app.loader.PriceDataLoader import PriceDataLoader
from app.loader.TechnicalIndicators import INDICATOR_REGISTRY
from app.models.strategy import Strategy
from app.schemas import StrategyRequest
import re
from typing import List, Dict, Any
import pandas as pd
import httpx

from app.schemas.PerformanceMetrics import PerformanceMetrics

router = APIRouter()

def build_expression(rules: List[Dict[str, Any]]) -> str:
    # 1) First build each atomic comparison
    exprs = [
        f"{rule['indicator'].lower()}_{rule['lookback']} "
        f"{rule['operator']} {rule['value']}"
        for rule in rules
    ]
    # 2) Now interleave with connectors from each rule (except the last)
    parts = [exprs[0]]
    for i, rule in enumerate(rules[:-1]):
        conn = rule.get('connector') or '&&'      # default to &&
        parts.append(f"{conn} {exprs[i+1]}")

    # 3) Join them with spaces
    return ' '.join(parts)

def call_indicator(name: str, **kwargs):
    func = INDICATOR_REGISTRY.get(name)
    if not func:
        raise ValueError(f"Function {name} is not a registered indicator.")
    return func(**kwargs)



@router.post("/api/save-strategy")
def save_strategy(strategy_data: StrategyRequest, db: Session = Depends(get_db)):
    # 🔹 Check if strategy exists (update case)
    strategy = None
    if hasattr(strategy_data, "id") and strategy_data.id:
        strategy = db.query(Strategy).filter(Strategy.id == strategy_data.id).first()

    if strategy:
        # 🔹 Update existing strategy
        strategy.name = strategy_data.strategy_name
        strategy.rebalance = strategy_data.rebalance
        strategy.universe = strategy_data.universe
        strategy.slots = strategy_data.slots
        strategy.capital = strategy_data.capital
        strategy.start_date = strategy_data.start_date
        strategy.end_date = strategy_data.end_date
        strategy.stoploss_pct = strategy_data.stoploss_pct
        strategy.takeprofit_pct = strategy_data.takeprofit_pct
        strategy.entry_rules = build_expression([rule.dict() for rule in strategy_data.entry_rules])
        strategy.exit_rules = build_expression([rule.dict() for rule in strategy_data.exit_rules])
        strategy.ranking = strategy_data.ranking
        strategy.stoploss_timing = strategy_data.stoploss_timing
        strategy.takeprofit_timing = strategy_data.takeprofit_timing
        strategy.entry_timing = strategy_data.entry_timing
        strategy.exit_timing = strategy_data.exit_timing
        strategy.ranking_lookback = strategy_data.ranking_lookback
        strategy.ranking_order = strategy_data.ranking_order
        strategy.min_quantity = strategy_data.min_quantity
        strategy.min_price = strategy_data.min_price
        strategy.system_type = strategy_data.system_type
        strategy.stoploss_type = strategy_data.stoploss_type
        strategy.takeprofit_type = strategy_data.takeprofit_type
        strategy.order_type = strategy_data.order_type
        strategy.limit_pct = strategy_data.limit_pct
        strategy.atr_limit_lookback = strategy_data.atr_limit_lookback

        action = "updated"

    else:
        # 🔹 Create new strategy
        strategy = Strategy(
            name=strategy_data.strategy_name,
            rebalance=strategy_data.rebalance,
            universe=strategy_data.universe,
            slots=strategy_data.slots,
            capital=strategy_data.capital,
            start_date=strategy_data.start_date,
            end_date=strategy_data.end_date,
            stoploss_pct=strategy_data.stoploss_pct,
            takeprofit_pct=strategy_data.takeprofit_pct,
            entry_rules=build_expression([rule.dict() for rule in strategy_data.entry_rules]),
            exit_rules=build_expression([rule.dict() for rule in strategy_data.exit_rules]),
            ranking=strategy_data.ranking,
            stoploss_timing=strategy_data.stoploss_timing,
            takeprofit_timing=strategy_data.takeprofit_timing,
            entry_timing=strategy_data.entry_timing,
            exit_timing=strategy_data.exit_timing,
            ranking_lookback=strategy_data.ranking_lookback,
            ranking_order=strategy_data.ranking_order,
            min_quantity=strategy_data.min_quantity,
            min_price=strategy_data.min_price,
            system_type=strategy_data.system_type,
            stoploss_type=strategy_data.stoploss_type,
            takeprofit_type=strategy_data.takeprofit_type,
            order_type = strategy_data.order_type,
            limit_pct = strategy_data.limit_pct,
            atr_limit_lookback = strategy_data.atr_limit_lookback
        )
        db.add(strategy)
        action = "created"



    for univ in UNIVERSES.keys():
        if strategy_data.universe == UNIVERSES[univ]:

            if univ == 'sp500':
                loader = PriceDataLoader(PricePath.sp500base_path)
            elif univ == 'liquid500':
                loader = PriceDataLoader(PricePath.liquid500base_path)
            else:
                loader = PriceDataLoader(PricePath.russell3000base_path)

            price_data = loader.load_all(rebalance=strategy_data.rebalance,universe=univ)
            date_to_active_tickers = price_data[f'{univ}_universe'].apply(lambda row: row[row == 1].index.tolist(), axis=1)
            df_out = date_to_active_tickers.to_frame(name="active_tickers")
            price_data[f'{univ}_universe'] = df_out["active_tickers"].apply(lambda x: ",".join(x)).to_frame()

            price_data.update(loader.load_spy_close(rebalance=strategy_data.rebalance))

            indictor_Set = set()

            # This is For LIMIT ATR PRODUCTION
            if strategy_data.atr_limit_lookback and strategy_data.atr_limit_lookback > 0 :
                result = call_indicator(FUNCTION_MAPPER["atr"],
                                        Highs=price_data[f'{strategy_data.rebalance}_highs'],
                                        Lows=price_data[f'{strategy_data.rebalance}_lows'],
                                        Closes=price_data[f'{strategy_data.rebalance}_closes'], length=strategy_data.atr_limit_lookback)
                indictor_Set.add(f'{FUNCTION_MAPPER["atr"]}_{strategy_data.atr_limit_lookback}')
                price_data[f'{FUNCTION_MAPPER["atr"]}_{strategy_data.atr_limit_lookback}'] = result


            # Entry Rule  Generation
            for rule in strategy_data.entry_rules:

                if rule.indicator == 'rsi':
                    result = call_indicator(FUNCTION_MAPPER[rule.indicator], prices=price_data[f'{strategy_data.rebalance}_closes'], n=rule.lookback)
                    indictor_Set.add(f'{rule.indicator}_{rule.lookback}')
                    price_data[f'{rule.indicator}_{rule.lookback}'] = result

                elif rule.indicator == 'adx':
                    result = call_indicator(FUNCTION_MAPPER[rule.indicator], Highs=price_data[f'{strategy_data.rebalance}_highs'],
                                            Lows=price_data[f'{strategy_data.rebalance}_lows'],Closes=price_data[f'{strategy_data.rebalance}_closes'], length=rule.lookback)
                    indictor_Set.add(f'{rule.indicator}_{rule.lookback}')
                    price_data[f'{rule.indicator}_{rule.lookback}'] = result

                elif rule.indicator == 'atr':
                    result = call_indicator(FUNCTION_MAPPER[rule.indicator], Highs=price_data[f'{strategy_data.rebalance}_highs'],
                                            Lows=price_data[f'{strategy_data.rebalance}_lows'],Closes=price_data[f'{strategy_data.rebalance}_closes'], length=rule.lookback)
                    indictor_Set.add(f'{rule.indicator}_{rule.lookback}')
                    price_data[f'{rule.indicator}_{rule.lookback}'] = result

                elif rule.indicator == 'hv':

                    result = call_indicator(FUNCTION_MAPPER[rule.indicator],
                                            prices=price_data[f'{strategy_data.rebalance}_closes'],
                                            n=rule.lookback)

                    price_data[f'{rule.indicator}_{rule.lookback}'] = result
                    indictor_Set.add(f'{rule.indicator}_{rule.lookback}')


                elif rule.indicator == 'sma':

                    result = call_indicator(FUNCTION_MAPPER[rule.indicator],
                                            prices=price_data[f'{strategy_data.rebalance}_closes'],
                                            lookback=rule.lookback)

                    price_data[f'{rule.indicator}_{rule.lookback}'] = result
                    indictor_Set.add(f'{rule.indicator}_{rule.lookback}')

                elif rule.indicator == 'crsi' and univ == univ == 'liquid500':

                    crsi_liq = pd.read_csv(f'{PricePath.liquid500base_path}/Lq500CRSI.csv',
                                            index_col=['Date'], parse_dates=True)

                    price_data[f'{rule.indicator}_{rule.lookback}'] = crsi_liq
                    indictor_Set.add(f'{rule.indicator}_{rule.lookback}')

                elif rule.indicator == 'relative_momentum':

                    # Indicator on stock closes
                    stock_indicator = call_indicator(
                        FUNCTION_MAPPER[rule.indicator],
                        df=price_data[f"{strategy_data.rebalance}_closes"],
                        lookback=rule.lookback,
                    )

                    # Indicator on SPY closes (broadcasted to all stock columns)
                    spy_indicator = call_indicator(
                        FUNCTION_MAPPER[rule.indicator],
                        df=price_data[f"{strategy_data.rebalance}_closes_spy"],
                        lookback=rule.lookback,
                    )

                    # Divide stock indicator by SPY indicator (aligning index)
                    relative_momentum = stock_indicator.div(spy_indicator, axis=0)
                    price_data[f'{rule.indicator}_{rule.lookback}'] = relative_momentum
                    indictor_Set.add(f'{rule.indicator}_{rule.lookback}')


            # Exit Rule  Generation
            for rule in strategy_data.exit_rules:
                if rule.indicator == 'rsi':
                    if f'{rule.indicator}_{rule.lookback}' not in indictor_Set:
                        result = call_indicator(FUNCTION_MAPPER[rule.indicator], prices=price_data[f'{strategy_data.rebalance}_closes'], n=rule.lookback)
                        price_data[f'{rule.indicator}_{rule.lookback}'] = result

                elif rule.indicator == 'adx':
                    if f'{rule.indicator}_{rule.lookback}' not in indictor_Set:
                        result = call_indicator(FUNCTION_MAPPER[rule.indicator], Highs=price_data[f'{strategy_data.rebalance}_highs'],
                                                Lows=price_data[f'{strategy_data.rebalance}_lows'],Closes=price_data[f'{strategy_data.rebalance}_closes'], length=rule.lookback)
                        indictor_Set.add(f'{rule.indicator}_{rule.lookback}')
                        price_data[f'{rule.indicator}_{rule.lookback}'] = result


                elif rule.indicator == 'atr':
                    if f'{rule.indicator}_{rule.lookback}' not in indictor_Set:
                        result = call_indicator(FUNCTION_MAPPER[rule.indicator], Highs=price_data[f'{strategy_data.rebalance}_highs'],
                                                Lows=price_data[f'{strategy_data.rebalance}_lows'],Closes=price_data[f'{strategy_data.rebalance}_closes'], length=rule.lookback)
                        indictor_Set.add(f'{rule.indicator}_{rule.lookback}')
                        price_data[f'{rule.indicator}_{rule.lookback}'] = result


                elif rule.indicator == 'hv':
                    if f'{rule.indicator}_{rule.lookback}' not in indictor_Set:
                        result = call_indicator(FUNCTION_MAPPER[rule.indicator],
                                                prices=price_data[f'{strategy_data.rebalance}_closes'],
                                                n=rule.lookback)

                        price_data[f'{rule.indicator}_{rule.lookback}'] = result
                        indictor_Set.add(f'{rule.indicator}_{rule.lookback}')

                elif rule.indicator == 'sma':
                    if f'{rule.indicator}_{rule.lookback}' not in indictor_Set:
                        result = call_indicator(FUNCTION_MAPPER[rule.indicator],
                                                prices=price_data[f'{strategy_data.rebalance}_closes'],
                                                lookback=rule.lookback)

                        price_data[f'{rule.indicator}_{rule.lookback}'] = result
                        indictor_Set.add(f'{rule.indicator}_{rule.lookback}')

                elif rule.indicator == 'crsi' and univ == 'liquid500':
                    if f'{rule.indicator}_{rule.lookback}' not in indictor_Set:
                        crsi_liq = pd.read_csv(f'{PricePath.liquid500base_path}/Lq500CRSI.csv',
                                               index_col=['Date'], parse_dates=True)

                        price_data[f'{rule.indicator}_{rule.lookback}'] = crsi_liq
                        indictor_Set.add(f'{rule.indicator}_{rule.lookback}')

                elif rule.indicator == 'relative_momentum':
                    if f'{rule.indicator}_{rule.lookback}' not in indictor_Set:
                        # Indicator on stock closes
                        stock_indicator = call_indicator(
                            FUNCTION_MAPPER[rule.indicator],
                            df=price_data[f"{strategy_data.rebalance}_closes"],
                            lookback=rule.lookback,
                        )

                        # Indicator on SPY closes (broadcasted to all stock columns)
                        spy_indicator = call_indicator(
                            FUNCTION_MAPPER[rule.indicator],
                            df=price_data[f"{strategy_data.rebalance}_closes_spy"],
                            lookback=rule.lookback,
                        )

                        # Divide stock indicator by SPY indicator (aligning index)
                        relative_momentum = stock_indicator.div(spy_indicator, axis=0)
                        price_data[f'{rule.indicator}_{rule.lookback}'] = relative_momentum
                        indictor_Set.add(f'{rule.indicator}_{rule.lookback}')


            # Ranking Indicator Generation
            if (strategy_data.ranking and strategy_data.ranking_lookback > 0):

                if strategy_data.ranking == 'hv':
                    if f'{strategy_data.ranking}_{strategy_data.ranking_lookback}' not in indictor_Set:
                        result = call_indicator(FUNCTION_MAPPER[strategy_data.ranking],
                                                prices=price_data[f'{strategy_data.rebalance}_closes'], n=strategy_data.ranking_lookback)

                        price_data[f'{strategy_data.ranking}_{strategy_data.ranking_lookback}'] = result

                        indictor_Set.add(f'{strategy_data.ranking}_{strategy_data.ranking_lookback}')
                        price_data[f'{strategy_data.ranking}_{strategy_data.ranking_lookback}'] = result

                elif strategy_data.ranking == 'atr':
                    if f'{strategy_data.ranking}_{strategy_data.ranking_lookback}' not in indictor_Set:
                        result = call_indicator(FUNCTION_MAPPER[strategy_data.ranking],
                                                Highs=price_data[f'{strategy_data.rebalance}_highs'],
                                                Lows=price_data[f'{strategy_data.rebalance}_lows'],
                                                Closes=price_data[f'{strategy_data.rebalance}_closes'],
                                                length=strategy_data.ranking_lookback)

                        indictor_Set.add(f'{strategy_data.ranking}_{strategy_data.ranking_lookback}')
                        price_data[f'{strategy_data.ranking}_{strategy_data.ranking_lookback}'] = result

                elif strategy_data.ranking == 'adx':
                    if f'{strategy_data.ranking}_{strategy_data.ranking_lookback}' not in indictor_Set:
                        result = call_indicator(FUNCTION_MAPPER[strategy_data.ranking],
                                                Highs=price_data[f'{strategy_data.rebalance}_highs'],
                                                Lows=price_data[f'{strategy_data.rebalance}_lows'],
                                                Closes=price_data[f'{strategy_data.rebalance}_closes'],
                                                length=strategy_data.ranking_lookback)

                        indictor_Set.add(f'{strategy_data.ranking}_{strategy_data.ranking_lookback}')
                        price_data[f'{strategy_data.ranking}_{strategy_data.ranking_lookback}'] = result

                elif strategy_data.ranking == 'sma':
                    if f'{strategy_data.ranking}_{strategy_data.ranking_lookback}' not in indictor_Set:
                        result = call_indicator(FUNCTION_MAPPER[strategy_data.ranking],
                                                prices=price_data[f'{strategy_data.rebalance}_closes'],
                                                lookback=strategy_data.ranking_lookback)

                        indictor_Set.add(f'{strategy_data.ranking}_{strategy_data.ranking_lookback}')
                        price_data[f'{strategy_data.ranking}_{strategy_data.ranking_lookback}'] = result

                elif strategy_data.ranking == 'rsi':

                    if f'{strategy_data.ranking}_{strategy_data.ranking_lookback}' not in indictor_Set:

                        result = call_indicator(FUNCTION_MAPPER[strategy_data.ranking],
                                                prices=price_data[f'{strategy_data.rebalance}_closes'], n=strategy_data.ranking_lookback)
                        indictor_Set.add(f'{strategy_data.ranking}_{strategy_data.ranking_lookback}')
                        price_data[f'{strategy_data.ranking}_{strategy_data.ranking_lookback}'] = result

                elif strategy_data.ranking == 'crsi' and univ == 'liquid500':

                    if f'{strategy_data.ranking}_{strategy_data.ranking_lookback}' not in indictor_Set:
                        crsi_liq = pd.read_csv(f'{PricePath.liquid500base_path}/Lq500CRSI.csv',
                                               index_col=['Date'], parse_dates=True)

                        price_data[f'{strategy_data.ranking}_{strategy_data.ranking_lookback}'] = crsi_liq
                        indictor_Set.add(f'{strategy_data.ranking}_{strategy_data.ranking_lookback}')

                elif strategy_data.ranking == 'relative_momentum':

                    # Indicator on stock closes
                    stock_indicator = call_indicator(
                        FUNCTION_MAPPER[strategy_data.ranking],
                        df=price_data[f"{strategy_data.rebalance}_closes"],
                        lookback=strategy_data.ranking_lookback,
                    )

                    # Indicator on SPY closes (broadcasted to all stock columns)
                    spy_indicator = call_indicator(
                        FUNCTION_MAPPER[strategy_data.ranking],
                        df=price_data[f"{strategy_data.rebalance}_closes_spy"],
                        lookback=strategy_data.ranking_lookback,
                    )

                    # Divide stock indicator by SPY indicator (aligning index)
                    relative_momentum = stock_indicator.div(spy_indicator, axis=0)
                    price_data[f'{strategy_data.ranking}_{strategy_data.ranking_lookback}'] = relative_momentum
                    indictor_Set.add(f'{strategy_data.ranking}_{strategy_data.ranking_lookback}')

            # All Dates Generation
            all_dates = price_data[f'{strategy_data.rebalance}_closes'].index
            all_dates_df = pd.DataFrame(data=all_dates, columns=['Date'])
            price_data[f'all_dates'] = all_dates_df


            # Max look back now it takes the default.

            trading_dates = loader.get_trading_dates( end_trading=strategy_data.end_date,
                                     use_data=True, daily_closes=price_data[f'{strategy_data.rebalance}_closes']
                                                     ,all_dates= all_dates,rebalance=strategy_data.rebalance)
            trading_days_df = pd.DataFrame(data=trading_dates, columns=['Date'])
            price_data[f'trading_dates'] = trading_days_df


            loader.uploadCommonPath(price_data=price_data)
    db.commit()
    db.refresh(strategy)

    return {
        "strategy_id": strategy.id,
        "status": f"successfully {action}"
    }


@router.post("/api/run-insample")
async def run_insample_backtest(strategy_data: StrategyRequest):
    try:

        strategy = Strategy(
            name=strategy_data.strategy_name,
            rebalance=strategy_data.rebalance,
            universe=strategy_data.universe,
            slots=strategy_data.slots,
            capital=strategy_data.capital,
            start_date=strategy_data.start_date,
            end_date=strategy_data.end_date,
            stoploss_pct=strategy_data.stoploss_pct,
            takeprofit_pct=strategy_data.takeprofit_pct,
            entry_rules=build_expression([rule.dict() for rule in strategy_data.entry_rules]),
            exit_rules=build_expression([rule.dict() for rule in strategy_data.exit_rules]),
            ranking=strategy_data.ranking,
            stoploss_timing=strategy_data.stoploss_timing,
            takeprofit_timing=strategy_data.takeprofit_timing,
            entry_timing=strategy_data.entry_timing,
            exit_timing=strategy_data.exit_timing,
            ranking_lookback=strategy_data.ranking_lookback,
            ranking_order=strategy_data.ranking_order,
            min_quantity=strategy_data.min_quantity,
            min_price=strategy_data.min_price,
            system_type=strategy_data.system_type,
            stoploss_type=strategy_data.stoploss_type,
            takeprofit_type=strategy_data.takeprofit_type,
            order_type = strategy_data.order_type,
            limit_pct = strategy_data.limit_pct,
            atr_limit_lookback = strategy_data.atr_limit_lookback
        )
        print(strategy)
        strategy_dict = strategy.to_dict()
        print("Strategy object as dictionary:")
        print(strategy_dict)
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "http://localhost:8080/api/backtest",  # Your external service endpoint
                    json=strategy.to_dict()
                )
            if response.status_code == 200:
                print(response.json())
                result = {"message": "Backtest completed", "equity_curve_path": "outputs/curve.png"}
                return result
            return {"error": f"External API failed: {response.status_code}"}
        except Exception as e:
            print(e)
            return {"error": f"Failed to call external API: {str(e)}"}
    except Exception as e:
        print(e)
        raise HTTPException(status_code=500, detail=str(e))



@router.get("/api/{strategy_id}/equity")
def get_equity(strategy_id: str):
    file_path = r"C:\Tharun\Projects\backtest_data\outputs\Equity.json"

    try:
        df = pd.read_json(file_path).T
    except Exception:
        raise HTTPException(status_code=404, detail="Equity file not found")

    df["equityValue"] = df["equityValue"] - 100000

    df['dailyDrawdown'] = -1 * df['dailyDrawdown']
    df.index.name = "date"

    data = [
        # Equity (subplot 1 - keep line)
        {
            "x": df.index.strftime("%Y-%m-%d").tolist(),
            "y": df["equityValue"].tolist(),
            "type": "scatter",
            "mode": "lines",
            "name": "Equity",
            "line": {"color": "green", "width": 2},  # yellow
            "hovertemplate": "Equity: %{y:,.0f}<br>Date: %{x|%Y-%m-%d}<extra></extra>",
            "xaxis": "x",
            "yaxis": "y",
        },
        # Drawdown (subplot 2 - area fill to zero)
        {
            "x": df.index.strftime("%Y-%m-%d").tolist(),
            "y": df["dailyDrawdown"].tolist(),
            "type": "scatter",
            "mode": "lines",
            "fill": "tozeroy",  # ✅ fill to zero
            "fillcolor": "rgba(220,38,38,0.6)",  # ✅ semi-transparent red
            "name": "Drawdown",
            "line": {"color": "rgba(220,38,38,1)", "width": 1},
            "hovertemplate": "Drawdown: %{y:,.0f}<br>Date: %{x|%Y-%m-%d}<extra></extra>",
            "xaxis": "x2",
            "yaxis": "y2",
        },
        # Utility (subplot 3 - area fill to zero)
        {
            "x": df.index.strftime("%Y-%m-%d").tolist(),
            "y": df["dayEndUtilityValue"].tolist(),
            "type": "scatter",
            "mode": "lines",
            "fill": "tozeroy",  # ✅ fill to zero
            "fillcolor": "rgba(16,185,129,0.6)",  # ✅ semi-transparent green
            "name": "Utility",
            "line": {"color": "rgba(16,185,129,1)", "width": 1},
            "hovertemplate": "Utility: %{y:,.2f}<br>Date: %{x|%Y-%m-%d}<extra></extra>",
            "xaxis": "x3",
            "yaxis": "y3",
        },
    ]

    layout = {
        "title": f"Equity Curve - {strategy_id}",
        "height": 800,
        "plot_bgcolor": "white",
        "paper_bgcolor": "white",
        "font": {"family": "Inter, sans-serif", "size": 12, "color": "#333"},
        "hovermode": "x unified",
        "legend": {"orientation": "h", "y": -0.2},

        # First subplot: Equity (60%)
        "xaxis": {
            "domain": [0, 1], "anchor": "y",
            "showgrid": True,
            "showticklabels": False,  # ❌ hide ticks
            "title": ""  # ❌ no title
        },
        "yaxis": {
            "domain": [0.40, 1], "anchor": "x",
            "title": "Equity"
        },

        # Second subplot: Drawdown (20%) — matches xaxis
        "xaxis2": {
            "domain": [0, 1], "anchor": "y2",
            "showgrid": True,
            "showticklabels": False,  # ❌ hide ticks
            "title": "",
            "matches": "x"  # ✅ sync zoom/pan
        },
        "yaxis2": {
            "domain": [0.20, 0.39], "anchor": "x2",
            "title": "Drawdown"
        },

        # Third subplot: Utility (20%) — matches xaxis
        "xaxis3": {
            "domain": [0, 1], "anchor": "y3",
            "showgrid": True,
            "showticklabels": True,  # ✅ only bottom shows ticks
            "title": "Date",
            "matches": "x"
        },
        "yaxis3": {
            "domain": [0, 0.19], "anchor": "x3",
            "title": "Utility"
        },
    }

    return json.loads(json.dumps({"data": data, "layout": layout}, cls=PlotlyJSONEncoder))

@router.get("/api/{strategy_id}/performance", response_model=PerformanceMetrics)
def get_performence(strategy_id: str, db: Session = Depends(get_db)):

    strategy = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    equity_df = pd.read_json(r'C:\Tharun\Projects\backtest_data\outputs\Equity.json').T
    equity_df.index.name = 'date'
    equity_df['dailyDrawdown'] = -1 * equity_df['dailyDrawdown']

    trade_df = pd.read_json(r'C:\Tharun\Projects\backtest_data\outputs\TradeList.json').T
    trade_df.index.name = 'id'


    return PerformanceMetrics.calculate_performance(equity_df, trade_df, strategy.capital)








def parse_expression(expr: str) -> List[Dict[str, Any]]:
    """
    Reverse of build_expression: takes a string like
    'rsi_2 < 30.0 && adx_10 > 30.0'
    and returns a list of rule dicts.
    """
    if not expr:
        return []

    # Split by connectors (&&, ||, AND, OR)
    tokens = re.split(r'\s+(?:&&|\|\||AND|OR)\s+', expr)

    # Extract connectors (keep order)
    connectors = re.findall(r'(?:&&|\|\||AND|OR)', expr)

    rules = []
    for i, token in enumerate(tokens):
        m = re.match(r'([a-zA-Z_]+)_(\d+)\s*([<>=!]+)\s*([\d.]+)', token.strip())
        if not m:
            continue
        indicator, lookback, operator, value = m.groups()
        rules.append({
            "indicator": indicator,   # match your indicators dict
            "lookback": int(lookback),
            "operator": operator,
            "value": float(value),
            "connector": connectors[i] if i < len(connectors) else ""
        })

    return rules




@router.get("/api/get-strategy/{id}", response_model=StrategyRequest)
def get_strategy(id: int, db: Session = Depends(get_db)):
    strategy = db.query(Strategy).filter(Strategy.id == id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    return StrategyRequest(
        id=strategy.id,
        strategy_name=strategy.name,
        rebalance=strategy.rebalance,
        universe=strategy.universe,
        slots=strategy.slots,
        capital=strategy.capital,
        start_date=str(strategy.start_date) if strategy.start_date else "",
        end_date=str(strategy.end_date) if strategy.end_date else "",
        stoploss_pct=strategy.stoploss_pct or 0.0,
        takeprofit_pct=strategy.takeprofit_pct or 0.0,
        stoploss_timing=strategy.stoploss_timing or "",
        takeprofit_timing=strategy.takeprofit_timing or "",
        entry_timing=strategy.entry_timing or "",
        exit_timing=strategy.exit_timing or "",
        ranking=strategy.ranking or "",
        ranking_lookback=strategy.ranking_lookback or 0,
        ranking_order=strategy.ranking_order or "",
        min_quantity=strategy.min_quantity or 0,
        min_price=strategy.min_price or 0.0,
        system_type=strategy.system_type or "",
        stoploss_type=strategy.stoploss_type or "",
        takeprofit_type=strategy.takeprofit_type or "",
        entry_rules=parse_expression(strategy.entry_rules),
        exit_rules=parse_expression(strategy.exit_rules),
        order_type = strategy.order_type or "",
        limit_pct = strategy.limit_pct or 0.0,
        atr_limit_lookback=strategy.atr_limit_lookback or 0
    )