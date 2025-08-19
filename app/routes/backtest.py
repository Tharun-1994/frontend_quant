from http.client import HTTPException

from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy import values
from sqlalchemy.orm import Session
import datetime as dt
from app.constants.PricePath import PricePath
from app.constants.static_config import UNIVERSES, FUNCTION_MAPPER, UNIVERSES_Codes
from app.database import get_db
from app.loader.PriceDataLoader import PriceDataLoader
from app.loader.TechnicalIndicators import IndicatorCalculator, INDICATOR_REGISTRY
from app.models.strategy import Strategy
from app.schemas import StrategyRequest
import json
from typing import List, Dict, Any
import pandas as pd
import httpx

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



@router.post("/save-strategy")
def save_strategy(strategy_data: StrategyRequest, db: Session = Depends(get_db)):

    strategy = Strategy(
        name=strategy_data.strategy_name,
        rebalance = strategy_data.rebalance,
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
        stoploss_timing = strategy_data.stoploss_timing,
        takeprofit_timing = strategy_data.takeprofit_timing,
        entry_timing = strategy_data.entry_timing,
        exit_timing = strategy_data.exit_timing,
        ranking_lookback = strategy_data.ranking_lookback,
        ranking_order = strategy_data.ranking_order
    )




    for univ in UNIVERSES.keys():
        if strategy_data.universe == UNIVERSES[univ]:
            loader = PriceDataLoader(PricePath.sp500base_path)
            price_data = loader.load_all(rebalance=strategy_data.rebalance,universe=univ)
            date_to_active_tickers = price_data[f'{univ}_universe'].apply(lambda row: row[row == 1].index.tolist(), axis=1)
            df_out = date_to_active_tickers.to_frame(name="active_tickers")
            price_data[f'{univ}_universe'] = df_out["active_tickers"].apply(lambda x: ",".join(x)).to_frame()

            indictor_Set = set()

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


            # Ranking Indicator Generation
            if (strategy_data.ranking and strategy_data.ranking_lookback > 0):

                if strategy_data.ranking == 'hv':
                    if f'{strategy_data.ranking}_{strategy_data.ranking_lookback}' not in indictor_Set:
                        result = call_indicator(FUNCTION_MAPPER[strategy_data.ranking],
                                                prices=price_data[f'{strategy_data.rebalance}_closes'], n=strategy_data.ranking_lookback)

                        price_data[f'{strategy_data.ranking}_{strategy_data.ranking_lookback}'] = result

                        indictor_Set.add(f'{strategy_data.ranking}_{strategy_data.ranking_lookback}')
                        price_data[f'{strategy_data.ranking}_{strategy_data.ranking_lookback}'] = result

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






    # db.add(strategy)
    # db.commit()
    # db.refresh(strategy)
    return {"strategy_id": strategy.id, "status": "saved"}


@router.post("/run-insample")
async def run_insample_backtest(strategy_data: StrategyRequest):
    try:

        strategy = Strategy(
            name=strategy_data.strategy_name,
            rebalance=strategy_data.rebalance,
            universe=UNIVERSES_Codes[strategy_data.universe],
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
            ranking_order = strategy_data.ranking_order
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
