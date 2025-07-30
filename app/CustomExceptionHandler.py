from fastapi import Request, FastAPI, logger
from fastapi.responses import JSONResponse

from app.main import app


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    # Optionally log
    logger.error(f"Unhandled error: {exc}")
    return JSONResponse(
        status_code=500,
        content={"message": "An unexpected error occurred."}
    )