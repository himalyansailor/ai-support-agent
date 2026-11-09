"""The agent dashboard's live connection: WS /inbox/v1/ws.

1. The Origin header must be settings.web_origin, else close 4403; an IP gets CONNECTS_PER_IP connects,
   else close 1013.
2. The first frame, within AUTH_TIMEOUT_SECONDS, is {"type": "auth", "token", "org_id"} with a dashboard
   access token (a widget session token is refused), else close 4401. The caller must be able to view the
   org's inbox (queries.answer, analytics.view or bot.configure), else close 4403 (a non-member too).
3. The server subscribes to the org's inbox channel and the member's notification channel, then sends
   {"type": "ready", "membership_id", "counts": {"mine", "unassigned", "bot"}} (counts read after
   subscribing, so a later change is relayed, never missed).
4. Client frames:
   - {"type": "ping"} -> {"type": "pong"}, and a presence beat. Every ping (and watch) first re-checks
     access in a short session: a deactivated user is closed 4401, a member removed from the org or
     without inbox access any more is closed 4403 (neither beats);
   - {"type": "watch", "conversation_id"}: follow one conversation, replacing any previous watch;
     answered {"type": "watching", "conversation_id"} once subscribed, or {"type": "error", "code":
     "not_found"} when it is not this org's;
   - {"type": "unwatch"}.
   A bad frame gets {"type": "error", "code": "invalid_frame"} and the socket stays open. A frame over
   MAX_FRAME_BYTES closes 1009; more than FRAME_BUDGET frames closes 1008.
5. Relayed events: `conversation.upsert`, `presence`, `note.added` (`{"type", "conversation_id"}`: ids only,
   the client refetches the notes) and `email.status` (`{"type", "conversation_id", "message_id", "status",
   "error_type"}`: ids and short codes only, so any inbox viewer may see it) (inbox channel), `notification` (the member's
   channel), and for the watched conversation `message`, `bot_token`, `bot_reset`, `bot_done`, `status` and
   `handoff`, each with the `conversation_id` added. Events can arrive out of order across channels and
   processes: clients keep the newest `version` per conversation upsert and the newest `at` (epoch ms) per
   membership's `presence`. A subscription that fails (a malformed payload, a lost Redis connection)
   closes 1011; one that ends cleanly (e.g. the pub/sub shutting down) closes 1012. The client
   reconnects either way. A send that finds the socket already closed ends the connection quietly.

Presence: a member whose role has queries.answer beats on auth and on every ping; it is cleared when that
membership's last socket in this process closes (another process's sockets keep beating; a crash is left
to the TTL). Other viewers (analytics.view or bot.configure only) cannot be assigned and never beat.

No database session is held while waiting on the socket or relaying: auth, counts and watch checks each
open a short one. Tokens are never logged.
"""

import asyncio
from collections import Counter
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import APIRouter, WebSocket
from sqlalchemy.ext.asyncio import AsyncSession

from supportapi.container import Container
from supportapi.deps import resolve_org_context, user_from_access_token
from supportapi.websocket import (
    CLOSE_FORBIDDEN,
    CLOSE_SERVER_ERROR,
    CLOSE_UNAUTHORIZED,
    JsonSocket,
    SocketClosed,
    admit,
    track_connection,
    until_closed,
)
from supportcore.db.models import Conversation, User
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import NotFoundError
from supportcore.inbox.events import inbox_channel, user_channel
from supportcore.inbox.service import InboxService
from supportcore.observability.context import job_context
from supportcore.observability.logging import get_logger
from supportcore.pubsub.ports import conversation_channel
from supportcore.rbac.checker import has_permission
from supportcore.rbac.permissions import Permission

router = APIRouter(prefix="/inbox/v1", tags=["inbox-realtime"])
logger = get_logger(__name__)

AUTH_TIMEOUT_SECONDS = 5.0
MAX_FRAME_BYTES = 16384
# Frames one connection may send, as (limit, window_seconds); over it the socket closes 1008.
FRAME_BUDGET = (120, 60)
CONNECTS_PER_IP = (60, 60)

VIEW_INBOX = (Permission.QUERIES_ANSWER, Permission.ANALYTICS_VIEW, Permission.BOT_CONFIGURE)
INBOX_EVENTS = frozenset({"conversation.upsert", "presence", "note.added", "email.status"})
USER_EVENTS = frozenset({"notification"})
CONVERSATION_EVENTS = frozenset({"message", "bot_token", "bot_reset", "bot_done", "status", "handoff"})

# (organisation id, membership id) -> this process's open sockets that beat presence for it.
_presence_sockets: Counter[tuple[UUID, UUID]] = Counter()


@dataclass(frozen=True)
class _Viewer:
    user_id: UUID
    organisation_id: UUID
    membership_id: UUID | None  # None: a platform admin viewing an org they are not a member of
    can_answer: bool  # queries.answer: assignable, so the socket keeps presence


