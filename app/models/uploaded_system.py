from sqlalchemy import Column, Integer, String, Float, Date, DateTime
from sqlalchemy.sql import func

from app.database import Base


class UploadedSystem(Base):
    """A finished, externally-produced system imported from CSVs (equity +
    tradelist) for side-by-side comparison. Not run through the engine.

    The raw CSVs live on disk under {UPLOADED_SYSTEMS_PATH}/{id}/; this row is
    just the registry + cached summary so the library list never recomputes.
    ``name`` is the user-facing label (may contain any characters, e.g.
    'pull_back_500_5%stp') — files and the API are keyed by ``id``, so the name
    never has to be path- or URL-safe.
    """
    __tablename__ = "uploaded_system"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False, unique=True)
    starting_capital = Column(Float, nullable=False, default=100000.0)

    # Cached from the equity CSV at upload time (for the library list).
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    n_trades = Column(Integer, nullable=True)

    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    def __repr__(self):
        return (f"<UploadedSystem(id={self.id}, name='{self.name}', "
                f"n_trades={self.n_trades})>")