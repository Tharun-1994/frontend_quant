from sqlalchemy import (
    Column, Integer, String, Numeric, Date, DateTime,
    ForeignKey
)
from sqlalchemy.sql import func
from app.database import Base


class SubstitutionOverride(Base):
    """
    One row per trader override action captured from substitution.csv.

    Each upload bumps `version` per (strategy_id, override_date), so the
    audit retains v1, v2, v3 of trader's decisions on the same morning.
    Latest version wins for the broker basket; earlier versions stay for audit.

    NOTE: the SQL column is `override_action` because ACTION is reserved in
    T-SQL. Python attribute + API field stay `action` via the rename trick
    (same pattern as MechanicDefinition.group → mechanic_group).
    """
    __tablename__ = "substitution_overrides"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_id = Column(Integer, ForeignKey("strategies_bucket.id"),
                         nullable=False)

    override_date = Column(Date, nullable=False)
    # The intended_trade_date the override applies to (D).

    version = Column(Integer, nullable=False, default=1)
    # Bumps on each re-upload for same (strategy, override_date).

    original_symbol = Column(String(20), nullable=False)
    # The symbol the system proposed.

    substitute_symbol = Column(String(20), nullable=True)
    # Only populated when action='substitute'.

    # ACTION is reserved in T-SQL — stored under `override_action` column,
    # accessed as `.action` from Python (same pattern as
    # MechanicDefinition.group → mechanic_group).
    action = Column("override_action", String(20), nullable=False)
    # 'elide' | 'substitute' | 'adjust_capital' | 'half_size'

    adjusted_capital = Column(Numeric(18, 2), nullable=True)
    # Only populated when action='adjust_capital'.

    csv_source_path = Column(String(500), nullable=True)
    # Full path of the substitution.csv this row was parsed from.
    # NULL when the override came in via the webapp (future trader page).

    uploaded_at = Column(DateTime, server_default=func.now(), nullable=False)
    uploaded_by = Column(String(100), nullable=True)

    # Free-text reason Vas provides per action in the substitution CSV.
    # e.g. 'chart,size' | 'results' | 'news on drones'
    reason_for_action = Column(String(500), nullable=True)

    def __repr__(self):
        return (f"<SubstitutionOverride(strategy={self.strategy_id}, "
                f"date={self.override_date}, action='{self.action}', "
                f"original='{self.original_symbol}')>")