import httpx
import pytest
from httpx import ASGITransport
from supportapi.main import create_app
from supportcore.config import Settings
from supportapi.deps import get_session
from supportcore.db.session import use_current_session
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from supportcore.testing import TEST_DATABASE_URL, db_engine, org_a, org_b, session, session_factory  # noqa: F401


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        env="test",
        database_url=TEST_DATABASE_URL,
        redis_url="redis://localhost:6379/0",
        rate_limiter="memory",
        presence="memory",
        pubsub="memory",
        usage_counter="memory",
        jwt_secret="test-secret-test-secret-test-secret-123",
        smtp_from="noreply@test.local",
        llm_tracer="none",
    )


@pytest.fixture
async def app(test_settings, db_engine):
    application = create_app(test_settings)
    yield application
    # ASGITransport does not run lifespan, so dispose the app's engine here.
    await application.state.engine.dispose()


@pytest.fixture
async def raw_client(app):
    """Client on the real get_session path: requests really commit to the test DB."""
    async with httpx.AsyncClient(transport=ASGITransport(app), base_url="https://test") as c:
        yield c


@pytest.fixture
async def client(app, session):
    """Client whose requests share the per-test connection and roll back at teardown.

    https base_url: env=test sets Secure on the refresh cookie, so httpx only sends it back over https.
    """

    async def isolated_session():
        async with AsyncSession(
            bind=session.bind, join_transaction_mode="create_savepoint", expire_on_commit=False
        ) as s:
            with use_current_session(s):
                try:
                    yield s
                    await s.commit()
                except BaseException:
                    await s.rollback()
                    raise

    # Overrides are keyed by the callable and inherit SessionDep's scope="function", so the
    # isolated session also commits (to its savepoint) before the response is sent.
    app.dependency_overrides[get_session] = isolated_session
    async with httpx.AsyncClient(transport=ASGITransport(app), base_url="https://test") as c:
        yield c
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture
async def auth_headers(client):
    """Bearer headers for a freshly signed-up user a@x.com."""
    r = await client.post("/auth/signup", json={"email": "a@x.com", "password": "correct-horse-1", "name": "A"})
    assert r.status_code == 201
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# --- knowledge base route tests -------------------------------------------------------------------
# Jobs (inline queue) open their own sessions on the app's engine, so they only see data that was really
# committed. These tests therefore use the real get_session path (`kb_client`) instead of the
# rollback-isolated `client`, and the `session_factory` fixture truncates every table at teardown.
# The pgvector kb_vectors_* tables the jobs create are dropped at teardown as well.


@pytest.fixture
def kb_settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        env="test",
        database_url=TEST_DATABASE_URL,
        redis_url="redis://localhost:6379/0",
        jwt_secret="test-secret-test-secret-test-secret-123",
        smtp_from="noreply@test.local",
        file_storage="local",
        local_storage_path=str(tmp_path / "uploads"),
        vector_store="pgvector",
        job_queue="inline",
        presence="memory",
        max_upload_bytes=2000,
        llm_tracer="none",
    )


@pytest.fixture
async def kb_app(kb_settings, db_engine, session_factory):
    application = create_app(kb_settings)
    yield application
    await application.state.job_queue.drain()
    async with db_engine.begin() as conn:
        names = (
            await conn.execute(text("SELECT tablename FROM pg_tables WHERE tablename LIKE 'kb\\_vectors\\_%'"))
        ).scalars()
        for name in list(names):
            await conn.execute(text(f'DROP TABLE IF EXISTS "{name}"'))
        await conn.execute(text("DELETE FROM vector_collections"))
    await application.state.container.adapters.aclose()
    await application.state.engine.dispose()


@pytest.fixture
async def kb_client(kb_app):
    async with httpx.AsyncClient(transport=ASGITransport(kb_app), base_url="https://test") as c:
        yield c
