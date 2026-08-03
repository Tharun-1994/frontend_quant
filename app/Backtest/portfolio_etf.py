"""
ETF Portfolio
=============
Portfolio manager for single-ETF backtesting.
Output format matches the Java BacktestResponseDto exactly:

    {
      "tradeLogger": { "<tradeId>": { TradeLog fields ... } },
      "equityLogger": { "<date>": { EquityLog fields ... } }
    }

TradeLog fields (Java):
    symbol, direction, quantity, capital,
    entryDate, entryPrice, entryValue, entryReason, entryTiming,
    exitDate, exitPrice, exitValue, exitReason, exitTiming,
    profit, profitPercentage, dayCount

EquityLog fields (Java):
    equityValue, dailyDrawdown, dayEndUtility, dayEndUtilityValue
"""

from datetime import date
from math import floor
from typing import Optional, Dict
from collections import OrderedDict


class ETFPortfolio:

    def __init__(self, starting_capital: float, slots: int, ticker: str):
        self.starting_capital = starting_capital
        self.slots = slots
        self.ticker = ticker.upper()

        # ---- Java-matching loggers (LinkedHashMap equivalents) ----
        self.trade_logger: OrderedDict[str, dict] = OrderedDict()     # tradeId → TradeLog
        self.equity_logger: OrderedDict[str, dict] = OrderedDict()    # date_str → EquityLog
        self.live_holdings_logger: Dict[str, list] = {}               # tradeId → [LiveHoldingsTracker]

        # ---- State ----
        self.unused_capital: float = starting_capital
        self.max_equity: float = float('-inf')
        self.max_equity_date: Optional[date] = None
        self.trade_counter: int = 0

        # ---- Position tracking ----
        self.live_trade: Optional[str] = None
        self.bars_since_entry: int = 0
        self.max_position_profit: float = 0.0
        self.pending_exit: Optional[str] = None

    # ------------------------------------------------------------------
    #  Properties
    # ------------------------------------------------------------------
    @property
    def has_open_position(self) -> bool:
        return self.live_trade is not None

    @property
    def live_count(self) -> int:
        return len(self.live_holdings_logger)

    # ------------------------------------------------------------------
    #  Position sizing  (matches Java: Math.floor(slotCapital / price))
    # ------------------------------------------------------------------
    def get_shares_amount(self, price: float) -> int:
        slot_capital = self.starting_capital / self.slots
        return int(floor(slot_capital / price))

    # ------------------------------------------------------------------
    #  ENTER TRADE  (mirrors Java enterTrade)
    # ------------------------------------------------------------------
    def enter_trade(
        self,
        trade_date: date,
        price: float,
        reason: str,
        regime_id: int = 0,
        entry_timing: str = 'close',
        direction: str = 'long',
        capital: float = 0,
    ) -> bool:
        amount = self.get_shares_amount(price)
        if amount <= 0:
            return False

        if self.live_count >= self.slots:
            return False

        self.trade_counter += 1
        trade_id = f'{self.ticker}_{trade_date.isoformat()}_{self.trade_counter}'

        entry_value = round(price * amount, 2)

        if capital <= 0:
            capital = self.starting_capital / self.slots

        # TradeLog (matches Java TradeLog fields exactly)
        self.trade_logger[trade_id] = {
            'symbol': self.ticker,
            'direction': direction,
            'quantity': amount,
            'capital': round(capital, 2),
            'entryDate': trade_date.isoformat(),
            'entryPrice': round(price, 4),
            'entryValue': round(entry_value, 2),
            'entryReason': reason,
            'entryTiming': entry_timing,
            'exitDate': None,
            'exitPrice': None,
            'exitValue': None,
            'exitReason': None,
            'exitTiming': None,
            'profit': None,
            'profitPercentage': None,
            'dayCount': 0,
            'regimeId': regime_id,
        }

        # LiveHoldingsTracker list (matches Java List<LiveHoldingsTracker>)
        self.live_holdings_logger[trade_id] = []

        self.unused_capital -= round(entry_value)
        self.live_trade = trade_id
        self.bars_since_entry = 0
        self.max_position_profit = 0.0
        self.pending_exit = None

        return True

    # ------------------------------------------------------------------
    #  EXIT TRADE  (mirrors Java exitTrade)
    # ------------------------------------------------------------------
    def close_trade(
        self,
        trade_date: date,
        price: float,
        reason: str,
        exit_timing: str = 'open',
    ) -> bool:
        if not self.has_open_position:
            return False

        trade_id = self.live_trade
        trade_log = self.trade_logger[trade_id]

        exit_value = round(price * trade_log['quantity'])
        profit = round(exit_value - trade_log['entryValue'], 2)
        entry_val = trade_log['entryValue'] if trade_log['entryValue'] != 0 else 1
        profit_pct = round(profit / entry_val, 4)

        trade_log['exitDate'] = trade_date.isoformat()
        trade_log['exitPrice'] = round(price, 4)
        trade_log['exitValue'] = round(exit_value, 2)
        trade_log['exitReason'] = reason
        trade_log['exitTiming'] = exit_timing
        trade_log['profit'] = profit
        trade_log['profitPercentage'] = profit_pct

        # Store value tracker then remove from live
        trade_log['valueTracker'] = list(self.live_holdings_logger.get(trade_id, []))
        self.live_holdings_logger.pop(trade_id, None)

        self.unused_capital += exit_value
        self.live_trade = None
        self.bars_since_entry = 0
        self.max_position_profit = 0.0
        self.pending_exit = None

        return True

    # ------------------------------------------------------------------
    #  MARK TO MARKET  (mirrors Java markToMarket)
    # ------------------------------------------------------------------
    def mark_to_market(self, trade_date: date, close_price: float):
        today_equity = self.unused_capital

        if self.live_holdings_logger:
            for trade_id in self.live_holdings_logger:
                trade_row = self.trade_logger[trade_id]
                amount = trade_row['quantity']
                eod_value = round(amount * close_price, 2)

                today_equity += eod_value

                # Append LiveHoldingsTracker (matches Java entity)
                self.live_holdings_logger[trade_id].append({
                    'symbol': trade_row['symbol'],
                    'endOfDayValue': eod_value,
                    'tradeDate': trade_date.isoformat(),
                })

                # Track max unrealised profit
                pnl = eod_value - trade_row['entryValue']
                if pnl > self.max_position_profit:
                    self.max_position_profit = pnl

        # Update max equity
        if today_equity > self.max_equity:
            self.max_equity = today_equity
            self.max_equity_date = trade_date

        daily_drawdown = round(self.max_equity - today_equity, 2)

        # EquityLog (matches Java EquityLog exactly)
        slot_capital = self.starting_capital / self.slots
        self.equity_logger[trade_date.isoformat()] = {
            'equityValue': round(today_equity, 2),
            'dailyDrawdown': daily_drawdown,
            'dayEndUtility': self.live_count,
            'dayEndUtilityValue': round(self.live_count * slot_capital, 2),
        }

    # ------------------------------------------------------------------
    #  UPDATE DAY COUNT  (mirrors Java updateTradeDayCount)
    # ------------------------------------------------------------------
    def update_trade_day_count(self):
        for trade_id in self.live_holdings_logger:
            self.trade_logger[trade_id]['dayCount'] += 1

    # ------------------------------------------------------------------
    #  END OF BACKTEST  (mirrors Java endOfBacktest)
    # ------------------------------------------------------------------
    def end_of_backtest(self, trade_date: date, close_price: float):
        if self.has_open_position:
            self.close_trade(trade_date, close_price, 'End Of Backtest', 'close')

        slot_capital = self.starting_capital / self.slots
        self.equity_logger[trade_date.isoformat()] = {
            'equityValue': round(self.unused_capital, 2),
            'dailyDrawdown': round(self.max_equity - self.unused_capital, 2),
            'dayEndUtility': self.live_count,
            'dayEndUtilityValue': round(self.live_count * slot_capital, 2),
        }

    # ------------------------------------------------------------------
    #  GET PORTFOLIO  (mirrors Java getPortfolio → BacktestResponseDto)
    # ------------------------------------------------------------------
    def get_portfolio(self) -> dict:
        """
        Returns the exact same structure as Java BacktestResponseDto:

            {
              "tradeLogger": { "SPY_2000-01-03_1": { ...TradeLog... }, ... },
              "equityLogger": { "2000-01-03": { ...EquityLog... }, ... }
            }
        """
        trade_logger_output = OrderedDict()
        for tid, tlog in self.trade_logger.items():
            row = {k: v for k, v in tlog.items() if k != 'valueTracker'}
            trade_logger_output[tid] = row

        return {
            'tradeLogger': trade_logger_output,
            'equityLogger': dict(self.equity_logger),
        }

    # ------------------------------------------------------------------
    #  Convenience helpers
    # ------------------------------------------------------------------
    def get_current_pnl(self, current_price: float) -> float:
        if not self.has_open_position:
            return 0.0
        t = self.trade_logger[self.live_trade]
        return (t['quantity'] * current_price) - t['entryValue']

    def get_live_holdings_symbols(self) -> set:
        """Mirrors Java getLiveHoldingsLogger → Set<String> of symbols."""
        return {tid.split('_')[0] for tid in self.live_holdings_logger}
