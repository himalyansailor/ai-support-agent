"""HTTP-layer observability for the voice app: request context and HTTP metrics, FastAPI tracing, /metrics, and
the request body cap.

Deliberate copies of apps/api/src/supportapi/observability.py (redact_query_string, instrument_app, MetricsEndpoint)
and supportapi/middleware.py (RequestContextMiddleware, BodySizeLimitMiddleware): supportcore must stay
framework-free and a shared web package is out of scope for 7a. Change both when changing either."""

import hmac
import json
import time
import uuid

from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Span
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from supportcore.config import Settings
from supportcore.observability.context import organisation_id_var, request_context
from supportcore.observability.metrics import HTTP_LATENCY, HTTP_REQUESTS, metrics_app

UNMATCHED_ROUTE = "unmatched"
_URL_ATTRIBUTES = ("http.url", "http.target", "url.full")


def redact_query_string(span: Span, scope: dict) -> None:
    """Query strings (here `call_id`) stay out of spans."""
    if not span.is_recording() or not scope.get("query_string"):
        return
    attributes = getattr(span, "attributes", None) or {}
    for key in _URL_ATTRIBUTES:
        value = attributes.get(key)
        if isinstance(value, str) and "?" in value:
            span.set_attribute(key, value.split("?", 1)[0])
    if "url.query" in attributes:
        span.set_attribute("url.query", "[redacted]")


def instrument_app(app: FastAPI, provider: TracerProvider) -> None:
    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=provider, exclude_spans=["send", "receive"], server_request_hook=redact_query_string
    )


class RequestContextMiddleware:
    """Pure ASGI middleware: request id, context, and HTTP metrics."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = Headers(scope=scope).get("x-request-id") or uuid.uuid4().hex
        start = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        with request_context(request_id):
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                route_label = getattr(scope.get("route"), "path", None) or UNMATCHED_ROUTE
                method = scope["method"]
                HTTP_REQUESTS.labels(
                    method=method, route=route_label, status=str(status_code), organisation_id=organisation_id_var() or ""
                ).inc()
                HTTP_LATENCY.labels(method=method, route=route_label).observe(time.perf_counter() - start)


class MetricsEndpoint:
    """Prometheus metrics. SUPPORT_METRICS_TOKEN set: requires `Authorization: Bearer <token>` (401 otherwise).
    Unset: served openly in dev/test, hidden (404) in prod. (A class, so Starlette treats it as a raw ASGI app.)"""

    def __init__(self, settings: Settings) -> None:
        self._app = metrics_app()
        self._token = settings.metrics_token.get_secret_value() if settings.metrics_token else None
        # The dev tunnel publishes this whole app, so once a public URL is configured /metrics (organisation
        # labels) needs the token and is hidden (404) without one. Without a public URL, no token: dev/test serve
        # loopback clients only.
        self._open = self._token is None and settings.env in ("dev", "test") and not settings.voice_public_url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._token is None and not (self._open and self._is_loopback(scope)):
            response = PlainTextResponse("Not Found", status_code=404)
        elif self._token is not None and not self._authorised(scope):
            response = PlainTextResponse("Unauthorized", status_code=401, headers={"WWW-Authenticate": "Bearer"})
        else:
            await self._app(scope, receive, send)
            return
        await response(scope, receive, send)

    @staticmethod
    def _is_loopback(scope: Scope) -> bool:
        client = scope.get("client")
        return client is not None and client[0] in ("127.0.0.1", "::1")

    def _authorised(self, scope: Scope) -> bool:
        assert self._token is not None
        scheme, _, credentials = Headers(scope=scope).get("authorization", "").partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(credentials.encode(), self._token.encode())


DEFAULT_BODY_LIMIT = 64 * 1024  # Twilio's webhook forms are a few KB


class _BodyTooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    """Pure ASGI request body cap, applied before routing and signature checks (the webhooks are public and parse
    forms). Over-limit requests get 413: by Content-Length without calling the app, or mid-stream once the counted
    bytes pass the cap."""

    def __init__(self, app: ASGIApp, limit: int = DEFAULT_BODY_LIMIT) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.limit:
            await self._reject(send)
            return

        received = 0
        started = False
        exceeded = False
        replaced = False

        async def receive_wrapper() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    exceeded = True
                    raise _BodyTooLarge
            return message

        async def send_wrapper(message: Message) -> None:
            nonlocal started, replaced
            if exceeded and not started:  # whatever the app answers is replaced by the 413
                if not replaced:
                    replaced = True
                    await self._reject(send)
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except _BodyTooLarge:
            if started:
                raise
            if not replaced:
                await self._reject(send)

    @staticmethod
    async def _reject(send: Send) -> None:
        body = json.dumps({"detail": "Request body is too large", "code": "request_too_large"}).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        await send({"type": "http.response.start", "status": 413, "headers": headers})
        await send({"type": "http.response.body", "body": body})
