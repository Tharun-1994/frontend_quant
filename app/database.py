"""
database.py
===========
SQLAlchemy engine + session factory.
All connection parameters come from settings.py — never hardcoded here.
"""

import urllib.parse
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from app.Settings import settings

odbc_conn_str = (
    f"DRIVER={settings.ODBC_DRIVER};"
    f"SERVER={settings.DB_SERVER};"
    f"DATABASE={settings.DB_NAME};"
    f"UID={settings.DB_USER};"
    f"PWD={settings.DB_PASSWORD};"
    f"Encrypt=no;"
)

SQLALCHEMY_DATABASE_URL = (
    f"mssql+pyodbc:///?odbc_connect={urllib.parse.quote_plus(odbc_conn_str)}"
)

engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency — yields a DB session and ensures it is closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()