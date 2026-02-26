"""
ETF Backtest Engine (Optimized)
================================
Main orchestrator for Individual ETF backtesting.

Key optimizations vs previous version:
- Minute-bar SL/TP uses raw numpy arrays (no iterrows/pandas overhead)
- All daily price/indicator lookups are O(1) dict.get()
- No perf timing context managers in hot loop
- Pre-cached regime flags to avoid repeated string comparisons
- Minimal function call overhead in inner loops
"""

import os
import json
import time
from datetime import date, datetime
from typing import Optional
from tqdm import tqdm

from app.Backtest.portfolio_etf import ETFPortfolio
from app.Backtest.price_data_etf import ETFPriceData
from app.Backtest.rule_evaluator import RuleEvaluator


class ETFBacktestEngine:

    def __init__(self):
        pass

    # ------------------------------------------------------------------
    #  PUBLIC
    # ------------------------------------------------------------------
    def run(self, strategy_data, base_input_path: str, output_path: str) -> dict:

        regimes = strategy_data.regimes
        first_regime = regimes[0]
        ticker = first_regime.regime_ticker.lower()

        capital = first_regime.capital or 37500
        slots = first_regime.slots or 1

        start_dt = self._parse_date(strategy_data.start_date)
        end_dt = self._parse_date(strategy_data.end_date)

        # --- Load data -----------------------------------------------
        t0 = time.perf_counter()
        print(f'[ETFBacktest] Loading parquets from {base_input_path} …')
        price_data = ETFPriceData(base_path=base_input_path, ticker=ticker)
        price_data.load()
        print(f'[ETFBacktest] Loaded in {time.perf_counter() - t0:.2f}s | '
              f'{len(price_data.all_dates)} dates | '
              f'{len(price_data.trading_dates)} trading dates')

        # --- Pre-cache regime flags -----------------------------------
        regime_cache = []
        for r in regimes:
            regime_cache.append({
                'regime': r,
                'sl_timing': (r.stoploss_timing or '').upper(),
                'tp_timing': (r.takeprofit_timing or '').upper(),
                'sl_type': (r.stoploss_type or '').upper(),
                'tp_type': (r.takeprofit_type or '').upper(),
                'sl_dollar': r.stoploss_dollar or 0,
                'tp_dollar': r.takeprofit_dollar or 0,
                'sl_pct': r.stoploss_pct or 0,
                'tp_pct': r.takeprofit_pct or 0,
                'max_time': r.max_time or 0,
                'entry_timing': (r.entry_timing or 'close').lower(),
                'banned_set': set(r.banned_months or []),
            })

        # --- Portfolio ------------------------------------------------
        portfolio = ETFPortfolio(starting_capital=capital, slots=slots,
                                 ticker=ticker.upper())

        system_type = (strategy_data.system_type or 'LONG').lower()

        # --- Local references for speed -------------------------------
        get_open = price_data.get_daily_open
        get_close = price_data.get_daily_close
        get_minute = price_data.get_minute_arrays
        trading_dates = price_data.trading_dates
        all_dates = price_data.all_dates

        # --- Run loop -------------------------------------------------
        t1 = time.perf_counter()
        print(f'[ETFBacktest] Running {start_dt} → {end_dt} …')

        active_regime = None
        active_rc = None  # cached regime flags

        for d in tqdm(all_dates, desc='ETF Backtest'):

            if d < start_dt or d > end_dt:
                continue

            today_open = get_open(d)
            today_close = get_close(d)
            if today_open is None or today_close is None:
                continue

            has_pos = portfolio.has_open_position

            # ============ START OF DAY ================================

            # 1. Pending exit at OPEN
            if portfolio.pending_exit and has_pos:
                portfolio.close_trade(d, today_open,
                                      portfolio.pending_exit, 'open')
                has_pos = False

            # 2. Intraday SL/TP scan (numpy-based)
            if has_pos and active_rc is not None:
                closed = self._intraday_sl_tp_fast(
                    d, get_minute, portfolio, active_rc)
                if closed:
                    has_pos = False

            # 3. Close-of-day SL/TP
            if has_pos and active_rc is not None:
                closed = self._check_close_sl_tp_fast(
                    d, today_close, portfolio, active_rc)
                if closed:
                    has_pos = False

            # ============ END OF DAY ==================================

            # 4. Mark to market
            portfolio.mark_to_market(d, today_close)

            # 5. Update day count
            if has_pos:
                portfolio.update_trade_day_count()
                portfolio.bars_since_entry += 1

            # --- Only on rebalance dates ------------------------------
            if d not in trading_dates:
                continue

            # 6a. Max-time check
            if has_pos and active_rc is not None:
                max_t = active_rc['max_time']
                if max_t > 0:
                    if portfolio.trade_logger[portfolio.live_trade]['dayCount'] >= max_t:
                        portfolio.pending_exit = f'MaxTime {max_t}'

            # 6b. Determine active regime
            active_regime, active_rc = self._determine_regime_fast(
                d, regimes, regime_cache, price_data, ticker)

            # 6c. Entry check
            if (not has_pos
                    and portfolio.pending_exit is None
                    and active_regime is not None):

                if d.month in active_rc['banned_set']:
                    continue

                if RuleEvaluator.evaluate_tree(
                        active_regime.entry_rules_tree, d, price_data, ticker):

                    entry_price = (today_close
                                   if active_rc['entry_timing'] == 'close'
                                   else today_open)

                    portfolio.enter_trade(
                        trade_date=d,
                        price=entry_price,
                        reason=f'Entry – Regime {active_regime.id}',
                        regime_id=active_regime.id,
                        entry_timing=active_rc['entry_timing'],
                        direction=system_type,
                        capital=active_regime.capital or capital,
                    )

        # ============ BACKTEST END ====================================
        last_close = get_close(end_dt)
        if last_close is None:
            for dd in reversed(all_dates):
                if dd <= end_dt:
                    last_close = get_close(dd)
                    if last_close is not None:
                        end_dt = dd
                        break

        if last_close:
            portfolio.end_of_backtest(end_dt, last_close)

        result = portfolio.get_portfolio()
        self._write_output(result, output_path)
        self._print_summary(result, portfolio)

        elapsed = time.perf_counter() - t1
        print(f'[ETFBacktest] Loop completed in {elapsed:.2f}s')

        return result

    # ------------------------------------------------------------------
    #  Regime selection (returns both regime obj and cached flags)
    # ------------------------------------------------------------------
    @staticmethod
    def _determine_regime_fast(d, regimes, regime_cache, price_data, ticker):
        for i, r in enumerate(regimes):
            tree = r.market_trend_rules_tree
            if not tree or not tree.get('children'):
                return r, regime_cache[i]
            if RuleEvaluator.evaluate_tree(tree, d, price_data, ticker):
                return r, regime_cache[i]
        return None, None

    # ------------------------------------------------------------------
    #  Intraday SL/TP — numpy arrays, zero pandas overhead
    # ------------------------------------------------------------------
    def _intraday_sl_tp_fast(self, d, get_minute, portfolio, rc) -> bool:
        """Returns True if trade was closed."""
        trade = portfolio.trade_logger[portfolio.live_trade]
        entry_price = trade['entryPrice']
        amount = trade['quantity']

        # Compute SL price
        sl_price = None
        if rc['sl_timing'] == 'INTRADAY':
            if rc['sl_type'] == 'DOLLAR_BASED' and rc['sl_dollar'] > 0:
                sl_price = entry_price - (rc['sl_dollar'] / amount)
            elif rc['sl_type'] == 'PCT_BASED' and rc['sl_pct'] > 0:
                sl_price = entry_price * (1 - rc['sl_pct'] / 100)

        # Compute TP price
        tp_price = None
        if rc['tp_timing'] == 'INTRADAY':
            if rc['tp_type'] == 'DOLLAR_BASED' and rc['tp_dollar'] > 0:
                tp_price = entry_price + (rc['tp_dollar'] / amount)
            elif rc['tp_type'] == 'PCT_BASED' and rc['tp_pct'] > 0:
                tp_price = entry_price * (1 + rc['tp_pct'] / 100)

        if sl_price is None and tp_price is None:
            return False

        arrays = get_minute(d)
        if arrays is None:
            return False

        opens, highs, lows, closes = arrays
        n = len(opens)

        for i in range(n):
            o = opens[i]
            h = highs[i]
            lo = lows[i]

            # Stop-loss
            if sl_price is not None:
                if o <= sl_price:
                    portfolio.close_trade(d, round(float(o), 2),
                                          f'StopLoss Hit: {sl_price:.2f}', 'open')
                    return True
                if lo <= sl_price:
                    portfolio.close_trade(d, round(sl_price, 2),
                                          f'StopLoss Hit: {sl_price:.2f}', 'stoploss price')
                    return True

            # Take-profit
            if tp_price is not None:
                if o >= tp_price:
                    portfolio.close_trade(d, round(float(o), 2),
                                          f'TakeProfit Hit: {tp_price:.2f}', 'open')
                    return True
                if h >= tp_price:
                    portfolio.close_trade(d, round(tp_price, 2),
                                          f'TakeProfit Hit: {tp_price:.2f}', 'takeprofit price')
                    return True

        return False

    # ------------------------------------------------------------------
    #  Close-of-day SL/TP
    # ------------------------------------------------------------------
    @staticmethod
    def _check_close_sl_tp_fast(d, today_close, portfolio, rc) -> bool:
        """Returns True if trade was closed."""
        trade = portfolio.trade_logger[portfolio.live_trade]
        entry_price = trade['entryPrice']
        amount = trade['quantity']
        current_pnl = (today_close - entry_price) * amount

        # CLOSE timing SL
        if rc['sl_timing'] == 'CLOSE' and rc['sl_dollar'] > 0:
            sl_p = entry_price - (rc['sl_dollar'] / amount)
            if today_close < sl_p:
                portfolio.close_trade(d, today_close,
                                      f'StopLoss Hit: {sl_p:.2f}', 'close')
                return True

        # CLOSE timing TP
        if rc['tp_timing'] == 'CLOSE' and rc['tp_dollar'] > 0:
            tp_p = entry_price + (rc['tp_dollar'] / amount)
            if today_close >= tp_p:
                portfolio.close_trade(d, today_close,
                                      f'TakeProfit Hit: {tp_p:.2f}', 'close')
                return True

        # NEXT_BAR_OPEN timing
        if rc['sl_timing'] == 'NEXT_BAR_OPEN' and rc['sl_dollar'] > 0:
            if current_pnl <= -rc['sl_dollar']:
                portfolio.pending_exit = 'StopLoss Hit – next bar open'
                return False

        if rc['tp_timing'] == 'NEXT_BAR_OPEN' and rc['tp_dollar'] > 0:
            if current_pnl >= rc['tp_dollar']:
                portfolio.pending_exit = 'TakeProfit Hit – next bar open'
                return False

        return False

    # ------------------------------------------------------------------
    #  Output
    # ------------------------------------------------------------------
    @staticmethod
    def _write_output(result: dict, output_path: str):
        os.makedirs(output_path, exist_ok=True)

        with open(os.path.join(output_path, 'backtest_result.json'), 'w') as f:
            json.dump(result, f, indent=2, default=str)

        with open(os.path.join(output_path, 'TradeList.json'), 'w') as f:
            json.dump(result['tradeLogger'], f, indent=2, default=str)

        with open(os.path.join(output_path, 'Equity.json'), 'w') as f:
            json.dump(result['equityLogger'], f, indent=2, default=str)

        print(f'[ETFBacktest] Output → {output_path}')

    @staticmethod
    def _print_summary(result: dict, portfolio):
        trades = result['tradeLogger']
        completed = {k: v for k, v in trades.items() if v.get('exitDate')}
        profits = [v['profit'] for v in completed.values() if v.get('profit') is not None]
        winners = [p for p in profits if p > 0]
        losers = [p for p in profits if p < 0]

        total_profit = sum(profits) if profits else 0
        win_rate = len(winners) / len(completed) * 100 if completed else 0
        pf_denom = abs(sum(losers)) if losers else 0
        profit_factor = round(sum(winners) / pf_denom, 2) if pf_denom else 'Inf'

        eq_values = result['equityLogger']
        dd_vals = [v['dailyDrawdown'] for v in eq_values.values()]
        max_dd = max(dd_vals) if dd_vals else 0

        print(f'\n--- ETF Backtest complete ---')
        print(f'  Trades:        {len(completed)}')
        print(f'  Winners:       {len(winners)}  |  Losers: {len(losers)}')
        print(f'  Win Rate:      {win_rate:.1f}%')
        print(f'  Total P&L:     ${total_profit:,.2f}')
        print(f'  Profit Factor: {profit_factor}')
        print(f'  Max Drawdown:  ${max_dd:,.2f}')

    @staticmethod
    def _parse_date(date_str: str) -> date:
        return datetime.strptime(date_str, '%Y-%m-%d').date()
