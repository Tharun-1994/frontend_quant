from app.schemas import StrategyRequest
from app.Backtest.backtest_engine_tradestation import backtestEngine


class BacktestService:
    async def run_tradestation_backtest(self, strategy_data: StrategyRequest):
        portfolio_dict = {}
        print('Test Call from service')
        backtestEngine.backtest_tradestation(strategy_data=strategy_data)
        return portfolio_dict

    async def run_java_backtest(self, strategy_data: StrategyRequest):
        import httpx
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                "http://localhost:8080/api/runbacktestv3",
                json=strategy_data.to_dict()
            )
            return response.json()


# Singleton instance
backtest_service = BacktestService()