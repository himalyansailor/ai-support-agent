"""The widget's live connection: WS /widget/v1/conversations/{id}/ws.

1. The Origin header must be settings.web_origin (the iframe page), else close 4403.
2. The first frame, within AUTH_TIMEOUT_SECONDS, is {"type": "auth", "token"}: a widget session
   token whose widget still exists and is enabled, else close 4401. The token's customer must own
   the conversation, else close 4404. The server answers {"type": "ready", "status"}.
3. Client frames: {"type": "message", "text", "client_id"} (rate limited per conversation and per
   organisation: {"type": "error", "code": "rate_limited", "retry_after"}), {"type": "talk_to_human"},
   {"type": "ping"} (answered {"type": "pong"}). Before a message or talk_to_human is acted on the
   widget is read again: once it is deleted or disabled, or requires a signed identity this session
   does not have, the socket gets {"type": "error", "code": "widget_unavailable"} and closes 4401. A frame over MAX_FRAME_BYTES closes the socket 1009;
   a bad frame gets {"type": "error", "code"} and the socket stays open.
4. Every event on the conversation's pub/sub channel is relayed to the socket. If the subscription
   fails the socket closes 1011 (the client reconnects).

No database session is held while waiting on the socket: each action opens its own short one.
Message text and tokens are never logged.
"""

import asyncio
from contextlib import suppress
from typing import Any
from uuid import UUID

from fastapi import APIRouter, WebSocket

from supportapi.container import Container
from supportapi.websocket import (
    CLOSE_NOT_FOUND,
    CLOSE_SERVER_ERROR,
    CLOSE_UNAUTHORIZED,
    JsonSocket,
    SocketClosed as _Closed,
    admit,
    track_connection,
    until_closed,
)
from supportcore.bot.turns import TurnRunner
from supportcore.conversations.service import (
    MAX_CUSTOMER_MESSAGE_LENGTH,
    ConversationService,
    InvalidConversationStateError,
    InvalidMessageError,
)
from supportcore.db.models import Customer, Widget
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import NotFoundError
from supportcore.observability.context import job_context
from supportcore.observability.logging import get_logger
from supportcore.pubsub.ports import conversation_channel
from supportcore.ratelimit.ports import RateLimitedError, enforce
from supportcore.widget.limits import CONFIG_PER_IP, MESSAGE_PER_CONVERSATION, MESSAGE_PER_ORG
from supportcore.widget.tokens import InvalidWidgetTokenError, WidgetClaims

router = APIRouter(prefix="/widget/v1", tags=["widget-public"])
logger = get_logger(__name__)

AUTH_TIMEOUT_SECONDS = 5.0
MAX_FRAME_BYTES = 16384
MAX_CLIENT_ID_LENGTH = 64
# Frames one connection may send, as (limit, window_seconds); over it the socket closes 1008.
FRAME_BUDGET = (60, 60)



