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
        if name == 'ADX':
            name = 'ADX_1'
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
                    regime_ticker=r.get("regime_ticker") or "",
                )
            return

        # LRA Patch 44 — LRA-shaped leaf: indicator field directly on node, no "rule" wrapper.
        # Identified by: has 'indicator' field and is not a 'group'.
        if "indicator" in node and node.get("type") != "group":
            yield Rule(
                indicator=node.get("indicator", ""),
                lookback=int(node.get("lookback") or 0),
                operator=node.get("operator") or "",
                value=float(node.get("value") or 0),
                connector="",
                label=None,
                value_type=None,
                value_indicator=node.get("value_indicator") or "",
                value_lookback=0,
                value_range_percent=0,
                params=node.get("params") or None,
                regime_ticker="",
            )
            return

        for c in node.get("children") or []:
            yield from GeneratePricesIndicators.iter_rules_from_tree(c)

    @staticmethod
    def _extract_tickers_from_tree(tree) -> set:
        """Extract unique regime_ticker values from a rule tree."""
        tickers = set()
        if not tree:
            return tickers
        for rule in GeneratePricesIndicators.iter_rules_from_tree(tree):
            if rule.regime_ticker:
                tickers.add(rule.regime_ticker.lower())
        return tickers

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
    def _compute_volatility_cut_indicators(rules, price_data, rebalance, indicator_set):
        """Volatility-cut (freeze/resume) indicators — SEPARATE from market-trend.
        Handles 'vix_close': exposes the loaded VIX closes under key 'vix_close_0'
        so the engine's VolatilityCutEvaluator can read it. The VIX closes are
        loaded by the ticker scan (regime_ticker=VIX -> {rebalance}_closes_vix)."""
        for rule in rules or []:
            if rule.indicator == 'vix_close':
                key = f'{rule.indicator}_{rule.lookback}'  # vix_close_0
                if key in indicator_set:
                    continue
                ticker = (rule.regime_ticker or 'vix').lower()
                src = price_data.get(f'{rebalance}_closes_{ticker}')
                if src is not None:
                    indicator_set.add(key)
                    price_data[key] = src

    @staticmethod
    def _compute_safety_net_indicators(safety_nets, price_data, rebalance, indicator_set):
        """Generate parquets needed by stateful safety-net policies.

        Walks `marketRegime.safety_nets` and emits the right derived indicator
        per policy type. Engine-side policies read these parquets via the
        existing frame loader instead of recomputing.

        spy_volatility:
          For each item, computes rolling-stddev-of-pct-change (unshifted)
          on the configured ticker's closes and stores under key
          '{ticker}_rolling_vol_close_{lookback}'. Default ticker=SPY,
          lookback=5. Matches Python L_SMR_STATIC's `all_safety_net_vol`.
        """
        for sn in safety_nets or []:
            # Pydantic v1 model → use .type / .params directly.
            # Tolerate dicts too (in case anyone passes raw JSON later).
            sn_type = (getattr(sn, "type", None) or
                       (sn.get("type") if isinstance(sn, dict) else "") or "").lower()
            params = (getattr(sn, "params", None) or
                      (sn.get("params") if isinstance(sn, dict) else {}) or {})

            if sn_type == "spy_volatility":
                ticker = (params.get("vol_ticker") or "SPY").lower()
                lookback = int(params.get("vol_lookback") or 5)
                key = f"{ticker}_rolling_vol_close_{lookback}"
                if key in indicator_set:
                    continue

                src = price_data.get(f"{rebalance}_closes_{ticker}")
                if src is None:
                    print(f"[safety-net] no closes for ticker '{ticker}', "
                          f"skipping {key}. Make sure the ticker is in the "
                          f"strategy data set.")
                    continue

                result = GeneratePricesIndicators.call_indicator(
                    FUNCTION_MAPPER["rolling_vol_close"],
                    prices=src, n=lookback)
                indicator_set.add(key)
                price_data[key] = result
                print(f"[safety-net] generated parquet '{key}' "
                      f"({len(result)} rows)")

            elif sn_type == "spy_volatility_pause":
                # Relative-threshold pause: emit BOTH the vol and its rolling median.
                # Engine policy reads both, computes effective threshold each day.
                ticker = (params.get("vol_ticker") or "SPY").lower()
                vol_lookback = int(params.get("vol_lookback") or 20)
                median_lookback = int(params.get("vol_median_lookback") or 252)

                src = price_data.get(f"{rebalance}_closes_{ticker}")
                if src is None:
                    print(f"[safety-net] no closes for ticker '{ticker}', "
                          f"skipping spy_volatility_pause for {ticker}.")
                    continue

                # 1) The vol series itself
                vol_key = f"{ticker}_rolling_vol_close_{vol_lookback}"
                if vol_key not in indicator_set:
                    vol = GeneratePricesIndicators.call_indicator(
                        FUNCTION_MAPPER["rolling_vol_close"],
                        prices=src, n=vol_lookback)
                    indicator_set.add(vol_key)
                    price_data[vol_key] = vol
                    print(f"[safety-net] generated parquet '{vol_key}' ({len(vol)} rows)")

                    # 2) The rolling median of that vol
                    # NOTE: filename encodes only vol_lookback. median_lookback stays
                    # in the computation but not the filename — keeps the key compatible
                    # with the engine's single-lookback frame loader. Means one median
                    # parquet per vol_lookback (regenerated when median_lookback changes).
                    median_key = f"{ticker}_rolling_vol_median_{vol_lookback}"
                    if median_key not in indicator_set:
                        result = GeneratePricesIndicators.call_indicator(
                            FUNCTION_MAPPER["rolling_vol_median"],
                            prices=src,
                            vol_lookback=vol_lookback,
                            median_lookback=median_lookback)
                        indicator_set.add(median_key)
                        price_data[median_key] = result
                        print(f"[safety-net] generated parquet '{median_key}' ({len(result)} rows, "
                              f"median_lookback={median_lookback} embedded in values)")

    @staticmethod
    def _compute_market_trend_rules(rules, marketRegime, price_data, rebalance, univ, indicator_set):
        """Compute primary indicators for market trend rules."""
        for rule in rules or []:
            # Each rule carries its own ticker (e.g. "SPY", "VIX")
            ticker = (rule.regime_ticker or marketRegime.regime_ticker or "").lower()

            if rule.indicator == 'sma':
                key = f'{ticker}_{rule.indicator}_{rule.lookback}'
                if key in indicator_set:
                    continue

                if univ.lower() == 'spy':
                    daily_df = GeneratePricesIndicators._get_resampled_spy(price_data, rebalance)
                    prices = daily_df[['Close']]
                else:
                    prices = price_data[f'{rebalance}_closes_{ticker}']

                result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[rule.indicator],
                                                                 prices=prices, lookback=rule.lookback)

                indicator_set.add(key)
                price_data[key] = result

            elif rule.indicator == 'atr':
                key = f'{rule.indicator}_{rule.lookback}'
                if key in indicator_set:
                    continue

                if univ.lower() == 'spy':
                    daily_df = GeneratePricesIndicators._get_resampled_spy(price_data, rebalance)
                    high_prices = daily_df[['High']].rename(columns={'High': 'spy'})
                    low_prices = daily_df[['Low']].rename(columns={'Low': 'spy'})
                    close_prices = daily_df[['Close']].rename(columns={'Close': 'spy'})
                else:
                    high_prices = price_data[f'{rebalance}_highs']
                    low_prices = price_data[f'{rebalance}_lows']
                    close_prices = price_data[f'{rebalance}_closes']

                result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[rule.indicator],
                                                                 Highs=high_prices, Lows=low_prices,
                                                                 Closes=close_prices, length=rule.lookback)

                indicator_set.add(key)
                price_data[key] = result

            elif rule.indicator == 'close':
                key = f'{rule.regime_ticker.lower()}_{rule.indicator}_{rule.lookback}'
                if key in indicator_set:
                    continue
                prices = price_data[f'{rebalance}_closes_{ticker}']
                # result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[rule.indicator],
                #                                                  prices=prices, lookback=rule.lookback)
                indicator_set.add(key)
                price_data[key] = prices

    @staticmethod
    def _compute_market_trend_value_indicators(rules, price_data, rebalance, univ, indicator_set):
        """Compute value-side indicators for market trend rules."""
        for rule in rules or []:
            if not rule.value_indicator:
                continue

            # Use per-rule ticker for market trend value indicators
            ticker = (rule.regime_ticker or "").lower()

            key = f'{rule.regime_ticker.lower()}_{rule.value_indicator}_{rule.value_lookback}'
            if key in indicator_set:
                continue

            if rule.value_indicator == 'sma':
                if univ.lower() == 'spy':
                    daily_df = GeneratePricesIndicators._get_resampled_spy(price_data, rebalance)
                    prices = daily_df[['Close']]
                elif ticker:
                    prices = price_data[f'{rebalance}_closes_{ticker}']
                else:
                    prices = price_data[f'{rebalance}_closes']

                result = GeneratePricesIndicators.call_indicator(
                    FUNCTION_MAPPER[rule.value_indicator],
                    prices=prices, lookback=rule.value_lookback)


            elif rule.value_indicator == 'atr':
                if univ.lower() == 'spy':
                    daily_df = GeneratePricesIndicators._get_resampled_spy(price_data, rebalance)
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
                    Closes=close_prices, length=rule.value_lookback)

            else:
                continue

            indicator_set.add(key)
            price_data[key] = result

    @staticmethod
    def _compute_rule_indicators(rules, price_data, rebalance, univ, indicator_set, crsi_base_path=None):
        """Compute the primary indicator for each rule.

        Patch 94: crsi_base_path overrides the CRSI folder when set (live
        execution passes the live folder so CRSI follows the live closes).
        None -> static backtest folder, preserving backtest/replay behaviour.
        """
        for rule in rules:
            key = f'{rule.indicator}_{rule.lookback}'
            if key in indicator_set:
                continue

            if rule.indicator in ('rsi', 'hv'):
                result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER[rule.indicator],
                                                                 prices=price_data[f'{rebalance}_closes'],
                                                                 n=rule.lookback)

            elif rule.indicator == 'ibs':
                # IBS = (close - low) / (high - low), per-bar within today's OHLC
                result = GeneratePricesIndicators.call_indicator(
                    FUNCTION_MAPPER['ibs'],
                    Closes=price_data[f'{rebalance}_closes'],
                    Highs=price_data[f'{rebalance}_highs'],
                    Lows=price_data[f'{rebalance}_lows'])
            elif rule.indicator == 'daily_range_pct':
                # LRA Patch 16: Daily Range % = (high - low) / low * 100
                result = GeneratePricesIndicators.call_indicator(
                    FUNCTION_MAPPER['daily_range_pct'],
                    Highs=price_data[f'{rebalance}_highs'],
                    Lows=price_data[f'{rebalance}_lows'])
            elif rule.indicator in ('consec_down', 'consec_up'):
                # Pure price-pattern derived series — ignores lookback param.
                # Patch 166: consec_up added (mirror streak, up closes).
                result = GeneratePricesIndicators.call_indicator(
                    FUNCTION_MAPPER[rule.indicator],
                    prices=price_data[f'{rebalance}_closes'])
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
                # Patch 93 (revised): parameter-aware CRSI filename per
                # universe. CRSI math depends on (RSI_length, UpDown_length,
                # ROC_length); each combo has its own file. The rule carries
                # the params via `rule.params` (defaults match the legacy
                # 3/2/100). universe_crsi_filename() builds the exact name
                # the maintenance service writes to, so reader and writer
                # always agree:
                #
                #     {universe_slug}_CRSI_R{rsi}_U{updown}_X{roc}.csv
                #
                # CRSI is read from the BACKTEST folder in both backtest and
                # live execution — the manual button maintains it daily
                # (universe_today_refresh.py Patch 93 Edit 1). production /
                # test_mode aren't in scope here (this helper is shared
                # across modes), so we don't branch on them; the backtest-
                # folder file is the single source of truth for CRSI.
                from app.utiliy.universeGenerations.universe_crsi import (
                    universe_crsi_filename,
                    CRSI_RSI_LENGTH, CRSI_UPDOWN_LENGTH, CRSI_ROC_LENGTH,
                )
                # Patch 100: lookback == RSI_length. The legacy generator is
                #   CRSI(prices, RSI_length=crsi_length, UpDown_length=2,
                #        ROC_length=100)  with crsi_length = 2,
                # so the rule's visible lookback (crsi_2) is the RSI length.
                # UpDown/ROC come from rule.params; rsi_length in params is
                # honored as fallback for any pre-100 saved rule.
                _params = rule.params or {}
                _lb = int(getattr(rule, 'lookback', 0) or 0)
                _rsi = _lb if _lb > 0 else int(_params.get('rsi_length', CRSI_RSI_LENGTH))
                _updown = int(_params.get('updown_length', CRSI_UPDOWN_LENGTH))
                _roc = int(_params.get('roc_length', CRSI_ROC_LENGTH))
                _fname = universe_crsi_filename(univ, _rsi, _updown, _roc)
                # Patch 97: generic CRSI folder — no per-universe branches. Live
                # execution passes crsi_base_path (the live folder); otherwise
                # read the static backtest folder universes/{univ}. A universe
                # whose strategy uses crsi but has no CRSI file now fails loudly
                # on read, instead of the old silent 'continue'.
                if crsi_base_path is not None:
                    _base = crsi_base_path
                else:
                    _base = f'{PricePath.backtestPath}/universes/{univ}'
                # Patch 100: SYNC generate-on-demand. If this variant's file
                # doesn't exist yet, create it now from the universe's
                # daily_closes — first-ever write computes FULL history
                # (universe_crsi.py window policy), so the whole backtest
                # range is covered. Cost: a few seconds once per variant
                # (vectorized CRSI); every later run just reads the file,
                # which the nightly/manual variant sweep keeps current.
                # Loud-fail is preserved: a universe with no daily_closes
                # raises FileNotFoundError inside extend_universe_crsi.
                import os as _os
                import datetime as _dt
                if not _os.path.exists(f'{_base}/{_fname}'):
                    from app.utiliy.universeGenerations.universe_crsi import (
                        extend_universe_crsi,
                    )
                    _gen = extend_universe_crsi(
                        universe_slug=univ,
                        base_path=_base,
                        end_date=_dt.date.today(),
                        rsi_length=_rsi,
                        updown_length=_updown,
                        roc_length=_roc,
                    )
                    print(f'[GeneratePricesIndicators] generated CRSI variant '
                          f'on demand: {_gen.get("filename")} '
                          f'({_gen.get("rows_appended")} rows)')
                result = pd.read_csv(f'{_base}/{_fname}',
                                     index_col=['Date'], parse_dates=True)


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
            elif rule.indicator == 'rolling_vol':
                # Rolling standard-deviation of 1-day-shifted pct changes.
                # Primarily used as a ranking indicator (mean-reversion strategies
                # rank by historical vol). Matches Python:
                #   prices.shift(1).pct_change().rolling(n).std()
                result = GeneratePricesIndicators.call_indicator(
                    FUNCTION_MAPPER['rolling_vol'],
                    prices=price_data[f'{rebalance}_closes'],
                    n=rule.lookback)

            elif rule.indicator == 'sharpe':
                p = rule.params or {}
                mom = int(p.get("momentum_lookback", 252))
                vol = int(p.get("vol_lookback", 252))
                skip = int(p.get("skip_days", 0))
                result = GeneratePricesIndicators.call_indicator(FUNCTION_MAPPER['sharpe'],
                                                                 prices=price_data[f'{rebalance}_closes'],
                                                                 momentum_lookback=mom,
                                                                 vol_lookback=vol, skip_days=skip)
                key = f'{rule.indicator}_{mom}_{vol}_{skip}'

            elif rule.indicator == 'roc':
                result = GeneratePricesIndicators.call_indicator(
                    FUNCTION_MAPPER['roc'],
                    df=price_data[f'{rebalance}_closes'],
                    lookback=rule.lookback
                ) * 100  # pct_change returns decimal (0.03), rules compare against percent (3)

            elif rule.indicator == 'close_minus_open':
                result = price_data[f'{rebalance}_closes'] - price_data[f'{rebalance}_opens']
                key = 'close_minus_open_0'  # lookback is always 0
            elif rule.indicator == 'vol_bucket':
                p = rule.params or {}
                reset_month = int(p.get("reset_month", 2))
                percentile = float(p.get("percentile", 7.5))
                length = int(p.get("length", 21))
                which = str(p.get("which", "last"))  # 'last' = original; not exposed in UI yet
                result = GeneratePricesIndicators._vol_bucket(
                    turnovers=price_data[f'{rebalance}_turnovers'],
                    unadj_closes=price_data[f'{rebalance}_unadjusted_closes'],
                    universe_active=price_data[f'{univ}_universe'],
                    reset_month=reset_month, percentile=percentile,
                    length=length, which=which,
                )
                key = 'vol_bucket_0'  # lookback always 0 (params live in the rule, not the key)
            else:
                continue

            indicator_set.add(key)
            price_data[key] = result

    @staticmethod
    def _vol_bucket(turnovers, unadj_closes, universe_active,
                    reset_month=2, percentile=7.5, length=21, which='last'):
        """
        Volume-bucket pass/fail (con_2 from the legacy rpt_strat).
        Returns a 0/1-style boolean frame: True where a ticker's `length`-day
        average of (turnover / unadjusted_close) exceeds an annual threshold.
        The threshold is the `percentile`-th value of universe members' single-day
        volume on the chosen trading day of `reset_month`, carried forward until the
        next reset. universe_active is the active_tickers comma-string frame.
        """
        uv = turnovers / unadj_closes
        avg = uv.rolling(length).mean()

        # active_tickers (comma string per date) -> set per date
        col = universe_active.columns[0]
        uni = universe_active.reindex(avg.index)
        uni_sets = {d: (set(s.split(",")) if isinstance(s, str) and s else set())
                    for d, s in uni[col].items()}

        # the chosen trading day of reset_month, per year ('last' wins, matching the original)
        by_year = {}
        for ts in avg.index:
            t = pd.Timestamp(ts)
            if t.month == reset_month:
                if which == 'last':
                    by_year[t.year] = ts
                else:
                    by_year.setdefault(t.year, ts)
        reset_days = list(by_year.values())

        # annual threshold: percentile of universe members' single-day volume on the reset day
        buckets = {}
        for ad in reset_days:
            ser = uv.loc[ad].dropna()
            members = [tk for tk in uni_sets.get(ad, set()) if tk in ser.index]
            if not members:
                continue
            sorted_ser = ser[members].sort_values(ascending=True)
            ban_len = round(len(sorted_ser) * percentile / 100)
            idx = ban_len - 1 if ban_len > 0 else 0
            buckets[ad] = float(sorted_ser.iloc[idx])

        # step function: carry each reset-day threshold forward to the next reset
        if buckets:
            s = pd.Series(list(buckets.values()),
                          index=pd.to_datetime(list(buckets.keys()))).sort_index()
            thresh = s.reindex(avg.index, method='ffill').fillna(0.0)
        else:
            thresh = pd.Series(0.0, index=avg.index)

        # 1 where avg_volume > threshold, else 0 (NaN average -> False -> 0)
        return avg.gt(thresh, axis=0).astype('float32')

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
    def generate(marketRegime: MarketRegimeBase, strategy: StrategyBucket,
                 production: bool = False, run_date=None,
                 start_date=None, lookback_buffer_days: int = 500,
                 test_mode: bool = False,
                 preloaded_raw=None,          # Patch 174: per-run shared dict
                 already_written=None):       # Patch 174: per-universe write-once
        """Compute indicator parquets for one (regime, strategy) pair.

        C1 Patch 3: when production=True, the final uploadCommonPath call
        writes to the universe-shared exec_data folder (date-stamped by
        run_date) instead of the per-strategy backtest_data folder.

        Patch 21: when start_date is supplied (execution mode), price_data
        is sliced to [start_date - lookback_buffer_days, ...] before any
        indicator computation. This is the Python half of the execution
        start floor agreed in the design: payload_builder.EXECUTION_START_DATE
        sets the engine's day-loop floor; this slice cuts what gets computed
        in the first place. Backtest mode (start_date=None) is unchanged —
        existing callers (notably app/routes/strategies.py:297) get the
        legacy full-history path.

        Args:
            marketRegime: a MarketRegime with universe + rule trees.
            strategy: the StrategyBucket containing rebalance + name.
            production: when True, write parquets to exec_data path.
            run_date: data date for the exec_data folder name. Defaults
                to today when None. Ignored when production=False.
            start_date: execution-mode lower bound (date). When set, price
                history before (start_date - lookback_buffer_days) is
                discarded before indicator computation. None = full
                history (backtest path).
            lookback_buffer_days: warmup buffer in calendar days. 500
                ≈ 340 trading days, comfortable for all current rules
                including SMA-252/HV-252. Ignored when start_date is None.
        """
        for univ in UNIVERSES.keys():
            if marketRegime.universe.lower() == univ.lower():

                # Patch 22: route to live (nightly-refreshed) folder when in
                # execution mode (production=True); fall back to static
                # backtest folder otherwise. Backtests must see frozen
                # historical data — the manager-demo tradelists were
                # generated against it and need to be reproducible.
                if production and not test_mode:
                    # Normal nightly: read from live folder (~5yr rolling window)
                    if univ == 'sp500':
                        loader = PriceDataLoader(PricePath.sp500_live_base_path)
                    elif univ.lower() == 'spy':
                        loader = PriceDataLoader(PricePath.spy_live_path)
                    elif univ == 'liquid500':
                        # Patch 92: live execution path for liquid500.
                        # Source: live_universes/liquid500/ (rebuilt
                        # nightly by live_universe_pipeline.py with the
                        # source-of-truth membership extended to today
                        # via extend_liquid500_membership pre-step).
                        loader = PriceDataLoader(PricePath.liquid500_live_base_path)
                    elif univ == 'russell3000':
                        # Patch 150: live execution path for russell3000.
                        # Source: live_universes/russell3000/ (rebuilt
                        # nightly by live_universe_pipeline.py from the
                        # Patch-148 registry spec). Same mechanism as
                        # sp500 / liquid500 above.
                        loader = PriceDataLoader(PricePath.russell3000_live_base_path)
                    else:
                        raise ValueError(
                            f"Universe '{univ}' has no live path configured. "
                            f"Add it to PricePath + live_universe_registry "
                            f"before enabling a strategy on this universe."
                        )
                elif test_mode:
                    # Test mode: read from full historical folder so any
                    # historical date is reachable. Output still goes to
                    # exec_data/{YYYYMMDD} because production=True.
                    if univ == 'sp500':
                        loader = PriceDataLoader(PricePath.sp500base_path)
                    elif univ.lower() == 'spy':
                        # Patch 156: LIVE source even in test_mode. The spy
                        # universe here is an EXECUTION dependency (the
                        # combined's market gate, Patch 154 fan-in), and
                        # execution data = short rolling window generated to
                        # the last date. The static spy folder is frozen at
                        # 2021-12-31 (Patch-155 guard evidence: DAILY_spy /
                        # spy_universe both end 2021-12-31), so the replay
                        # window can never be satisfied from it. Safe by
                        # evidence: no strategy replays the spy universe
                        # historically — any such replay would have failed
                        # on the same 2021 wall. live_universes/spy is
                        # rebuilt nightly (LIVE_REGISTRY slug 'spy').
                        loader = PriceDataLoader(PricePath.spy_live_path)
                    elif univ == 'liquid500':
                        # Patch 91: replay/test_mode path for liquid500 — full
                        # historical folder (same source as the backtest branch
                        # below), so any historical date is reachable. The nightly
                        # (production) already handles liquid500 via Patch 92.
                        loader = PriceDataLoader(PricePath.liquid500base_path)
                    elif univ == 'russell3000':
                        # Patch 150: replay/test_mode path for russell3000 —
                        # full historical backtest folder (same source as
                        # the backtest branch below) so any historical date
                        # is reachable; mirrors the liquid500 Patch-91
                        # pattern. NOTE: only as fresh as its last
                        # regeneration — the [freshness] line below prints
                        # coverage, and the combined gate asserts loudly if
                        # the run_date bar is missing.
                        loader = PriceDataLoader(PricePath.russell3000base_path)
                    else:
                        raise ValueError(f"Unknown universe for test_mode: {univ}")
                else:
                    if univ == 'sp500':
                        loader = PriceDataLoader(PricePath.sp500base_path)
                    elif univ == 'sp100':
                        loader = PriceDataLoader(PricePath.sp100base_path)
                    elif univ == 'nasdaq100':
                        loader = PriceDataLoader(PricePath.nasdaq100base_path)
                    elif univ == 'liquid500':
                        loader = PriceDataLoader(PricePath.liquid500base_path)
                    elif univ == 'russell3000':
                        loader = PriceDataLoader(PricePath.russell3000base_path)
                    elif univ == 'lra14':  # LRA Patch 44
                        loader = PriceDataLoader(PricePath.lra_14base_path)
                    elif univ.lower() == 'SPY'.lower():
                        loader = PriceDataLoader(PricePath.spy_path)
                    else:
                        raise ValueError(f"Unknown universe: {univ}")

                # Patch 174: within-run universe cache. Loading a 10k-column
                # universe dominates refresh time and is identical per pair.
                # Shallow copy is safe: the Patch-21 slice REASSIGNS
                # (price_data[k] = sliced) and never mutates shared frames.
                if preloaded_raw is not None:
                    price_data = dict(preloaded_raw)
                    print(f'[GeneratePricesIndicators] Patch 174: reusing '
                          f'shared universe data ({len(price_data)} frames)')
                else:
                    price_data = loader.load_all(rebalance=strategy.rebalance, universe=univ)

                # Patch 104: freshness visibility. Print exactly what price
                # coverage this run is building indicators from, per universe,
                # so a stale-Norgate night is diagnosable straight from the
                # nightly log / eod_run_log instead of surfacing three steps
                # later as a fill_resolver KeyError. Logged for every mode
                # (production / test / backtest) - the source path disambiguates.
                try:
                    _src = getattr(loader, 'base_path', '?')
                    for _k, _df in price_data.items():
                        if _k.endswith('_closes') and not _k.endswith('_unadjusted_closes'):
                            _last = _df.index.max()
                            _first = _df.index.min()
                            print(f'[exec_data_refresh][freshness] universe={univ} '
                                  f'source={_src} closes: {_first:%Y-%m-%d} .. '
                                  f'{_last:%Y-%m-%d} rows={len(_df)} '
                                  f'tickers={_df.shape[1]}')
                        elif _k.startswith('DAILY_') and univ.lower() == 'spy':
                            print(f'[exec_data_refresh][freshness] universe={univ} '
                                  f'source={_src} daily: '
                                  f'{_df.index.min():%Y-%m-%d} .. '
                                  f'{_df.index.max():%Y-%m-%d} rows={len(_df)}')
                except Exception as _e:
                    # Diagnostics must never break generation - but say so loudly.
                    print(f'[exec_data_refresh][freshness] universe={univ} '
                          f'FAILED to summarise price coverage: '
                          f'{type(_e).__name__}: {_e}')

                # Patch 94: in live execution (production, not replay) read CRSI
                # from the live folder — the live pipeline generates it there from
                # the same live closes. Backtest/replay leave it None -> static.
                # Patch 97: generic — in live execution read CRSI from the live
                # folder for ANY universe (the live pipeline generates it there
                # per active universe). Backtest/replay leave it None -> static.
                _crsi_base = None
                if production and not test_mode:
                    _crsi_base = f'{PricePath.live_universes_root}/{univ}'

                # Patch 21: execution-mode slice. Before this, price_data carried
                # 1998 → today (28+ years × ~600 tickers per indicator). Sliced
                # to [start_date - lookback_buffer_days, ...] every downstream
                # indicator compute runs over ~4 years instead. Backtest mode
                # (start_date=None) skips the slice entirely.
                if start_date is not None:
                    import datetime as _dt_p21
                    if isinstance(start_date, str):
                        _start = _dt_p21.date.fromisoformat(start_date)
                    elif isinstance(start_date, _dt_p21.datetime):
                        _start = start_date.date()
                    else:
                        _start = start_date  # already a date
                    _cutoff = pd.Timestamp(_start - _dt_p21.timedelta(days=lookback_buffer_days))
                    _kept = 0
                    _dropped = 0
                    _emptied = []   # Patch 155: frames the slice reduced to ZERO rows
                    for _k, _df in list(price_data.items()):
                        # Slice only DataFrames/Series whose index is datetime-based.
                        # Non-temporal entries (sector mappings, scalars) untouched.
                        if hasattr(_df, 'index') and isinstance(_df.index, pd.DatetimeIndex):
                            _before = len(_df)
                            _sliced = _df[_df.index >= _cutoff]
                            price_data[_k] = _sliced
                            _kept += len(_sliced)
                            _dropped += (_before - len(_sliced))
                            if _before > 0 and len(_sliced) == 0:
                                _emptied.append(
                                    (_k, _df.index.min(), _df.index.max()))
                    # Patch 155: loud failure over a downstream IndexError.
                    # A frame emptied by the slice means the SOURCE folder's
                    # coverage ends before the execution window — every
                    # consumer after this point is doomed to a confusing
                    # empty-array error. Name the keys, their real coverage,
                    # and the cutoff so the operator sees the data problem,
                    # not a symptom three layers down.
                    if _emptied:
                        _detail = "; ".join(
                            f"{k} covers {a.date()}..{b.date()}"
                            for k, a, b in _emptied[:5])
                        _more = (f" (+{len(_emptied)-5} more)"
                                 if len(_emptied) > 5 else "")
                        raise ValueError(
                            f"Patch-21 slice (cutoff {_cutoff.date()}, "
                            f"start_date {_start} - {lookback_buffer_days}d "
                            f"buffer) emptied {len(_emptied)} frame(s) for "
                            f"universe='{univ}': {_detail}{_more}. The "
                            f"source folder's data ends before the "
                            f"execution window. Live mode: check the "
                            f"nightly live_universes refresh for this "
                            f"universe. Replay/test_mode: the STATIC "
                            f"universe folder must be regenerated to cover "
                            f"the replay date.")
                    print(f'[GeneratePricesIndicators] Patch 21 slice: cutoff={_cutoff.date()} '
                          f'kept_rows={_kept} dropped_rows={_dropped} '
                          f'(start_date={_start}, buffer={lookback_buffer_days}d)')

                if univ.lower() != 'spy':
                    date_to_active_tickers = price_data[f'{univ}_universe'].apply(
                        lambda row: row[row == 1].index.tolist(), axis=1)
                    df_out = date_to_active_tickers.to_frame(name="active_tickers")
                    price_data[f'{univ}_universe'] = df_out["active_tickers"].apply(lambda x: ",".join(x)).to_frame()

                    # Load closes for each unique ticker referenced in market trend
                    # and volatility-cut (freeze/resume) rules.
                    mt_tickers = GeneratePricesIndicators._extract_tickers_from_tree(
                        marketRegime.market_trend_rules_tree)
                    # Volatility-cut (freeze/resume) rules may reference VIX/SPY etc. too
                    mt_tickers |= GeneratePricesIndicators._extract_tickers_from_tree(
                        getattr(marketRegime, "freeze_rules_tree", None))
                    mt_tickers |= GeneratePricesIndicators._extract_tickers_from_tree(
                        getattr(marketRegime, "resume_rules_tree", None))

                    if not mt_tickers:
                        mt_tickers = {'spy'}  # fallback to SPY if no tickers specified
                    for ticker in mt_tickers:
                        if ticker == 'spy':
                            price_data.update(loader.load_spy_close(rebalance=strategy.rebalance))
                        else:
                            # Load closes for other tickers (VIX, GLD, etc.)
                            try:
                                price_data.update(loader.load_ticker_close(
                                    ticker=ticker, rebalance=strategy.rebalance))
                            except Exception as e:
                                print(f"[WARNING] Could not load closes for ticker '{ticker}': {e}")

                indictor_Set = set()
                # Patch 174: when reusing the shared per-run dict, seed the
                # dedup set with keys earlier pairs computed -- every
                # branch's existing `if key in indicator_set: continue`
                # guard then gives cross-pair reuse for free.
                if preloaded_raw is not None:
                    indictor_Set.update(k for k in price_data.keys()
                                        if not k.startswith('_p174'))

                # # This is For LIMIT ATR PRODUCTION
                GeneratePricesIndicators._compute_atr_limits(marketRegime, price_data, strategy.rebalance,
                                                             indictor_Set)

                entry_rules = list(GeneratePricesIndicators.iter_rules_from_tree(marketRegime.entry_rules_tree))
                exit_rules = list(GeneratePricesIndicators.iter_rules_from_tree(marketRegime.exit_rules_tree))
                market_trend_rules = list(
                    GeneratePricesIndicators.iter_rules_from_tree(marketRegime.market_trend_rules_tree))

                # LRA Patch 44 — also iterate the LRA leg trees so their indicators
                # (ibs, daily_range_pct, rsi) get generated as parquets. iter_rules_from_tree
                # was extended above to recognize LRA-shaped leaves; the existing rules
                # list is augmented in-place so downstream _compute_rule_indicators picks them up.
                lra_long = list(GeneratePricesIndicators.iter_rules_from_tree(
                    getattr(marketRegime, "entry_rules_tree_long", None)))
                lra_short = list(GeneratePricesIndicators.iter_rules_from_tree(
                    getattr(marketRegime, "entry_rules_tree_short", None)))
                entry_rules.extend(lra_long)
                entry_rules.extend(lra_short)

                freeze_rules = list(
                    GeneratePricesIndicators.iter_rules_from_tree(getattr(marketRegime, "freeze_rules_tree", None)))
                resume_rules = list(
                    GeneratePricesIndicators.iter_rules_from_tree(getattr(marketRegime, "resume_rules_tree", None)))

                # Entry Indicator Rules
                GeneratePricesIndicators._compute_rule_indicators(entry_rules, price_data, strategy.rebalance, univ,
                                                                  indictor_Set, crsi_base_path=_crsi_base)
                # Entry Value Indicators Rules
                GeneratePricesIndicators._compute_value_indicators(entry_rules, price_data, strategy.rebalance, univ,
                                                                   indictor_Set)
                # Exit Indicator Rules
                GeneratePricesIndicators._compute_rule_indicators(exit_rules, price_data, strategy.rebalance, univ,
                                                                  indictor_Set, crsi_base_path=_crsi_base)

                # Exit Value Indicator Value
                GeneratePricesIndicators._compute_value_indicators(exit_rules, price_data, strategy.rebalance, univ,
                                                                   indictor_Set)

                # Ranking Indicator Generation
                GeneratePricesIndicators._compute_ranking(marketRegime, price_data, strategy.rebalance, univ,
                                                          indictor_Set)

                # Market Trend Rule  Generation
                GeneratePricesIndicators._compute_market_trend_rules(market_trend_rules, marketRegime, price_data,
                                                                     strategy.rebalance, univ,
                                                                     indicator_set=indictor_Set)

                # Market Trend Value Indicators
                GeneratePricesIndicators._compute_market_trend_value_indicators(market_trend_rules, price_data,
                                                                                strategy.rebalance, univ,
                                                                                indicator_set=indictor_Set)

                # Volatility-cut (freeze/resume) — dedicated, separate from market-trend.
                # Volatility-cut (freeze/resume) — dedicated, separate from market-trend.
                GeneratePricesIndicators._compute_volatility_cut_indicators(
                    freeze_rules, price_data, strategy.rebalance, indictor_Set)
                GeneratePricesIndicators._compute_volatility_cut_indicators(
                    resume_rules, price_data, strategy.rebalance, indictor_Set)

                # Safety-net indicators — derived parquets needed by stateful
                # policies in the engine (e.g. spy_volatility's rolling vol).
                GeneratePricesIndicators._compute_safety_net_indicators(
                    getattr(marketRegime, "safety_nets", None),
                    price_data, strategy.rebalance, indictor_Set)

                # Trading Days — Patch 50: pass run_date in production mode so
                # trading_dates extends through the live data date, not the
                # backtest end stored in strategy.end_date.
                GeneratePricesIndicators._compute_trading_dates(
                    price_data, strategy.rebalance, univ, strategy, loader,
                    run_date=run_date if production else None)

                # Sector Mapping (for sector-based ranking filter)
                if marketRegime.sector_level and marketRegime.sector_limit:
                    price_data.update(loader.load_sector_mapping())

                # Vol/Turnover filter parquets
                # Generated when vol_filter is enabled on the regime.
                # Patch 117: rolling window is now vol_filter.avg_lookback
                # (default 21). Legacy main script uses rolling(21) via
                # vol_avg_lookback/turnover_avg_lookback params; the previous
                # hardcoded rolling(200) here was a parity bug.
                # avg_volume   = rolling(avg_lookback).mean(turnovers / unadj_closes)
                # avg_turnover = rolling(avg_lookback).mean(closes   * volumes)
                if getattr(marketRegime, "vol_filter", None) and getattr(marketRegime.vol_filter, "enabled", False):
                    # Patch 117: loud validation — reject nonsense lookbacks
                    # instead of silently producing empty/garbage parquets.
                    _avg_lb = getattr(marketRegime.vol_filter, "avg_lookback", 21)
                    if not isinstance(_avg_lb, int) or _avg_lb < 1:
                        raise ValueError(
                            f"vol_filter.avg_lookback must be a positive int, got {_avg_lb!r} "
                            f"(strategy={strategy.name})")
                    _rebalance = strategy.rebalance
                    # Patch 165: LOUD failure over silent skip. The previous
                    # `.get(...) is not None` guards quietly produced NO
                    # parquets when a source key was missing — the vol
                    # filter then ran inert in the engine with zero trace
                    # (this file's own Patch-117 comment promises the
                    # opposite). load_all provides every one of these
                    # unconditionally, so a miss means the source CSVs are
                    # absent from the universe base path — say so.
                    _vol_srcs = (f'{_rebalance}_turnovers',
                                 f'{_rebalance}_unadjusted_closes',
                                 f'{_rebalance}_closes',
                                 f'{_rebalance}_volumes',
                                 f'{_rebalance}_closes_spy')
                    _missing = [k for k in _vol_srcs
                                if price_data.get(k) is None]
                    if _missing:
                        raise ValueError(
                            f"vol_filter enabled but source frame(s) "
                            f"missing from price_data: {_missing} "
                            f"(strategy={strategy.name}, universe={univ}). "
                            f"PriceDataLoader.load_all provides these "
                            f"unconditionally — a miss means the source "
                            f"CSVs are absent from the universe base path.")
                    unadj_vol = (price_data[f'{_rebalance}_turnovers']
                                 / price_data[f'{_rebalance}_unadjusted_closes'])
                    price_data['avg_volume'] = unadj_vol.rolling(_avg_lb).mean()
                    price_data['avg_turnover'] = (
                        price_data[f'{_rebalance}_closes']
                        * price_data[f'{_rebalance}_volumes']
                    ).rolling(_avg_lb).mean()
                # Patch 167 v2: LIMIT_HV entry-price frame. Params ride
                # regime.limit_params (JSON column) -- keys: hv_lookback,
                # divider, lower, upper, reduction. The engine loads the
                # FIXED name 'hv_limit.parquet' (PriceLoader hvLimitEnabled)
                # and computes limit = close x (1 +/- clamp(HV(lb)/divider,
                # lower, upper)/100 x reduction). Loud on missing keys --
                # an inert limit mode must never ship silently.
                if (getattr(marketRegime, 'order_type', '') or '').upper() == 'LIMIT_HV':
                    _lp = getattr(marketRegime, 'limit_params', None) or {}
                    _need = ('hv_lookback', 'divider', 'lower', 'upper', 'reduction')
                    _bad = [k for k in _need
                            if _lp.get(k) is None or float(_lp[k]) <= 0]
                    if _bad:
                        raise ValueError(
                            f"order_type=LIMIT_HV but limit_params key(s) "
                            f"missing/invalid: {_bad} (strategy={strategy.name}). "
                            f"Required keys: {list(_need)}.")
                    price_data['hv_limit'] = GeneratePricesIndicators.call_indicator(
                        FUNCTION_MAPPER['hv'],
                        prices=price_data[f'{strategy.rebalance}_closes'],
                        n=int(_lp['hv_lookback']))

                    # Patch 165: engine-name alias. The engine's PriceLoader
                    # reads 'closes_spy.parquet' (unprefixed); the loader
                    # key here is '{rebalance}_closes_spy', which the writer
                    # would emit as 'DAILY_closes_spy.parquet' — a file the
                    # engine never looks at. Alias so uploadCommonPath
                    # writes the name the engine loads.
                    price_data['closes_spy'] = price_data[f'{_rebalance}_closes_spy']

                # C1 Patch 3: forward production + run_date so the loader
                # can branch to the exec_data folder. Backtest callers
                # omit these args (defaults: production=False, run_date=None)
                # and take the legacy backtest_data path — same as before.
                _excluded = getattr(GeneratePricesIndicators, '_excluded_tickers_cache', None)
                loader.uploadCommonPath(
                    price_data=price_data,
                    universe=univ,
                    strategy_name=strategy.name,
                    production=production,
                    run_date=run_date,
                    excluded_tickers=_excluded,
                    already_written=already_written,   # Patch 174
                )
                return price_data   # Patch 174: caller promotes into the run cache

    @staticmethod
    def _compute_trading_dates(price_data, rebalance, univ, strategy, loader, run_date=None):
        """Compute all_dates and trading_dates.

        Patch 50: in production mode (run_date is not None), use run_date as
        the end_trading bound instead of strategy.end_date. strategy.end_date
        in DB is the BACKTEST end date — often historical (PullBack: 2021-12-31).
        Using it for live execution silently truncates trading_dates to the
        backtest end, so the engine's lastBar lands on a stale historical
        date instead of yesterday. The engine then ranks against ancient
        price data and proposes tickers that were active back then — which
        in Norgate's data model carry their POST-delisting names today
        (e.g. CTRA was in the S&P 500 in 2021, so the column 'CTRA-202605'
        has membership=1 on 2021-12-31 even though it delisted in 2026-05).

        Backtest mode (run_date=None) keeps the original behavior: use
        strategy.end_date verbatim. This preserves manager-demo
        reproducibility against the frozen static parquets.
        """
        if univ.lower() == 'spy':
            daily_closes = price_data['DAILY_spy'][['Close']]
            all_dates = price_data['DAILY_spy']['Close'].index
        else:
            daily_closes = price_data[f'{rebalance}_closes']
            all_dates = price_data[f'{rebalance}_closes'].index

        price_data['all_dates'] = pd.DataFrame(data=all_dates, columns=['Date'])

        # Patch 50: production mode → run_date; backtest mode → strategy.end_date
        end_trading = run_date if run_date is not None else strategy.end_date

        trading_dates = loader.get_trading_dates(
            start_trading=strategy.start_date,
            end_trading=end_trading,
            use_data=True,
            daily_closes=daily_closes,
            all_dates=all_dates,
            rebalance=rebalance)

        price_data['trading_dates'] = pd.DataFrame(data=trading_dates, columns=['Date'])