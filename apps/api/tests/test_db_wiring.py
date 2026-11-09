import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select, text
from supportapi.deps import SessionDep
from supportapi.main import create_app
from supportcore.db.models import Organisation
from supportcore.db.session import get_current_session


async def test_readyz_ok_with_db(raw_client):
    r = await raw_client.get("/readyz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


async def test_readyz_503_when_db_unreachable(test_settings):
    bad = test_settings.model_copy(update={"database_url": "postgresql+asyncpg://x:x@127.0.0.1:1/x"})
    app = create_app(bad)
    async with httpx.AsyncClient(transport=ASGITransport(app), base_url="http://test") as c:
        r = await c.get("/readyz")
    assert r.status_code == 503 and r.json() == {"status": "unavailable"}


def test_get_current_session_outside_request_raises():
    with pytest.raises(RuntimeError):
        get_current_session()


@pytest.fixture
def session_routes(app):
    @app.post("/_t/ok/{slug}")
    async def ok(s: SessionDep, slug: str):
        assert get_current_session() is s
        s.add(Organisation(name=slug, slug=slug))
        return {"ok": True}

    @app.post("/_t/fail/{slug}")
    async def fail(s: SessionDep, slug: str):
        s.add(Organisation(name=slug, slug=slug))
        await s.flush()
        raise RuntimeError("boom")

    return app


async def _find(app, slug):
    async with app.state.session_factory() as s:
        return (await s.execute(select(Organisation).where(Organisation.slug == slug))).scalar_one_or_none()


async def test_get_session_commits_on_success(session_routes, raw_client):
    try:
        r = await raw_client.post("/_t/ok/dep-commit")
        assert r.status_code == 200
        assert await _find(session_routes, "dep-commit") is not None
    finally:
        async with session_routes.state.session_factory() as s:
            await s.execute(text("DELETE FROM organisations WHERE slug='dep-commit'"))
            await s.commit()


async def test_get_session_rolls_back_on_error(session_routes):
    async with httpx.AsyncClient(
        transport=ASGITransport(session_routes, raise_app_exceptions=False), base_url="http://test"
    ) as c:
        r = await c.post("/_t/fail/dep-rollback")
    assert r.status_code == 500
    assert await _find(session_routes, "dep-rollback") is None


async def test_route_sees_fixture_seeded_data_and_rolls_back(app, client, session, org_a):
    @app.get("/_t/org/{slug}")
    async def read(s: SessionDep, slug: str):
        row = (await s.execute(select(Organisation).where(Organisation.slug == slug))).scalar_one_or_none()
        return {"found": row is not None}

    @app.post("/_t/iso-write")
    async def write(s: SessionDep):
        s.add(Organisation(name="iso", slug="iso-write"))
        return {"ok": True}

    assert (await client.get("/_t/org/org-a")).json() == {"found": True}
    assert (await client.post("/_t/iso-write")).status_code == 200
    # visible within the test's transaction
    assert (await client.get("/_t/org/iso-write")).json() == {"found": True}
    assert (await session.execute(select(Organisation).where(Organisation.slug == "iso-write"))).scalar_one()


async def test_route_write_did_not_persist(app):
    assert await _find(app, "iso-write") is None
    assert await _find(app, "org-a") is None


async def test_readyz_503_when_db_check_hangs(app, raw_client, monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager

    from supportapi.routers import health

    class HangingEngine:
        @asynccontextmanager
        async def connect(self):
            await asyncio.sleep(3600)
            yield

    real_engine = app.state.engine
    monkeypatch.setattr(health, "READY_TIMEOUT_SECONDS", 0.05)
    app.state.engine = HangingEngine()
    try:
        r = await asyncio.wait_for(raw_client.get("/readyz"), timeout=5)
    finally:
        app.state.engine = real_engine
    assert r.status_code == 503 and r.json() == {"status": "unavailable"}


def test_readyz_timeout_is_two_seconds():
    from supportapi.routers import health

    assert health.READY_TIMEOUT_SECONDS == 2.0
