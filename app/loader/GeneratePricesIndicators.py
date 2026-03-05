from app.constants.PricePath import PricePath
from app.constants.static_config import UNIVERSES, FUNCTION_MAPPER, timeframe_map
from app.loader.PriceDataLoader import PriceDataLoader
from app.loader.TechnicalIndicators import IndicatorCalculator, INDICATOR_REGISTRY
from app.models.strategy_bucket import StrategyBucket
from typing import Any, Dict, Iterator, Optional
from app.schemas.strategy import MarketRegimeBase
import pandas as pd
from app.schemas.strategy import Rule

class GeneratePricesIndicators:
    @staticmethod
    def call_indicator(name: str, **kwargs):
        func = INDICATOR_REGISTRY.get(name)
        if not func:
            raise ValueError(f"Function {name} is not a registered indicator.")
        return func(**kwargs)

    @staticmethod
    def iter_rules_from_tree(node: Optional[Dict[str, Any]]) -> Iterator[Rule]:
        if not node:
            return

        if node.get("type") == "rule":
            r = node.get("rule") or {}
            if r:
                # ✅ build Rule safely (handles missing keys)
                yield Rule(
                    indicator=r.get("indicator", ""),
                    lookback=int(r.get("lookback") or 0),
                    operator=r.get("operator") or "",
                    value=float(r.get("value") or 0),
                    connector=r.get("connector") or "",
                    label=r.get("label"),
                    value_type=r.get("value_type"),
                    value_indicator=r.get("value_indicator") or "",
                    value_lookback=int(r.get("value_lookback") or 0),
                    value_range_percent=int(r.get("value_range_percent") or 0),
                    params=r.get("params") or None,
                )
            return

        for c in node.get("children") or []:
            yield from GeneratePricesIndicators.iter_rules_from_tree(c)

    @staticmethod
    def resample_to_timeframe(price_data, timeframe='D'):
        """
        Resample intraday data to any timeframe

        Parameters:
        -----------
        price_data : DataFrame with OHLC data
        timeframe : str
            'D' = Daily
            'W' = Weekly
            'M' = Monthly
            'H' = Hourly
            '5T' or '5min' = 5 minutes
            '15T' = 15 minutes, etc.
        """
        resampled_df = price_data.resample(timeframe).agg({
            col: agg for col, agg in {
                'Open': 'first',
                'High': 'max',
                'Low': 'min',
                'Close': 'last',
                'Up': 'sum',
                'Down': 'sum',
            }.items() if col in price_data.columns
        }).dropna(subset=['Open'])  # Remove empty periods

        return resampled_df

    @staticmethod
    def _compute_atr_limits(marketRegime, price_data, rebalance, indicator_set):
        """Compute ATR indicators needed for LIMIT order, Stoploss, Take Profit."""
        atr_lookbacks = []

        if (marketRegime.order_type and marketRegime.order_type == 'LIMIT_ATR'
                and marketRegime.atr_limit_lookback and marketRegime.atr_limit_lookback > 0):
            atr_lookbacks.append(marketRegime.atr_limit_lookback)

        if (marketRegime.stoploss_type and marketRegime.stoploss_type == 'ATR_BASED'
                and marketRegime.atr_lookback_stp and marketRegime.atr_lookback_stp > 0):
            atr_lookbacks.append(marketRegime.atr_lookback_stp)

        if (marketRegime.takeprofit_type and marketRegime.takeprofit_type == 'ATR_BASED'
                and marketRegime.atr_lookback_tp and marketRegime.atr_lookback_tp > 0):
            atr_lookbacks.append(marketRegime.atr_lookback_tp)

        for lookback in atr_lookbacks:
            key = f'{FUNCTION_MAPPER["atr"]}_{lookback}'
            if key not in indicator_set:
                result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER["atr"],
                                                                 Highs=price_data[f'{rebalance}_highs'],
                                                                 Lows=price_data[f'{rebalance}_lows'],
                                                                 Closes=price_data[f'{rebalance}_closes'],
                                                                 length=lookback)
                indicator_set.add(key)
                price_data[key] = result

    @staticmethod
    def _get_resampled_spy(price_data, rebalance):
        """Return SPY data resampled to the strategy's rebalance frequency.

        Uses MINUTE_spy (minute bars) as the source when available, since
        indicators must be computed from minute-aggregated daily data to
        match TradeStation. Falls back to DAILY_spy if minute data is
        unavailable.
        """
        rebalance_timeframe = timeframe_map.get(rebalance, 'D')
        # Prefer minute data so aggregated OHLC matches TradeStation
        source = price_data.get('MINUTE_spy', price_data.get('DAILY_spy'))
        return GeneratePricesIndicators.resample_to_timeframe(
            source, timeframe=rebalance_timeframe)

    @staticmethod
    def _compute_market_trend_rules(rules, marketRegime, price_data, rebalance, univ, indicator_set):
        """Compute primary indicators for market trend rules."""
        for rule in rules or []:
            if rule.indicator == 'sma':
                key = f'{marketRegime.regime_ticker.lower()}_{rule.indicator}_{rule.lookback}'
                if key in indicator_set:
                    continue

                if univ.lower() == 'spy':
                    daily_df = GeneratePricesIndicators._get_resampled_spy(price_data, rebalance)
                    prices = daily_df[['Close']]
                else:
                    prices = price_data[f'{rebalance}_closes_{marketRegime.regime_ticker.lower()}']

                result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[rule.indicator],
                                                                 prices=prices, lookback=rule.lookback)

                indicator_set.add(key)
                price_data[key] = result

            elif rule.indicator == 'atr':
                key = f'{rule.indicator}_{rule.lookback}'
                if key in indicator_set:
                    continue

                if univ.lower() == 'spy':
                    # daily_df = GeneratePricesIndicators._get_resampled_spy(price_data, rebalance)
                    daily_df = price_data['DAILY_spy']
                    high_prices = daily_df[['High']].rename(columns={'High': 'spy'})
                    low_prices = daily_df[['Low']].rename(columns={'Low': 'spy'})
                    close_prices = daily_df[['Close']].rename(columns={'Close': 'spy'})
                else:
                    high_prices = price_data[f'{rebalance}_highs']
                    low_prices = price_data[f'{rebalance}_lows']
                    close_prices = price_data[f'{rebalance}_closes']

                result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[rule.indicator],
                                                                 Highs=high_prices, Lows=low_prices,
                                                                 Closes=close_prices, length=rule.lookback,method= 'simple')

                indicator_set.add(key)
                price_data[key] = result

    @staticmethod
    def _compute_market_trend_value_indicators(rules, price_data, rebalance, univ, indicator_set):
        """Compute value-side indicators for market trend rules."""
        for rule in rules or []:
            if not rule.value_indicator:
                continue

            key = f'{rule.value_indicator}_{rule.value_lookback}'
            if key in indicator_set:
                continue

            if rule.value_indicator == 'sma':
                if univ.lower() == 'spy':
                    daily_df = GeneratePricesIndicators._get_resampled_spy(price_data, rebalance)
                    prices = daily_df[['Close']]
                else:
                    prices = price_data[f'{rebalance}_closes']

                result = GeneratePricesIndicators.call_indicator(
                    FUNCTION_MAPPER[rule.value_indicator],
                    prices=prices, lookback=rule.value_lookback)

            elif rule.value_indicator == 'atr':
                if univ.lower() == 'spy':
                    # daily_df = GeneratePricesIndicators._get_resampled_spy(price_data, rebalance)
                    daily_df = price_data['DAILY_spy']

                    high_prices = daily_df[['High']].rename(columns={'High': 'spy'})
                    low_prices = daily_df[['Low']].rename(columns={'Low': 'spy'})
                    close_prices = daily_df[['Close']].rename(columns={'Close': 'spy'})
                else:
                    high_prices = price_data[f'{rebalance}_highs']
                    low_prices = price_data[f'{rebalance}_lows']
                    close_prices = price_data[f'{rebalance}_closes']

                result = GeneratePricesIndicators.call_indicator(
                    FUNCTION_MAPPER[rule.value_indicator],
                    Highs=high_prices, Lows=low_prices,
                    Closes=close_prices, length=rule.value_lookback,method= 'simple')

            else:
                continue

            indicator_set.add(key)
            price_data[key] = result
    @staticmethod
    def _compute_rule_indicators(rules, price_data, rebalance, univ, indicator_set):
        """Compute the primary indicator for each rule."""
        for rule in rules:
            key = f'{rule.indicator}_{rule.lookback}'
            if key in indicator_set:
                continue

            if rule.indicator in ('rsi', 'hv'):
                result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[rule.indicator],
                                                                 prices=price_data[f'{rebalance}_closes'],
                                                                 n=rule.lookback)

            elif rule.indicator in ('adx', 'atr'):
                result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[rule.indicator],
                                                                 Highs=price_data[f'{rebalance}_highs'],
                                                                 Lows=price_data[f'{rebalance}_lows'],
                                                                 Closes=price_data[f'{rebalance}_closes'],
                                                                 length=rule.lookback)

            elif rule.indicator == 'sma':
                result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[rule.indicator],
                                                                 prices=price_data[f'{rebalance}_closes'],
                                                                 lookback=rule.lookback)

            elif rule.indicator == 'crsi':
                if univ == 'liquid500':
                    result = pd.read_csv(f'{PricePath.liquid500base_path}/Lq500CRSI.csv',
                                         index_col=['Date'], parse_dates=True)
                elif univ == 'sp500':
                    result = pd.read_csv(f'{PricePath.sp500base_path}/sp500CRSI.csv',
                                         index_col=['Date'], parse_dates=True)
                else:
                    continue

            elif rule.indicator == 'relative_momentum':
                stock_indicator = GeneratePricesIndicators.call_indicator(
                    FUNCTION_MAPPER[rule.indicator],
                    df=price_data[f"{rebalance}_closes"], lookback=rule.lookback)
                spy_indicator = GeneratePricesIndicators.call_indicator(
                    FUNCTION_MAPPER[rule.indicator],
                    df=price_data[f"{rebalance}_closes_spy"], lookback=rule.lookback)
                result = stock_indicator.div(spy_indicator, axis=0)

            elif rule.indicator == 'average_volume':
                result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER['sma'],
                                                                 prices=price_data[f'{rebalance}_volumes'],
                                                                 lookback=rule.lookback)

            elif rule.indicator == 'n_week_high_recent':
                p = rule.params or {}
                n_week_days = int(p.get("n_week_days", 252))
                within_days = int(p.get("within_days", 20))
                result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER['n_week_high_recent'],
                                                                 closes=price_data[f'{rebalance}_closes'],
                                                                 week_high_in_days=n_week_days,
                                                                 high_last_days=within_days)
                key = f'{rule.indicator}_{n_week_days}_{within_days}'

            else:
                continue

            indicator_set.add(key)
            price_data[key] = result

    @staticmethod
    def _compute_value_indicators(rules, price_data, rebalance, univ, indicator_set):
        """Compute value-side indicators (the RHS of a comparison) for entry or exit rules."""
        for rule in rules:
            if not rule.value_indicator:
                continue

            if rule.value_indicator == 'sma':
                key = f'{rule.value_indicator}_{rule.value_lookback}'
                if key in indicator_set:
                    continue

                if univ.lower() == 'spy':
                    daily_df = GeneratePricesIndicators._get_resampled_spy(price_data, rebalance)
                    prices = daily_df[['Close']]
                else:
                    prices = price_data[f'{rebalance}_closes']

                result = GeneratePricesIndicators.call_indicator(
                    FUNCTION_MAPPER[rule.value_indicator],
                    prices=prices,
                    lookback=rule.value_lookback)

                indicator_set.add(key)
                price_data[key] = result

            elif rule.value_indicator == 'range_close':
                if univ.lower() != 'spy':
                    continue

                key = f'{rule.value_indicator}_{rule.value_range_percent}'
                if key in indicator_set:
                    continue

                daily_df = GeneratePricesIndicators._get_resampled_spy(price_data, rebalance)

                close_prices = daily_df[['Close']].rename(columns={'Close': 'spy'})
                high_prices = daily_df[['High']].rename(columns={'High': 'spy'})
                low_prices = daily_df[['Low']].rename(columns={'Low': 'spy'})

                day_range = high_prices - low_prices
                range_close = low_prices + (day_range * (rule.value_range_percent / 100))

                indicator_set.add(key)
                price_data[key] = range_close

    @staticmethod
    def _compute_ranking(marketRegime, price_data, rebalance, univ, indicator_set):
        """Compute ranking indicator."""
        if not marketRegime.ranking or not marketRegime.ranking_lookback > 0:
            return

        # relative_momentum for ranking uses ROC (different from entry/exit)
        if marketRegime.ranking == 'relative_momentum':
            key = f'{marketRegime.ranking}_{marketRegime.ranking_lookback}'
            if key in indicator_set:
                return

            rm1 = IndicatorCalculator.ROC(price_data[f"{rebalance}_closes"],
                                          marketRegime.ranking_lookback)
            rm2 = IndicatorCalculator.ROC(price_data[f"{rebalance}_closes_spy"],
                                          marketRegime.ranking_lookback)
            rm2_series = rm2.iloc[:, 0]
            relative_momentum = rm1.div(rm2_series, axis=0)

            indicator_set.add(key)
            price_data[key] = relative_momentum
            return

        # Everything else (hv, atr, adx, sma, rsi, crsi) — same as rule indicators
        # Build a temporary Rule-like object to reuse _compute_rule_indicators
        ranking_rule = Rule(
            indicator=marketRegime.ranking,
            lookback=marketRegime.ranking_lookback,
            operator='', value=0, connector=''
        )
        GeneratePricesIndicators._compute_rule_indicators(
            [ranking_rule], price_data, rebalance, univ, indicator_set)


    @staticmethod
    def generate(marketRegime : MarketRegimeBase,strategy: StrategyBucket):
        for univ in UNIVERSES.keys():
            if marketRegime.universe.lower() == univ.lower():

                if univ == 'sp500':
                    loader = PriceDataLoader(PricePath.sp500base_path)
                elif univ == 'liquid500':
                    loader = PriceDataLoader(PricePath.liquid500base_path)
                elif univ.lower() == 'SPY'.lower():
                    loader = PriceDataLoader(PricePath.spy_path)
                else:
                    loader = PriceDataLoader(PricePath.russell3000base_path)



                price_data = loader.load_all(rebalance=strategy.rebalance,universe=univ)

                if univ.lower() != 'spy':
                    date_to_active_tickers = price_data[f'{univ}_universe'].apply(lambda row: row[row == 1].index.tolist(), axis=1)
                    df_out = date_to_active_tickers.to_frame(name="active_tickers")
                    price_data[f'{univ}_universe'] = df_out["active_tickers"].apply(lambda x: ",".join(x)).to_frame()

                    price_data.update(loader.load_spy_close(rebalance=strategy.rebalance))

                indictor_Set = set()

                # # This is For LIMIT ATR PRODUCTION
                GeneratePricesIndicators._compute_atr_limits(marketRegime, price_data, strategy.rebalance,
                                                             indictor_Set)


                entry_rules = list(GeneratePricesIndicators.iter_rules_from_tree(marketRegime.entry_rules_tree))
                exit_rules = list(GeneratePricesIndicators.iter_rules_from_tree(marketRegime.exit_rules_tree))
                market_trend_rules = list(GeneratePricesIndicators.iter_rules_from_tree(marketRegime.market_trend_rules_tree))

                #Entry Indicator Rules
                GeneratePricesIndicators._compute_rule_indicators(entry_rules, price_data, strategy.rebalance, univ,
                                                                  indictor_Set)
                #Entry Value Indicators Rules
                GeneratePricesIndicators._compute_value_indicators(entry_rules, price_data, strategy.rebalance, univ,
                                                                   indictor_Set)
                #Exit Indicator Rules
                GeneratePricesIndicators._compute_rule_indicators(exit_rules, price_data, strategy.rebalance, univ,
                                                                  indictor_Set)

                #Exit Value Indicator Value
                GeneratePricesIndicators._compute_value_indicators(exit_rules, price_data, strategy.rebalance, univ,
                                                                   indictor_Set)


                # Ranking Indicator Generation
                GeneratePricesIndicators._compute_ranking(marketRegime, price_data, strategy.rebalance, univ,
                                                          indictor_Set)


                # Market Trend Rule  Generation
                GeneratePricesIndicators._compute_market_trend_rules(market_trend_rules, marketRegime, price_data,
                                                                     strategy.rebalance, univ, indicator_set=indictor_Set)

                # Market Trend Value Indicators
                GeneratePricesIndicators._compute_market_trend_value_indicators(market_trend_rules, price_data,
                                                                                strategy.rebalance, univ, indicator_set= indictor_Set)

                # Trading Days
                GeneratePricesIndicators._compute_trading_dates(price_data, strategy.rebalance, univ, strategy, loader)


                loader.uploadCommonPath(price_data=price_data,universe=univ,strategy_name = strategy.name)

    @staticmethod
    def _compute_trading_dates(price_data, rebalance, univ, strategy, loader):
        """Compute all_dates and trading_dates."""
        if univ.lower() == 'spy':
            daily_closes = price_data['DAILY_spy'][['Close']]
            all_dates = price_data['DAILY_spy']['Close'].index
        else:
            daily_closes = price_data[f'{rebalance}_closes']
            all_dates = price_data[f'{rebalance}_closes'].index

        price_data['all_dates'] = pd.DataFrame(data=all_dates, columns=['Date'])

        trading_dates = loader.get_trading_dates(
            start_trading=strategy.start_date,
            end_trading=strategy.end_date,
            use_data=True,
            daily_closes=daily_closes,
            all_dates=all_dates,
            rebalance=rebalance)

        price_data['trading_dates'] = pd.DataFrame(data=trading_dates, columns=['Date'])