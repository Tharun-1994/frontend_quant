from app.constants.static_config import UNIVERSES_Codes
from pathlib import Path
import datetime as dt

class PricePath:



    backtestPath = r'C:\Tharun\Projects\backtest_data'

    commonBacktestingPath = r'C:\Tharun\Projects\backtest_data\inputs'



    sp500BacktestingPath = f'{commonBacktestingPath}/sp500'
    lq500BacktestingPath = f'{commonBacktestingPath}/liquid500'
    r3000BacktestingPath = f'{commonBacktestingPath}/r3000'

    # Universes Path
    sp500base_path = r'C:\Tharun\Projects\backtest_data\universes\sp500'
    liquid500base_path = r'C:\Tharun\Projects\backtest_data\universes\liquid500'
    russell3000base_path = r'C:\Tharun\Projects\backtest_data\universes\russell3000'
    spy_path = r'C:\Tharun\Projects\backtest_data\universes\spy'
    sp100base_path = r'C:\Tharun\Projects\backtest_data\universes\sp100'
    nasdaq100base_path = r'C:\Tharun\Projects\backtest_data\universes\nasdaq100'
    lra_14base_path = r'C:\Tharun\Projects\backtest_data\universes\lra_14'  # LRA Patch 44

    index_path = r'C:\Tharun\Projects\backtest_data\universes\index'

    # ── Live universe paths — nightly-refreshed, used ONLY by execution ────
    # Backtest paths above stay static (manually maintained, frozen for the
    # manager-demo tradelists). Live paths below are rewritten every nightly
    # by live_universe_pipeline.py with only ~5 years of Norgate data —
    # enough to feed the 2023-01-01 execution start floor with indicator
    # warmup buffer, never enough to corrupt the backtest snapshot.
    live_universes_root = r'C:\Tharun\Projects\backtest_data\live_universes'
    sp500_live_base_path = r'C:\Tharun\Projects\backtest_data\live_universes\sp500'
    spy_live_path = r'C:\Tharun\Projects\backtest_data\live_universes\spy'
    # Patch 92: live path for liquid500 execution.
    # live_universe_pipeline.py rebuilds this nightly from the
    # source-of-truth membership at universes/liquid500/liquid500.csv,
    # pulling ~5 years of Norgate prices for the active members.
    liquid500_live_base_path = r'C:\Tharun\Projects\backtest_data\live_universes\liquid500'

    commonOutputPath = r'C:\Tharun\Projects\backtest_data\outputs'

    @staticmethod
    def getExecDataInputPath(universe="", run_date=None):
        """C1 Patch 1: exec_data path for production (nightly execution) runs.

        Layout: <DATA_ROOT>/exec_data/{YYYYMMDD}/{universe}/
          - YYYYMMDD = run_date = the *data date* (the day Norgate just posted
            EOD data). If C1 fires on Tuesday night using Tuesday's close,
            the folder is named with Tuesday's date.
          - universe = e.g. "sp500" — universe-shared, no strategy segment.
            Multiple strategies on the same universe overwrite each other
            with identical content (indicator parquets are deterministic
            per-universe).

        Engine reads this path via Patch 14: middleware sends the full
        {YYYYMMDD}-stamped path as `data_root` in ExecutionStepRequestDto,
        BacktestContext.executionDataRoot is set from it, and
        BacktestContext.inputPath() resolves {data_root}/{universe}/
        instead of the legacy backtest_data/{strategy}/input/{universe}.

        Position Manager passes the trade date (= D = run_date + 1 trading
        day) separately as ExecutionStepRequestDto.runDate.
        """

        date_str = (run_date or dt.date.today()).strftime("%Y%m%d")
        full_path = Path(PricePath.backtestPath) / 'exec_data' / date_str / universe
        full_path.mkdir(parents=True, exist_ok=True)
        return str(full_path)

    @staticmethod
    def getBacktestInputPath(universe="", strategy_name=""):
        valid_universes = [
            UNIVERSES_Codes['S&P 500'],
            UNIVERSES_Codes['S&P 100'],
            UNIVERSES_Codes['Nasdaq 100'],
            UNIVERSES_Codes['Liquid 500'],
            UNIVERSES_Codes['Russell 3000'],
            UNIVERSES_Codes['SPY'],
            UNIVERSES_Codes['LRA 14'],  # LRA Patch 44.4 — was returning None silently
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
