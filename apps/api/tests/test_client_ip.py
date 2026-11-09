import httpx
from httpx import ASGITransport
from fastapi import FastAPI
from starlette.requests import Request
from supportapi.client_ip import client_ip
from supportapi.errors import register_error_handlers
from supportcore.ratelimit.ports import RateLimitedError


def _request(xff: str | None, peer: str | None = "9.9.9.9") -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    return Request({"type": "http", "headers": headers, "client": (peer, 1234) if peer else None})


def test_client_ip_ignores_forwarded_without_trusted_hops():
    assert client_ip(_request("1.1.1.1, 2.2.2.2"), 0) == "9.9.9.9"


def test_client_ip_uses_nth_from_right():
    assert client_ip(_request("1.1.1.1, 2.2.2.2"), 1) == "2.2.2.2"
    assert client_ip(_request("1.1.1.1, 2.2.2.2"), 2) == "1.1.1.1"


def test_client_ip_falls_back_to_peer_when_header_too_short_or_missing():
    assert client_ip(_request("1.1.1.1"), 2) == "9.9.9.9"
    assert client_ip(_request(None), 1) == "9.9.9.9"
    assert client_ip(_request(None, peer=None), 0) == "unknown"


def test_client_ip_accepts_asgi_scope():
    scope = {"type": "websocket", "headers": [(b"x-forwarded-for", b"3.3.3.3")], "client": ("8.8.8.8", 1)}
    assert client_ip(scope, 1) == "3.3.3.3"
    assert client_ip(scope, 0) == "8.8.8.8"


async def test_rate_limited_error_sets_retry_after_header():
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/x")
    async def x():
        raise RateLimitedError(retry_after_seconds=17)

    async with httpx.AsyncClient(transport=ASGITransport(app), base_url="https://test") as c:
        r = await c.get("/x")
    assert r.status_code == 429
    assert r.json()["code"] == "rate_limited"
    assert r.headers["retry-after"] == "17"
