"""C1: the request transaction must commit BEFORE the response starts.

Driven with a raw ASGI scope/receive/send on purpose: httpx's ASGITransport waits for the
whole app call to finish before returning, which hides a commit that runs after the response.
"""

import asyncio
import json
from uuid import uuid4

import httpx
from httpx import ASGITransport
from sqlalchemy import event, text

from supportapi.deps import SessionDep, get_session
from supportcore.db.models import Organisation
from supportapi.routers import admin, audit, auth, health, invites, me, orgs, roles, team


async def _drive(app, method: str, path: str, payload: dict) -> list[str]:
    """Run one request through the ASGI app; return the ordered log of commits and sends."""
    body = json.dumps(payload).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"test"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 443),
    }
    log: list[str] = []
    done = asyncio.Event()
    sent_body = False

    async def receive():
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": body, "more_body": False}
        await done.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        log.append(message["type"])
        if message["type"] == "http.response.start":
            log.append(f"status:{message['status']}")
        if message["type"] == "http.response.body" and not message.get("more_body"):
            done.set()

    def on_commit(_conn):  # a real COMMIT on a connection (savepoint releases do not fire this)
        log.append("commit")

    engine = app.state.engine.sync_engine
    event.listen(engine, "commit", on_commit)
    try:
        await app(scope, receive, send)
    finally:
        event.remove(engine, "commit", on_commit)
        done.set()
    return log


async def test_signup_commits_before_response_starts(app):
    email = f"c1-{uuid4().hex[:12]}@x.com"
    try:
        log = await _drive(app, "POST", "/auth/signup", {"email": email, "password": "correct-horse-1", "name": "C"})
        assert "status:201" in log
        assert "commit" in log
        assert log.index("commit") < log.index("http.response.start"), log
    finally:
        async with app.state.session_factory() as s:
            await s.execute(text("DELETE FROM users WHERE email = :e"), {"e": email})
            await s.commit()


async def test_failed_commit_is_reported_as_500(app):
    slug = f"c1-dup-{uuid4().hex[:8]}"

    @app.post("/_t/dup")
    async def dup(s: SessionDep):
        # Two rows with the same unique slug, never flushed: the error surfaces at COMMIT time.
        s.add(Organisation(name="a", slug=slug))
        s.add(Organisation(name="b", slug=slug))
        return {"ok": True}

    async with httpx.AsyncClient(
        transport=ASGITransport(app, raise_app_exceptions=False), base_url="https://test"
    ) as c:
        r = await c.post("/_t/dup")
    assert r.status_code == 500


def _session_scopes(dependant):
    for dep in dependant.dependencies:
        if dep.call is get_session:
            yield dep.scope
        yield from _session_scopes(dep)


def test_every_route_uses_function_scoped_session():
    """Mixing scopes would give one request two sessions (FastAPI caches per call+scope)."""
    found = False
    for module in (admin, audit, auth, health, invites, me, orgs, roles, team):
        for route in module.router.routes:
            for scope in _session_scopes(route.dependant):
                found = True
                assert scope == "function", route.path
    assert found
