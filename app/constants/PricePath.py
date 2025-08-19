
class PricePath:

    commonBacktestingPath = r'C:\Tharun\Projects\backtest_data\inputs'
    sp500base_path=r'C:\Tharun\Projects\backtest_data\universes\sp500'
    commonOutputPath = r'C:\Tharun\Projects\backtest_data\outputs'

    @staticmethod
    def getCommonPath():
        return PricePath.commonBacktestingPath

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

