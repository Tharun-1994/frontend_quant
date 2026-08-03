# Patch 119d (FK-fix): combined-system membership + allocation config.
# Corrections vs the originally delivered file:
#   - FK target table is "strategies_bucket" (plural) — matches
#     StrategyBucket.__tablename__ in app/models/strategy_bucket.py
#   - Integer (not BigInteger) — strategies_bucket.id is INT
from sqlalchemy import Column, Integer, Boolean, Text, ForeignKey
from app.database import Base


class CombinedMember(Base):
    __tablename__ = "combined_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    combined_strategy_id = Column(Integer, ForeignKey("strategies_bucket.id"), nullable=False)
    member_strategy_id = Column(Integer, ForeignKey("strategies_bucket.id"), nullable=False)
    priority = Column(Integer, nullable=False)            # 1 = processed first
    is_active = Column(Boolean, nullable=False, default=True)
    # JSON list of member_strategy_ids whose same-day candidate symbols seed
    # this member's ticker_count (legacy: six seeded from one+five).
    seed_sources_json = Column(Text, nullable=False, default="[]")
    # JSON MemberOverrides — per-member multiplier overrides for duplicate
    # tickers (legacy priority=='six' behaviour, now generic).
    overrides_json = Column(Text, nullable=False, default="null")


class CombinedConfig(Base):
    __tablename__ = "combined_config"

    combined_strategy_id = Column(Integer, ForeignKey("strategies_bucket.id"), primary_key=True)
    config_json = Column(Text, nullable=False)             # CombinedAllocationConfig JSON