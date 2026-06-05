from app.constants.static_config import UNIVERSES_Codes
from pathlib import Path

class PricePath:



    backtestPath = r'C:\Tharun\Projects\backtest_data'

    commonBacktestingPath = r'C:\Tharun\Projects\backtest_data\inputs'



    sp500BacktestingPath = f'{commonBacktestingPath}/sp500'
    lq500BacktestingPath = f'{commonBacktestingPath}/liquid500'
    r3000BacktestingPath = f'{commonBacktestingPath}/r3000'

    # Universes Path
    sp500base_path=r'C:\Tharun\Projects\backtest_data\universes\sp500'
    liquid500base_path=r'C:\Tharun\Projects\backtest_data\universes\liquid500'
    russell3000base_path=r'C:\Tharun\Projects\backtest_data\universes\russell3000'
    spy_path = r'C:\Tharun\Projects\backtest_data\universes\spy'
    sp100base_path = r'C:\Tharun\Projects\backtest_data\universes\sp100'
    nasdaq100base_path = r'C:\Tharun\Projects\backtest_data\universes\nasdaq100'

    index_path=r'C:\Tharun\Projects\backtest_data\universes\index'

    commonOutputPath = r'C:\Tharun\Projects\backtest_data\outputs'

    @staticmethod
    def getBacktestInputPath(universe="",strategy_name=""):
        valid_universes = [
            UNIVERSES_Codes['S&P 500'],
            UNIVERSES_Codes['S&P 100'],
            UNIVERSES_Codes['Nasdaq 100'],
            UNIVERSES_Codes['Liquid 500'],
            UNIVERSES_Codes['Russell 3000'],
            UNIVERSES_Codes['SPY']
        ]

        if universe in valid_universes:
            # 2. Construct the full path
            full_path = Path(PricePath.backtestPath)/ strategy_name/ 'input' / universe

            # 3. Create the directory if it doesn't exist
            full_path.mkdir(parents=True, exist_ok=True)

            # 4. Return as string (if your system requires strings)
            return str(full_path)

    @staticmethod
    def getCommonOutputPath():
        return PricePath.commonOutputPath

    @staticmethod
    def universe(path):
        return f'{path}.csv'

    @staticmethod
    def close(path):
        return f'{path}/daily_closes.csv'

    @staticmethod
    def opens(path):
        return f'{path}/daily_opens.csv'

    @staticmethod
    def highs(path):
        return f'{path}/daily_highs.csv'

    @staticmethod
    def lows(path):
        return f'{path}/daily_lows.csv'

    @staticmethod
    def volumes(path):
        return f'{path}/daily_volumes.csv'

    @staticmethod
    def unadjustedCloses(path):
        return f'{path}/daily_unadjusted.csv'

    @staticmethod
    def turnovers(path):
        return f'{path}/daily_turnovers.csv'

    @staticmethod
    def spy_closes(path):
        return f'{path}/daily_closes_spy.csv'

    @staticmethod
    def spy_prices(path):
        """Legacy: original single SPY file."""
        return f'{path}/SPY.txt'

    @staticmethod
    def spy_daily_prices(path):
        """SPY daily OHLC (one row per day)."""
        return f'{path}/SPY_1M/SPY_1D.txt'

    @staticmethod
    def spy_minute_prices(path):
        """SPY minute OHLC (one row per minute bar)."""
        return f'{path}/SPY_1M/SPY_1M.txt'
