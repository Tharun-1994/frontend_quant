from app.constants.static_config import UNIVERSES_Codes


class PricePath:

    commonBacktestingPath = r'C:\Tharun\Projects\backtest_data\inputs'



    sp500BacktestingPath = f'{commonBacktestingPath}/sp500'
    lq500BacktestingPath = f'{commonBacktestingPath}/liquid500'
    r3000BacktestingPath = f'{commonBacktestingPath}/r3000'

    # Universes Path
    sp500base_path=r'C:\Tharun\Projects\backtest_data\universes\sp500'
    liquid500base_path=r'C:\Tharun\Projects\backtest_data\universes\liquid500'
    russell3000base_path=r'C:\Tharun\Projects\backtest_data\universes\russell3000'

    index_path=r'C:\Tharun\Projects\backtest_data\universes\index'

    commonOutputPath = r'C:\Tharun\Projects\backtest_data\outputs'

    @staticmethod
    def getBacktestInputPath(universe=""):
        if universe == UNIVERSES_Codes['S&P 500']:
            return PricePath.sp500BacktestingPath
        elif universe == UNIVERSES_Codes['Liquid 500']:
            return PricePath.lq500BacktestingPath
        elif universe == UNIVERSES_Codes['Russell 3000']:
            return PricePath.r3000BacktestingPath

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
    def lows(path):
        return f'{path}/daily_lows.csv'

    @staticmethod
    def volumes(path):
        return f'{path}/daily_volumes.csv'

    @staticmethod
    def unadjustedCloses(path):
        return f'{path}/daily_unadjusted.csv'

    @staticmethod
    def spy_closes(path):
        return f'{path}/daily_closes_spy.csv'

