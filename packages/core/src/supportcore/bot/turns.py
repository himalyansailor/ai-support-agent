"""The turn runner: runs bot turns in the background, one at a time per conversation, persists each
turn's BotTurn row and reply message, and publishes the conversation's live events.

Events on `conversation_channel(id)`:

    {"type": "message", "message": {id, role, text, citations, client_id, created_at}}
    {"type": "bot_token", "delta": str}        a piece of the reply as it streams
    {"type": "bot_reset"}                      discard the text streamed so far
    {"type": "bot_done", "outcome": str, "offer_human": bool}
    {"type": "handoff", "status": "open", "agent": {"name"} | null}
    {"type": "status", "status": str}          (close_idle_conversations; an agent's resolve, close,
                                               reopen or hand back, see ConversationService.transition)

The handoff (with its `agent_joined` / `no_agent_available` system message) is published by
AssignmentService.route_to_team after its commit, for both the graph's handoff node and request_human.

A turn task takes the conversation's turn lease (another running turn picks the new message up),
answers every unanswered customer message in one turn (joined with blank lines; the BotTurn's
`message_id` is the newest of them, see ConversationService.unanswered_customer_messages),
releases the lease and loops while unanswered messages remain. Nothing runs unless the
conversation is in `bot` status. No session is held across the graph run (it calls the providers).
Logs carry ids only: never message text.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar
from uuid import UUID

from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.analytics.events import record
from supportcore.analytics.pricing import cost_micros, price_for
from supportcore.billing.entitlements import EntitlementService
from supportcore.bot.graph import TurnResult, compile_bot_graph, hand_back, run_turn
from supportcore.bot.runtime import BotRuntime
from supportcore.bot.state import TurnInput, TurnSink
from supportcore.conversations.service import (
    TURN_LEASE_SECONDS,
    ConversationService,
    InvalidConversationStateError,
    ensure_transition,
)
from supportcore.db.models import BotTurn, Conversation
from supportcore.db.models.conversation import TURN_OUTCOMES
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.db.uow import UnitOfWork
from supportcore.errors import NotFoundError
from supportcore.inbox.events import message_payload
from supportcore.observability.context import job_context
from supportcore.observability.logging import get_logger
from supportcore.pubsub.ports import conversation_channel

__all__ = ["CUSTOMER_REQUEST", "PLAN_LIMIT", "PostedMessage", "TurnRunner", "message_payload", "store_bot_turn"]

logger = get_logger(__name__)

# The risk decision recorded when the customer asks for a person with the widget's button.
CUSTOMER_REQUEST = {
    "action": "escalate",
    "rule_key": "customer_request",
    "rule_name": "Customer request",
    "reason": "The customer asked to talk to a person",
    "source": "customer",
}

# The decision recorded when the organisation's plan has no messages left: the bot pauses and the team answers.
PLAN_LIMIT = {
    "action": "escalate",
    "rule_key": "plan_limit",
    "rule_name": "Plan limit",
    "reason": "The plan's message limit has been reached",
    "source": "plan_limit",
}

GraphFactory = Callable[[], Awaitable[CompiledStateGraph]]

LEASE_SECONDS = TURN_LEASE_SECONDS  # 120
LEASE_RENEW_SECONDS = 40  # a running turn extends its lease to now + LEASE_SECONDS this often
CANCEL_NOTICE_TIMEOUT_SECONDS = 2.0
ANSWER_NOW_RETRY_SECONDS = 0.5  # answer_now: how often it retries a lease another turn holds...
ANSWER_NOW_WAIT_SECONDS = 10  # ...and for how long
EXTRA_SINK_TIMEOUT_SECONDS = 5.0  # one call of answer_now's extra sink; a slower one is dropped for the turn

_T = TypeVar("_T")


@dataclass(frozen=True)
class PostedMessage:
    message: dict[str, Any]  # message_payload()
    created: bool  # False: a resend with the same client_id, already stored
    status: str  # the conversation's status


@dataclass(frozen=True)
class _Pending:
    widget_id: UUID | None
    language: str
    channel: str
    message_ids: list[UUID]
    texts: list[str]


def _recorded_outcome(result: TurnResult) -> str:
    return result.outcome if result.outcome in TURN_OUTCOMES else "error"


async def store_bot_turn(
    session: AsyncSession,
    organisation_id: UUID,
    conversation: Conversation,
    *,
    message_id: UUID | None,
    reply_message_id: UUID | None,
    outcome: str,
    no_answer: bool,
    decision: Mapping[str, Any] | None,
    retrieval: Mapping[str, Any] | None,
    tool_calls: Sequence[Mapping[str, Any]],
    model: Mapping[str, Any],
    latency_ms: int | None,
    trace_id: str | None,
) -> BotTurn:
    """Adds the BotTurn row (with the model's price looked up into `model.cost_micros`) and the `bot_turn`
    analytics event; the caller commits. Shared by the chat/voice turn runner and the Realtime voice session."""
    model = dict(model)
    if model.get("provider") and model.get("model"):
        price = await price_for(session, model["provider"], model["model"])
        model["cost_micros"] = cost_micros(price, model.get("input_tokens"), model.get("output_tokens"))
    row = BotTurn(
        conversation_id=conversation.id,
        message_id=message_id,
        reply_message_id=reply_message_id,
        outcome=outcome,
        no_answer=no_answer,
        risk=dict(decision or {}),
        retrieval=dict(retrieval or {}),
        tool_calls=[dict(c) for c in tool_calls],
        model=model,
        latency_ms=latency_ms,
        trace_id=trace_id,
    )
    await TenantScopedRepository(session, BotTurn, organisation_id).add(row)
    record(
        session,
        organisation_id,
        conversation.id,
        "bot_turn",
        channel=conversation.channel,
        data={
            "outcome": outcome,
            "no_answer": no_answer,
            "rule_key": (decision or {}).get("rule_key"),
            "rule_name": (decision or {}).get("rule_name"),
            "input_tokens": model.get("input_tokens"),
            "output_tokens": model.get("output_tokens"),
            "provider": model.get("provider"),
            "model": model.get("model"),
            "cost_micros": model.get("cost_micros"),  # None until the platform sets the model's price
            "kb_ids": list((retrieval or {}).get("kb_ids") or []),
        },
    )
    return row


class _Lease:
    """The turn lease's current value: renewals replace it, the release uses the latest one."""

    def __init__(self, value: datetime) -> None:
        self.value = value


class _PublishingSink:
    """Relays the streamed reply to the conversation's channel."""

    def __init__(self, publish: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._publish = publish

    async def token(self, delta: str) -> None:
        await self._publish({"type": "bot_token", "delta": delta})

    async def reset(self) -> None:
        await self._publish({"type": "bot_reset"})


class _ForwardingSink:
    """Streams to the publishing sink and to a caller's extra sink. The extra sink is best effort: one that
    raises or is slower than EXTRA_SINK_TIMEOUT_SECONDS is dropped for the rest of the turn, so the turn
    (and its persistence) never depends on it."""

    def __init__(self, primary: TurnSink, extra: TurnSink, conversation_id: UUID) -> None:
        self._primary = primary
        self._extra: TurnSink | None = extra
        self._conversation_id = conversation_id

    async def token(self, delta: str) -> None:
        await self._primary.token(delta)
        if self._extra is not None:
            await self._forward(self._extra.token(delta))

    async def reset(self) -> None:
        await self._primary.reset()
        if self._extra is not None:
            await self._forward(self._extra.reset())

    async def _forward(self, call: Awaitable[None]) -> None:
        try:
            await asyncio.wait_for(call, EXTRA_SINK_TIMEOUT_SECONDS)
        except Exception as exc:  # includes TimeoutError
            self._extra = None
            logger.warning(
                "turn_extra_sink_dropped",
                conversation_id=str(self._conversation_id),
                error_type=type(exc).__name__,
            )


class TurnRunner:
    def __init__(
        self,
        runtime: BotRuntime,
        graph: CompiledStateGraph | None = None,
        *,
        graph_factory: GraphFactory | None = None,
        lease_seconds: float = LEASE_SECONDS,
        renew_seconds: float = LEASE_RENEW_SECONDS,
    ) -> None:
        """`graph` is compiled on the first turn when not given (the checkpointer opens then)."""
        self._rt = runtime
        self._lease_seconds = lease_seconds
        self._renew_seconds = renew_seconds
        self._graph = graph
        self._graph_factory = graph_factory or (lambda: compile_bot_graph(runtime))
        self._graph_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    # --- background turns ----------------------------------------------------------------------------

    def submit(self, organisation_id: UUID, conversation_id: UUID) -> None:
        """Starts a tracked task that answers the conversation's unanswered messages."""
        if self._closed:
            logger.info("bot_turn_not_started_closed", conversation_id=str(conversation_id))
            return
        task = asyncio.get_running_loop().create_task(self._run(organisation_id, conversation_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Waits until no turn task is running (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def aclose(self, timeout: float = 10) -> None:
        """Cancels the outstanding turns and waits up to `timeout` seconds for them to release
        their leases. Later submits are ignored."""
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)

    async def graph(self) -> CompiledStateGraph:
        """The compiled bot graph (compiled on first use)."""
        return await self._graph_ready()

    async def hand_back(self, conversation_id: UUID) -> bool:
        """Resumes the conversation's graph if it is waiting for a person (ConversationService's bot_hand_back)."""
        return await hand_back(await self._graph_ready(), conversation_id)

    async def _graph_ready(self) -> CompiledStateGraph:
        if self._graph is None:
            async with self._graph_lock:
                if self._graph is None:
                    self._graph = await self._graph_factory()
        return self._graph

    async def _run(self, organisation_id: UUID, conversation_id: UUID) -> None:
        with job_context(organisation_id, f"turn:{conversation_id}"):
            last_started: UUID | None = None
            in_turn = False  # a reply may be streaming to the widget
            try:
                while True:
                    acquired = await self._acquire(organisation_id, conversation_id)
                    if acquired is None:
                        return  # a running turn will pick the new message up
                    lease = _Lease(acquired)
                    try:
                        pending = await self._pending(organisation_id, conversation_id)
                        if pending is not None and pending.message_ids[-1] == last_started:
                            # The last turn stored no reply for it: do not loop on the same message.
                            logger.warning("bot_turn_not_answered", conversation_id=str(conversation_id))
                            return
                        if pending is not None and pending.channel != "voice":
                            # Voice is limited when a call connects, never mid-call (its utterances are not metered).
                            if await self._paused_for_plan_limit(organisation_id, conversation_id):
                                return
                        if pending is not None:
                            last_started = pending.message_ids[-1]
                            in_turn = True
                            await self._with_renewal(
                                organisation_id, conversation_id, lease, self._turn(organisation_id, conversation_id, pending)
                            )
                            in_turn = False
                    finally:
                        await self._release(organisation_id, conversation_id, lease.value)
                    # Re-checked after the release: a message that arrived meanwhile found the lease taken.
                    if await self._pending(organisation_id, conversation_id) is None:
                        return
            except asyncio.CancelledError:
                if in_turn:  # shutdown mid-turn: let the widget drop its live bubble
                    with suppress(Exception, asyncio.CancelledError):
                        await asyncio.wait_for(
                            asyncio.shield(self._publish_failed(conversation_id)), CANCEL_NOTICE_TIMEOUT_SECONDS
                        )
                raise
            except Exception as exc:
                logger.warning(
                    "bot_turn_failed",
                    organisation_id=str(organisation_id),
                    conversation_id=str(conversation_id),
                    error_type=type(exc).__name__,
                )
                await self._publish_failed(conversation_id)

    async def _paused_for_plan_limit(self, organisation_id: UUID, conversation_id: UUID) -> bool:
        """True when the plan's messages are exceeded: the conversation (if the bot still has it) goes to the team
        and no turn runs. Checked after the triggering message was counted, so the message equal to the limit is
        still answered."""
        async with UnitOfWork(self._rt.session_factory) as uow:
            usage = await EntitlementService(uow.session, organisation_id, self._rt.adapters).meter("messages")
            if not usage.exceeded:
                return False
            conversation = await self._conversation(uow, organisation_id, conversation_id, for_update=True)
            await uow.session.refresh(conversation)
            if conversation.status == "bot":
                await self._rt.assignment(uow.session, organisation_id).route_to_team(conversation, PLAN_LIMIT)
            await uow.commit()
        logger.info(
            "bot_paused_plan_limit",
            organisation_id=str(organisation_id),
            conversation_id=str(conversation_id),
            used=usage.used,
            limit=usage.limit,
        )
        return True

    async def _publish_failed(self, conversation_id: UUID) -> None:
        await self._publish(conversation_id, {"type": "bot_reset"})
        await self._publish(conversation_id, {"type": "bot_done", "outcome": "error", "offer_human": False})

    async def answer_now(
        self, organisation_id: UUID, conversation_id: UUID, sink: TurnSink | None = None
    ) -> TurnResult | None:
        """Answers the conversation's unanswered messages now, in one turn, and returns its result (the voice
        engine calls this per caller utterance). Takes the turn lease, retrying every ANSWER_NOW_RETRY_SECONDS
        for up to ANSWER_NOW_WAIT_SECONDS while another turn holds it (TimeoutError after that). The reply
        streams to the conversation's channel and to `sink`; a `sink` that raises or stalls is dropped, the
        turn is persisted regardless. None when the conversation is not in `bot` status, has nothing pending, or
        the runner is closed. The call is tracked like a background turn: `aclose()` cancels it (and publishes
        the failure notice); a message that arrives meanwhile is answered by a background turn afterwards (not on voice: it
        waits for the next call). Registers the current task for `aclose()`; voice callers run it inside their own
        shielded task."""
        if self._closed:
            return None
        task = asyncio.current_task()
        if task is not None:  # aclose() cancels and waits for a running voice turn like a background one
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        try:
            return await self._answer_now(organisation_id, conversation_id, sink)
        finally:
            if task is not None:
                self._tasks.discard(task)

    async def _answer_now(
        self, organisation_id: UUID, conversation_id: UUID, sink: TurnSink | None
    ) -> TurnResult | None:
        with job_context(organisation_id, f"turn:{conversation_id}"):
            loop = asyncio.get_running_loop()
            deadline = loop.time() + ANSWER_NOW_WAIT_SECONDS
            while True:
                acquired = await self._acquire(organisation_id, conversation_id)
                if acquired is not None:
                    break
                if loop.time() + ANSWER_NOW_RETRY_SECONDS > deadline:
                    raise TimeoutError("The conversation's turn lease stayed busy")
                await asyncio.sleep(ANSWER_NOW_RETRY_SECONDS)
            lease = _Lease(acquired)
            result: TurnResult | None = None
            voice = False
            try:
                pending = await self._pending(organisation_id, conversation_id)
                if pending is None:
                    return None
                voice = pending.channel == "voice"
                try:
                    result = await self._with_renewal(
                        organisation_id,
                        conversation_id,
                        lease,
                        self._turn(organisation_id, conversation_id, pending, sink),
                    )
                except asyncio.CancelledError:
                    with suppress(Exception, asyncio.CancelledError):
                        await asyncio.wait_for(
                            asyncio.shield(self._publish_failed(conversation_id)), CANCEL_NOTICE_TIMEOUT_SECONDS
                        )
                    raise
                except Exception:
                    await self._publish_failed(conversation_id)
                    raise
                return result
            finally:
                await self._release(organisation_id, conversation_id, lease.value)
                # Like `_run`: a message that arrived while this turn held the lease found it taken. Not on voice:
                # a caller's utterance stays pending for the voice engine's next answer_now, which batches it.
                if result is not None and not voice and not self._closed:
                    with suppress(Exception):
                        if await self._pending(organisation_id, conversation_id) is not None:
                            self.submit(organisation_id, conversation_id)

    async def _with_renewal(
        self, organisation_id: UUID, conversation_id: UUID, lease: _Lease, work: Awaitable[_T]
    ) -> _T:
        """Runs `work` while renewing the lease every renew_seconds, so a long turn keeps it."""

        async def renew() -> None:
            while True:
                await asyncio.sleep(self._renew_seconds)
                async with UnitOfWork(self._rt.session_factory) as uow:
                    renewed = await self._service(uow, organisation_id).renew_turn_lease(
                        conversation_id, lease.value, self._lease_seconds
                    )
                    await uow.commit()
                if renewed is None:
                    logger.warning("bot_turn_lease_lost", conversation_id=str(conversation_id))
                    return
                lease.value = renewed

        renewer = asyncio.create_task(renew())
        try:
            return await work
        finally:
            renewer.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await renewer

    async def _acquire(self, organisation_id: UUID, conversation_id: UUID) -> datetime | None:
        async with UnitOfWork(self._rt.session_factory) as uow:
            lease = await self._service(uow, organisation_id).acquire_turn_lease(conversation_id, self._lease_seconds)
            await uow.commit()
            return lease

    async def _release(self, organisation_id: UUID, conversation_id: UUID, lease: datetime) -> None:
        async with UnitOfWork(self._rt.session_factory) as uow:
            await self._service(uow, organisation_id).release_turn_lease(conversation_id, lease)
            await uow.commit()

    async def _pending(self, organisation_id: UUID, conversation_id: UUID) -> _Pending | None:
        """The unanswered messages, or None when there are none or the bot is not answering."""
        async with UnitOfWork(self._rt.session_factory) as uow:
            conversation = await TenantScopedRepository(uow.session, Conversation, organisation_id).get(
                conversation_id
            )
            if conversation is None or conversation.status != "bot":
                return None
            messages = await self._service(uow, organisation_id).unanswered_customer_messages(conversation_id)
            if not messages:
                return None
            return _Pending(
                widget_id=conversation.widget_id,
                language=conversation.language,
                channel=conversation.channel,
                message_ids=[m.id for m in messages],
                texts=[m.text for m in messages],
            )

    async def _turn(
        self, organisation_id: UUID, conversation_id: UUID, pending: _Pending, extra_sink: TurnSink | None = None
    ) -> TurnResult:
        graph = await self._graph_ready()
        turn = TurnInput(
            organisation_id=organisation_id,
            conversation_id=conversation_id,
            widget_id=pending.widget_id,
            message_id=pending.message_ids[-1],
            text="\n\n".join(pending.texts),
            language=pending.language,
            channel=pending.channel,
        )
        sink: TurnSink = _PublishingSink(lambda e: self._publish(conversation_id, e))
        if extra_sink is not None:
            sink = _ForwardingSink(sink, extra_sink, conversation_id)
        result = await run_turn(graph, turn, sink)
        published, superseded = await self._persist(organisation_id, conversation_id, pending, result)
        if superseded:  # a person took over mid-turn: the widget drops what was streamed
            await self._publish(conversation_id, {"type": "bot_reset"})
        for message in published:
            await self._publish(conversation_id, {"type": "message", "message": message})
        await self._publish(
            conversation_id,
            {
                "type": "bot_done",
                "outcome": _recorded_outcome(result),
                "offer_human": result.offer_human and not superseded,
            },
        )
        return result

    async def _persist(
        self,
        organisation_id: UUID,
        conversation_id: UUID,
        pending: _Pending,
        result: TurnResult,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Stores the reply and the BotTurn. Returns the messages to publish (the reply; the handoff
        node's system message went out with its handoff event) and whether the turn was superseded:
        the conversation left `bot` while it ran (claimed, assigned, the customer asked for a person)
        other than by the turn's own handoff. A superseded turn's reply is not stored; its BotTurn is
        kept (the trace) without a reply."""
        async with UnitOfWork(self._rt.session_factory) as uow:
            # The row lock orders this against a concurrent claim: either the claim sees the reply, or
            # this sees the claim.
            conversation = await TenantScopedRepository(uow.session, Conversation, organisation_id).get(
                conversation_id, for_update=True
            )
            if conversation is None:  # deleted meanwhile
                return [], False
            await uow.session.refresh(conversation)
            # Not `bot` and not moved by this turn's own handoff (a handoff that found it already with a
            # person did not route): someone took over mid-turn.
            superseded = conversation.status != "bot" and not result.routed
            service = self._service(uow, organisation_id)
            published: list[dict[str, Any]] = []
            reply = None
            if result.reply is not None and not superseded:
                reply = await service.add_message(
                    conversation, "bot", result.reply["text"], result.reply.get("citations") or ()
                )
                published.append(message_payload(reply))
                # The preview and unread flag move on (after a handoff too: its upsert went out before this).
                self._rt.publisher().conversation_changed(uow.session, organisation_id, conversation_id)
            await store_bot_turn(
                uow.session,
                organisation_id,
                conversation,
                message_id=pending.message_ids[-1],  # the turn answered every message up to it
                reply_message_id=reply.id if reply is not None else None,
                outcome=_recorded_outcome(result),
                no_answer=result.no_answer,
                decision=result.decision,
                retrieval=result.retrieval,
                tool_calls=result.tool_calls,
                model=result.model,
                latency_ms=result.latency_ms,
                trace_id=result.trace_id,
            )
            await uow.commit()
        if superseded:
            logger.info("bot_turn_superseded", conversation_id=str(conversation_id), status=conversation.status)
        return published, superseded

    # --- customer actions (the widget WebSocket) -----------------------------------------------------

    async def post_customer_message(
        self, organisation_id: UUID, conversation_id: UUID, text: str, client_id: str | None
    ) -> PostedMessage:
        """Stores a customer message, publishes it and, while the bot is answering, starts a turn. A message
        to a `pending` conversation (waiting on the customer) moves it back to `open`; the inbox gets an
        upsert after the commit, and with the team (`open`, `pending`) no turn starts. Raises
        InvalidMessageError (text not 1-4000 characters after stripping), NotFoundError, and
        InvalidConversationStateError once the conversation has ended."""
        async with UnitOfWork(self._rt.session_factory) as uow:
            # The row lock orders concurrent posts: each message's timestamp (the database clock) is
            # taken after the previous one committed, so no message lands behind a turn's cutoff.
            conversation = await self._conversation(uow, organisation_id, conversation_id, for_update=True)
            if conversation.status not in ("bot", "open", "pending"):
                raise InvalidConversationStateError("The conversation has ended")
            message, created = await self._service(uow, organisation_id).add_customer_message(
                conversation, text, client_id
            )
            if created and conversation.status == "pending":
                ensure_transition("pending", "open", "customer_message")
                conversation.status = "open"
                await uow.session.flush()
            if created:  # with the bot or the team: the Bot / Mine / All views show the new message
                self._rt.publisher().conversation_changed(uow.session, organisation_id, conversation_id)
            posted = PostedMessage(message_payload(message), created, conversation.status)
            await uow.commit()
        if created:
            await self._publish(conversation_id, {"type": "message", "message": posted.message})
            if posted.status == "bot":
                self.submit(organisation_id, conversation_id)
        return posted

    async def request_human(self, organisation_id: UUID, conversation_id: UUID) -> str:
        """The customer asked for a person: routes to the team directly (not through the graph);
        route_to_team publishes the system message and the handoff after the commit. A no-op unless
        the bot is answering. Returns the conversation's status."""
        async with UnitOfWork(self._rt.session_factory) as uow:
            conversation = await self._conversation(uow, organisation_id, conversation_id)
            if conversation.status != "bot":  # already with a person, or ended: no lock taken
                return conversation.status
            conversation = await self._conversation(uow, organisation_id, conversation_id, for_update=True)
            await uow.session.refresh(conversation)
            if conversation.status != "bot":
                return conversation.status
            await self._rt.assignment(uow.session, organisation_id).route_to_team(conversation, CUSTOMER_REQUEST)
            status = conversation.status
            await uow.commit()
        return status

    # --- internals -----------------------------------------------------------------------------------

    def _service(self, uow: UnitOfWork, organisation_id: UUID) -> ConversationService:
        return ConversationService(
            uow.session, organisation_id, self._rt.events, usage_counter=self._rt.adapters.usage_counter
        )

    @staticmethod
    async def _conversation(
        uow: UnitOfWork, organisation_id: UUID, conversation_id: UUID, *, for_update: bool = False
    ) -> Conversation:
        conversation = await TenantScopedRepository(uow.session, Conversation, organisation_id).get(
            conversation_id, for_update=for_update
        )
        if conversation is None:
            raise NotFoundError("Conversation not found")
        return conversation

    async def _publish(self, conversation_id: UUID, event: dict[str, Any]) -> None:
        """Live events are best effort: the turn is stored whether or not they are delivered."""
        try:
            await self._rt.adapters.pubsub.publish(conversation_channel(conversation_id), event)
        except Exception as exc:
            logger.warning(
                "conversation_event_publish_failed",
                conversation_id=str(conversation_id),
                event_type=event.get("type"),
                error_type=type(exc).__name__,
            )
