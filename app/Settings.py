"""
settings.py
===========
Single source of truth for all environment-specific configuration.

Usage
-----
All other modules import from here:
    from app.settings import settings

Local development — create a .env file in the project root:
    DB_SERVER=DESKTOP-GNEUBQ4
    DB_NAME=quant_py_db
    DB_USER=admin
    DB_PASSWORD=admin
    BACKTEST_DATA_PATH=C:\\Tharun\\Projects\\backtest_data
    CORS_ORIGINS=http://localhost:3000,http://localhost:5173

Production — set the same keys as real environment variables.
Never commit credentials to source control.
"""

import os
from dotenv import load_dotenv

load_dotenv()  # reads .env if present; silently ignored if .env doesn't exist


class Settings:
    # ── Database (MS SQL Server via ODBC) ─────────────────────────────────────
    DB_SERVER:   str = os.getenv("DB_SERVER",   "DESKTOP-GNEUBQ4")
    DB_NAME:     str = os.getenv("DB_NAME",     "quant_py_db")
    DB_USER:     str = os.getenv("DB_USER",     "admin")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "admin")
    ODBC_DRIVER: str = os.getenv("ODBC_DRIVER", "{ODBC Driver 18 for SQL Server}")

    # ── File system paths ─────────────────────────────────────────────────────
    BACKTEST_DATA_PATH: str = os.getenv(
        "BACKTEST_DATA_PATH",
        r"C:\Tharun\Projects\backtest_data"
    )

    # ── Server ────────────────────────────────────────────────────────────────
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8001"))

    # ── CORS — comma-separated list of allowed origins ────────────────────────
    # Default allows common React dev servers; tighten in production.
    CORS_ORIGINS: list[str] = [
        o.strip()
        for o in os.getenv(
            "CORS_ORIGINS",
            "http://localhost:3000,http://localhost:5173,http://127.0.0.1:3000"
        ).split(",")
        if o.strip()
    ]

    @property
    def BACKTEST_JAVA_URL(self) -> str:
        return os.getenv("BACKTEST_JAVA_URL", "http://localhost:8080")


settings = Settings()