class _Connection(JsonSocket):
    def __init__(self, websocket: WebSocket, container: Container, runner: TurnRunner) -> None:
        super().__init__(websocket, max_frame_bytes=MAX_FRAME_BYTES, frame_budget=FRAME_BUDGET)
        self.container = container
        self.runner = runner
        self.session_factory = websocket.app.state.session_factory

    # --- auth ----------------------------------------------------------------------------------------

    async def authenticate(self, conversation_id: UUID) -> WidgetClaims | None:
        """The claims of a customer who owns the conversation, or None after closing the socket."""
        try:
            frame = await asyncio.wait_for(self.receive_frame(), AUTH_TIMEOUT_SECONDS)
        except TimeoutError:
            await self.close(CLOSE_UNAUTHORIZED)
            return None
        token = frame.get("token") if frame is not None and frame.get("type") == "auth" else None
        if not isinstance(token, str) or not token:
            await self.close(CLOSE_UNAUTHORIZED)
            return None
        try:
            claims = self.container.widget_tokens.decode(token)
        except InvalidWidgetTokenError:
            await self.close(CLOSE_UNAUTHORIZED)
            return None
        async with self.session_factory() as session:
            widget = await TenantScopedRepository(session, Widget, claims.organisation_id).get(claims.widget_id)
            if widget is None or not widget.enabled:  # deleted or disabled since the token was issued
                await self.close(CLOSE_UNAUTHORIZED)
                return None
        status = await self.status(claims, conversation_id)
        if status is None:
            await self.close(CLOSE_NOT_FOUND)
            return None
        return claims

    async def widget_still_serves(self, claims: WidgetClaims) -> bool:
        """The token's widget still exists, is enabled, and (when it requires a signed identity) the
        session's customer is identified. One short read."""
        async with self.session_factory() as session:
            widget = await TenantScopedRepository(session, Widget, claims.organisation_id).get(claims.widget_id)
            if widget is None or not widget.enabled:
                return False
            if not widget.identity_required:
                return True
            customer = await TenantScopedRepository(session, Customer, claims.organisation_id).get(claims.customer_id)
            return customer is not None and customer.external_id is not None

    async def require_widget(self, claims: WidgetClaims) -> None:
        """Raises _Closed (after an error frame and close 4401) when the widget no longer serves this session."""
        if await self.widget_still_serves(claims):
            return
        with suppress(Exception):
            await self.send({"type": "error", "code": "widget_unavailable"})
        await self.close(CLOSE_UNAUTHORIZED)
        raise _Closed(CLOSE_UNAUTHORIZED)

    async def status(self, claims: WidgetClaims, conversation_id: UUID) -> str | None:
        """The conversation's status; None when it is not this customer's (or is gone)."""
        async with self.session_factory() as session:
            service = ConversationService(session, claims.organisation_id, self.container.events)
            try:
                return (await service.get_for_customer(conversation_id, claims.customer_id)).status
            except NotFoundError:
                return None

    # --- loops ---------------------------------------------------------------------------------------

    async def receive_loop(self, claims: WidgetClaims, conversation_id: UUID) -> None:
        while True:
            frame = await self.receive_counted_frame()  # a flooding client is closed 1008
            if frame is None:
                await self.send({"type": "error", "code": "invalid_frame"})
                continue
            kind = frame["type"]
            if kind == "ping":
                await self.send({"type": "pong"})
            elif kind == "message":
                await self.on_message(claims, conversation_id, frame)
            elif kind == "talk_to_human":
                await self.on_talk_to_human(claims, conversation_id)
            else:
                await self.send({"type": "error", "code": "invalid_frame"})

    async def on_message(self, claims: WidgetClaims, conversation_id: UUID, frame: dict[str, Any]) -> None:
        text, client_id = frame.get("text"), frame.get("client_id")
        if (
            not isinstance(text, str)
            or not 1 <= len(text.strip()) <= MAX_CUSTOMER_MESSAGE_LENGTH
            or not (client_id is None or (isinstance(client_id, str) and 1 <= len(client_id) <= MAX_CLIENT_ID_LENGTH))
        ):
            await self.send({"type": "error", "code": "invalid_message"})
            return
        await self.require_widget(claims)
        limiter = self.container.adapters.rate_limiter
        try:
            await enforce(limiter, f"conv:{conversation_id}:msg", MESSAGE_PER_CONVERSATION)
            await enforce(limiter, f"org:{claims.organisation_id}:msg", MESSAGE_PER_ORG)
            posted = await self.runner.post_customer_message(claims.organisation_id, conversation_id, text, client_id)
        except RateLimitedError as exc:
            await self.send({"type": "error", "code": "rate_limited", "retry_after": exc.retry_after_seconds})
            return
        except InvalidMessageError:
            await self.send({"type": "error", "code": "invalid_message"})
            return
        except InvalidConversationStateError:
            await self.send({"type": "error", "code": "invalid_conversation_state"})
            return
        except NotFoundError:  # deleted meanwhile
            await self.close(CLOSE_NOT_FOUND)
            raise _Closed(CLOSE_NOT_FOUND) from None
        if not posted.created:  # a resend: this client may have missed the first echo
            await self.send({"type": "message", "message": posted.message})

    async def on_talk_to_human(self, claims: WidgetClaims, conversation_id: UUID) -> None:
        await self.require_widget(claims)
        try:
            await enforce(self.container.adapters.rate_limiter, f"conv:{conversation_id}:human", MESSAGE_PER_CONVERSATION)
            await self.runner.request_human(claims.organisation_id, conversation_id)
        except RateLimitedError as exc:
            await self.send({"type": "error", "code": "rate_limited", "retry_after": exc.retry_after_seconds})
        except NotFoundError:
            await self.close(CLOSE_NOT_FOUND)
            raise _Closed(CLOSE_NOT_FOUND) from None


@router.websocket("/conversations/{conversation_id}/ws")
async def conversation_socket(websocket: WebSocket, conversation_id: UUID) -> None:
    with track_connection("widget"):
        await _conversation_socket(websocket, conversation_id)


async def _conversation_socket(websocket: WebSocket, conversation_id: UUID) -> None:
    container: Container = websocket.app.state.container
    settings = container.settings
    # Accepted before any close, so the browser sees the close code instead of a failed handshake.
    await websocket.accept()
    connection = _Connection(websocket, container, container.turn_runner)  # type: ignore[arg-type]
    if not await admit(connection, settings, container.adapters.rate_limiter, "ip:ws", CONFIG_PER_IP):
        return
    try:
        claims = await connection.authenticate(conversation_id)
    except _Closed:
        return
    if claims is None:
        return
    with job_context(claims.organisation_id, f"ws:{conversation_id}"):
        await _serve(connection, claims, conversation_id)


async def _serve(connection: _Connection, claims: WidgetClaims, conversation_id: UUID) -> None:
    pubsub = connection.container.adapters.pubsub
    try:
        async with pubsub.subscribe(conversation_channel(conversation_id)) as stream:
            # Read after subscribing: a change before this is in `ready`, any later one is relayed.
            status = await connection.status(claims, conversation_id)
            if status is None:
                await connection.close(CLOSE_NOT_FOUND)
                return
            await connection.send({"type": "ready", "status": status})
            if status == "bot":  # re-drive a message a cancelled turn left unanswered (a no-op otherwise)
                connection.runner.submit(claims.organisation_id, conversation_id)
            async with asyncio.TaskGroup() as tasks:
                receiver = tasks.create_task(until_closed(connection.receive_loop(claims, conversation_id)))
                relay = tasks.create_task(
                    connection.relay(stream, "widget_ws_relay_failed", conversation_id=str(conversation_id))
                )
                # Whichever loop ends first (client gone, or the subscription failed) stops the other.
                receiver.add_done_callback(lambda _: relay.cancel())
                relay.add_done_callback(lambda _: receiver.cancel())
    except _Closed:  # closed (by the client, or the relay) while sending `ready`
        pass
    except Exception as exc:  # the subscription could not be made (e.g. Redis is down)
        logger.warning("widget_ws_failed", conversation_id=str(conversation_id), error_type=type(exc).__name__)
        await connection.close(CLOSE_SERVER_ERROR)

