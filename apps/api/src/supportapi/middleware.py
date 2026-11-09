import json
import re
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from supportcore.observability.context import organisation_id_var, request_context
from supportcore.observability.metrics import HTTP_LATENCY, HTTP_REQUESTS

UNMATCHED_ROUTE = "unmatched"


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
                route = scope.get("route")
                route_label = getattr(route, "path", None) or UNMATCHED_ROUTE
                method = scope["method"]
                HTTP_REQUESTS.labels(
                    method=method,
                    route=route_label,
                    status=str(status_code),
                    organisation_id=organisation_id_var() or "",
                ).inc()
                HTTP_LATENCY.labels(method=method, route=route_label).observe(time.perf_counter() - start)


DEFAULT_BODY_LIMIT = 1024 * 1024
UPLOAD_OVERHEAD = 65536  # multipart framing around the file
_UPLOAD_PATH = re.compile(r"^/orgs/[^/]+/kbs/[^/]+/documents$")


class _BodyTooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    """Pure ASGI request body cap, applied before routing and auth (FastAPI parses multipart bodies, and
    Starlette spools files to disk, before any dependency runs). `default_limit` for every request;
    `upload_limit` for POST /orgs/{org}/kbs/{kb}/documents. Over-limit requests get 413 in the usual error
    envelope: by Content-Length without calling the app, or mid-stream once the counted bytes pass the cap."""

    def __init__(self, app: ASGIApp, default_limit: int = DEFAULT_BODY_LIMIT, upload_limit: int = DEFAULT_BODY_LIMIT) -> None:
        self.app = app
        self.default_limit = default_limit
        self.upload_limit = upload_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_upload = scope["method"] == "POST" and _UPLOAD_PATH.match(scope["path"]) is not None
        limit = self.upload_limit if is_upload else self.default_limit
        code = "upload_too_large" if is_upload else "request_too_large"

        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await self._reject(send, code)
            return

        received = 0
        started = False  # the app's own response has begun
        exceeded = False
        replaced = False  # our 413 has been sent in place of the app's response

        async def receive_wrapper() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise _BodyTooLarge
            return message

        async def send_wrapper(message: Message) -> None:
            # FastAPI turns an exception while reading the body into its own 400/422; once the cap was
            # exceeded, whatever the app answers is replaced by the 413.
            nonlocal started, replaced
            if exceeded and not started:
                if not replaced:
                    replaced = True
                    await self._reject(send, code)
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except _BodyTooLarge:
            if started:
                raise  # too late for a clean 413: drop the connection
            if not replaced:
                await self._reject(send, code)

    @staticmethod
    async def _reject(send: Send, code: str) -> None:
        body = json.dumps({"detail": "Request body is too large", "code": code}).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        await send({"type": "http.response.start", "status": 413, "headers": headers})
        await send({"type": "http.response.body", "body": body})
