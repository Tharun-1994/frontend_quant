from http.client import HTTPException

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import HTMLResponse
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.models import *  # ensures both models are registered






from app.routes.backtest import router as backtest_router
from app.routes.equity_view import router as equity_router
from app.constants.static_config import INDICATORS, UNIVERSES, OPERATORS, CONNECTORS, REBALACE, SIGNAL_TIMING, \
    RISK_TIMING, RANKING_ORDERS, SYSTEM_TYPE, STOPLOSS_TYPE, TAKEPROFIT_TYPE
import pandas as pd
from app.database import get_db


from typing import List, Dict, Any
from app.database import engine, Base
from app.schemas.StrategyResponse import StrategyResponse

app = FastAPI()
Base.metadata.create_all(bind=engine)
app.include_router(backtest_router)
app.include_router(equity_router)

origins = [
    "http://localhost:3000",  # Default for Create React App
    "http://localhost:4000",  # Default for Vite/React
    "http://127.0.0.1:3000", # Sometimes the browser uses the IP
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Remember to narrow this down in production.
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static files (e.g., Tailwind CSS)

app.mount("/static", StaticFiles(directory=r"C:\Tharun\Projects\SourceCode\frontend_quant\app/static"), name="static")

# Jinja2 templates
templates = Jinja2Templates(directory=r"C:\Tharun\Projects\SourceCode\frontend_quant\app\templates")




@app.get("/strategies/new", response_class=HTMLResponse)
async def new_strategy(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "strategy": None,
        "universes": UNIVERSES,
        "indicators": INDICATORS,
        "operators": OPERATORS,
        "connectors": CONNECTORS,
        "rebalance": REBALACE,
        "signal_timing": SIGNAL_TIMING,
        "risk_timing": RISK_TIMING,
        "ranking_orders": RANKING_ORDERS,
        "system_types": SYSTEM_TYPE,
        "stoploss_types": STOPLOSS_TYPE,
        "takeProfit_types": TAKEPROFIT_TYPE,
    })


# @app.get("/", response_class=HTMLResponse)
# async def get_dashboard(request: Request, db: Session = Depends(get_db)):
#     # Query all strategies
#     strategies = db.query(Strategy).all()
#
#     # Pass list to template
#     return templates.TemplateResponse(
#         "dashboard.html",
#         {"request": request, "strategies": strategies}
#     )


@app.get("/api/strategies", response_model=List[StrategyResponse])
async def get_strategies(db: Session = Depends(get_db)):
    strategies = db.query(StrategyBucket).all()

    return strategies   # return ORM objects, not dicts/jsonable_encoder



# @app.get("/api/{strategy_id}/edit", response_class=HTMLResponse)
# async def edit_strategy(request: Request, strategy_id: int, db: Session = Depends(get_db)):
#     strategy = db.query(Strategy).filter(Strategy.id == strategy_id).first()
#     if not strategy:
#         raise HTTPException(status_code=404, detail="Strategy not found")
#
#     strategy.entry_rules = parse_expression(strategy.entry_rules)
#     strategy.exit_rules = parse_expression(strategy.exit_rules)
#
#     return templates.TemplateResponse("index.html", {
#         "request": request,
#         "strategy": strategy,
#         "universes": UNIVERSES,
#         "indicators": INDICATORS,
#         "operators": OPERATORS,
#         "connectors": CONNECTORS,
#         "rebalance": REBALACE,
#         "signal_timing": SIGNAL_TIMING,
#         "risk_timing": RISK_TIMING,
#         "ranking_orders": RANKING_ORDERS,
#         "system_types": SYSTEM_TYPE,
#         "stoploss_types": STOPLOSS_TYPE,
#         "takeProfit_types": TAKEPROFIT_TYPE,
#     })

@app.get("/test")
async def convertion():
    Equity = pd.read_json(r'C:\Tharun\Projects\backtest_data\Regular_income\output\Equity.json')
    tradeList = pd.read_json(r'C:\Tharun\Projects\backtest_data\Regular_income\output\TradeList.json')
    Equity.T.to_csv(r'C:\Tharun\Projects\backtest_data\Regular_income\output\Equity.csv')
    tradeList.T.to_csv(r'C:\Tharun\Projects\backtest_data\Regular_income\output\TradeList.csv')







# Main entry point
def main():
    uvicorn.run("app.main:app", host="192.168.1.65", port=8001, reload=True)

# Only run if this file is executed directly
if __name__ == "__main__":
    main()
