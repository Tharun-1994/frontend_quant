from sqlalchemy import Column, Integer, String, Text, DateTime
from sqlalchemy.sql import func
from app.database import Base


class MechanicDefinition(Base):
    """
    One row per *mechanic* (stop-loss, take-profit, ranking, order type, …).

    Stores all human-readable content — descriptions, examples, guidance —
    exactly like IndicatorDefinition does for indicators. Engineers never edit
    this table directly; a non-engineer fills it via the admin page (or it is
    seeded from seed_mechanic_descriptions.py).

    Structural facts (which schema fields a mechanic controls, its option enums,
    which regimes expose it) live in mechanic_registry.py and are merged into the
    API response by the route — NOT stored here. This keeps the table free of
    JSON columns and keeps mechanic_registry.py the single source of truth for
    structure, the DB the single source of truth for prose.
    """
    __tablename__ = "mechanic_definitions"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # ── Identity ──────────────────────────────────────────────────────────────
    mechanic_key = Column(String(50), nullable=False, unique=True)
    # The code key, e.g. "stop_loss", "take_profit", "ranking".
    # Must match a key in MECHANIC_REGISTRY exactly.

    display_name = Column(String(100), nullable=False)
    # Human-readable label shown in the Rule-info table / drawer header.

    # NOTE: "group" is a reserved word in SQL Server, so the DB column is named
    # "mechanic_group". The Python attribute and the API field stay "group".
    group = Column("mechanic_group", String(50), nullable=False)
    # Which tab this mechanic appears under on the Rule-info "Mechanics" view.
    # One of MECHANIC_GROUPS: Exit & Risk | Order & Execution | Selection & Sizing
    #                         | Concentration | Calendar & Liquidity | Regime

    # ── Content fields (filled via admin page / seed by a non-engineer) ───────
    what_it_is = Column(Text, nullable=True)
    # Plain English: what does this mechanic actually do to a trade? 2-3 sentences.

    how_it_works = Column(Text, nullable=True)
    # Simple explanation of the behaviour / formula in words.
    # Enough for a non-engineer to understand without reading engine code.

    why_use_it = Column(Text, nullable=True)
    # When and why would a trader reach for this? What problem does it solve?

    how_to_use_it = Column(Text, nullable=True)
    # Practical guidance: typical values, what each option changes, trade-offs.

    example_rule = Column(String(150), nullable=True)
    # A concrete worked example, e.g. "entry $50, ATR $2, x2 -> stop at $46".

    example_explanation = Column(Text, nullable=True)
    # Plain English explanation of the example above.

    params_description = Column(Text, nullable=True)
    # Plain English description of the fields this mechanic exposes
    # (the config_fields / option_values shown in the editor).

    caution_note = Column(Text, nullable=True)
    # Any warning a non-engineer should know — silent failure modes, footguns.
    # e.g. "With EOD timing the stop is only checked on the daily close."

    # ── Technical metadata (controlled by engineer via mechanic_registry.py) ──
    status = Column(String(20), nullable=False, default="live")
    # live | engine_only | roadmap. Only "live" mechanics are seeded today.

    sort_order = Column(Integer, nullable=False, default=99)
    # Display order within the group on the page. Lower = shown first.

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
            "mechanic_key": self.mechanic_key,
            "display_name": self.display_name,
            "group": self.group,
            "what_it_is": self.what_it_is,
            "how_it_works": self.how_it_works,
            "why_use_it": self.why_use_it,
            "how_to_use_it": self.how_to_use_it,
            "example_rule": self.example_rule,
            "example_explanation": self.example_explanation,
            "params_description": self.params_description,
            "caution_note": self.caution_note,
            "status": self.status,
            "sort_order": self.sort_order,
            "is_complete": self.is_complete(),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return f"<MechanicDefinition(key='{self.mechanic_key}', complete={self.is_complete()})>"