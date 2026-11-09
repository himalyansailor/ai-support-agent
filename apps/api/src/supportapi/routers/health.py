import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter(tags=["health"])

# A hung database must fail the readiness probe quickly, not hold it open.
READY_TIMEOUT_SECONDS = 2.0


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


async def _check_db(request: Request) -> None:
    async with request.app.state.engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


@router.get("/readyz")
async def readyz(request: Request):
    try:
        await asyncio.wait_for(_check_db(request), timeout=READY_TIMEOUT_SECONDS)
    except Exception:  # includes TimeoutError
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ok"}
