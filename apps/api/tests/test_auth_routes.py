import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from supportapi.container import Container
from supportcore.db.models import RefreshToken
from supportcore.identity.service import REFRESH_REUSE_GRACE_SECONDS

COOKIE = "support_refresh"
SIGNUP = {"email": "a@x.com", "password": "correct-horse-1", "name": "A"}


async def _signup(client, **overrides):
    return await client.post("/auth/signup", json={**SIGNUP, **overrides})


def _refresh_cookie(response) -> str:
    return response.cookies[COOKIE]


async def _refresh_with(client, raw: str):
    client.cookies.clear()
    return await client.post("/auth/refresh", headers={"Cookie": f"{COOKIE}={raw}"})


def test_container_wires_default_adapters(app):
    container = app.state.container
    assert isinstance(container, Container)
    assert "argon2" in container.hasher_registry.keys()
    assert "jwt" in container.token_registry.keys()
    assert container.hasher.verify(container.hasher.hash("correct-horse-1"), "correct-horse-1")


async def test_signup_route_sets_httponly_cookie(client):
    r = await client.post("/auth/signup", json={"email":"a@x.com","password":"correct-horse-1","name":"A"})
    assert r.status_code == 201
    cookie = r.headers["set-cookie"]
    assert "support_refresh=" in cookie
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()
    assert "refresh_token" not in r.json()


async def test_signup_cookie_attributes(client):
    r = await _signup(client)
    cookie = r.headers["set-cookie"].lower()
    assert "path=/auth" in cookie
    assert "max-age=2592000" in cookie
    assert "secure" in cookie  # env=test is not dev


async def test_signup_cookie_not_secure_in_dev(test_settings, db_engine, session):
    import httpx
    from httpx import ASGITransport
    from supportapi.deps import get_session
    from supportapi.main import create_app
    from sqlalchemy.ext.asyncio import AsyncSession

    dev_app = create_app(test_settings.model_copy(update={"env": "dev"}))

    async def isolated():
        async with AsyncSession(bind=session.bind, join_transaction_mode="create_savepoint") as s:
            yield s
            await s.commit()

    dev_app.dependency_overrides[get_session] = isolated
    try:
        async with httpx.AsyncClient(transport=ASGITransport(dev_app), base_url="http://test") as c:
            r = await _signup(c)
        assert r.status_code == 201
        assert "secure" not in r.headers["set-cookie"].lower()
    finally:
        await dev_app.state.engine.dispose()


async def test_signup_response_body(client):
    r = await _signup(client, email=" A@X.com ")
    body = r.json()
    assert set(body) == {"access_token", "user"}
    assert set(body["user"]) == {"id", "email", "name", "is_platform_admin"}
    assert body["user"]["email"] == "a@x.com"
    assert body["user"]["is_platform_admin"] is False
    assert "password_hash" not in r.text


async def test_signup_email_taken_409(client):
    assert (await _signup(client)).status_code == 201
    r = await _signup(client, email="A@x.com")
    assert r.status_code == 409
    assert "detail" in r.json()


async def test_signup_weak_password_422(client):
    r = await _signup(client, password="short")
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str)


async def test_login_ok_and_bad_credentials(client):
    await _signup(client)
    client.cookies.clear()
    ok = await client.post("/auth/login", json={"email": "a@x.com", "password": "correct-horse-1"})
    assert ok.status_code == 200
    assert ok.json()["user"]["email"] == "a@x.com"
    assert COOKIE in ok.cookies
    bad = await client.post("/auth/login", json={"email": "a@x.com", "password": "wrong-horse-1"})
    unknown = await client.post("/auth/login", json={"email": "z@x.com", "password": "wrong-horse-1"})
    assert bad.status_code == unknown.status_code == 401
    assert bad.json() == unknown.json()


