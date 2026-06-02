"""
config_route.py
===============
GET /api/config

Returns all static dropdown/config data that the React frontend needs
to render form fields (universes, indicators, operators, etc.).

This replaces the old Jinja2 template context injection.
React fetches this once on startup and caches it in context.

Adding a new dropdown
---------------------
1. Add the dict to constants/static_config.py.
2. Add it as a key in the response dict below.
React picks it up automatically on next build.
"""

from fastapi import APIRouter

from app.constants.static_config import (
    UNIVERSES,
    INDICATORS,
    OPERATORS,
    CONNECTORS,
    REBALANCE,
    SIGNAL_TIMING,
    RISK_TIMING,
    RANKING_ORDERS,
    SYSTEM_TYPE,
    STOPLOSS_TYPE,
    TAKEPROFIT_TYPE,
)

router = APIRouter(prefix="/api", tags=["config"])


@router.get("/config", summary="All static dropdown config for the React frontend")
def get_config():
    """
    Returns all dropdown options and config constants used by the React app.
    Shapes are intentionally simple key→label dicts so React can iterate them
    directly to render <select> options.
    """
    return {
        "universes":       UNIVERSES,
        "indicators":      INDICATORS,
        "operators":       OPERATORS,
        "connectors":      CONNECTORS,
        "rebalance":       REBALANCE,
        "signal_timing":   SIGNAL_TIMING,
        "risk_timing":     RISK_TIMING,
        "ranking_orders":  RANKING_ORDERS,
        "system_types":    SYSTEM_TYPE,
        "stoploss_types":  STOPLOSS_TYPE,
        "takeprofit_types": TAKEPROFIT_TYPE,
    }