def _only(types: frozenset[str]) -> Any:
    return lambda event: event if event.get("type") in types else None


class _Connection(JsonSocket):
    def __init__(self, websocket: WebSocket, container: Container) -> None:
        super().__init__(websocket, max_frame_bytes=MAX_FRAME_BYTES, frame_budget=FRAME_BUDGET)
        self.container = container
        self.session_factory = websocket.app.state.session_factory
        self._loops: list[asyncio.Task[None]] = []
        self._tasks: asyncio.TaskGroup | None = None
        self._watch: asyncio.Task[None] | None = None

    # --- auth ----------------------------------------------------------------------------------------

    async def authenticate(self) -> _Viewer | None:
        """The viewer behind a valid auth frame, or None after closing the socket (4401 or 4403)."""
        try:
            frame = await asyncio.wait_for(self.receive_frame(), AUTH_TIMEOUT_SECONDS)
        except TimeoutError:
            frame = None
        token = frame.get("token") if frame is not None and frame.get("type") == "auth" else None
        org_id = _uuid(frame.get("org_id")) if frame is not None else None
        if not isinstance(token, str) or not token or org_id is None:
            await self.close(CLOSE_UNAUTHORIZED)
            return None
        async with self.session_factory() as session:
            user = await user_from_access_token(session, self.container, token)
            if user is None:
                await self.close(CLOSE_UNAUTHORIZED)
                return None
            viewer = await self._authorize(session, user, org_id)
        if viewer is None:
            await self.close(CLOSE_FORBIDDEN)
        return viewer

    async def _authorize(self, session: AsyncSession, user: User | None, org_id: UUID) -> _Viewer | None:
        """The viewer when the (still active) user may view the org's inbox, else None."""
        if user is None or not user.is_active:
            return None
        try:
            ctx = await resolve_org_context(session, user, org_id, self.container)
        except NotFoundError:
            return None
        if not any(has_permission(ctx.permissions, p) for p in VIEW_INBOX):
            return None
        return _Viewer(
            user_id=user.id,
            organisation_id=org_id,
            membership_id=ctx.membership.id if ctx.membership is not None else None,
            can_answer=ctx.membership is not None and has_permission(ctx.permissions, Permission.QUERIES_ANSWER),
        )

    async def recheck(self, viewer: _Viewer) -> _Viewer:
        """The viewer as of now (one short read). Raises SocketClosed after closing 4401 when the user was
        deactivated, or 4403 when they left the org or lost inbox access."""
        async with self.session_factory() as session:
            user = await session.get(User, viewer.user_id)
            if user is None or not user.is_active:
                await self.close_and_raise(CLOSE_UNAUTHORIZED)
            current = await self._authorize(session, user, viewer.organisation_id)
        if current is None:
            await self.close_and_raise(CLOSE_FORBIDDEN)
        assert current is not None
        return current

    async def counts(self, viewer: _Viewer) -> dict[str, int]:
        async with self.session_factory() as session:
            return await InboxService(session, viewer.organisation_id, self.container.adapters).counts(
                viewer.membership_id
            )

    async def beat(self, viewer: _Viewer) -> None:
        """Best effort: a missed beat is retried on the next ping (the TTL outlasts two of them)."""
        if not viewer.can_answer or viewer.membership_id is None:
            return
        try:
            await self.container.adapters.presence.beat(viewer.organisation_id, viewer.membership_id)
        except Exception as exc:
            logger.warning("inbox_ws_presence_failed", membership_id=str(viewer.membership_id), error_type=type(exc).__name__)

    # --- loops ---------------------------------------------------------------------------------------

    async def serve(self, viewer: _Viewer, streams: list[tuple[Any, frozenset[str]]]) -> None:
        async with asyncio.TaskGroup() as tasks:
            self._tasks = tasks
            self._loops = [tasks.create_task(until_closed(self.receive_loop(viewer)))]
            for stream, types in streams:
                self._loops.append(tasks.create_task(self.relay(stream, "inbox_ws_relay_failed", _only(types))))
            # Whichever loop ends first (client gone, or a subscription failed) stops the others.
            for loop in self._loops:
                loop.add_done_callback(lambda _: self.stop())

    def stop(self) -> None:
        for task in self._loops:
            task.cancel()
        if self._watch is not None:
            self._watch.cancel()

    async def receive_loop(self, viewer: _Viewer) -> None:
        while True:
            frame = await self.receive_counted_frame()
            if frame is None:
                await self.send({"type": "error", "code": "invalid_frame"})
                continue
            kind = frame["type"]
            if kind == "ping":
                viewer = await self.recheck(viewer)  # a removed member stops here, before beating
                await self.beat(viewer)
                await self.send({"type": "pong"})
            elif kind == "watch":
                viewer = await self.recheck(viewer)
                await self.on_watch(viewer, frame)
            elif kind == "unwatch":
                await self.unwatch()
            else:
                await self.send({"type": "error", "code": "invalid_frame"})

    async def on_watch(self, viewer: _Viewer, frame: dict[str, Any]) -> None:
        conversation_id = _uuid(frame.get("conversation_id"))
        if conversation_id is None:
            await self.send({"type": "error", "code": "invalid_frame"})
            return
        async with self.session_factory() as session:
            conversations = TenantScopedRepository(session, Conversation, viewer.organisation_id)
            found = await conversations.get(conversation_id) is not None
        if not found:
            await self.send({"type": "error", "code": "not_found"})
            return
        await self.unwatch()
        assert self._tasks is not None
        subscribed: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        watch = self._tasks.create_task(self._relay_conversation(conversation_id, subscribed))
        # A watch that ends by itself (its subscription failed) ends the socket; a replaced one does not.
        watch.add_done_callback(lambda task: None if task.cancelled() else self.stop())
        self._watch = watch
        await subscribed
        await self.send({"type": "watching", "conversation_id": str(conversation_id)})

    async def unwatch(self) -> None:
        """Ends the current watch's subscription (and waits for it, so nothing of it is relayed after)."""
        watch, self._watch = self._watch, None
        if watch is not None:
            watch.cancel()
            await asyncio.wait([watch])  # never swallows a cancellation of this task

    async def _relay_conversation(self, conversation_id: UUID, subscribed: asyncio.Future[None]) -> None:
        def frame(event: dict[str, Any]) -> dict[str, Any] | None:
            if event.get("type") not in CONVERSATION_EVENTS:
                return None
            return {**event, "conversation_id": str(conversation_id)}

        try:
            async with self.container.adapters.pubsub.subscribe(conversation_channel(conversation_id)) as stream:
                subscribed.set_result(None)
                await self.relay(stream, "inbox_ws_relay_failed", frame, conversation_id=str(conversation_id))
        except Exception as exc:  # the subscription could not be made
            logger.warning("inbox_ws_watch_failed", conversation_id=str(conversation_id), error_type=type(exc).__name__)
            await self.close(CLOSE_SERVER_ERROR)
        finally:
            if not subscribed.done():
                subscribed.cancel()


