"""
seed_indicator_descriptions.py
================================
Populates all indicator descriptions in the database.
All content is sourced directly from the actual engine code:
  - TechnicalIndicators.py  (calculation implementations)
  - GeneratePricesIndicators.py  (dispatch logic)
  - regimeConfig.ts  (availability)

SAFE TO RE-RUN — only writes to fields that are currently NULL.
Existing descriptions are never overwritten.

Run with:
    python -m app.services.seed_indicator_descriptions
"""

from app.database import SessionLocal
from app.models.IndicatorDefinition import IndicatorDefinition

# ---------------------------------------------------------------------------
# All descriptions keyed by indicator_key
# ---------------------------------------------------------------------------
DESCRIPTIONS = {

    "rsi": {
        "what_it_is": (
            "RSI (Relative Strength Index) measures how overbought or oversold a stock "
            "is on a scale of 0 to 100. A low reading means the stock has been falling "
            "heavily and may be ready to bounce. A high reading means it has been rising "
            "strongly and may be due for a pullback."
        ),
        "how_it_works": (
            "The engine uses Wilder's smoothing method. Each day's price change is "
            "split into an up-move and a down-move. Both are smoothed using an "
            "exponential average over the lookback period. RSI is then calculated as: "
            "100 - (100 / (1 + average_up / average_down)). The result is always "
            "between 0 and 100. The first 'lookback' rows produce no value while the "
            "average warms up."
        ),
        "why_use_it": (
            "RSI is the most widely used entry trigger for mean-reversion strategies — "
            "strategies that bet a stock will recover after a sharp drop. When RSI falls "
            "to an extreme low it signals the stock has been sold too hard, too fast. "
            "For exits, a rising RSI signals the bounce is complete and it is time to "
            "close the trade."
        ),
        "how_to_use_it": (
            "Lookback of 2 gives an aggressive, short-term signal that triggers often. "
            "Lookback of 14 is the classic setting — triggers less frequently but with "
            "more conviction. For entries set the threshold below 30 (oversold). For "
            "exits set it above 60-70 (recovered). The lower the entry threshold, the "
            "fewer trades you get but each starts from a more extreme dip."
        ),
        "example_rule": "rsi < 25",
        "example_explanation": (
            "Enter a trade when the stock's 2-day RSI drops below 25. This means the "
            "stock has been falling hard for several days and is deeply oversold. The "
            "strategy bets that sellers have overshot and the price will recover."
        ),
    },

    "crsi": {
        "what_it_is": (
            "CRSI (Connors RSI) is a more sensitive version of standard RSI, designed "
            "to catch very short-term oversold conditions more accurately. It produces "
            "a value between 0 and 100 — the lower the number, the more extreme the "
            "short-term sell-off."
        ),
        "how_it_works": (
            "CRSI is the average of three components: (1) a 3-day RSI of closing prices, "
            "(2) a 2-day RSI of the up/down streak — how many days in a row the stock "
            "has been rising or falling continuously, and (3) the percentile rank of "
            "today's 1-day return compared to the last 100 days. Because the data is "
            "pre-computed from a CSV file, it is only available for the Liquid 500 and "
            "S&P 500 universes."
        ),
        "why_use_it": (
            "CRSI triggers at more extreme short-term conditions than plain RSI, so each "
            "signal carries higher conviction. It is popular in professional quant funds "
            "for aggressive short-term mean-reversion strategies. A CRSI below 10 means "
            "the stock has had a severe 2-3 day sell-off, been falling for several days "
            "in a row, and today's loss was in the bottom 10% of all historical moves."
        ),
        "how_to_use_it": (
            "The lookback is fixed at 2 and cannot be changed. A threshold below 10 is "
            "very aggressive — very few trades, very extreme entries. Below 20-25 is "
            "more moderate. Only use CRSI when the strategy universe is Liquid 500 or "
            "S&P 500. If you use it with Russell 3000 it will silently produce no "
            "signals — no error is shown."
        ),
        "example_rule": "crsi < 10",
        "example_explanation": (
            "Enter when CRSI drops below 10. The stock has had an extreme short-term "
            "sell-off, has been falling continuously for several days, and today's loss "
            "ranked in the bottom 10% of all historical daily moves — a very high "
            "conviction oversold signal."
        ),
    },

    "roc": {
        "what_it_is": (
            "ROC (Rate of Change) measures how much a stock's price has moved as a "
            "percentage over a given number of days. A positive ROC means the stock "
            "has risen over that period. A negative ROC means it has fallen."
        ),
        "how_it_works": (
            "The engine calculates ROC as: (today's close - close N days ago) / "
            "(close N days ago), then multiplies by 100 to give a percentage. "
            "For example, a 20-day ROC of 5.0 means the stock is up 5% over the "
            "last 20 trading days. Note: the value is returned as a percentage "
            "number (e.g. 5.0), not a decimal (e.g. 0.05) — set your thresholds "
            "accordingly."
        ),
        "why_use_it": (
            "ROC is a simple, direct measure of recent trend direction and strength. "
            "It is commonly used as a trend filter — only enter long positions in "
            "stocks that have been rising over the lookback period. It can also be "
            "used for ranking: select the top N stocks by highest ROC to invest in "
            "the strongest recent performers."
        ),
        "how_to_use_it": (
            "Use 20 days for a short-term trend filter, 90-252 days for medium to "
            "long-term momentum. A threshold of 0 simply filters for stocks that "
            "are up over the period. A positive threshold (e.g. 10) selects only "
            "stocks up at least 10%. Remember thresholds are percentages: use 5, "
            "not 0.05."
        ),
        "example_rule": "roc > 0",
        "example_explanation": (
            "Only enter stocks that have a positive rate of change over the lookback "
            "period — meaning the stock has been trending upward. This avoids entering "
            "positions in stocks that are in a downtrend."
        ),
    },

    "relative_momentum": {
        "what_it_is": (
            "Relative Momentum measures how strongly a stock has moved compared to "
            "the broad market (SPY) over the same period. A value above 1.0 means "
            "the stock has outperformed SPY. A value below 1.0 means it has "
            "underperformed."
        ),
        "how_it_works": (
            "The engine calculates the Rate of Change (ROC) for the individual stock "
            "and divides it by the ROC of SPY over the same lookback period. "
            "Result = stock_ROC / SPY_ROC. If a stock is up 15% while SPY is up 10%, "
            "relative momentum is 1.5. SPY data is always loaded alongside the stock "
            "universe specifically to support this calculation."
        ),
        "why_use_it": (
            "Relative momentum is used to find stocks that are leading the market — "
            "rising faster than the index. These are the stocks with the strongest "
            "internal momentum. It is especially useful for ranking: invest in the "
            "top N stocks ranked by relative momentum to concentrate in the current "
            "market leaders."
        ),
        "how_to_use_it": (
            "A threshold of 1.0 means the stock is keeping pace with SPY. Above 1.0 "
            "means outperforming. Use a 90-day lookback for medium-term momentum, "
            "252 days for annual momentum. Commonly used as a ranking indicator "
            "rather than a binary rule — rank all stocks by relative momentum and "
            "select the top 10 or 20."
        ),
        "example_rule": "relative_momentum > 1.0",
        "example_explanation": (
            "Only consider stocks whose price gain over the lookback period is "
            "greater than SPY's gain over the same period. This filters out "
            "market laggards and concentrates on the strongest performers."
        ),
    },

    "sma": {
        "what_it_is": (
            "SMA (Simple Moving Average) is the average closing price over the last "
            "N days. It smooths out daily price noise to reveal the underlying trend "
            "direction. A rising SMA means the trend is up; a falling SMA means the "
            "trend is down."
        ),
        "how_it_works": (
            "The engine uses a rolling window: it sums the last N closing prices and "
            "divides by N. Each new day, the oldest price drops off and the latest "
            "price is added. The first N-1 rows produce no value. A 200-day SMA moves "
            "slowly and is rarely disrupted by short-term swings. A 10-day SMA reacts "
            "quickly to recent price changes."
        ),
        "why_use_it": (
            "SMA is the most widely used trend filter in systematic trading. Price "
            "above SMA means the stock is in an uptrend — the wind is at your back. "
            "Price below SMA means you are fighting the trend. Used as a value "
            "indicator on the right-hand side, SMA provides a dynamic comparison "
            "level that moves with the market rather than staying fixed."
        ),
        "how_to_use_it": (
            "200-day SMA: the gold standard long-term trend filter. If SPY is above "
            "its 200-day SMA the broad market is healthy. 50-day SMA: medium-term "
            "trend for individual stocks. 10-20 day SMA: short-term momentum context. "
            "SMA can also be used on the right-hand side of a rule: compare close to "
            "sma_200 to check if the stock is above its long-term average."
        ),
        "example_rule": "close > sma (200 days)",
        "example_explanation": (
            "Only enter a trade when the stock's current price is above its 200-day "
            "moving average. This confirms the stock is in a long-term uptrend and "
            "significantly reduces the chance of catching a falling stock."
        ),
    },

    "adx": {
        "what_it_is": (
            "ADX (Average Directional Index) measures how strongly a stock is "
            "trending on a scale of 0 to 100. Importantly, ADX does not tell you "
            "which direction the trend is going — only how strong or weak it is. "
            "High ADX = strong trend. Low ADX = choppy, sideways market."
        ),
        "how_it_works": (
            "The engine calculates two directional movement indicators — one for "
            "upward moves (using daily highs) and one for downward moves (using "
            "daily lows). Both are smoothed using Wilder's exponential average. "
            "ADX is then the exponential average of the absolute difference between "
            "these two indicators, divided by their sum, multiplied by 100. A rising "
            "ADX means the trend is strengthening; a falling ADX means it is weakening."
        ),
        "why_use_it": (
            "ADX is used to avoid trading in choppy, sideways markets where momentum "
            "strategies typically lose money. By requiring ADX above a minimum level "
            "before entering, you ensure you are only trading when a real trend is "
            "present. It is also used in volatility / freeze rules to pause trading "
            "when the market loses its trend character."
        ),
        "how_to_use_it": (
            "ADX below 20 generally indicates a weak, choppy market — avoid "
            "trend-following entries. ADX above 20-25 indicates a trending market. "
            "ADX above 40 indicates a very strong trend. Use a 14-day lookback for "
            "standard trend measurement. Note: ADX can be high during both strong "
            "uptrends and strong downtrends — always combine with a direction filter "
            "like SMA or price level."
        ),
        "example_rule": "adx > 20",
        "example_explanation": (
            "Only enter trades when ADX is above 20, confirming the stock is in a "
            "trending state rather than moving sideways. This prevents entering "
            "positions in stocks that are just oscillating without clear direction."
        ),
    },

    "n_week_high_recent": {
        "what_it_is": (
            "N-Week High Recent is a boolean indicator — it is either true or false. "
            "It is true when a stock made its highest price of the last N weeks "
            "within the last X trading days. It identifies stocks that have recently "
            "broken out to new highs."
        ),
        "how_it_works": (
            "The engine looks back over two windows simultaneously. The first window "
            "(n_week_days, default 252 days = 52 weeks) finds the highest price over "
            "the past year. The second window (within_days, default 20 days) finds "
            "the highest price over the most recent 20 days. If these two highs are "
            "equal — meaning the highest point in the last 20 days is also the highest "
            "point of the past year — the indicator returns true."
        ),
        "why_use_it": (
            "Stocks breaking out to new 52-week highs often continue higher — momentum "
            "tends to persist after a breakout. This indicator is the entry trigger for "
            "breakout strategies: buy stocks that have just hit a new high, as the "
            "break above prior resistance signals strong buying interest. It filters out "
            "stocks that made their high months ago and have since pulled back."
        ),
        "how_to_use_it": (
            "The default settings (252 days window, within the last 20 days) check for "
            "52-week highs hit in the last month. Reduce within_days to 5 for very "
            "recent breakouts only. Increase n_week_days to 504 (2 years) for longer "
            "term breakout significance. This indicator uses the IS_TRUE operator — "
            "set the operator to 'is true' in the rule builder."
        ),
        "example_rule": "n_week_high_recent is true",
        "example_explanation": (
            "Enter a trade when the stock has made a new 52-week high within the last "
            "20 trading days. This confirms the stock is breaking out to new highs "
            "with fresh momentum, rather than having peaked months ago."
        ),
    },

    "atr": {
        "what_it_is": (
            "ATR (Average True Range) measures how much a stock typically moves in a "
            "single trading day, in dollar terms. A stock with an ATR of $3 swings "
            "about $3 on a normal day. ATR tells you nothing about direction — only "
            "about how volatile the daily moves are."
        ),
        "how_it_works": (
            "Each day's 'true range' is the largest of three values: today's high "
            "minus today's low, today's high minus yesterday's close, and yesterday's "
            "close minus today's low. This accounts for overnight gaps. ATR is then "
            "the Wilder's exponential moving average of these true ranges over the "
            "lookback period. Larger gaps and larger daily swings produce a higher ATR."
        ),
        "why_use_it": (
            "ATR has two main uses: as a volatility filter (only trade stocks with "
            "manageable daily swings) and as a stop-loss tool (set your stop at a "
            "multiple of ATR below entry so that normal daily moves do not trigger "
            "an exit). Because ATR adapts to market conditions, it automatically "
            "widens stops in volatile markets and tightens them in calm markets."
        ),
        "how_to_use_it": (
            "For a volatility filter: set ATR less than a dollar threshold to avoid "
            "stocks that swing too wildly. For a stop: place the exit when close "
            "falls below entry minus 1.5 to 2 times ATR. A 14-day lookback is "
            "standard. Shorter lookbacks (5-7 days) react faster to recent volatility "
            "spikes. Note: ATR is a dollar amount — a $200 stock with ATR $4 is less "
            "volatile in percentage terms than a $20 stock with ATR $4."
        ),
        "example_rule": "atr < 3.50",
        "example_explanation": (
            "Only consider stocks whose average daily true range is below $3.50. "
            "This filters out highly volatile or thinly traded stocks where daily "
            "price swings are too large to manage risk reliably."
        ),
    },

    "hv": {
        "what_it_is": (
            "Historical Volatility (HV) measures how much a stock's price has been "
            "fluctuating as an annualised percentage. Unlike ATR, it is expressed as "
            "a percentage of the price level, making it comparable across stocks of "
            "different prices. An HV of 30 means the stock's annual price movement "
            "standard deviation is 30%."
        ),
        "how_it_works": (
            "The engine calculates the natural log of each day's price change "
            "(log returns), takes the rolling standard deviation over the lookback "
            "period, then multiplies by the square root of 252 to annualise it, "
            "and multiplies by 100 to express as a percentage. This is the industry "
            "standard method for measuring realised volatility."
        ),
        "why_use_it": (
            "Historical volatility is used to filter out stocks that are too risky "
            "for the strategy's risk tolerance, and to pause or exit positions when "
            "volatility spikes above a threshold. Unlike ATR which gives a dollar "
            "amount, HV gives a percentage that is directly comparable across "
            "different stocks and time periods."
        ),
        "how_to_use_it": (
            "Typical values: below 20% is calm, 20-40% is moderate, above 60% is "
            "high volatility. Use a 20-day lookback for recent volatility, 60 days "
            "for medium-term, 252 days for annual. For a volatility filter in entry "
            "rules, set HV less than 0.40 (40%) to avoid high-volatility stocks. "
            "In volatility / freeze rules, pause the strategy when HV spikes above "
            "your threshold."
        ),
        "example_rule": "hv < 0.40",
        "example_explanation": (
            "Only enter trades in stocks whose annualised historical volatility is "
            "below 40%. This avoids highly volatile stocks where position sizing "
            "and risk management become difficult to control."
        ),
    },

    "close": {
        "what_it_is": (
            "Close Price is simply today's adjusted closing price of the stock or "
            "regime ticker. It requires no calculation — the engine reads it directly "
            "from the price data. When used in market regime rules it is applied to "
            "the regime ticker (SPY, VIX, or GLD) rather than individual stocks."
        ),
        "how_it_works": (
            "No computation is performed. The engine reads the closing price directly "
            "from the loaded price data for the relevant date and ticker. When used "
            "as a regime ticker rule (e.g. VIX close > 30), the engine loads the "
            "closing prices for that specific ticker (SPY, VIX, GLD) separately from "
            "the stock universe data."
        ),
        "why_use_it": (
            "Close price is used for simple price-level filters — minimum price "
            "requirements, price targets, and direct comparisons. As a market regime "
            "indicator it is the most direct way to classify market conditions: "
            "SPY above a level means bull market, VIX above a level means high fear, "
            "GLD above a level means risk-off environment."
        ),
        "how_to_use_it": (
            "For stock filtering: set a minimum price (e.g. close > 10) to avoid "
            "penny stocks. For regime rules: combine with a regime ticker — set "
            "regime_ticker to VIX and use close > 30 to detect high-fear periods. "
            "Close can also be used as a value indicator on the right-hand side "
            "to compare two price levels against each other."
        ),
        "example_rule": "close > 10",
        "example_explanation": (
            "Only consider stocks trading above $10. This filters out penny stocks "
            "and very cheap stocks where bid-ask spreads and liquidity can "
            "significantly erode strategy returns."
        ),
    },

    "unadjusted_close": {
        "what_it_is": (
            "Unadjusted Close Price is the raw closing price before any adjustments "
            "for stock splits or dividend payments. Most price data is adjusted "
            "backwards for these events — the unadjusted price is what the stock "
            "actually traded at on that day."
        ),
        "how_it_works": (
            "The engine reads the unadjusted close from a separate price column in "
            "the price data files. No calculation is performed. When a stock splits "
            "2-for-1, the adjusted close of prior days is halved to make the series "
            "continuous — the unadjusted close retains the original values."
        ),
        "why_use_it": (
            "Price thresholds and minimum price filters should ideally use the "
            "unadjusted close because the actual market price is what determines "
            "liquidity, bid-ask spreads, and whether a stock is a penny stock. "
            "An adjusted close of $5 might mean the stock actually trades at $50 "
            "post-split — only the unadjusted close reflects the real current price."
        ),
        "how_to_use_it": (
            "Use in place of close when setting minimum price thresholds for "
            "liquidity filters. For example, require unadjusted_close > 5 to "
            "exclude stocks trading below $5 in the real market. No lookback is "
            "needed — it is a direct price read."
        ),
        "example_rule": "unadjusted_close > 5",
        "example_explanation": (
            "Only consider stocks whose actual (unadjusted) market price is above "
            "$5. This is a more reliable minimum price filter than using the "
            "adjusted close, which can be misleadingly low for stocks that have "
            "had splits."
        ),
    },

    "close_minus_open": {
        "what_it_is": (
            "Close Minus Open is the difference between today's closing price and "
            "today's opening price. A positive value means the stock closed above "
            "where it opened — buyers were in control during the session. A negative "
            "value means sellers dominated and the stock lost ground through the day."
        ),
        "how_it_works": (
            "The engine calculates this directly: close_minus_open = today's close "
            "- today's open. No lookback is used. The result is in dollar terms — "
            "a value of 1.5 means the stock closed $1.50 above its opening price. "
            "This is computed fresh each day from the current session's open and close."
        ),
        "why_use_it": (
            "This indicator captures intraday bias and is commonly used in "
            "mean-reversion strategies. If a stock opens high (gap up) but then "
            "sells off and closes below its open, that intraday weakness is a "
            "short-term bearish signal. Buying at the close after such a day bets "
            "on a recovery the next morning."
        ),
        "how_to_use_it": (
            "Use close_minus_open < 0 to find stocks that sold off intraday (closed "
            "below open) as a mean-reversion entry trigger. Use close_minus_open > 0 "
            "to confirm intraday strength as a momentum confirmation. The threshold "
            "is in dollars — set it relative to the typical price of stocks in your "
            "universe."
        ),
        "example_rule": "close_minus_open < 0",
        "example_explanation": (
            "Enter a trade when the stock closed below where it opened — indicating "
            "intraday selling pressure. A mean-reversion strategy bets that this "
            "intraday weakness is temporary and the stock will recover the following "
            "day."
        ),
    },

    "range_close": {
        "what_it_is": (
            "Range Close measures where SPY's closing price sits within its daily "
            "high-to-low range, expressed as a percentage level. A value near the top "
            "means SPY closed strong — buyers were in control all day. A value near "
            "the bottom means SPY closed weak — sellers dominated."
        ),
        "how_it_works": (
            "The formula is: Low + (High - Low) x value_range_percent / 100. "
            "You set value_range_percent (e.g. 70) to define a price level within "
            "that day's range. The rule then checks whether SPY's close is above or "
            "below that level. For example, 70% means the level sits in the top 30% "
            "of the day's range — if SPY closed above it, buyers dominated the day."
        ),
        "why_use_it": (
            "Range Close is an intraday market strength gauge. When SPY closes near "
            "its daily high it signals broad market momentum and is a favourable "
            "backdrop for entering long positions. It is particularly useful in ETF "
            "strategies and as a market regime filter because it captures the quality "
            "of the day's price action rather than just the direction."
        ),
        "how_to_use_it": (
            "Set value_range_percent to 70 to check if SPY closed in the top 30% "
            "of its range — a strong close. Set it to 50 to check if SPY closed above "
            "the midpoint of its range. Only available when the strategy universe is "
            "SPY. Use as a market entry gate: only enter trades on days when SPY "
            "closes strong."
        ),
        "example_rule": "spy_close > range_close (70%)",
        "example_explanation": (
            "Only enter a trade on days when SPY closes in the top 30% of its daily "
            "high-to-low range. This ensures the broad market closed strong and "
            "avoids entering on days when the market sold off into the close."
        ),
    },

    "vix_close": {
        "what_it_is": (
            "VIX Close Price is the closing level of the CBOE Volatility Index — the "
            "market's expectation of how much the S&P 500 will move over the next 30 "
            "days. Known as the 'fear gauge', a high VIX means markets are fearful "
            "and uncertain. A low VIX means markets are calm and complacent."
        ),
        "how_it_works": (
            "Internally this uses the close indicator routed to the VIX ticker. "
            "The engine loads VIX closing prices separately from the stock universe "
            "data and makes them available as a regime condition. A VIX above 20 "
            "indicates elevated anxiety. Above 30 is high fear — typical during "
            "market sell-offs. Above 40 is extreme fear — rare, usually during crises."
        ),
        "why_use_it": (
            "VIX is used as a market regime gate: when fear is high, mean-reversion "
            "strategies often struggle because sharp bounces fail to sustain. Pausing "
            "entries when VIX is elevated protects the strategy during the most "
            "turbulent periods. It can also be used to allow entries only when VIX "
            "is high — some strategies specifically hunt for fear-driven sell-offs."
        ),
        "how_to_use_it": (
            "VIX below 20: calm market — normal entry conditions. VIX between 20-30: "
            "elevated concern — consider reducing position sizes. VIX above 30: high "
            "fear — many strategies pause entries here. For entry rules: use "
            "vix_close < 25 to only trade in calm conditions. For volatility freeze "
            "rules: use vix_close > 30 to suspend trading during high-fear periods."
        ),
        "example_rule": "vix_close < 25",
        "example_explanation": (
            "Only allow new entries when the VIX is below 25, indicating the market "
            "is in a relatively calm state. This avoids entering new positions during "
            "high-volatility fear events when price behaviour is least predictable."
        ),
    },

    "average_volume": {
        "what_it_is": (
            "Average Volume is the moving average of the number of shares traded per "
            "day over the lookback period. It measures how liquid a stock is — how "
            "easy it is to buy or sell without moving the price against you."
        ),
        "how_it_works": (
            "The engine uses the same SMA calculation applied to daily volume data "
            "instead of price data. It averages the number of shares traded each day "
            "over the lookback period. A stock with average volume of 1,000,000 "
            "trades 1 million shares per day on average. Lower volume means the "
            "stock is thinly traded and harder to execute in."
        ),
        "why_use_it": (
            "Liquidity filtering is essential for systematic strategies. A stock with "
            "low average volume may be difficult to enter or exit at the expected "
            "price — you end up moving the market against yourself. Setting a minimum "
            "average volume ensures the strategy only trades in stocks where "
            "execution is practical and slippage is manageable."
        ),
        "how_to_use_it": (
            "A threshold of 500,000 shares/day is a common minimum for US equities. "
            "For larger strategies or more conservative liquidity requirements, use "
            "1,000,000 or higher. A 20-day lookback captures recent trading activity. "
            "Use a longer lookback (60 days) to smooth out short-term spikes caused "
            "by news events."
        ),
        "example_rule": "average_volume > 500000",
        "example_explanation": (
            "Only consider stocks that trade more than 500,000 shares per day on "
            "average. This ensures sufficient liquidity to enter and exit positions "
            "without significantly moving the stock price or suffering excessive "
            "slippage."
        ),
    },

    "sharpe": {
        "what_it_is": (
            "Sharpe Ratio measures the quality of a stock's recent return by dividing "
            "its gain by its volatility. A high Sharpe ratio means the stock has been "
            "generating good returns relative to how much it has been moving around. "
            "A low or negative Sharpe means poor risk-adjusted performance."
        ),
        "how_it_works": (
            "The engine calculates: momentum / volatility, where momentum is the "
            "percentage price change over momentum_lookback days, and volatility is "
            "the rolling standard deviation of daily returns over vol_lookback days. "
            "The skip_days parameter shifts the momentum calculation back by that "
            "number of days to avoid short-term reversal effects. All three "
            "parameters default to 252 days with skip_days of 0."
        ),
        "why_use_it": (
            "Sharpe ratio is used to rank stocks by the quality of their return rather "
            "than just the size of it. A stock up 30% with extreme volatility is less "
            "attractive than one up 20% with calm, steady moves. This makes Sharpe "
            "an excellent ranking indicator for selecting the best risk-adjusted "
            "performers from a universe of candidates."
        ),
        "how_to_use_it": (
            "Commonly used as a ranking indicator rather than a binary entry filter. "
            "Select the top N stocks ranked by highest Sharpe ratio. As a filter: "
            "sharpe > 0 means the stock has positive risk-adjusted returns over the "
            "lookback period. Set momentum_lookback and vol_lookback via the params "
            "fields — both default to 252 (one trading year). Use skip_days of 21 "
            "to skip the most recent month and avoid short-term reversal noise."
        ),
        "example_rule": "sharpe > 0.5",
        "example_explanation": (
            "Only enter stocks with a Sharpe ratio above 0.5, meaning they have "
            "delivered meaningful returns relative to their volatility over the past "
            "year. This filters out stocks with erratic or low-quality price "
            "appreciation."
        ),
    },
    "vol_bucket": {
        "what_it_is": (
            "A minimum-liquidity filter. It checks whether a stock trades enough volume "
            "to be worth holding, rejecting the thinnest names in the universe. The bar a "
            "stock must clear is set once a year and is based on how the whole universe was "
            "trading at that time."
        ),
        "how_it_works": (
            "Once a year, on the last trading day of the chosen reset month, the engine looks "
            "at every universe member's volume for that single day (volume = turnover divided "
            "by unadjusted close), sorts them low to high, and takes the value at the chosen "
            "percentile — for example the 7.5th percentile. That number becomes the threshold "
            "and is held until the next year's reset. Each day, a stock passes if its rolling "
            "average volume over the chosen window (e.g. 21 days) is above that threshold. The "
            "result is 1 for pass, 0 for fail."
        ),
        "why_use_it": (
            "Mean-reversion entries often surface beaten-down, low-priced names, and some are "
            "too illiquid to trade at the size the strategy assumes. Filtering them out keeps "
            "the tradable list realistic. Setting the threshold once a year — rather than every "
            "day — keeps the bar stable instead of drifting with short-term volume spikes."
        ),
        "how_to_use_it": (
            "Use it as an entry filter compared with == 1 (keep stocks that pass). The defaults "
            "(reset month February, 7.5th percentile, 21-day window) reproduce the legacy system. "
            "A higher percentile is stricter and removes more names; a longer window smooths the "
            "per-stock volume more. The reset month lets you test how a different annual snapshot "
            "of liquidity affects results."
        ),
        "example_rule": "vol_bucket == 1",
        "example_explanation": (
            "Only enter stocks whose 21-day average volume is above the annual liquidity "
            "threshold set on the last trading day of February. Stocks below the threshold "
            "(value 0) are skipped as too illiquid."
        ),
    },
}


# ---------------------------------------------------------------------------
# Seed function
# ---------------------------------------------------------------------------

def seed_descriptions():
    db = SessionLocal()
    updated = []
    skipped = []
    not_found = []

    try:
        for key, content in DESCRIPTIONS.items():
            row = (
                db.query(IndicatorDefinition)
                .filter(IndicatorDefinition.indicator_key == key)
                .first()
            )

            if row is None:
                not_found.append(key)
                continue

            changed = False
            for field, value in content.items():
                # Only write if the field is currently empty
                if not getattr(row, field):
                    setattr(row, field, value)
                    changed = True

            if changed:
                updated.append(key)
            else:
                skipped.append(key)

        db.commit()

        print("── Seed complete ──────────────────────────────────────────")
        print(f"  Updated  : {len(updated)}")
        if updated:
            for k in updated:
                print(f"    + {k}")
        print(f"  Skipped  : {len(skipped)} (already had descriptions)")
        if not_found:
            print(f"  Not found: {not_found} — run sync first")
        print("───────────────────────────────────────────────────────────")

    except Exception as e:
        db.rollback()
        print(f"ERROR: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed_descriptions()