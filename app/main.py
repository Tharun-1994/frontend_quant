"""
main.py
=======
FastAPI application entrypoint.

What lives here
---------------
- App creation and middleware (CORS)
- Router registration
- Startup hook (indicator sync)
- Global exception handler
- Uvicorn entry point

What does NOT live here
-----------------------
- Any business logic  →  routes/
- Any static data     →  constants/static_config.py
- Any credentials     →  settings.py  (reads from .env or env vars)
- Any HTML rendering  →  gone; React is the frontend
"""

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from app.database import engine, Base, SessionLocal
from app.models import *  # registers all ORM models with Base
from app.routes.backtest import router as backtest_router
from app.routes.config_route import router as config_router
from app.routes.eod import router as eod_router
from app.routes.tradelist import router as tradelist_router
from app.routes.equity_view import router as equity_router
from app.routes.indicators_route import router as indicators_router
from app.routes.strategies import router as strategies_router
from app.routes.uploaded_systems import router as uploaded_systems_router
from app.services.sync_indicators import sync_indicators
from app.routes.mechanics_route import router as mechanics_router
from app.services.sync_mechanics import sync_mechanics
# mechanics:END
from app.Settings import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create DB tables and sync indicator registry on every startup."""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        result = sync_indicators(db)
        logger.info(result.summary())
    finally:
        db.close()
    # mechanics:BEGIN  (revert: delete this block)
    db = SessionLocal()
    try:
        mech_result = sync_mechanics(db)
        logger.info(mech_result.summary())
    finally:
        db.close()
    # mechanics:END
    yield  # app runs here


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Quant Strategy API",
    description="Backend API for the quant strategy builder. React frontend is separate.",
    version="1.0.0",
    lifespan=lifespan,
)

# ── CORS ──────────────────────────────────────────────────────────────────────
# Origins are configured in settings.py / .env.
# In production: set CORS_ORIGINS to your actual React domain.

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(config_router)
app.include_router(strategies_router)
app.include_router(backtest_router)
app.include_router(equity_router)
app.include_router(indicators_router)
# mechanics:BEGIN  (revert: delete this block)
app.include_router(mechanics_router)
# mechanics:END
# C3: nightly EOD execution endpoints (PM trigger)
app.include_router(eod_router)
# F2/F3: tradelist read + stop-override patch + basket review
app.include_router(tradelist_router)
# System Comparison: upload + compare equity/tradelist CSVs
app.include_router(uploaded_systems_router)

# ── Global exception handler ──────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url)
    return JSONResponse(
        status_code=500,
        content={"detail": "An unexpected error occurred."},
    )


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
def health():
    """Simple liveness probe."""
    return {"status": "ok"}


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    uvicorn.run(
        "app.main:app",
        host="192.168.1.66",
        port=settings.PORT,
        reload=True,
    )


if __name__ == "__main__":
    main()