def _uuid(value: Any) -> UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


@router.websocket("/ws")
async def inbox_socket(websocket: WebSocket) -> None:
    with track_connection("inbox"):
        await _inbox_socket(websocket)


async def _inbox_socket(websocket: WebSocket) -> None:
    container: Container = websocket.app.state.container
    # Accepted before any close, so the browser sees the close code instead of a failed handshake.
    await websocket.accept()
    connection = _Connection(websocket, container)
    if not await admit(connection, container.settings, container.adapters.rate_limiter, "ip:inbox-ws", CONNECTS_PER_IP):
        return
    try:
        with job_context(None, "inbox-ws"):  # the org is bound once the membership is resolved
            viewer = await connection.authenticate()
    except SocketClosed:
        return
    if viewer is None:
        return
    key = (viewer.organisation_id, viewer.membership_id) if viewer.can_answer and viewer.membership_id else None
    if key is not None:
        _presence_sockets[key] += 1
    try:
        with job_context(viewer.organisation_id, f"inbox-ws:{viewer.membership_id or viewer.user_id}"):
            await _serve(connection, viewer)
    finally:
        if key is not None:
            _presence_sockets[key] -= 1
            if _presence_sockets[key] <= 0:
                del _presence_sockets[key]
                await _clear_presence(container, *key)


async def _clear_presence(container: Container, organisation_id: UUID, membership_id: UUID) -> None:
    try:
        await container.adapters.presence.clear(organisation_id, membership_id)
    except Exception as exc:  # the TTL expires it anyway
        logger.warning("inbox_ws_presence_failed", membership_id=str(membership_id), error_type=type(exc).__name__)


async def _serve(connection: _Connection, viewer: _Viewer) -> None:
    pubsub = connection.container.adapters.pubsub
    try:
        async with AsyncExitStack() as stack:
            streams = [(await stack.enter_async_context(pubsub.subscribe(inbox_channel(viewer.organisation_id))), INBOX_EVENTS)]
            if viewer.membership_id is not None:
                stream = await stack.enter_async_context(pubsub.subscribe(user_channel(viewer.membership_id)))
                streams.append((stream, USER_EVENTS))
            await connection.beat(viewer)
            # Read after subscribing: a change before this is in the counts, any later one is relayed.
            counts = await connection.counts(viewer)
            membership_id = str(viewer.membership_id) if viewer.membership_id is not None else None
            await connection.send({"type": "ready", "membership_id": membership_id, "counts": counts})
            await connection.serve(viewer, streams)
    except SocketClosed:  # closed (by the client, or a relay) while sending `ready`
        pass
    except Exception as exc:  # a subscription could not be made (e.g. Redis is down), or a loop failed
        logger.warning("inbox_ws_failed", organisation_id=str(viewer.organisation_id), error_type=type(exc).__name__)
        await connection.close(CLOSE_SERVER_ERROR)
