from sqlalchemy import Column, Integer, String, Date, DECIMAL, DateTime, Text, Numeric, Float
from sqlalchemy.inspection import inspect
from decimal import Decimal
from sqlalchemy.sql import func
from app.database import Base
import datetime
from sqlalchemy.orm import Mapped, mapped_column
class Strategy(Base):
    __tablename__ = "strategies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    rebalance = Column(String(255), nullable=False)
    name = Column(String(255), nullable=False)
    universe = Column(String, nullable=False)
    slots = Column(Integer)
    capital = Column(Numeric(18, 2))
    start_date = Column(Date)
    end_date = Column(Date)
    stoploss_pct = Column(Numeric(5, 2))
    takeprofit_pct = Column(Numeric(5, 2))
    entry_rules = Column(String, nullable=False)   # Store JSON as text
    exit_rules = Column(String, nullable=False)
    stoploss_timing = Column(String, nullable=False)
    takeprofit_timing =  Column(String, nullable=False)
    entry_timing = Column(String, nullable=False)
    exit_timing = Column(String, nullable=False)
    ranking = Column(String(255))
    ranking_lookback = Column(Numeric(5, 2))
    ranking_order = Column(String, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    min_quantity: Mapped[float] = mapped_column(Numeric(5, 2))
    min_price: Mapped[float] = mapped_column(Float)
    system_type = Column(String, nullable=False)
    stoploss_type = Column(String, nullable=False)
    takeprofit_type = Column(String, nullable=False)
    order_type = Column(String, nullable=False)
    limit_pct : Mapped[float] = mapped_column(Float)
    atr_limit_lookback = Column(Numeric(18, 2))




    @property
    def strategy_name(self):
        return self.name


    def __repr__(self):
        return (f"<Strategy(id={self.id}, name='{self.name}', universe='{self.universe}', "
                f"slots={self.slots}, capital={self.capital}, start_date={self.start_date}, "
                f"end_date={self.end_date}, stoploss_pct={self.stoploss_pct}, "
                f"takeprofit_pct={self.takeprofit_pct}, ranking='{self.ranking}')>")

    def to_dict(self):
        """
        Converts a SQLAlchemy Strategy ORM object to a dictionary.
        Handles conversion of Decimal, Date, and DateTime objects to JSON-serializable types.
        """
        data = {}
        # Iterate over all mapped columns
        for column in inspect(self).mapper.column_attrs:
            key = column.key
            value = getattr(self, key)

            if isinstance(value, Decimal):
                data[key] = float(value)  # Convert Decimal to float for JSON
            elif isinstance(value, (datetime.datetime, datetime.date)):
                data[key] = value.isoformat()  # Convert datetime/date to ISO 8601 string
            # Add other custom type handling here if needed (e.g., UUID, custom enums)
            else:
                data[key] = value
        return data