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
            'Open': 'first',
            'High': 'max',
            'Low': 'min',
            'Close': 'last',
            'Up': 'sum',
            'Down': 'sum'
        }).dropna(subset=['Open'])  # Remove empty periods

        return resampled_df

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

                # This is For LIMIT ATR PRODUCTION
                if (marketRegime.order_type and marketRegime.order_type == 'LIMIT_ATR'
                        and  marketRegime.atr_limit_lookback and marketRegime.atr_limit_lookback > 0) :
                    result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER["atr"],
                                            Highs=price_data[f'{strategy.rebalance}_highs'],
                                            Lows=price_data[f'{strategy.rebalance}_lows'],
                                            Closes=price_data[f'{strategy.rebalance}_closes'], length=marketRegime.atr_limit_lookback)
                    indictor_Set.add(f'{FUNCTION_MAPPER["atr"]}_{marketRegime.atr_limit_lookback}')
                    price_data[f'{FUNCTION_MAPPER["atr"]}_{marketRegime.atr_limit_lookback}'] = result

                # This is For LIMIT ATR PRODUCTION For Stoploss
                if (marketRegime.stoploss_type and marketRegime.stoploss_type == 'ATR_BASED'
                        and marketRegime.atr_lookback_stp and marketRegime.atr_lookback_stp > 0):
                    result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER["atr"],
                                                 Highs=price_data[f'{strategy.rebalance}_highs'],
                                                 Lows=price_data[f'{strategy.rebalance}_lows'],
                                                 Closes=price_data[f'{strategy.rebalance}_closes'],
                                                 length=marketRegime.atr_lookback_stp)
                    indictor_Set.add(f'{FUNCTION_MAPPER["atr"]}_{marketRegime.atr_lookback_stp}')
                    price_data[f'{FUNCTION_MAPPER["atr"]}_{marketRegime.atr_lookback_stp}'] = result

                # This is For LIMIT ATR PRODUCTION For Take Profit
                if (marketRegime.takeprofit_type and marketRegime.takeprofit_type == 'ATR_BASED'
                        and marketRegime.atr_lookback_tp and marketRegime.atr_lookback_tp > 0):
                    result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER["atr"],
                                            Highs=price_data[f'{strategy.rebalance}_highs'],
                                            Lows=price_data[f'{strategy.rebalance}_lows'],
                                            Closes=price_data[f'{strategy.rebalance}_closes'],
                                            length=marketRegime.atr_lookback_tp)
                    indictor_Set.add(f'{FUNCTION_MAPPER["atr"]}_{marketRegime.atr_lookback_tp}')
                    price_data[f'{FUNCTION_MAPPER["atr"]}_{marketRegime.atr_lookback_tp}'] = result


                print()
                entry_rules = list(GeneratePricesIndicators.iter_rules_from_tree(marketRegime.entry_rules_tree))
                exit_rules = list(GeneratePricesIndicators.iter_rules_from_tree(marketRegime.exit_rules_tree))
                market_trend_rules = list(GeneratePricesIndicators.iter_rules_from_tree(marketRegime.market_trend_rules_tree))


                # Entry Rule  Generation
                for rule in entry_rules:

                    if rule.indicator == 'rsi':
                        result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[rule.indicator], prices=price_data[f'{strategy.rebalance}_closes'], n=rule.lookback)
                        indictor_Set.add(f'{rule.indicator}_{rule.lookback}')
                        price_data[f'{rule.indicator}_{rule.lookback}'] = result

                    elif rule.indicator == 'adx':
                        result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[rule.indicator], Highs=price_data[f'{strategy.rebalance}_highs'],
                                                Lows=price_data[f'{strategy.rebalance}_lows'],Closes=price_data[f'{strategy.rebalance}_closes'], length=rule.lookback)
                        indictor_Set.add(f'{rule.indicator}_{rule.lookback}')
                        price_data[f'{rule.indicator}_{rule.lookback}'] = result

                    elif rule.indicator == 'atr':
                        result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[rule.indicator], Highs=price_data[f'{strategy.rebalance}_highs'],
                                                Lows=price_data[f'{strategy.rebalance}_lows'],Closes=price_data[f'{strategy.rebalance}_closes'], length=rule.lookback)
                        indictor_Set.add(f'{rule.indicator}_{rule.lookback}')
                        price_data[f'{rule.indicator}_{rule.lookback}'] = result

                    elif rule.indicator == 'hv':

                        result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[rule.indicator],
                                                prices=price_data[f'{strategy.rebalance}_closes'],
                                                n=rule.lookback)

                        price_data[f'{rule.indicator}_{rule.lookback}'] = result
                        indictor_Set.add(f'{rule.indicator}_{rule.lookback}')


                    elif rule.indicator == 'sma':

                        result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[rule.indicator],
                                                prices=price_data[f'{strategy.rebalance}_closes'],
                                                lookback=rule.lookback)

                        price_data[f'{rule.indicator}_{rule.lookback}'] = result
                        indictor_Set.add(f'{rule.indicator}_{rule.lookback}')

                    elif rule.indicator == 'crsi' and  univ == 'liquid500':

                        crsi_liq = pd.read_csv(f'{PricePath.liquid500base_path}/Lq500CRSI.csv',
                                                index_col=['Date'], parse_dates=True)

                        price_data[f'{rule.indicator}_{rule.lookback}'] = crsi_liq
                        indictor_Set.add(f'{rule.indicator}_{rule.lookback}')
                    elif rule.indicator == 'crsi' and univ == 'sp500':
                        crsi_liq = pd.read_csv(f'{PricePath.sp500base_path}/sp500CRSI.csv',
                                                index_col=['Date'], parse_dates=True)

                        price_data[f'{rule.indicator}_{rule.lookback}'] = crsi_liq
                        indictor_Set.add(f'{rule.indicator}_{rule.lookback}')

                    elif rule.indicator == 'relative_momentum':

                        # Indicator on stock closes
                        stock_indicator = GeneratePricesIndicators.call_indicator(
                            FUNCTION_MAPPER[rule.indicator],
                            df=price_data[f"{strategy.rebalance}_closes"],
                            lookback=rule.lookback,
                        )

                        # Indicator on SPY closes (broadcasted to all stock columns)
                        spy_indicator = GeneratePricesIndicators.call_indicator(
                            FUNCTION_MAPPER[rule.indicator],
                            df=price_data[f"{strategy.rebalance}_closes_spy"],
                            lookback=rule.lookback,
                        )

                        # Divide stock indicator by SPY indicator (aligning index)
                        relative_momentum = stock_indicator.div(spy_indicator, axis=0)
                        price_data[f'{rule.indicator}_{rule.lookback}'] = relative_momentum
                        indictor_Set.add(f'{rule.indicator}_{rule.lookback}')

                    elif rule.indicator == 'average_volume':
                        result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER['sma'],
                                                prices=price_data[f'{strategy.rebalance}_volumes'],
                                                lookback=rule.lookback)

                        price_data[f'{rule.indicator}_{rule.lookback}'] = result
                        indictor_Set.add(f'{rule.indicator}_{rule.lookback}')

                    elif rule.indicator == "n_week_high_recent":
                        p = rule.params or {}
                        n_week_days = int(p.get("n_week_days", 252))
                        within_days = int(p.get("within_days", 20))
                        result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER['n_week_high_recent'],
                                                                         closes=price_data[f'{strategy.rebalance}_closes'],
                                                                         week_high_in_days=n_week_days,high_last_days=within_days)

                        price_data[f'{rule.indicator}_{n_week_days}_{within_days}'] = result
                        indictor_Set.add(f'{rule.indicator}_{n_week_days}_{within_days}')




                # Entry Value Indicators
                for rule in entry_rules:
                    if rule.value_indicator == 'sma':
                        if f'{rule.value_indicator}_{rule.value_lookback}' not in indictor_Set:

                            if univ.lower() != 'spy':
                                result = GeneratePricesIndicators.call_indicator(
                                    FUNCTION_MAPPER[rule.value_indicator],
                                    prices=price_data[
                                        f'{strategy.rebalance}_closes'],
                                    lookback=rule.value_lookback)
                            elif univ.lower() == 'spy' :

                                rebalance_timeframe = timeframe_map.get(strategy.rebalance, 'D')
                                daily_df = GeneratePricesIndicators.resample_to_timeframe(price_data['DAILY_spy'], timeframe=rebalance_timeframe)  # Remove days with no price data

                                result = GeneratePricesIndicators.call_indicator(
                                    FUNCTION_MAPPER[rule.value_indicator],
                                    prices=daily_df[['Close']],
                                    lookback=rule.value_lookback)



                            price_data[f'{rule.value_indicator}_{rule.value_lookback}'] = result
                            indictor_Set.add(f'{rule.value_indicator}_{rule.value_lookback}')


                    elif rule.value_indicator == 'range_close':
                        if univ.lower() == 'spy':
                            print('')
                            rebalance_timeframe = timeframe_map.get(strategy.rebalance, 'D')
                            daily_df = GeneratePricesIndicators.resample_to_timeframe(price_data['DAILY_spy'],
                                                                                      timeframe=rebalance_timeframe)

                            close_prices = daily_df[['Close']].rename(columns={'Close': 'spy'})
                            high_prices = daily_df[['High']].rename(columns={'High': 'spy'})
                            low_prices = daily_df[['Low']].rename(columns={'Low': 'spy'})

                            day_range = high_prices - low_prices
                            range_close = low_prices + (day_range * (rule.value_range_percent/100))

                            price_data[f'{rule.value_indicator}_{rule.value_range_percent}'] = range_close
                            indictor_Set.add(f'{rule.value_indicator}_{rule.value_range_percent}')



                # Exit Rule  Generation
                for rule in exit_rules:
                    if rule.indicator == 'rsi':
                        if f'{rule.indicator}_{rule.lookback}' not in indictor_Set:
                            result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[rule.indicator], prices=price_data[f'{strategy.rebalance}_closes'], n=rule.lookback)
                            price_data[f'{rule.indicator}_{rule.lookback}'] = result

                    elif rule.indicator == 'adx':
                        if f'{rule.indicator}_{rule.lookback}' not in indictor_Set:
                            result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[rule.indicator], Highs=price_data[f'{strategy.rebalance}_highs'],
                                                    Lows=price_data[f'{strategy.rebalance}_lows'],Closes=price_data[f'{strategy.rebalance}_closes'], length=rule.lookback)
                            indictor_Set.add(f'{rule.indicator}_{rule.lookback}')
                            price_data[f'{rule.indicator}_{rule.lookback}'] = result


                    elif rule.indicator == 'atr':
                        if f'{rule.indicator}_{rule.lookback}' not in indictor_Set:
                            result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[rule.indicator], Highs=price_data[f'{strategy.rebalance}_highs'],
                                                    Lows=price_data[f'{strategy.rebalance}_lows'],Closes=price_data[f'{strategy.rebalance}_closes'], length=rule.lookback)
                            indictor_Set.add(f'{rule.indicator}_{rule.lookback}')
                            price_data[f'{rule.indicator}_{rule.lookback}'] = result


                    elif rule.indicator == 'hv':
                        if f'{rule.indicator}_{rule.lookback}' not in indictor_Set:
                            result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[rule.indicator],
                                                    prices=price_data[f'{strategy.rebalance}_closes'],
                                                    n=rule.lookback)

                            price_data[f'{rule.indicator}_{rule.lookback}'] = result
                            indictor_Set.add(f'{rule.indicator}_{rule.lookback}')

                    elif rule.indicator == 'sma':
                        if f'{rule.indicator}_{rule.lookback}' not in indictor_Set:
                            result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[rule.indicator],
                                                    prices=price_data[f'{strategy.rebalance}_closes'],
                                                    lookback=rule.lookback)

                            price_data[f'{rule.indicator}_{rule.lookback}'] = result
                            indictor_Set.add(f'{rule.indicator}_{rule.lookback}')

                    elif rule.indicator == 'crsi' and univ == 'liquid500':
                        if f'{rule.indicator}_{rule.lookback}' not in indictor_Set:
                            crsi_liq = pd.read_csv(f'{PricePath.liquid500base_path}/Lq500CRSI.csv',
                                                   index_col=['Date'], parse_dates=True)

                            price_data[f'{rule.indicator}_{rule.lookback}'] = crsi_liq
                            indictor_Set.add(f'{rule.indicator}_{rule.lookback}')

                    elif rule.indicator == 'crsi' and univ == 'sp500':
                        crsi_liq = pd.read_csv(f'{PricePath.sp500base_path}/sp500CRSI.csv',
                                                index_col=['Date'], parse_dates=True)

                        price_data[f'{rule.indicator}_{rule.lookback}'] = crsi_liq
                        indictor_Set.add(f'{rule.indicator}_{rule.lookback}')

                    elif rule.indicator == 'relative_momentum':
                        if f'{rule.indicator}_{rule.lookback}' not in indictor_Set:
                            # Indicator on stock closes
                            stock_indicator = GeneratePricesIndicators.call_indicator(
                                FUNCTION_MAPPER[rule.indicator],
                                df=price_data[f"{strategy.rebalance}_closes"],
                                lookback=rule.lookback,
                            )

                            # Indicator on SPY closes (broadcasted to all stock columns)
                            spy_indicator = GeneratePricesIndicators.call_indicator(
                                FUNCTION_MAPPER[rule.indicator],
                                df=price_data[f"{strategy.rebalance}_closes_spy"],
                                lookback=rule.lookback,
                            )

                            # Divide stock indicator by SPY indicator (aligning index)
                            relative_momentum = stock_indicator.div(spy_indicator, axis=0)
                            price_data[f'{rule.indicator}_{rule.lookback}'] = relative_momentum
                            indictor_Set.add(f'{rule.indicator}_{rule.lookback}')

                    elif rule.indicator == "n_week_high_recent":
                        p = rule.params or {}
                        n_week_days = int(p.get("n_week_days", 252))
                        within_days = int(p.get("within_days", 20))
                        result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER['n_week_high_recent'],
                                                                         closes=price_data[f'{strategy.rebalance}_closes'],
                                                                         week_high_in_days=n_week_days,high_last_days=within_days)

                        price_data[f'{rule.indicator}_{n_week_days}_{within_days}'] = result
                        indictor_Set.add(f'{rule.indicator}_{n_week_days}_{within_days}')


                # Exit Value Indicators
                # Exit Value Indicators
                for rule in exit_rules:
                    if rule.value_indicator == 'sma':
                        indicator_key = f'{rule.value_indicator}_{rule.value_lookback}'

                        if indicator_key not in indictor_Set:
                            # Determine price data source based on universe
                            if univ.lower() == 'spy':
                                rebalance_timeframe = timeframe_map.get(strategy.rebalance, 'D')
                                daily_df = GeneratePricesIndicators.resample_to_timeframe(
                                    price_data['DAILY_spy'],
                                    timeframe=rebalance_timeframe
                                )
                                prices = daily_df[['Close']]
                            else:
                                prices = price_data[f'{strategy.rebalance}_closes']

                            # Calculate indicator
                            result = GeneratePricesIndicators.call_indicator(
                                FUNCTION_MAPPER[rule.value_indicator],
                                prices=prices,
                                lookback=rule.value_lookback
                            )

                            indictor_Set.add(f'{rule.value_indicator}_{rule.value_lookback}')
                            price_data[f'{rule.value_indicator}_{rule.value_lookback}'] = result

                    elif rule.value_indicator == 'rangeclose':
                        print('')



                # Ranking Indicator Generation
                if (marketRegime.ranking and marketRegime.ranking_lookback > 0):

                    if marketRegime.ranking == 'hv':
                        if f'{marketRegime.ranking}_{marketRegime.ranking_lookback}' not in indictor_Set:
                            result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[marketRegime.ranking],
                                                    prices=price_data[f'{strategy.rebalance}_closes'], n=marketRegime.ranking_lookback)

                            price_data[f'{marketRegime.ranking}_{marketRegime.ranking_lookback}'] = result

                            indictor_Set.add(f'{marketRegime.ranking}_{marketRegime.ranking_lookback}')
                            price_data[f'{marketRegime.ranking}_{marketRegime.ranking_lookback}'] = result

                    elif marketRegime.ranking == 'atr':
                        if f'{marketRegime.ranking}_{marketRegime.ranking_lookback}' not in indictor_Set:
                            result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[marketRegime.ranking],
                                                    Highs=price_data[f'{strategy.rebalance}_highs'],
                                                    Lows=price_data[f'{strategy.rebalance}_lows'],
                                                    Closes=price_data[f'{strategy.rebalance}_closes'],
                                                    length=marketRegime.ranking_lookback)

                            indictor_Set.add(f'{marketRegime.ranking}_{marketRegime.ranking_lookback}')
                            price_data[f'{marketRegime.ranking}_{marketRegime.ranking_lookback}'] = result

                    elif marketRegime.ranking == 'adx':
                        if f'{marketRegime.ranking}_{marketRegime.ranking_lookback}' not in indictor_Set:
                            result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[marketRegime.ranking],
                                                    Highs=price_data[f'{strategy.rebalance}_highs'],
                                                    Lows=price_data[f'{strategy.rebalance}_lows'],
                                                    Closes=price_data[f'{strategy.rebalance}_closes'],
                                                    length=marketRegime.ranking_lookback)

                            indictor_Set.add(f'{marketRegime.ranking}_{marketRegime.ranking_lookback}')
                            price_data[f'{marketRegime.ranking}_{marketRegime.ranking_lookback}'] = result

                    elif marketRegime.ranking == 'sma':
                        if f'{marketRegime.ranking}_{marketRegime.ranking_lookback}' not in indictor_Set:
                            result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[marketRegime.ranking],
                                                    prices=price_data[f'{strategy.rebalance}_closes'],
                                                    lookback=marketRegime.ranking_lookback)

                            indictor_Set.add(f'{marketRegime.ranking}_{marketRegime.ranking_lookback}')
                            price_data[f'{marketRegime.ranking}_{marketRegime.ranking_lookback}'] = result

                    elif marketRegime.ranking == 'rsi':

                        if f'{marketRegime.ranking}_{marketRegime.ranking_lookback}' not in indictor_Set:

                            result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[marketRegime.ranking],
                                                    prices=price_data[f'{strategy.rebalance}_closes'], n=marketRegime.ranking_lookback)
                            indictor_Set.add(f'{marketRegime.ranking}_{marketRegime.ranking_lookback}')
                            price_data[f'{marketRegime.ranking}_{marketRegime.ranking_lookback}'] = result

                    elif marketRegime.ranking == 'crsi' and univ == 'liquid500':

                        if f'{marketRegime.ranking}_{marketRegime.ranking_lookback}' not in indictor_Set:
                            crsi_liq = pd.read_csv(f'{PricePath.liquid500base_path}/Lq500CRSI.csv',
                                                   index_col=['Date'], parse_dates=True)

                            price_data[f'{marketRegime.ranking}_{marketRegime.ranking_lookback}'] = crsi_liq
                            indictor_Set.add(f'{marketRegime.ranking}_{marketRegime.ranking_lookback}')

                    elif marketRegime.ranking == 'crsi' and univ == 'sp500':
                        crsi_sp = pd.read_csv(f'{PricePath.sp500base_path}/sp500CRSI.csv',
                                                index_col=['Date'], parse_dates=True)

                        price_data[f'{marketRegime.ranking}_{marketRegime.ranking_lookback}'] = crsi_sp
                        indictor_Set.add(f'{marketRegime.ranking}_{marketRegime.ranking_lookback}')

                    elif marketRegime.ranking == 'relative_momentum':

                        rm1 = IndicatorCalculator.ROC(price_data[f"{strategy.rebalance}_closes"],
                                                      marketRegime.ranking_lookback)
                        rm2 = IndicatorCalculator.ROC(price_data[f"{strategy.rebalance}_closes_spy"],
                                                      marketRegime.ranking_lookback)

                        # take SPY ROC as Series
                        rm2_series = rm2.iloc[:, 0]

                        # row-wise divide, no column mismatch
                        relative_momentum = rm1.div(rm2_series, axis=0)
                        price_data[f'{marketRegime.ranking}_{marketRegime.ranking_lookback}'] = relative_momentum
                        indictor_Set.add(f'{marketRegime.ranking}_{marketRegime.ranking_lookback}')


                # Market Trend Rule  Generation

                for rule in market_trend_rules or []:
                    if rule.indicator == 'sma':



                        if f'{rule.indicator}_{rule.lookback}' not in indictor_Set:

                            if univ == 'SPY':
                                rebalance_timeframe = timeframe_map.get(strategy.rebalance, 'D')
                                daily_df = GeneratePricesIndicators.resample_to_timeframe(
                                    price_data['DAILY_spy'],
                                    timeframe=rebalance_timeframe
                                )
                                prices = daily_df[['Close']]
                                result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[rule.indicator],
                                                        prices=prices,
                                                        lookback=rule.lookback)
                            else:

                                result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[rule.indicator],
                                                        prices=price_data[f'{strategy.rebalance}_closes_{marketRegime.regime_ticker.lower()}'],
                                                        lookback=rule.lookback)

                            price_data[f'{marketRegime.regime_ticker.lower()}_{rule.indicator}_{rule.lookback}'] = result
                            indictor_Set.add(f'{marketRegime.regime_ticker.lower()}_{rule.indicator}_{rule.lookback}')

                    elif rule.indicator == 'atr':
                        if f'{rule.indicator}_{rule.lookback}' not in indictor_Set:

                            if univ.lower() == 'spy':
                                rebalance_timeframe = timeframe_map.get(strategy.rebalance, 'D')
                                daily_df = GeneratePricesIndicators.resample_to_timeframe(price_data['DAILY_spy'],timeframe=rebalance_timeframe)

                                close_prices = daily_df[['Close']].rename(columns={'Close': 'spy'})
                                high_prices = daily_df[['High']].rename(columns={'High': 'spy'})
                                low_prices = daily_df[['Low']].rename(columns={'Low': 'spy'})

                                result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[rule.indicator], Highs=high_prices,
                                                        Lows=low_prices,Closes=close_prices, length=rule.lookback)
                            else:

                                result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[rule.indicator], Highs=price_data[f'{strategy.rebalance}_highs'],
                                                        Lows=price_data[f'{strategy.rebalance}_lows'],Closes=price_data[f'{strategy.rebalance}_closes'], length=rule.lookback)
                            indictor_Set.add(f'{rule.indicator}_{rule.lookback}')
                            price_data[f'{rule.indicator}_{rule.lookback}'] = result


                # Market Trend Value Indicators
                for rule in market_trend_rules:
                    if rule.value_indicator == 'sma':
                        indicator_key = f'{rule.value_indicator}_{rule.value_lookback}'

                        if indicator_key not in indictor_Set:
                            # Determine price data source based on universe
                            if univ.lower() == 'spy':
                                rebalance_timeframe = timeframe_map.get(strategy.rebalance, 'D')
                                daily_df = GeneratePricesIndicators.resample_to_timeframe(
                                    price_data['DAILY_spy'],
                                    timeframe=rebalance_timeframe
                                )
                                prices = daily_df[['Close']]
                            else:
                                prices = price_data[f'{strategy.rebalance}_closes']

                            # Calculate indicator
                            result = GeneratePricesIndicators.call_indicator(
                                FUNCTION_MAPPER[rule.value_indicator],
                                prices=prices,
                                lookback=rule.value_lookback
                            )
                            indictor_Set.add(f'{rule.value_indicator}_{rule.value_lookback}')
                            price_data[f'{rule.value_indicator}_{rule.value_lookback}'] = result

                    elif rule.value_indicator == 'atr':
                        if f'{rule.value_indicator}_{rule.value_lookback}' not in indictor_Set:

                            if univ.lower() == 'spy':
                                rebalance_timeframe = timeframe_map.get(strategy.rebalance, 'D')
                                daily_df = GeneratePricesIndicators.resample_to_timeframe(
                                    price_data['DAILY_spy'],
                                    timeframe=rebalance_timeframe
                                )

                                close_prices = daily_df[['Close']].rename(columns={'Close': 'spy'})
                                high_prices = daily_df[['High']].rename(columns={'High': 'spy'})
                                low_prices = daily_df[['Low']].rename(columns={'Low': 'spy'})

                                # Calculate indicator
                                result = GeneratePricesIndicators.call_indicator(
                                    FUNCTION_MAPPER[rule.value_indicator],
                                    Highs=high_prices,
                                    Lows=low_prices,
                                    Closes=close_prices,
                                    length=rule.value_lookback
                                )
                            else:
                                # Calculate indicator
                                result = GeneratePricesIndicators.call_indicator(
                                    FUNCTION_MAPPER[rule.value_indicator],
                                    Highs=price_data[f'{strategy.rebalance}_highs'],
                                    Lows=price_data[f'{strategy.rebalance}_lows'],
                                    Closes=price_data[f'{strategy.rebalance}_closes'],
                                    length=rule.value_lookback
                                )
                            indictor_Set.add(f'{rule.value_indicator}_{rule.value_lookback}')
                            price_data[f'{rule.value_indicator}_{rule.value_lookback}'] = result

                if univ.lower() == 'spy':

                    # All Dates Generation
                    all_dates = price_data['DAILY_spy']['Close'].index
                    all_dates_df = pd.DataFrame(data=all_dates, columns=['Date'])
                    price_data[f'all_dates'] = all_dates_df

                    # Max look back now it takes the default.
                    trading_dates = loader.get_trading_dates(start_trading=strategy.start_date,
                                                             end_trading=strategy.end_date,
                                                             use_data=True,
                                                             daily_closes=price_data['DAILY_spy'][['Close']]
                                                             , all_dates=all_dates, rebalance=strategy.rebalance)
                    trading_days_df = pd.DataFrame(data=trading_dates, columns=['Date'])
                    price_data[f'trading_dates'] = trading_days_df
                else:

                    # All Dates Generation
                    all_dates = price_data[f'{strategy.rebalance}_closes'].index
                    all_dates_df = pd.DataFrame(data=all_dates, columns=['Date'])
                    price_data[f'all_dates'] = all_dates_df


                    # Max look back now it takes the default.
                    trading_dates = loader.get_trading_dates( start_trading=strategy.start_date, end_trading=strategy.end_date,
                                             use_data=True, daily_closes=price_data[f'{strategy.rebalance}_closes']
                                                             ,all_dates= all_dates,rebalance=strategy.rebalance)
                    trading_days_df = pd.DataFrame(data=trading_dates, columns=['Date'])
                    price_data[f'trading_dates'] = trading_days_df


                loader.uploadCommonPath(price_data=price_data,universe=univ,strategy_name = strategy.name)


