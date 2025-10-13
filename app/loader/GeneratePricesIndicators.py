from app.constants.PricePath import PricePath
from app.constants.static_config import UNIVERSES, FUNCTION_MAPPER
from app.loader.PriceDataLoader import PriceDataLoader
from app.loader.TechnicalIndicators import IndicatorCalculator, INDICATOR_REGISTRY
from app.models.strategy_bucket import StrategyBucket

from app.schemas.strategy import MarketRegimeBase
import pandas as pd

class GeneratePricesIndicators:
    @staticmethod
    def call_indicator(name: str, **kwargs):
        func = INDICATOR_REGISTRY.get(name)
        if not func:
            raise ValueError(f"Function {name} is not a registered indicator.")
        return func(**kwargs)

    @staticmethod
    def generate(marketRegime : MarketRegimeBase,strategy: StrategyBucket):
        for univ in UNIVERSES.keys():
            if marketRegime.universe == univ:

                if univ == 'sp500':
                    loader = PriceDataLoader(PricePath.sp500base_path)
                elif univ == 'liquid500':
                    loader = PriceDataLoader(PricePath.liquid500base_path)
                else:
                    loader = PriceDataLoader(PricePath.russell3000base_path)

                price_data = loader.load_all(rebalance=strategy.rebalance,universe=univ)
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




                # Entry Rule  Generation
                for rule in marketRegime.entry_rules:

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


                # Exit Rule  Generation
                for rule in marketRegime.exit_rules:
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

                for rule in marketRegime.market_trend_rules or []:
                    if rule.indicator == 'sma':
                        if f'{rule.indicator}_{rule.lookback}' not in indictor_Set:
                            result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[rule.indicator],
                                                    prices=price_data[f'{strategy.rebalance}_closes_{marketRegime.regime_ticker.lower()}'],
                                                    lookback=rule.lookback)

                            price_data[f'{marketRegime.regime_ticker.lower()}_{rule.indicator}_{rule.lookback}'] = result
                            indictor_Set.add(f'{marketRegime.regime_ticker.lower()}_{rule.indicator}_{rule.lookback}')



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


                loader.uploadCommonPath(price_data=price_data,universe=univ)


