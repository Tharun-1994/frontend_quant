"""
Integration Guide
=================
Shows how to wire the ETF backtest engine into your existing codebase.

Your existing flow:
    Router  →  BacktestService.run_tradestation_backtest()
            →  BacktestEngine.backtest_tradestation()

Updated BacktestEngine.backtest_tradestation() implementation below.
"""
from app.Backtest.backtest_engine_etf import ETFBacktestEngine


# ==============================================================================
#  Option 1:  Update your existing BacktestEngine class
# ==============================================================================

# In your existing BacktestEngine file, add:




class BacktestEngine:
    """Your existing class – just fill in backtest_tradestation."""

    # Base path where strategy parquets are stored
    # Pattern: {BACKTEST_DATA_PATH}/{strategy_name}/input/{universe}/
    BACKTEST_DATA_PATH = r'C:\Tharun\Projects\backtest_data'

    def backtest_tradestation(self, strategy_data):
        """
        Run Individual ETF backtest using minute-bar data.

        Parameters
        ----------
        strategy_data : StrategyRequest
            Parsed from the API POST body.

        Returns
        -------
        dict : Backtest result (summary, equity_curve, trades).
        """
        # Derive paths from strategy config
        strategy_name = strategy_data.name  # e.g. "Spy Etf"
        universe = strategy_data.regimes[0].universe.lower()  # e.g. "spy"

        input_path = (
            f'{self.BACKTEST_DATA_PATH}/{strategy_name}/input/{universe}'
        )
        output_path = (
            f'{self.BACKTEST_DATA_PATH}/{strategy_name}/output'
        )

        # Run
        engine = ETFBacktestEngine()
        result = engine.run(
            strategy_data=strategy_data,
            base_input_path=input_path,
            output_path=output_path,
        )

        return result


# ==============================================================================
#  Option 2:  Update BacktestService (async wrapper)
# ==============================================================================

class BacktestService:
    """Your existing service class."""

    def __init__(self):
        self.backtest_engine = BacktestEngine()

    async def run_tradestation_backtest(self, strategy_data):
        """Async wrapper that delegates to the synchronous engine."""
        # If the engine is CPU-bound and you want true async,
        # run in a thread pool:
        import asyncio
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            self.backtest_engine.backtest_tradestation,
            strategy_data,
        )
        return result


# ==============================================================================
#  Option 3:  Standalone test script (for quick validation)
# ==============================================================================

def run_standalone_test():
    """
    Run a quick test without the FastAPI server.
    Copy your strategy JSON into this function.
    """
    import json
    from app.schemas.strategy import StrategyRequest

    strategy_json = {
        "id": 14,
        "name": "Spy Etf",
        "rebalance": "DAILY",
        "start_date": "2000-01-01",
        "end_date": "2021-12-31",
        "min_quantity": 1,
        "min_price": 0,
        "system_type": "LONG",
        "market_regime_type": "Individual ETFs - Simple",
        "regimes": [
            {
                "id": 1044,
                "strategy_id": 14,
                "regime_type": "Individual ETFs - Simple",
                "regime_ticker": "SPY",
                "entry_timing": "close",
                "exit_timing": "open",
                "stoploss_type": "",
                "takeprofit_type": "DOLLAR_BASED",
                "stoploss_dollar": 0,
                "takeprofit_dollar": 2000,
                "stoploss_timing": "",
                "takeprofit_timing": "INTRADAY",
                "order_type": "NORMAL",
                "universe": "SPY",
                "capital": 37500,
                "slots": 1,
                "max_time": 11,
                "banned_months": [],
                "market_trend_rules_tree": {
                    "type": "group", "id": "root", "logic": "AND",
                    "children": [{
                        "type": "group", "logic": "AND",
                        "children": [{
                            "type": "rule",
                            "rule": {
                                "indicator": "atr", "lookback": 68,
                                "operator": ">", "value": 0,
                                "value_type": "indicator_price",
                                "value_indicator": "atr", "value_lookback": 24
                            }
                        }]
                    }]
                },
                "entry_rules_tree": {
                    "type": "group", "id": "root", "logic": "AND",
                    "children": [{
                        "type": "group", "logic": "AND",
                        "children": [
                            {
                                "type": "rule",
                                "rule": {
                                    "indicator": "close", "lookback": 0,
                                    "operator": "<", "value": 0,
                                    "value_type": "indicator_price",
                                    "value_indicator": "range_close",
                                    "value_lookback": 200,
                                    "value_range_percent": 25
                                }
                            },
                            {
                                "type": "rule",
                                "rule": {
                                    "indicator": "close", "lookback": 0,
                                    "operator": ">", "value": 0,
                                    "value_type": "indicator_price",
                                    "value_indicator": "sma",
                                    "value_lookback": 200
                                }
                            }
                        ]
                    }]
                },
                "exit_rules_tree": {"type": "group", "id": "root", "logic": "AND", "children": []},
                "freeze_rules_tree": {"type": "group", "id": "root", "logic": "AND", "children": []},
                "resume_rules_tree": {"type": "group", "id": "root", "logic": "AND", "children": []}
            },
            {
                "id": 1045,
                "strategy_id": 14,
                "regime_type": "Individual ETFs - Simple",
                "regime_ticker": "SPY",
                "entry_timing": "close",
                "exit_timing": "open",
                "stoploss_type": "DOLLAR_BASED",
                "takeprofit_type": "DOLLAR_BASED",
                "stoploss_dollar": 6375,
                "takeprofit_dollar": 3750,
                "stoploss_timing": "INTRADAY",
                "takeprofit_timing": "INTRADAY",
                "order_type": "NORMAL",
                "universe": "SPY",
                "capital": 37500,
                "slots": 1,
                "max_time": 24,
                "banned_months": [],
                "market_trend_rules_tree": {
                    "type": "group", "id": "root", "logic": "AND",
                    "children": [{
                        "type": "group", "logic": "AND",
                        "children": [{
                            "type": "rule",
                            "rule": {
                                "indicator": "atr", "lookback": 68,
                                "operator": "<", "value": 0,
                                "value_type": "indicator_price",
                                "value_indicator": "atr", "value_lookback": 24
                            }
                        }]
                    }]
                },
                "entry_rules_tree": {
                    "type": "group", "id": "root", "logic": "AND",
                    "children": [{
                        "type": "group", "logic": "AND",
                        "children": [{
                            "type": "rule",
                            "rule": {
                                "indicator": "close", "lookback": 0,
                                "operator": "<", "value": 0,
                                "value_type": "indicator_price",
                                "value_indicator": "range_close",
                                "value_lookback": 0,
                                "value_range_percent": 10
                            }
                        }]
                    }]
                },
                "exit_rules_tree": {"type": "group", "id": "root", "logic": "AND", "children": []},
                "freeze_rules_tree": {"type": "group", "id": "root", "logic": "AND", "children": []},
                "resume_rules_tree": {"type": "group", "id": "root", "logic": "AND", "children": []}
            }
        ]
    }

    strategy = StrategyRequest(**strategy_json)
    engine = BacktestEngine()
    result = engine.backtest_tradestation(strategy)

    print(f"\n{'='*60}")
    print(json.dumps(result['summary'], indent=2))


if __name__ == '__main__':
    run_standalone_test()
