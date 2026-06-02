from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime
from sqlalchemy.sql import func
from app.database import Base


class IndicatorDefinition(Base):
    """
    One row per indicator.
    Stores all human-readable content — descriptions, examples, guidance.
    Engineers never edit this table directly; they use the admin page.
    """
    __tablename__ = "indicator_definitions"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # ── Identity ──────────────────────────────────────────────────────────────
    indicator_key = Column(String(50), nullable=False, unique=True)
    # The code key used in the strategy builder, e.g. "rsi", "atr", "sma".
    # Must match exactly what _compute_rule_indicators uses.

    display_name = Column(String(100), nullable=False)
    # Human-readable label shown in the UI, e.g. "RSI (Relative Strength Index)"

    category = Column(String(50), nullable=True)
    # Grouping label for the Indicators page.
    # Values: Momentum | Trend | Volatility | Price | Volume | Risk-adjusted

    # ── Content fields (filled via admin page by a non-engineer) ──────────────
    what_it_is = Column(Text, nullable=True)
    # Plain English: what does this indicator actually measure?
    # No jargon. 2-3 sentences.

    how_it_works = Column(Text, nullable=True)
    # Simple explanation of the calculation logic.
    # Enough for a non-engineer to understand without reading code.

    why_use_it = Column(Text, nullable=True)
    # Why would someone pick this indicator when building a strategy?
    # What problem does it solve?

    how_to_use_it = Column(Text, nullable=True)
    # Practical guidance: typical values, what the lookback controls,
    # what thresholds are common, what the number means in practice.

    example_rule = Column(String(150), nullable=True)
    # A concrete rule string as it appears in the builder, e.g. "rsi < 25"

    example_explanation = Column(Text, nullable=True)
    # Plain English explanation of the example_rule above.
    # e.g. "Enter a trade when the 2-day RSI drops below 25 — the stock
    # has fallen sharply and may be ready to bounce."

    # ── Technical metadata (filled by engineer via indicator_registry.py) ─────
    has_lookback = Column(Boolean, nullable=False, default=True)
    # Does this indicator require a lookback period (number of days)?

    default_lookback = Column(Integer, nullable=True)
    # Recommended starting lookback in days. NULL if has_lookback is False.

    has_params = Column(Boolean, nullable=False, default=False)
    # Does this indicator have extra configuration fields beyond lookback?
    # e.g. sharpe has momentum_lookback, vol_lookback, skip_days.
    # e.g. n_week_high_recent has n_week_days, within_days.

    params_description = Column(Text, nullable=True)
    # If has_params is True: plain English description of the extra fields
    # and what each one controls. NULL if has_params is False.

    universe_restriction = Column(String(100), nullable=True)
    # Is this indicator only available for certain universes?
    # e.g. "liquid500 and sp500 only". NULL means available everywhere.

    caution_note = Column(Text, nullable=True)
    # Any warning a non-engineer should know before using this indicator.
    # e.g. "Silently skipped for unsupported universes — no error is raised."
    # NULL if there are no caveats.

    # ── Display ───────────────────────────────────────────────────────────────
    sort_order = Column(Integer, nullable=False, default=99)
    # Controls the order indicators appear within their category on the page.
    # Lower number = shown first.

    # ── Audit ─────────────────────────────────────────────────────────────────
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(),
                        onupdate=func.now(), nullable=False)

    def is_complete(self) -> bool:
        """Returns True if all content fields have been filled in."""
        return all([
            self.what_it_is,
            self.how_it_works,
            self.why_use_it,
            self.how_to_use_it,
            self.example_rule,
            self.example_explanation,
        ])

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "indicator_key": self.indicator_key,
            "display_name": self.display_name,
            "category": self.category,
            "what_it_is": self.what_it_is,
            "how_it_works": self.how_it_works,
            "why_use_it": self.why_use_it,
            "how_to_use_it": self.how_to_use_it,
            "example_rule": self.example_rule,
            "example_explanation": self.example_explanation,
            "has_lookback": self.has_lookback,
            "default_lookback": self.default_lookback,
            "has_params": self.has_params,
            "params_description": self.params_description,
            "universe_restriction": self.universe_restriction,
            "caution_note": self.caution_note,
            "sort_order": self.sort_order,
            "is_complete": self.is_complete(),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return f"<IndicatorDefinition(key='{self.indicator_key}', complete={self.is_complete()})>"


class IndicatorAvailability(Base):
    """
    One row per indicator × regime_type × section × side combination.
    Answers: which indicators are available in which tab, for which regime,
    on which side of the rule?

    Example rows for RSI:
        rsi | Normal               | entry         | lhs
        rsi | Simple               | entry         | lhs
        rsi | Complex              | entry         | lhs
        rsi | Normal               | exit          | lhs
        rsi | Complex              | volatility    | lhs
        rsi | Individual ETFs - Simple | entry     | lhs
    """
    __tablename__ = "indicator_availability"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # ── Foreign key (logical — no DB constraint to keep migrations simple) ────
    indicator_key = Column(String(50), nullable=False)
    # Must match indicator_definitions.indicator_key exactly.

    # ── Where this indicator appears ──────────────────────────────────────────
    regime_type = Column(String(50), nullable=False)
    # Which strategy type: "Normal" | "Simple" | "Complex" |
    #                      "Individual ETFs - Simple"
    # Use "ALL" if an indicator is available in every regime type.

    section = Column(String(20), nullable=False)
    # Which tab on the Indicators page:
    # "entry" | "exit" | "market_regime" | "volatility"

    side = Column(String(5), nullable=False)
    # "lhs" = left-hand side (the indicator being measured)
    # "rhs" = right-hand side (the value being compared against)

    # ── Optional context ──────────────────────────────────────────────────────
    context_note = Column(String(200), nullable=True)
    # Extra context specific to this combination.
    # e.g. "Applied to the regime ticker (SPY, VIX, GLD), not individual stocks."
    # e.g. "SPY universe only — calculated from SPY High/Low/Close."

    sort_order = Column(Integer, nullable=False, default=99)
    # Display order within the tab for this regime type.

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "indicator_key": self.indicator_key,
            "regime_type": self.regime_type,
            "section": self.section,
            "side": self.side,
            "context_note": self.context_note,
            "sort_order": self.sort_order,
        }

    def __repr__(self):
        return (f"<IndicatorAvailability("
                f"key='{self.indicator_key}', "
                f"regime='{self.regime_type}', "
                f"section='{self.section}', "
                f"side='{self.side}')>")