async def test_refresh_rotates_cookie(client):
    r1 = await _signup(client)
    raw1 = _refresh_cookie(r1)
    r2 = await client.post("/auth/refresh")  # cookie jar sends support_refresh back
    assert r2.status_code == 200
    assert r2.json()["user"]["email"] == "a@x.com"
    raw2 = _refresh_cookie(r2)
    assert raw2 != raw1
    me = await client.get("/me", headers={"Authorization": f"Bearer {r2.json()['access_token']}"})
    assert me.status_code == 200


async def test_refresh_without_cookie_401(client):
    client.cookies.clear()
    assert (await client.post("/auth/refresh")).status_code == 401


async def _age_rotation(session, raw: str, seconds: float) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == hashlib.sha256(raw.encode()).hexdigest())
        .values(revoked_at=datetime.now(UTC) - timedelta(seconds=seconds))
    )


async def test_refresh_reuse_revokes_family_over_http(client, session):
    raw1 = _refresh_cookie(await _signup(client))
    raw2 = _refresh_cookie(await _refresh_with(client, raw1))
    await _age_rotation(session, raw1, REFRESH_REUSE_GRACE_SECONDS + 1)
    assert (await _refresh_with(client, raw1)).status_code == 401  # replay
    # the family revocation must survive the 401 (request transaction rolls back on error)
    assert (await _refresh_with(client, raw2)).status_code == 401


async def test_concurrent_refresh_from_two_tabs_keeps_session(client):
    raw1 = _refresh_cookie(await _signup(client))
    tab_a = await _refresh_with(client, raw1)
    tab_b = await _refresh_with(client, raw1)  # same cookie, a moment later
    assert tab_a.status_code == 200 and tab_b.status_code == 200
    assert (await _refresh_with(client, _refresh_cookie(tab_a))).status_code == 200
    assert (await _refresh_with(client, _refresh_cookie(tab_b))).status_code == 200


async def test_logout_clears_cookie_and_revokes(client):
    raw = _refresh_cookie(await _signup(client))
    r = await client.post("/auth/logout")
    assert r.status_code == 204
    set_cookie = r.headers["set-cookie"].lower()
    assert "support_refresh=" in set_cookie and ("max-age=0" in set_cookie or "expires=" in set_cookie)
    assert "path=/auth" in set_cookie
    assert (await _refresh_with(client, raw)).status_code == 401


async def test_logout_without_cookie_204(client):
    client.cookies.clear()
    assert (await client.post("/auth/logout")).status_code == 204


async def test_me_requires_auth(client):
    assert (await client.get("/me")).status_code == 401
    bad = await client.get("/me", headers={"Authorization": "Bearer not-a-token"})
    assert bad.status_code == 401


async def test_me_returns_user_and_empty_memberships(client, auth_headers):
    r = await client.get("/me", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["email"] == "a@x.com"
    assert set(body["user"]) == {"id", "email", "name", "is_platform_admin"}
    assert body["memberships"] == []


async def test_me_rejects_inactive_user(client, auth_headers, session):
    from sqlalchemy import update
    from supportcore.db.models import User

    await session.execute(update(User).where(User.email == "a@x.com").values(is_active=False))
    assert (await client.get("/me", headers=auth_headers)).status_code == 401


@pytest.mark.parametrize("email", ["nope", "a b@x.com", "a@x", "a@x.com\r\nBcc: z@x.com", "<a@x.com>", "a,b@x.com"])
async def test_signup_rejects_malformed_email(client, email):
    assert (await _signup(client, email=email)).status_code == 422


@pytest.mark.parametrize("name", ["   ", "\t\n"])
async def test_signup_rejects_blank_name(client, name):
    assert (await _signup(client, name=name)).status_code == 422


async def test_signup_trims_and_normalises(client):
    r = await _signup(client, email="  Mixed.Case@X.com ", name="  Ann  ")
    assert r.status_code == 201
    assert r.json()["user"]["email"] == "mixed.case@x.com" and r.json()["user"]["name"] == "Ann"
