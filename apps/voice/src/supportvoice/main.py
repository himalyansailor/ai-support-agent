import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.routing import Route
from supportcore.config import Settings, get_settings
from supportcore.db.session import create_engine, session_factory
from supportcore.observability.logging import configure_logging, get_logger
from supportcore.observability.tracing import configure_tracing

from supportvoice.callback_routes import router as callback_router
from supportvoice.container import build_voice_container
from supportvoice.media_ws import router as media_router
from supportvoice.observability import (
    BodySizeLimitMiddleware,
    MetricsEndpoint,
    RequestContextMiddleware,
    instrument_app,
)
from supportvoice.testing_routes import include_test_routes
from supportvoice.transfer_routes import router as transfer_router
from supportvoice.twilio_routes import router as twilio_router

SERVICE_NAME = "support-voice"
READY_TIMEOUT_SECONDS = 2.0
VOICE_DB_MAX_OVERFLOW = 10  # connections on top of voice_db_pool_size

logger = get_logger(__name__)


def voice_settings(settings: Settings | None = None) -> Settings:
    """The environment's settings, with the voice process's own service name (logs, traces)."""
    return (settings or get_settings()).model_copy(update={"service_name": SERVICE_NAME})


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = voice_settings(settings)
    configure_logging(settings)
    engine = create_engine(settings, pool_size=settings.voice_db_pool_size, max_overflow=VOICE_DB_MAX_OVERFLOW)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            try:
                await app.state.container.aclose()
            finally:
                await engine.dispose()

    app = FastAPI(title="Support Voice", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory(engine)
    app.state.container = build_voice_container(settings, engine)
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    async def _check_db() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    @app.get("/ready")
    async def ready():
        try:
            await asyncio.wait_for(_check_db(), timeout=READY_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001  (includes TimeoutError)
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ok"}

    # Route (not Mount) so /metrics is served without a trailing-slash redirect.
    app.router.routes.append(Route("/metrics", endpoint=MetricsEndpoint(settings), include_in_schema=False))
    app.include_router(twilio_router)
    app.include_router(transfer_router)
    app.include_router(callback_router)
    app.include_router(media_router)
    include_test_routes(app, settings)
    instrument_app(app, configure_tracing(settings, engine))
    return app
