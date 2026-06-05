"""
seed_mechanic_descriptions.py
=============================
Populates all mechanic descriptions in the database.
All content is sourced directly from the actual engine code:
  - backtest_engine_src  (BacktestServiceImplV2, PortfolioServiceImplV2,
                          StrategyBuilderServiceImplV2, VolatilityCutEvaluator)
  - schemas/strategy.py  (the fields each mechanic controls)
  - constants/options.ts (the option enums)

SAFE TO RE-RUN — only writes to fields that are currently NULL.
Existing descriptions are never overwritten.

Run with:
    python -m app.constants.seed_mechanic_descriptions
(Run sync_mechanics first so the rows exist.)

ENGINE-ACCURACY NOTES (verified against backtest_engine_src):
  - Stop-loss / Take-profit: the engine implements NORMAL and ATR_BASED only.
    DOLLAR_BASED is offered in the form but is NOT honoured by the engine.
  - Look Inside Bar: the engine source has no minute-bar SL/TP scanning; the
    toggle is saved but SL/TP are evaluated on daily bars today.
  Both facts are reflected in the relevant caution_note fields below.
"""

from app.database import SessionLocal
from app.models.MechanicDefinition import MechanicDefinition

# ---------------------------------------------------------------------------
# All descriptions keyed by mechanic_key  (must match mechanic_registry.py)
# ---------------------------------------------------------------------------
DESCRIPTIONS = {

    # ══ Exit & Risk ═══════════════════════════════════════════════════════════
    "stop_loss": {
        "what_it_is": (
            "An automatic exit that closes a losing position once it falls a set "
            "amount below your entry price. It is the core risk control of every "
            "strategy — it decides the most you are willing to lose on one trade."
        ),
        "how_it_works": (
            "Three styles. NORMAL exits when the price reaches entry x (1 - Stoploss%/100) "
            "— a flat percentage below entry. ATR_BASED places the stop entry minus "
            "(Stoploss% x ATR), using the prior day's ATR over the ATR lookback, so the "
            "buffer is wider for volatile stocks and tighter for calm ones. (Short trades "
            "mirror this — the stop sits above entry.) The level is checked either on the "
            "daily close (EOD) or against the intraday path (INTRADAY)."
        ),
        "why_use_it": (
            "It caps the damage from any single position that goes against you. Without "
            "a stop, one runaway loser can undo many winners. A volatility-scaled (ATR) "
            "stop keeps risk consistent across a universe of mixed-volatility names."
        ),
        "how_to_use_it": (
            "For NORMAL, 3-8% is typical. For ATR, set Stoploss% as the ATR multiplier "
            "(e.g. 2) and an ATR lookback (e.g. 14): a larger multiplier means a wider "
            "stop and fewer stop-outs. Leave the field blank or 0 to disable the stop "
            "entirely."
        ),
        "example_rule": "NORMAL, Stoploss% 5  ->  exit at entry x 0.95",
        "example_explanation": (
            "A position entered at $100 is closed if it falls to $95. In ATR mode with "
            "ATR $2 and multiplier 2, the stop instead sits $4 below entry, at $96."
        ),
        "params_description": (
            "Stoploss Type (NORMAL / ATR_BASED / DOLLAR_BASED). "
            "Stoploss %: the percent below entry for NORMAL, or the ATR multiplier for ATR_BASED. "
            "Stoploss Dollar: a fixed dollar loss, used only for DOLLAR_BASED. "
            "ATR Lookback (Stoploss): the ATR window in days, used only for ATR_BASED. "
            "Stoploss Timing: EOD (checked on the daily close) or INTRADAY."
        ),
        "caution_note": (
            "The current backtest engine implements NORMAL and ATR_BASED only — "
            "DOLLAR_BASED is selectable in the form but is NOT yet honoured by the engine, "
            "so avoid it until it is wired in. With EOD timing the stop is checked only on "
            "the daily close, so an intraday breach that recovers by the close will not trigger."
        ),
    },

    "take_profit": {
        "what_it_is": (
            "An automatic exit that closes a winning position once it has gained a set "
            "amount above your entry — the mirror image of the stop-loss, locking in "
            "profit at a target."
        ),
        "how_it_works": (
            "NORMAL exits at entry x (1 + Takeprofit%/100) — a flat percentage above entry. "
            "ATR_BASED sets the target a distance of (Takeprofit% x Stoploss% x ATR) above "
            "entry, using the prior day's ATR — so the stop-loss percent also scales the "
            "target distance. The level is checked on the daily close (EOD) or intraday."
        ),
        "why_use_it": (
            "Mean-reversion trades often give back gains if held too long. A take-profit "
            "books the bounce while it is there, rather than waiting for an exit rule that "
            "may fire later and lower."
        ),
        "how_to_use_it": (
            "For NORMAL, set the percentage gain you want to capture (e.g. 10-15%). For "
            "ATR, remember the distance multiplies BOTH Takeprofit% and Stoploss% by ATR. "
            "Leave blank or 0 to disable. Whichever of stop, target, or exit rule triggers "
            "first wins the trade."
        ),
        "example_rule": "NORMAL, Takeprofit% 15  ->  exit at entry x 1.15",
        "example_explanation": (
            "A position entered at $100 is closed for profit if it rises to $115."
        ),
        "params_description": (
            "Takeprofit Type (NORMAL / ATR_BASED / DOLLAR_BASED). "
            "Takeprofit %: the percent above entry for NORMAL, or part of the ATR distance for ATR_BASED. "
            "Takeprofit Dollar: a fixed dollar gain, used only for DOLLAR_BASED. "
            "ATR Lookback (Takeprofit): the ATR window in days, used only for ATR_BASED. "
            "Takeprofit Timing: EOD or INTRADAY."
        ),
        "caution_note": (
            "As with the stop-loss, the engine implements NORMAL and ATR_BASED only — "
            "DOLLAR_BASED is not yet honoured. Note the ATR target distance multiplies the "
            "stop-loss percent as well as the take-profit percent, so changing the stop also "
            "moves the ATR target."
        ),
    },

    "max_time": {
        "what_it_is": (
            "A time-based exit: a hard cap on how many trading days a position may be held. "
            "When the cap is reached the trade is closed no matter what, even if neither the "
            "stop, the target, nor an exit rule has fired."
        ),
        "how_it_works": (
            "The engine counts the trading days a position has been open. Once that count "
            "reaches Max Time, it closes the trade with the reason 'MaxTime'. It is a "
            "backstop, not a signal."
        ),
        "why_use_it": (
            "It stops capital being tied up indefinitely in a trade that is going nowhere, "
            "freeing the slot for a fresher opportunity — useful for short-horizon "
            "mean-reversion systems where a trade should resolve quickly or not at all."
        ),
        "how_to_use_it": (
            "Set it to the maximum number of trading days you would tolerate holding a "
            "stalled position (e.g. 5-15 for a short-term system). Leave blank or 0 to "
            "disable."
        ),
        "example_rule": "Max Time 10  ->  close any trade still open after 10 trading days",
        "example_explanation": (
            "A position that has neither hit its target nor its stop after 10 trading days "
            "is closed automatically to free the slot."
        ),
        "params_description": (
            "Max Time: the maximum holding period in trading days. 0 or blank disables it."
        ),
        "caution_note": (
            "The count is in trading days held, not calendar days. It applies to every open "
            "position once enabled."
        ),
    },

    "risk_timing": {
        "what_it_is": (
            "Controls WHEN the stop-loss and take-profit levels are checked each day: once "
            "on the close (EOD), or continuously against the intraday price path (INTRADAY)."
        ),
        "how_it_works": (
            "With EOD, the engine compares the level only to that day's closing price. With "
            "INTRADAY, it also checks the day's open, high and low, so a stop or target can "
            "trigger mid-session at the level itself rather than waiting for the close."
        ),
        "why_use_it": (
            "INTRADAY is more realistic for tight stops that would be breached and exited "
            "intraday in live trading. EOD is simpler and avoids being shaken out by an "
            "intraday spike that reverses by the close."
        ),
        "how_to_use_it": (
            "Use INTRADAY when your stop or target is tight enough that intraday moves "
            "matter; use EOD for end-of-day systems. Set it the same way for both the "
            "stop-loss and the take-profit unless you have a specific reason not to."
        ),
        "example_rule": "Stoploss Timing INTRADAY  ->  stop can trigger mid-session",
        "example_explanation": (
            "With INTRADAY, a stop at $96 fires the moment price touches $96 during the day. "
            "With EOD, it fires only if the day CLOSES at or below $96."
        ),
        "params_description": (
            "EOD: checked once on the daily close. INTRADAY: checked against the day's "
            "open / high / low path."
        ),
        "caution_note": (
            "INTRADAY is only offered for some strategy types. Realistic intraday SL/TP also "
            "depends on the engine having intraday data for the names — see Look Inside Bar."
        ),
    },

    # ══ Order & Execution ═════════════════════════════════════════════════════
    "order_type": {
        "what_it_is": (
            "How the entry order is placed: a plain market order, or a limit order that "
            "only fills if the price reaches a level you set."
        ),
        "how_it_works": (
            "NORMAL is a market order filled at the chosen bar. LIMIT places the order a "
            "set percent away from the close — for a long, below it at close x (1 - Limit%/100), "
            "for a short, above it — and fills only if price trades there. LIMIT_ATR is the "
            "same idea but the offset is volatility-scaled: close minus (Limit% x ATR). "
            "Orders are only placed up to the number of free slots."
        ),
        "why_use_it": (
            "A limit lets you demand a better entry price than the current close — for "
            "mean-reversion, buying only after a further dip. The trade-off is that some "
            "limits never fill, so you take fewer positions."
        ),
        "how_to_use_it": (
            "Use NORMAL when you want to be in regardless of price. Use LIMIT with a small "
            "percent (e.g. 1-3%) to insist on a pullback; use LIMIT_ATR to make that pullback "
            "scale with each stock's volatility (set Limit% as the ATR multiplier and an ATR "
            "lookback)."
        ),
        "example_rule": "LIMIT, Limit% 2  ->  long fills only if price dips to close x 0.98",
        "example_explanation": (
            "Instead of buying at the $100 close, the order waits for a dip to $98 and fills "
            "only if the stock trades down to it; otherwise no position is taken that day."
        ),
        "params_description": (
            "Order Type (NORMAL / LIMIT / LIMIT_ATR). "
            "Limit %: percent away from the close for LIMIT, or the ATR multiplier for LIMIT_ATR. "
            "ATR Lookback (Limit): the ATR window in days, used only for LIMIT_ATR."
        ),
        "caution_note": (
            "LIMIT and LIMIT_ATR orders can go unfilled — that means fewer trades but better "
            "average prices, and it is a major reason a backtest tradelist and a live tradelist "
            "differ (the backtest fills any limit the day's range reaches; live may miss it)."
        ),
    },

    "signal_timing": {
        "what_it_is": (
            "Decides which bar and price a fired signal is acted on — the next bar's open, "
            "this bar's close, or the end-of-day close."
        ),
        "how_it_works": (
            "'Next bar Open' acts on the following session's opening price. 'This Bar Close' "
            "executes at the same day's close. 'EOD Close' handles exits at the end of the "
            "day. Entry timing and exit timing are set independently."
        ),
        "why_use_it": (
            "It models the realistic delay between a signal and a fill. Acting on the next "
            "open is the honest default; acting on the same close assumes you could trade on "
            "a price you only know once the bar has finished."
        ),
        "how_to_use_it": (
            "Prefer 'Next bar Open' for entries to avoid look-ahead. Use 'This Bar Close' "
            "only for systems that genuinely trade at the close."
        ),
        "example_rule": "Entry timing = Next bar Open",
        "example_explanation": (
            "A signal that fires today is filled at tomorrow's opening price, not at a price "
            "from today that you could not actually have traded."
        ),
        "params_description": (
            "Next bar Open: act on the next session's open. "
            "This Bar Close: act on today's close. "
            "EOD Close: end-of-day close (exits)."
        ),
        "caution_note": (
            "'This Bar Close' can flatter a backtest because it assumes a fill at a price "
            "only known after the bar closes (look-ahead). 'Next bar Open' is the realistic choice."
        ),
    },

    "look_inside_bar": {
        "what_it_is": (
            "A switch that controls how finely the stop-loss and take-profit are checked: "
            "on minute-bar data inside the day, or on the daily bar only."
        ),
        "how_it_works": (
            "When ON, the engine is intended to scan intraday minute bars so an SL/TP level "
            "touched mid-day registers. When OFF, it uses only the daily open / high / low / "
            "close. It changes whether a stop breached intraday but recovered by the close "
            "actually triggers."
        ),
        "why_use_it": (
            "Minute-bar checking is the most faithful way to model tight intraday stops and "
            "targets; daily-bar checking is faster and adequate for end-of-day systems."
        ),
        "how_to_use_it": (
            "Enable it only on strategies where intraday SL/TP precision matters. It is "
            "exposed for ETF (Individual ETFs) strategies."
        ),
        "example_rule": "Look Inside Bar = ON",
        "example_explanation": (
            "With it ON, a $96 stop touched at 11am triggers then. With it OFF, the engine "
            "only sees the day's summary bar."
        ),
        "params_description": (
            "A single on/off toggle. ON = minute-bar SL/TP scanning; OFF = daily bar only."
        ),
        "caution_note": (
            "Two limits: it is exposed for ETF strategies only, and the current engine source "
            "does NOT contain minute-bar SL/TP scanning — the toggle is saved but SL/TP are "
            "evaluated on daily bars today. Confirm it is wired before relying on intraday precision."
        ),
    },

    "rebalance_constraints": {
        "what_it_is": (
            "Strategy-wide settings: how often the strategy re-evaluates signals, and the "
            "minimum price and share quantity a candidate must meet to be traded."
        ),
        "how_it_works": (
            "Rebalance (Daily / Weekly / Monthly) sets the cadence at which signals are "
            "checked and the book refreshed. Minimum Price screens out low-priced stocks; "
            "Minimum Quantity sets the smallest share count per fill."
        ),
        "why_use_it": (
            "Rebalance frequency matches the strategy's horizon. The price and quantity "
            "minimums keep you out of illiquid, hard-to-fill names that distort backtests "
            "and add slippage live."
        ),
        "how_to_use_it": (
            "Use Daily for short-term systems, Weekly/Monthly for slower ones. Set Minimum "
            "Price to exclude penny stocks (e.g. $5). Set Minimum Quantity to a sensible "
            "round lot."
        ),
        "example_rule": "Rebalance DAILY, Minimum Price 5",
        "example_explanation": (
            "Signals are checked every trading day, and stocks priced under $5 are never "
            "entered."
        ),
        "params_description": (
            "Rebalance (DAILY / WEEKLY / MONTHLY). Minimum Price: lowest share price allowed. "
            "Minimum Quantity: smallest number of shares per position."
        ),
        "caution_note": (
            "Minimum Price is a liquidity and quality screen — very low-priced stocks carry "
            "higher slippage and spread risk."
        ),
    },

    # ══ Selection & Sizing ════════════════════════════════════════════════════
    "ranking": {
        "what_it_is": (
            "A tie-breaker for when more entry signals fire on a day than you have open "
            "slots. It sorts the candidates by an indicator and takes only the best ones."
        ),
        "how_it_works": (
            "The engine ranks every qualifying candidate by the chosen Ranking Indicator "
            "(computed at the Ranking Lookback) and fills the free slots with the top of the "
            "list. Ranking Order = Ascending takes the lowest values; Descending takes the "
            "highest."
        ),
        "why_use_it": (
            "When a strategy generates more signals than it can hold, ranking makes sure the "
            "capital goes to the strongest candidates by your chosen measure rather than an "
            "arbitrary order."
        ),
        "how_to_use_it": (
            "Pick the indicator that expresses 'best' for your edge (e.g. ROC for momentum) "
            "and a lookback for it, then choose Descending for highest-is-best or Ascending "
            "for lowest-is-best. The Sector Level / Max Per Sector fields on this card add a "
            "concentration cap (see Sector filter)."
        ),
        "example_rule": "Rank by ROC, Descending  ->  fill slots with the highest-ROC names",
        "example_explanation": (
            "If 30 stocks signal but only 10 slots are free, the 10 with the highest 20-day "
            "ROC are taken and the rest are skipped that day."
        ),
        "params_description": (
            "Ranking Indicator: the measure used to sort candidates. "
            "Ranking Lookback: the indicator's window in days. "
            "Ranking Order: Ascending (take lowest) or Descending (take highest)."
        ),
        "caution_note": (
            "Ranking only matters when signals exceed free slots. The ranking indicator must "
            "exist precomputed at the chosen lookback. It is offered for equity strategy types."
        ),
    },

    "top_n_selection": {
        "what_it_is": (
            "A rule-level comparison that keeps only the top N names by an indicator, rather "
            "than comparing to a fixed value. It is set inside a rule via the compare-to type."
        ),
        "how_it_works": (
            "Two flavours. 'Top N (raw)' ranks the indicator across ALL tickers in the data "
            "(~2000) and keeps the top N. 'Top N (within active universe)' ranks ONLY the "
            "tickers that are in today's active universe and keeps the top N of those. "
            "Ranking Order chooses lowest (Ascending) or highest (Descending)."
        ),
        "why_use_it": (
            "It expresses 'the best N right now' directly inside a rule — e.g. the 10 "
            "strongest names — instead of a hand-picked threshold that drifts as the market moves."
        ),
        "how_to_use_it": (
            "Choose the compare-to type, set N in the value field, and set Ranking Order. "
            "Almost always pick 'within active universe' so the ranking respects the "
            "strategy's universe rather than the entire dataset."
        ),
        "example_rule": "ROC TOP 10 in universe (highest)",
        "example_explanation": (
            "Keeps the 10 highest-ROC stocks among the current universe members; with the "
            "'raw' option it would instead rank across every ticker in the data."
        ),
        "params_description": (
            "Comparison type: Value / Indicator-Price / Top N (raw) / Top N (within active universe). "
            "Value: N, the number of names to keep. "
            "Ranking Order: Ascending (lowest N) or Descending (highest N)."
        ),
        "caution_note": (
            "'Top N (raw)' ranks the whole dataset, not just your universe, so it can select "
            "names outside what you trade and produce a very different basket from "
            "'within active universe'. The two are easy to confuse — pick deliberately."
        ),
    },

    "universe": {
        "what_it_is": (
            "The set of stocks the strategy is allowed to trade — S&P 500, Russell 3000, or "
            "Liquid 500."
        ),
        "how_it_works": (
            "Every entry candidate must be a member of the chosen universe. The universe also "
            "determines which indicators are available, because some indicators are only "
            "precomputed for certain universes."
        ),
        "why_use_it": (
            "It sets the breadth and liquidity profile of the strategy: a broad universe "
            "(Russell 3000) gives more signals, a focused one (Liquid 500 / S&P 500) gives "
            "larger, more liquid names."
        ),
        "how_to_use_it": (
            "Match the universe to the strategy and the indicators it uses. If a rule relies "
            "on CRSI or Range-close, the universe must support it (see the caution)."
        ),
        "example_rule": "Universe = Liquid 500",
        "example_explanation": (
            "Only the 500 most liquid names are eligible, and indicators such as CRSI become "
            "available."
        ),
        "params_description": (
            "S&P 500 / Russell 3000 / Liquid 500. ETF strategies use the ETF selector "
            "(e.g. SPY, GLD) instead of this."
        ),
        "caution_note": (
            "Some indicators silently produce no signal outside their supported universe: "
            "CRSI needs Liquid 500 or S&P 500, and Range-close needs SPY. Used elsewhere they "
            "never fire and no error is shown — check the indicator's own page."
        ),
    },

    "capital_slots": {
        "what_it_is": (
            "The two numbers that drive position sizing: Capital is the total money the "
            "strategy runs, and Slots is the number of positions it can hold at once."
        ),
        "how_it_works": (
            "Each slot is allocated roughly Capital divided by Slots. The engine only opens "
            "new entries up to the number of free slots (Slots minus current holdings), so "
            "Slots is also the hard cap on concurrent positions."
        ),
        "why_use_it": (
            "Together they set both how much each trade risks and how diversified the book is. "
            "More slots means smaller, more diversified positions; fewer slots means larger, "
            "more concentrated ones."
        ),
        "how_to_use_it": (
            "Pick Slots for the diversification you want (e.g. 10-20) and Capital for the "
            "account size. Remember Slots is the N that Ranking and rule-level Top-N select down to."
        ),
        "example_rule": "Capital 100,000, Slots 10  ->  ~$10,000 per position, max 10 at once",
        "example_explanation": (
            "With $100k across 10 slots, each new trade is sized around $10k and the strategy "
            "never holds more than 10 names."
        ),
        "params_description": (
            "Capital: total money the strategy deploys. "
            "Slots: number of concurrent positions; also the per-trade size divisor."
        ),
        "caution_note": (
            "Slots interacts with Ranking and Top-N — it is the number of names they trim the "
            "candidate list down to each day."
        ),
    },

    # ══ Concentration ═════════════════════════════════════════════════════════
    "sector_filter": {
        "what_it_is": (
            "A cap on how many positions the strategy may hold in the same sector at once, "
            "to avoid over-concentrating in one part of the market."
        ),
        "how_it_works": (
            "The engine counts current holdings per sector and, for each new candidate, "
            "allows it only while that sector's count is below Max Per Sector. Sector Level "
            "selects how finely sectors are defined."
        ),
        "why_use_it": (
            "Without it, a strategy can pile most of its slots into one hot sector, turning a "
            "diversified system into a concentrated sector bet. The cap enforces breadth."
        ),
        "how_to_use_it": (
            "Set Max Per Sector to the most names you want in any one sector (e.g. 2-3), and "
            "choose a Sector Level for the grouping granularity. Set 0 to disable."
        ),
        "example_rule": "Max Per Sector 2  ->  at most 2 names per sector held at once",
        "example_explanation": (
            "If two technology names are already held, further technology candidates are "
            "skipped until a slot frees up, even if they have strong signals."
        ),
        "params_description": (
            "Sector Level (0-5): the sector classification depth; 0 disables. "
            "Max Per Sector: the maximum simultaneous positions allowed in one sector."
        ),
        "caution_note": (
            "Both fields must be set and a sector map must be loaded for the universe; with "
            "Max Per Sector 0 the filter is off."
        ),
    },

    "duplicates": {
        "what_it_is": (
            "Controls whether the same ticker can occupy more than one slot at the same time, "
            "and how many such duplicate holdings are allowed across the book."
        ),
        "how_it_works": (
            "Max Per Ticker caps how many times one ticker can be held simultaneously. Max "
            "Duplicate Sets caps how many duplicated holdings the whole portfolio may carry; "
            "once at that cap, no further duplicates are taken."
        ),
        "why_use_it": (
            "Some strategies deliberately scale into the same name on repeated signals; "
            "others should never double up. These fields make that behaviour explicit instead "
            "of accidental."
        ),
        "how_to_use_it": (
            "Set Max Per Ticker to 1 for no duplicates (the usual case). Raise it, and set Max "
            "Duplicate Sets, only if you intend to stack the same name."
        ),
        "example_rule": "Max Per Ticker 1  ->  never hold the same stock twice",
        "example_explanation": (
            "If a ticker is already held and signals again, the second signal is ignored "
            "rather than opening a second position in the same name."
        ),
        "params_description": (
            "Max Per Ticker: how many simultaneous positions allowed in one ticker (1 = no duplicates). "
            "Max Duplicate Sets: how many duplicate holdings the portfolio may carry (0 = no limit)."
        ),
        "caution_note": (
            "Leave Max Per Ticker at 1 unless you specifically want to scale into the same "
            "name — duplicates concentrate risk in one stock."
        ),
    },

    "gap_filter": {
        "what_it_is": (
            "A filter that skips an entry when the stock opens too far away from the previous "
            "day's close — i.e. when it gaps sharply up or down."
        ),
        "how_it_works": (
            "The engine computes the gap as (open - previous close) / previous close x 100 "
            "and, if its absolute size exceeds Max Gap%, refuses the entry for that name "
            "that day. It is only active when Max Gap% is greater than 0."
        ),
        "why_use_it": (
            "A large gap usually means news has changed the situation since the signal was "
            "generated. Skipping big gaps avoids chasing a stock that has already moved away "
            "from the price your rule assumed."
        ),
        "how_to_use_it": (
            "Set Max Gap% to the largest opening jump you will accept (e.g. 3-5%). Smaller "
            "values are stricter and skip more names. Set 0 to disable."
        ),
        "example_rule": "Max Gap% 5  ->  skip any name that opens more than 5% from prior close",
        "example_explanation": (
            "A stock that closed at $100 and opens at $107 (+7%) is skipped, because the "
            "favourable setup the rule found no longer exists at that price."
        ),
        "params_description": (
            "Max Gap %: the maximum absolute open-vs-previous-close gap allowed, as a percent. "
            "0 disables the filter."
        ),
        "caution_note": (
            "The check is on the absolute gap, so it filters both up-gaps and down-gaps. "
            "A value of 0 turns it off entirely."
        ),
    },

    # ══ Calendar & Liquidity ══════════════════════════════════════════════════
    "banned_months": {
        "what_it_is": (
            "A list of calendar months in which the strategy takes no new entries — a simple "
            "seasonality filter."
        ),
        "how_it_works": (
            "On any date that falls in a banned month, entry signals are suppressed. Months "
            "are given as numbers (1 = January … 12 = December)."
        ),
        "why_use_it": (
            "Some strategies perform poorly in specific months (e.g. thin summer or year-end "
            "periods). Banning those months sidesteps the weak stretch without changing the "
            "rest of the logic."
        ),
        "how_to_use_it": (
            "Select the months to sit out. Use sparingly and with evidence — month bans are "
            "easy to overfit to past data."
        ),
        "example_rule": "Banned months = {12}  ->  no new entries in December",
        "example_explanation": (
            "Throughout December the strategy stops opening new trades, then resumes in January."
        ),
        "params_description": (
            "A list of month numbers (1-12) in which new entries are blocked."
        ),
        "caution_note": (
            "It blocks new entries only — positions already open continue to manage out "
            "normally during a banned month."
        ),
    },

    "tdom_filters": {
        "what_it_is": (
            "Calendar filters that block entries on a particular trading day of the month or "
            "on a particular weekday, optionally only within certain months."
        ),
        "how_it_works": (
            "Each rule can block on a trading-day-of-month position (0 = the 1st trading day, "
            "1 = the 2nd, …) or on a weekday (0 = Monday … 4 = Friday), and can restrict that "
            "block to a list of months. If any rule matches a date, entries are blocked that day."
        ),
        "why_use_it": (
            "It targets known calendar effects more precisely than banning a whole month — "
            "e.g. avoiding the first trading day of the month, or never entering on Fridays."
        ),
        "how_to_use_it": (
            "Add a rule per pattern you want to block. Remember the trading-day index is "
            "0-based. Combine with month restrictions for effects that only occur in certain "
            "months."
        ),
        "example_rule": "tdom 0  ->  no entries on the first trading day of any month",
        "example_explanation": (
            "Entries are suppressed on each month's first trading day; a weekday rule of 4 "
            "would instead suppress all Friday entries."
        ),
        "params_description": (
            "Per rule: tdom (0-indexed trading day of month), weekday (0=Mon … 4=Fri), and an "
            "optional list of months the rule applies to."
        ),
        "caution_note": (
            "The trading-day position is 0-indexed (0 = first trading day, not 1). Multiple "
            "rules combine — a date is blocked if ANY rule matches."
        ),
    },

    "vol_turnover_filter": {
        "what_it_is": (
            "A liquidity filter that drops the least-traded names by volume and by turnover, "
            "using different cut-offs depending on whether the market is in an uptrend or a "
            "downtrend."
        ),
        "how_it_works": (
            "Each day the engine decides the regime from SPY: bull if yesterday's SPY close "
            "is above its 200-day moving average, otherwise bear. It then excludes the bottom "
            "X% of names by volume and the bottom Y% by turnover, where X and Y are the bull "
            "or the bear percentages depending on the regime."
        ),
        "why_use_it": (
            "Liquidity needs tighten in falling markets. Switching to stricter cut-offs in a "
            "bear regime keeps the strategy out of thin names exactly when spreads widen and "
            "fills get worse."
        ),
        "how_to_use_it": (
            "Enable it, then set the bull and bear percentiles for volume and turnover. "
            "Defaults exclude the bottom 20% (bull) / 45% (bear) by volume and 35% (bull) / "
            "5% (bear) by turnover. Leave it disabled to skip all of this."
        ),
        "example_rule": "vol_pct_bull 0.20, vol_pct_bear 0.45",
        "example_explanation": (
            "In an uptrend the bottom 20% of names by volume are excluded; in a downtrend the "
            "bottom 45% are excluded, tightening liquidity when it matters most."
        ),
        "params_description": (
            "Enabled: on/off. SPY ticker: the index used for the bull/bear decision. "
            "vol_pct_bull / vol_pct_bear: the bottom-volume fraction excluded in each regime. "
            "turnover_pct_bull / turnover_pct_bear: the bottom-turnover fraction excluded in each regime."
        ),
        "caution_note": (
            "This is different from the Volume bucket indicator (a once-a-year liquidity gate). "
            "This filter recomputes the cut-off every day and switches it on the SPY-vs-SMA200 regime."
        ),
    },

    # ══ Regime ════════════════════════════════════════════════════════════════
    "market_trend_rules": {
        "what_it_is": (
            "Rules applied to a market index (the regime ticker — SPY, VIX, or GLD) that turn "
            "the whole strategy on or off depending on the state of the market."
        ),
        "how_it_works": (
            "The rules are evaluated each day on the regime ticker, not on individual stocks. "
            "When they are true the strategy is 'on' and may take entries; when false it stands "
            "aside."
        ),
        "why_use_it": (
            "Most long strategies do better in uptrends and worse in downtrends. Gating on a "
            "market-trend condition lets the strategy only trade when the broad backdrop is "
            "favourable."
        ),
        "how_to_use_it": (
            "Choose the regime ticker and write a condition for when to be active (e.g. SPY "
            "above its 200-day SMA, or VIX below a level). It is available on ETF, Simple, and "
            "Complex strategy types."
        ),
        "example_rule": "SPY close > SPY SMA(200)  ->  strategy active only in uptrends",
        "example_explanation": (
            "On days the S&P 500 is above its 200-day average the strategy trades; on days it "
            "is below, new entries are suppressed."
        ),
        "params_description": (
            "Regime ticker (SPY / VIX / GLD), a market-trend type, and the rule tree evaluated "
            "on that index."
        ),
        "caution_note": (
            "The indicators in these rules apply to the index itself, not to the individual "
            "stocks you trade."
        ),
    },

    "freeze_resume": {
        "what_it_is": (
            "A pair of market-level rule sets that temporarily pause the strategy (freeze) and "
            "later restart it (resume), based on conditions evaluated once per day."
        ),
        "how_it_works": (
            "The freeze rules define the dates on which new entries are halted; the resume "
            "rules define when trading switches back on. They are evaluated at the market level "
            "and also support calendar conditions (e.g. a specific month)."
        ),
        "why_use_it": (
            "It is a rule-driven risk-off switch — step aside during stress (e.g. when the "
            "market drops below a trend line) and re-enter only once a calmer condition is met, "
            "without deleting the strategy's normal logic."
        ),
        "how_to_use_it": (
            "Write a freeze condition (e.g. SPY below its 200-day SMA) and a resume condition "
            "(e.g. SPY back above it). It is available on Complex strategies."
        ),
        "example_rule": "Freeze when SPY < SMA(200); resume when SPY > SMA(200)",
        "example_explanation": (
            "The strategy stops opening new trades while the market is below trend and starts "
            "again once it recovers above trend."
        ),
        "params_description": (
            "A freeze rule tree and a resume rule tree, both evaluated on market-level data; "
            "calendar (month) conditions are supported."
        ),
        "caution_note": (
            "Freeze pauses NEW entries only — it does not close positions you already hold; "
            "those continue to exit via their normal stop, target, or exit rules. Available on "
            "Complex strategies."
        ),
    },

    "close_on_regime_exit": {
        "what_it_is": (
            "A toggle that decides what happens to open positions when the market trend shifts "
            "away from this regime: force them closed, or let them run to their normal exits."
        ),
        "how_it_works": (
            "When ON, the engine closes this regime's open positions at the next open as soon "
            "as the market-trend condition stops being met. When OFF, those positions stay on "
            "and exit only via their stop, target, or exit rules."
        ),
        "why_use_it": (
            "In a multi-regime strategy you may want a regime's trades cleared out the moment "
            "its market condition ends, rather than lingering into a backdrop they were not "
            "designed for."
        ),
        "how_to_use_it": (
            "Turn it ON for regimes whose positions should not outlive the regime; leave it OFF "
            "to let trades manage out naturally after a switch. It is relevant when market-trend "
            "rules are in use."
        ),
        "example_rule": "Close on regime exit = ON",
        "example_explanation": (
            "When the market trend that activated this regime turns off, the regime's open "
            "positions are closed at the next open instead of being carried forward."
        ),
        "params_description": (
            "A single on/off toggle. ON = force-close this regime's positions at next open on "
            "a regime switch; OFF = exit them normally."
        ),
        "caution_note": (
            "Relevant only for strategies that use market-trend rules / multiple regimes."
        ),
    },

}


# ---------------------------------------------------------------------------
# Seed function  (NULL-safe — mirrors seed_indicator_descriptions.py)
# ---------------------------------------------------------------------------

def seed_mechanic_descriptions():
    db = SessionLocal()
    updated = []
    skipped = []
    not_found = []

    try:
        for key, content in DESCRIPTIONS.items():
            row = (
                db.query(MechanicDefinition)
                .filter(MechanicDefinition.mechanic_key == key)
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

        print("── Mechanic seed complete ─────────────────────────────────")
        print(f"  Updated  : {len(updated)}")
        if updated:
            for k in updated:
                print(f"    + {k}")
        print(f"  Skipped  : {len(skipped)} (already had descriptions)")
        if not_found:
            print(f"  Not found: {not_found} — run sync_mechanics first")
        print("───────────────────────────────────────────────────────────")

    except Exception as e:
        db.rollback()
        print(f"ERROR: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed_mechanic_descriptions()