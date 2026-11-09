"""HTTP-layer observability wiring: FastAPI tracing and the /metrics endpoint (core stays framework-free)."""

import hmac

from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Span
from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import Receive, Scope, Send

from supportcore.config import Settings
from supportcore.observability.metrics import metrics_app


# Server-span attributes that can carry the query string (old and new HTTP semantic conventions).
_URL_ATTRIBUTES = ("http.url", "http.target", "url.full")


def redact_query_string(span: Span, scope: dict) -> None:
    """Server request hook: query strings carry search queries, which must never reach spans. Strips the
    query from the URL attributes and blanks `url.query`."""
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
    # No span per ASGI send/receive: on the widget WebSocket that would be one span per streamed token.
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        exclude_spans=["send", "receive"],
        server_request_hook=redact_query_string,
    )


class MetricsEndpoint:
    """Prometheus metrics, which carry per-organisation labels, so not public.

    SUPPORT_METRICS_TOKEN set: requires `Authorization: Bearer <token>` (401 otherwise).
    Unset: served openly in dev/test, hidden (404) in prod.
    (A class, so Starlette treats it as a raw ASGI app.)
    """

    def __init__(self, settings: Settings) -> None:
        self._app = metrics_app()
        self._token = settings.metrics_token.get_secret_value() if settings.metrics_token else None
        self._open = self._token is None and settings.env in ("dev", "test")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._token is None and not self._open:
            response = PlainTextResponse("Not Found", status_code=404)
        elif self._token is not None and not self._authorised(scope):
            response = PlainTextResponse(
                "Unauthorized", status_code=401, headers={"WWW-Authenticate": "Bearer"}
            )
        else:
            await self._app(scope, receive, send)
            return
        await response(scope, receive, send)

    def _authorised(self, scope: Scope) -> bool:
        assert self._token is not None
        scheme, _, credentials = Headers(scope=scope).get("authorization", "").partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(credentials.encode(), self._token.encode())
