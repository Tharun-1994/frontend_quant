from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn
from app.routes.backtest import router as backtest_router
from app.routes.equity_view import router as equity_router
from app.constants.static_config import INDICATORS, UNIVERSES, OPERATORS, CONNECTORS, REBALACE, SIGNAL_TIMING, \
    RISK_TIMING, RANKING_ORDERS
import pandas as pd
app = FastAPI()
app.include_router(backtest_router)
app.include_router(equity_router)
# Serve static files (e.g., Tailwind CSS)

app.mount("/static", StaticFiles(directory=r"C:\Tharun\Projects\SourceCode\frontend_quant\app/static"), name="static")

# Jinja2 templates
templates = Jinja2Templates(directory=r"C:\Tharun\Projects\SourceCode\frontend_quant\app\templates")

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "universes": UNIVERSES,
        "indicators": INDICATORS,
        "operators": OPERATORS,
        "connectors": CONNECTORS,
        "rebalance" : REBALACE,
        "signal_timing":SIGNAL_TIMING,
        "risk_timing" : RISK_TIMING,
        "ranking_orders":RANKING_ORDERS
    })
@app.get("/test")
async def convertion():
    Equity = pd.read_json(r'C:\Tharun\Projects\backtest_data\outputs\Equity.json')
    tradeList = pd.read_json(r'C:\Tharun\Projects\backtest_data\outputs\TradeList.json')
    Equity.T.to_csv(r'C:\Tharun\Projects\backtest_data\outputs\Equity.csv')
    tradeList.T.to_csv(r'C:\Tharun\Projects\backtest_data\outputs\TradeList.csv')

# Main entry point
def main():
    uvicorn.run("app.main:app", host="127.0.0.1", port=8001, reload=True)

# Only run if this file is executed directly
if __name__ == "__main__":
    main()
