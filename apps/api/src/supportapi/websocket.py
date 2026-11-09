"""Pieces the widget and inbox WebSockets share: the JSON frame socket (one send at a time, a frame size
cap, a per-connection frame budget), the Origin and per-IP connect checks, and the pub/sub relay loop.

A relay iterates its subscription with a plain `async for`: cancelling the relay task ends the iterator
cleanly (never wrap a pub/sub iterator's `anext()` in `wait_for`).
"""

import asyncio
import json
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager, suppress
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from supportapi.client_ip import client_ip
from supportcore.config import Settings
from supportcore.observability.logging import get_logger
from supportcore.observability.metrics import WS_CONNECTIONS
from supportcore.ratelimit.ports import RateLimitedError, RateLimiter, enforce

logger = get_logger(__name__)

CLOSE_FOREIGN_ORIGIN = 4403
CLOSE_FORBIDDEN = 4403
CLOSE_UNAUTHORIZED = 4401
CLOSE_NOT_FOUND = 4404
CLOSE_POLICY_VIOLATION = 1008
CLOSE_TOO_BIG = 1009
CLOSE_SERVER_ERROR = 1011
CLOSE_SERVICE_RESTART = 1012
CLOSE_TRY_AGAIN_LATER = 1013


@contextmanager
def track_connection(kind: str) -> Iterator[None]:
    """support_ws_connections{kind}: kind is `widget` or `inbox`; counts the socket until the handler ends."""
    gauge = WS_CONNECTIONS.labels(kind=kind)
    gauge.inc()
    try:
        yield
    finally:
        gauge.dec()


class SocketClosed(Exception):
    """The socket was closed (by us, with `code`, or by the client)."""

    def __init__(self, code: int | None = None) -> None:
        self.code = code


class JsonSocket:
    """A WebSocket exchanging JSON-object frames that carry a string `type`."""

    def __init__(self, websocket: WebSocket, *, max_frame_bytes: int, frame_budget: tuple[int, int]) -> None:
        self.ws = websocket
        self.max_frame_bytes = max_frame_bytes
        self.frame_budget = frame_budget  # (limit, window_seconds); over it the socket closes 1008
        self._send_lock = asyncio.Lock()
        self._frame_times: deque[float] = deque()

    async def send(self, frame: dict[str, Any]) -> None:
        """Raises SocketClosed when the socket was already closed (e.g. by a relay that failed meanwhile)."""
        async with self._send_lock:
            if self.ws.application_state == WebSocketState.DISCONNECTED:
                raise SocketClosed()
            try:
                await self.ws.send_text(json.dumps(frame))
            except RuntimeError:  # Starlette's "send once a close message has been sent"
                if self.ws.application_state == WebSocketState.DISCONNECTED:
                    raise SocketClosed() from None
                raise

    async def close(self, code: int) -> None:
        if self.ws.application_state == WebSocketState.DISCONNECTED:
            return
        with suppress(Exception):  # the client may be gone already
            await self.ws.close(code)

    async def close_and_raise(self, code: int) -> None:
        await self.close(code)
        raise SocketClosed(code)

    async def receive_frame(self) -> dict[str, Any] | None:
        """The next frame as a JSON object; None when it is not one. Raises SocketClosed."""
        message = await self.ws.receive()
        if message["type"] == "websocket.disconnect":
            raise SocketClosed()
        data = message.get("text")
        size = len(data.encode()) if data is not None else len(message.get("bytes") or b"")
        if size > self.max_frame_bytes:
            await self.close_and_raise(CLOSE_TOO_BIG)
        if data is None:
            data = (message.get("bytes") or b"").decode("utf-8", errors="replace")
        try:
            frame = json.loads(data)
        except ValueError:
            return None
        return frame if isinstance(frame, dict) and isinstance(frame.get("type"), str) else None

    async def receive_counted_frame(self) -> dict[str, Any] | None:
        """receive_frame, then the frame budget: a flooding client is closed 1008 (every frame counts)."""
        frame = await self.receive_frame()
        if not self.within_budget():
            await self.close_and_raise(CLOSE_POLICY_VIOLATION)
        return frame

    def within_budget(self) -> bool:
        limit, window = self.frame_budget
        now = time.monotonic()
        while self._frame_times and self._frame_times[0] <= now - window:
            self._frame_times.popleft()
        if len(self._frame_times) >= limit:
            return False
        self._frame_times.append(now)
        return True

    async def relay(
        self,
        stream: AsyncIterator[dict[str, Any]],
        failure_event: str,
        transform: Callable[[dict[str, Any]], dict[str, Any] | None] = lambda event: event,
        **log_ids: str,
    ) -> None:
        """Forwards every channel event (`transform` may rewrite or drop one). The subscription ends for good
        on a malformed payload or a lost Redis connection: the socket then closes 1011 so the client
        reconnects; a subscription that simply ends closes 1012."""
        try:
            async for event in stream:
                frame = transform(event)
                if frame is not None:
                    await self.send(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.ws.application_state != WebSocketState.DISCONNECTED:
                logger.warning(failure_event, error_type=type(exc).__name__, **log_ids)
            await self.close(CLOSE_SERVER_ERROR)
        else:
            await self.close(CLOSE_SERVICE_RESTART)


async def admit(
    socket: JsonSocket, settings: Settings, limiter: RateLimiter, key_prefix: str, limit: tuple[int, int]
) -> bool:
    """The socket (already accepted, so the browser sees close codes) comes from settings.web_origin (else
    4403) and its IP is within `limit` connects (else 1013). False after closing it."""
    if socket.ws.headers.get("origin") != settings.web_origin:
        await socket.close(CLOSE_FOREIGN_ORIGIN)
        return False
    ip = client_ip(socket.ws, settings.trusted_proxy_hops)
    try:
        await enforce(limiter, f"{key_prefix}:{ip}", limit)
    except RateLimitedError:
        await socket.close(CLOSE_TRY_AGAIN_LATER)
        return False
    return True


async def until_closed(loop: Any) -> None:
    with suppress(SocketClosed, WebSocketDisconnect):
        await loop
