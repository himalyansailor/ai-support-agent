import httpx
import pytest
from httpx import ASGITransport
from supportcore.config import Settings
from supportcore.testing import TEST_DATABASE_URL, db_engine, session_factory  # noqa: F401
from supportvoice.main import create_app
from voice_helpers import PUBLIC_URL, RecordingQueue


@pytest.fixture
def voice_settings() -> Settings:
    return Settings(
        _env_file=None,
        env="test",
        database_url=TEST_DATABASE_URL,
        redis_url="redis://localhost:6379/0",
        rate_limiter="memory",
        presence="memory",
        pubsub="memory",
        jwt_secret="test-secret-test-secret-test-secret-123",
        llm_tracer="none",
        telephony="fake",
        voice_public_url=PUBLIC_URL,
        voice_max_calls=2,
    )


@pytest.fixture
async def app(voice_settings, db_engine, session_factory):  # noqa: F811
    application = create_app(voice_settings)
    application.state.container.adapters.job_queue = RecordingQueue()
    yield application
    await application.state.container.aclose()
    await application.state.engine.dispose()


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=ASGITransport(app), base_url=PUBLIC_URL) as c:
        yield c


