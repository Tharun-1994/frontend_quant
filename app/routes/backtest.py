from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.strategy import Strategy
from app.schemas import StrategyRequest
import json
from typing import List
router = APIRouter()


def build_expression(rules: List[dict]) -> str:
    expr_parts = []
    for rule in rules:
        part = f"{rule['indicator'].lower()}_{rule['lookback']} {rule['operator']} {rule['value']}"
        expr_parts.append(part)
    # Now join with connectors
    connectors = [rule.get('connector', '') for rule in rules[1:]]  # Skip first
    expression = expr_parts[0]
    for i, connector in enumerate(connectors):
        if connector:
            expression += f" {connector.lower()} {expr_parts[i+1]}"
        else:
            expression += f" and {expr_parts[i+1]}"
    return expression



@router.post("/save-strategy")
def save_strategy(strategy_data: StrategyRequest, db: Session = Depends(get_db)):
    strategy = Strategy(
        name=strategy_data.strategy_name,
        universe=strategy_data.universe,
        slots=strategy_data.slots,
        capital=strategy_data.capital,
        start_date=strategy_data.start_date,
        end_date=strategy_data.end_date,
        stoploss_pct=strategy_data.stoploss_pct,
        takeprofit_pct=strategy_data.takeprofit_pct,
        entry_rules=build_expression([rule.dict() for rule in strategy_data.entry_rules]),
        exit_rules=build_expression([rule.dict() for rule in strategy_data.exit_rules]),
        ranking=strategy_data.ranking
    )

    print(strategy_data)

    indicators_to_prepare = []
    for rule in strategy_data.entry_rules:
        indicators_to_prepare.append()


    # db.add(strategy)
    # db.commit()
    # db.refresh(strategy)
    return {"strategy_id": strategy.id, "status": "saved"}
