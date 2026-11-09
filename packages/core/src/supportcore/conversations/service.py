"""Conversations between a customer and the bot (or a person), their messages, the agent actions on them
(reply, status transitions, hand back, priority) and the bot's turn lease.

Message text (customer or agent) is never logged here or in the events: only ids, roles and lengths.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import DateTime, func, or_, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.analytics.events import record
from supportcore.billing.counter_ports import UsageCounter
from supportcore.billing.usage import record_usage
from supportcore.conversations.events import (
    ConversationEscalated,
    ConversationEvent,
    ConversationResolved,
    ConversationStarted,
    CsatSubmitted,
)
from supportcore.db.models import BotTurn, Call, Chunk, Conversation, Customer, Document, Membership, Message, User, Widget
from supportcore.db.models.conversation import CONVERSATION_CHANNELS, PRIORITIES
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import DomainError, NotFoundError
from supportcore.events import EventBus
from supportcore.inbox import sla
from supportcore.inbox.events import agent_event, message_payload
from supportcore.observability.logging import get_logger
from supportcore.voice.errors import VoiceNotAvailableError
from supportcore.widget.strings import strings_for

if TYPE_CHECKING:
    from supportcore.inbox.publisher import InboxPublisher

__all__ = [
    "ConversationAdminRow",
    "ConversationTrace",
    "ConversationService",
    "InvalidContactEmailError",
    "InvalidConversationStateError",
    "InvalidCsatError",
    "InvalidMessageError",
    "InvalidPriorityError",
    "MAX_AGENT_MESSAGE_LENGTH",
    "MAX_CUSTOMER_MESSAGE_LENGTH",
    "TRANSITIONS",
    "ensure_transition",
]

logger = get_logger(__name__)


def _str(value: UUID | None) -> str | None:
    return str(value) if value is not None else None

MAX_CUSTOMER_MESSAGE_LENGTH = 4000
MAX_AGENT_MESSAGE_LENGTH = 10000
PREVIEW_LENGTH = 120
MAX_CSAT_COMMENT_LENGTH = 1000
MAX_EMAIL_LENGTH = 320
TURN_LEASE_SECONDS = 120
_METERED_CHANNELS = ("chat", "email")  # a customer message in these counts against the plan; voice is metered by minutes
_ROLES = ("customer", "bot", "agent", "system")
# Priority on escalation: only a business risk topic (rule_key `topic:<id>`) or a write-intent tool
# call (`write_action`) makes the conversation `high`. Every other rule (asks_for_human, frustration,
# low_confidence, repeated_failure, customer_request, bot_unavailable, ...) keeps the current
# priority. A higher priority (`urgent`) is never lowered.
_HIGH_PRIORITY_RULE_KEY = "write_action"
_HIGH_PRIORITY_RULE_PREFIX = "topic:"
_PRIORITY_RANK = {priority: rank for rank, priority in enumerate(PRIORITIES)}
_ESCALATION_FIELDS = ("action", "rule_key", "rule_name", "reason", "source")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# The status transitions, (from, to) -> the event that makes it. Anything else is InvalidConversationStateError;
# `closed` is final. Entering the team is AssignmentService (route_to_team, claim, assign), a reply is
# add_agent_message, a customer message is TurnRunner.post_customer_message; `transition` does the rest.
TRANSITIONS: dict[tuple[str, str], str] = {
    ("bot", "open"): "enter_team",
    ("open", "pending"): "agent_reply",
    ("pending", "open"): "customer_message",
    ("open", "resolved"): "resolve",
    ("pending", "resolved"): "resolve",
    ("open", "bot"): "hand_back",
    ("pending", "bot"): "hand_back",
    ("resolved", "open"): "reopen",
    ("bot", "closed"): "close",
    ("open", "closed"): "close",
    ("pending", "closed"): "close",
    ("resolved", "closed"): "close",
}
# The events an agent asks for by the status they lead to (POST .../status, POST .../hand-back).
_AGENT_EVENTS = {"resolved": "resolve", "closed": "close", "open": "reopen", "bot": "hand_back"}
_REPLY_STATUSES = ("open", "pending")
# The analytics event of the agent transitions other than resolve.
_TRANSITION_EVENT_KINDS = {"close": "closed", "reopen": "reopened", "hand_back": "handed_back"}

# Resumes the conversation's bot graph if it is interrupted waiting for a person; True when it was.
BotHandBack = Callable[[UUID], Awaitable[bool]]


class InvalidConversationStateError(DomainError):
    def __init__(self, message: str = "That is not possible in the conversation's current state") -> None:
        super().__init__(message)


class InvalidMessageError(DomainError):
    def __init__(self, max_length: int = MAX_CUSTOMER_MESSAGE_LENGTH) -> None:
        super().__init__(f"A message must be 1-{max_length} characters")


class InvalidPriorityError(DomainError):
    def __init__(self) -> None:
        super().__init__(f"The priority must be one of {', '.join(PRIORITIES)}")


def ensure_transition(from_status: str, to_status: str, event: str) -> None:
    """InvalidConversationStateError unless `event` moves a conversation from `from_status` to `to_status`."""
    if TRANSITIONS.get((from_status, to_status)) != event:
        raise InvalidConversationStateError()


class InvalidCsatError(DomainError):
    def __init__(self, message: str = "The score must be 1-5 and the comment at most 1000 characters") -> None:
        super().__init__(message)


class InvalidContactEmailError(DomainError):
    def __init__(self) -> None:
        super().__init__("Enter a valid email address")


@dataclass(frozen=True)
class ConversationAdminRow:
    conversation: Conversation
    customer: Customer
    last_message_preview: str | None


@dataclass(frozen=True)
class ConversationTrace:
    """A conversation for the dashboard: messages, bot turns and, per turn id, its retrieved chunks
    as {chunk_id, document_name, ordinal, score, confidence, rerank_score}."""

    row: ConversationAdminRow
    messages: list[Message]
    turns: list[BotTurn]
    chunks: dict[UUID, list[dict[str, Any]]]


class ConversationService:
    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        events: EventBus,
        *,
        publisher: InboxPublisher | None = None,
        bot_hand_back: BotHandBack | None = None,
        usage_counter: UsageCounter | None = None,
    ) -> None:
        """`publisher` is needed by the agent actions (they publish after the commit), `bot_hand_back` by hand
        back (TurnRunner.hand_back). `usage_counter`: the billing counter customer messages are metered against;
        None leaves the counter untouched (the ledger row is still written by whoever meters)."""
        self._usage_counter = usage_counter
        self._session = session
        self._org_id = organisation_id
        self._events = events
        self._publisher = publisher
        self._bot_hand_back = bot_hand_back
        self._conversations = TenantScopedRepository(session, Conversation, organisation_id)
        self._messages = TenantScopedRepository(session, Message, organisation_id)
        self._widgets = TenantScopedRepository(session, Widget, organisation_id)
        self._turns = TenantScopedRepository(session, BotTurn, organisation_id)

    # --- conversations -----------------------------------------------------------------------------

    async def create(
        self,
        customer_id: UUID,
        widget_id: UUID | None,
        language: str,
        is_test: bool,
        channel: str = "chat",
        subject: str | None = None,
    ) -> Conversation:
        if channel not in CONVERSATION_CHANNELS:
            raise ValueError(f"Unknown conversation channel {channel!r}")
        conversation = Conversation(
            customer_id=customer_id,
            widget_id=widget_id,
            channel=channel,
            subject=subject,
            status="bot",
            language=language,
            is_test=is_test,
            last_message_at=datetime.now(UTC),
        )
        await self._conversations.add(conversation)
        await self._session.flush()
        self._record(conversation, "conversation_created", data={"widget_id": _str(widget_id), "is_test": is_test})
        if self._publisher is not None:  # the widget route has one; fixtures and seed scripts need none
            self._publisher.conversation_changed(self._session, self._org_id, conversation.id)
        await self._emit(
            ConversationStarted(
                organisation_id=self._org_id,
                conversation_id=conversation.id,
                customer_id=customer_id,
                widget_id=widget_id,
                is_test=is_test,
            ),
            "conversation_started",
            widget_id=str(widget_id) if widget_id else None,
            is_test=is_test,
        )
        return conversation

    async def list_for_customer(self, customer_id: UUID, limit: int = 20) -> list[Conversation]:
        return await self._conversations.list(
            Conversation.customer_id == customer_id,
            order_by=(Conversation.last_message_at.desc(), Conversation.id),
            limit=limit,
        )

    async def get_for_customer(self, conversation_id: UUID, customer_id: UUID) -> Conversation:
        """Another customer's (or organisation's) conversation looks like a missing one."""
        stmt = self._conversations.select().where(
            Conversation.id == conversation_id, Conversation.customer_id == customer_id
        )
        conversation = (await self._session.execute(stmt)).scalar_one_or_none()
        if conversation is None:
            raise NotFoundError("Conversation not found")
        return conversation

    async def locked(self, conversation_id: UUID) -> Conversation:
        """The conversation, its row locked (FOR UPDATE) and refreshed as of the lock; NotFoundError otherwise."""
        conversation = await self._conversations.get(conversation_id, for_update=True)
        if conversation is None:
            raise NotFoundError("Conversation not found")
        await self._session.refresh(conversation)
        return conversation

    async def end(self, conversation: Conversation, *, idle: bool = False) -> Conversation:
        """The customer ends the chat (or, with `idle`, it idled out). Already resolved or closed conversations are
        left alone. The caller holds the row lock (see locked()); after the commit the widget gets
        `{"type": "status", "status": "resolved"}` and the inbox an upsert. The `resolved` analytics event says who
        resolved it (RESOLVED_BY in supportcore.analytics.events): `bot` while the bot had it, else `idle` or
        `customer`."""
        if conversation.status in ("bot", "open", "pending"):
            publisher = self._require_publisher()
            by = "bot" if conversation.status == "bot" else "idle" if idle else "customer"
            conversation.status = "resolved"
            conversation.resolved_at = datetime.now(UTC)
            await self._session.flush()
            self._record(conversation, "resolved", data={"by": by, "membership_id": None})
            publisher.widget(self._session, conversation.id, [{"type": "status", "status": "resolved"}])
            publisher.conversation_changed(self._session, self._org_id, conversation.id)
            await self._emit(
                ConversationResolved(
                    organisation_id=self._org_id, conversation_id=conversation.id, customer_id=conversation.customer_id
                ),
                "conversation_resolved",
            )
        return conversation

    async def rate(self, conversation: Conversation, score: int, comment: str | None) -> Conversation:
        """CSAT, once, after the conversation was resolved and only when the widget asks for it."""
        if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
            raise InvalidCsatError()
        comment = (comment or "").strip() or None
        if comment is not None and len(comment) > MAX_CSAT_COMMENT_LENGTH:
            raise InvalidCsatError()
        if conversation.status != "resolved" or conversation.csat_score is not None:
            raise InvalidConversationStateError("Feedback can be left once, after the conversation has ended")
        widget = await self._widgets.get(conversation.widget_id) if conversation.widget_id else None
        if widget is None or not widget.csat_enabled:
            raise InvalidConversationStateError("Feedback is not enabled for this widget")
        conversation.csat_score, conversation.csat_comment = score, comment
        await self._session.flush()
        self._record(conversation, "csat", data={"score": score})
        await self._emit(
            CsatSubmitted(
                organisation_id=self._org_id,
                conversation_id=conversation.id,
                customer_id=conversation.customer_id,
                score=score,
            ),
            "csat_submitted",
            score=score,
        )
        return conversation

    async def set_contact_email(self, conversation: Conversation, email: str) -> Conversation:
        """Where a person can reach the customer when nobody is available; only while waiting for one. The
        caller holds the row lock (see locked()); the inbox gets an upsert after the commit."""
        if conversation.status != "open":
            raise InvalidConversationStateError("A contact email is only needed while waiting for a person")
        email = (email or "").strip()
        if len(email) > MAX_EMAIL_LENGTH or not _EMAIL.match(email):
            raise InvalidContactEmailError()
        publisher = self._require_publisher()
        if conversation.contact_email != email:
            conversation.contact_email = email
            await self._session.flush()
            publisher.conversation_changed(self._session, self._org_id, conversation.id)
        return conversation

    async def escalate(self, conversation: Conversation, decision: Mapping[str, Any]) -> Conversation:
        """Hands the conversation to a person. `decision` is the risk decision:
        {action, rule_key, rule_name, reason, source}."""
        conversation.escalation = {field: decision.get(field) for field in _ESCALATION_FIELDS}
        conversation.status = "open"
        rule_key = str(decision.get("rule_key") or "")
        is_risk = rule_key.startswith(_HIGH_PRIORITY_RULE_PREFIX) or rule_key == _HIGH_PRIORITY_RULE_KEY
        if is_risk and _PRIORITY_RANK[conversation.priority] < _PRIORITY_RANK["high"]:
            conversation.priority = "high"
        await self._session.flush()
        await self._emit(
            ConversationEscalated(
                organisation_id=self._org_id,
                conversation_id=conversation.id,
                customer_id=conversation.customer_id,
                action=str(decision.get("action")),
                rule_key=decision.get("rule_key"),
            ),
            "conversation_escalated",
            action=decision.get("action"),
            rule_key=decision.get("rule_key"),
        )
        return conversation

    # --- agent actions (the inbox; the caller holds the row lock, see locked()) --------------------------

    async def add_agent_message(self, conversation: Conversation, membership: Membership, text: str) -> Message:
        """A team member's reply, only while the conversation is with the team (`open` or `pending`). It is the
        first response if none was recorded, moves `open` to `pending` (waiting on the customer), and after the
        commit goes to the widget and the inbox. It never starts a bot turn."""
        text = (text or "").strip()
        if not 1 <= len(text) <= MAX_AGENT_MESSAGE_LENGTH:
            raise InvalidMessageError(MAX_AGENT_MESSAGE_LENGTH)
        if conversation.status not in _REPLY_STATUSES:
            raise InvalidConversationStateError("Replies can only be sent while the conversation is with the team")
        publisher = self._require_publisher()
        if conversation.channel == "voice" and await self._is_phone_call(conversation.id):
            raise VoiceNotAvailableError("Phone callers can't receive text replies; call them back")
        message = await self._insert(conversation, "agent", text, (), None, author_membership_id=membership.id)
        await self.attach_author_names([message])
        if conversation.first_response_at is None:
            conversation.first_response_at = message.created_at
            self._record(
                conversation,
                "first_agent_response",
                membership_id=membership.id,
                data={"membership_id": str(membership.id)},
            )
        if conversation.status == "open":
            ensure_transition("open", "pending", "agent_reply")
            conversation.status = "pending"
        await self._session.flush()
        publisher.widget(self._session, conversation.id, [{"type": "message", "message": message_payload(message)}])
        publisher.conversation_changed(self._session, self._org_id, conversation.id)
        logger.info(
            "agent_replied",
            conversation_id=str(conversation.id),
            message_id=str(message.id),
            membership_id=str(membership.id),
            status=conversation.status,
        )
        return message

    async def _is_phone_call(self, conversation_id: UUID) -> bool:
        source = (
            await self._session.execute(
                TenantScopedRepository(self._session, Call, self._org_id)
                .select(Call.source)
                .where(Call.conversation_id == conversation_id, Call.direction == "inbound")
            )
        ).scalar_one_or_none()
        return source == "phone"

    async def mark_callback_done(self, conversation: Conversation) -> Conversation:
        """`requested` -> `done` (a person called the customer back); anything else is InvalidConversationStateError.
        The caller holds the row lock (see locked()); the inbox gets an upsert after the commit."""
        if conversation.callback_status != "requested":
            raise InvalidConversationStateError("No callback is waiting on this conversation")
        publisher = self._require_publisher()
        conversation.callback_status = "done"
        await self._session.flush()
        publisher.conversation_changed(self._session, self._org_id, conversation.id)
        logger.info("callback_done", conversation_id=str(conversation.id))
        return conversation

    async def transition(self, conversation: Conversation, to: str, actor: Membership) -> Conversation:
        """An agent's status change: `resolved` (resolve), `closed` (close), `open` (reopen, from `resolved`
        only) or `bot` (hand back). Every other change is InvalidConversationStateError, as is any change the
        TRANSITIONS table does not allow from the current status, and a hand back on an email or phone-call conversation. The widget gets `{"type": "status"}` and the
        inbox an upsert after the commit."""
        event = _AGENT_EVENTS.get(to)
        if event is None:
            raise InvalidConversationStateError()
        if event == "hand_back" and conversation.channel == "email":
            # Email has no bot: the ticket would leave the team views and later idle-close as AI-resolved.
            raise InvalidConversationStateError("An email conversation cannot be handed back to the bot")
        if event == "hand_back" and conversation.channel == "voice" and await self._is_phone_call(conversation.id):
            # Nobody is on the line for the bot to talk to; a browser call's conversation continues in the widget.
            raise InvalidConversationStateError("A phone call conversation cannot be handed back to the bot")
        from_status = conversation.status
        ensure_transition(from_status, to, event)
        publisher = self._require_publisher()
        widget_events: list[dict[str, Any]] = []
        now = datetime.now(UTC)
        if event == "hand_back":
            widget_events.append(await self._hand_back(conversation))
            widget_events.append(agent_event(None))
        elif event == "resolve":
            conversation.resolved_at = now
        elif event == "reopen":
            conversation.resolved_at = None
            await sla.on_reopen(self._session, self._org_id, self._events, conversation, now)
        conversation.status = to
        await self._session.flush()
        if event == "resolve":
            self._record(
                conversation, "resolved", membership_id=actor.id, data={"by": "agent", "membership_id": str(actor.id)}
            )
        else:
            self._record(conversation, _TRANSITION_EVENT_KINDS[event], membership_id=actor.id)
        widget_events.append({"type": "status", "status": to})
        publisher.widget(self._session, conversation.id, widget_events)
        publisher.conversation_changed(self._session, self._org_id, conversation.id)
        logger.info(
            "conversation_transitioned",
            conversation_id=str(conversation.id),
            actor_membership_id=str(actor.id),
            from_status=from_status,
            to_status=to,
        )
        if event == "resolve":
            await self._emit(
                ConversationResolved(
                    organisation_id=self._org_id, conversation_id=conversation.id, customer_id=conversation.customer_id
                ),
                "conversation_resolved",
            )
        return conversation

    async def set_priority(self, conversation: Conversation, priority: str) -> Conversation:
        if priority not in PRIORITIES:
            raise InvalidPriorityError()
        publisher = self._require_publisher()
        if conversation.priority != priority:
            conversation.priority = priority
            await sla.recompute_for_priority(self._session, self._org_id, self._events, conversation)
            await self._session.flush()
            publisher.conversation_changed(self._session, self._org_id, conversation.id)
            logger.info("conversation_priority_set", conversation_id=str(conversation.id), priority=priority)
        return conversation

    async def _hand_back(self, conversation: Conversation) -> dict[str, Any]:
        """Returns the conversation to the bot: resumes its graph when it is interrupted at the handoff (an
        escalation; a claimed bot conversation or a "talk to a person" request has no interrupt), clears the
        assignee, the escalation and the SLA due dates, and posts `back_to_bot`. Returns the widget event of that system message. The graph is
        resumed before the commit, under the row lock, so no bot turn can start on the interrupted graph."""
        if self._bot_hand_back is None:
            raise RuntimeError("Hand back needs the bot graph (bot_hand_back)")
        resumed = await self._bot_hand_back(conversation.id)
        # Back with the bot: no assignee, no escalation, no SLA clock (first_response_at is kept as history).
        conversation.assignee_membership_id = None
        conversation.escalation = None
        sla.clear_cycle(conversation)
        system = await self.add_message(conversation, "system", strings_for(conversation.language)["back_to_bot"])
        logger.info("conversation_handed_back", conversation_id=str(conversation.id), graph_resumed=resumed)
        return {"type": "message", "message": message_payload(system)}

    def _require_publisher(self) -> InboxPublisher:
        if self._publisher is None:
            raise RuntimeError("Agent actions need an InboxPublisher")
        return self._publisher

    # --- admin reads (dashboard; the customer-facing methods above never expose these) -------------

    def _admin_select(self) -> Any:
        latest_text = (
            self._messages.select(func.left(Message.text, PREVIEW_LENGTH))
            .where(Message.conversation_id == Conversation.id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
            .scalar_subquery()
        )
        return (
            self._conversations.select(Conversation, Customer, latest_text)
            .join(
                Customer,
                (Customer.id == Conversation.customer_id) & (Customer.organisation_id == Conversation.organisation_id),
            )
        )

    async def admin_list(
        self,
        *,
        status: str | None = None,
        widget_id: UUID | None = None,
        include_test: bool = False,
        after: tuple[datetime, UUID] | None = None,
        limit: int = 50,
    ) -> list[ConversationAdminRow]:
        """Newest activity first, keyset-paged on (last_message_at, id)."""
        stmt = self._admin_select()
        if status is not None:
            stmt = stmt.where(Conversation.status == status)
        if widget_id is not None:
            stmt = stmt.where(Conversation.widget_id == widget_id)
        if not include_test:
            stmt = stmt.where(Conversation.is_test.is_(False))
        if after is not None:
            # Same direction as the ORDER BY below: last_message_at descending, id descending.
            stmt = stmt.where(tuple_(Conversation.last_message_at, Conversation.id) < tuple_(*after))
        stmt = stmt.order_by(Conversation.last_message_at.desc(), Conversation.id.desc()).limit(limit)
        return [ConversationAdminRow(c, cust, preview) for c, cust, preview in (await self._session.execute(stmt)).all()]

    async def admin_detail(self, conversation_id: UUID) -> ConversationTrace | None:
        """None when the conversation does not exist in this organisation."""
        found = (await self._session.execute(self._admin_select().where(Conversation.id == conversation_id))).first()
        if found is None:
            return None
        row = ConversationAdminRow(*found)
        messages = await self._messages.list(
            Message.conversation_id == conversation_id, order_by=(Message.created_at, Message.id), limit=1000
        )
        turns, chunks = await self.turn_traces(conversation_id)
        return ConversationTrace(row, messages, turns, chunks)

    async def turn_traces(self, conversation_id: UUID) -> tuple[list[BotTurn], dict[UUID, list[dict[str, Any]]]]:
        """The conversation's bot turns (oldest first) and, per turn id, the chunks each retrieved."""
        turns = await self._turns.list(
            BotTurn.conversation_id == conversation_id, order_by=(BotTurn.created_at, BotTurn.id), limit=1000
        )
        return turns, await self._hydrate_chunks(turns)

    async def _hydrate_chunks(self, turns: list[BotTurn]) -> dict[UUID, list[dict[str, Any]]]:
        """One query for every chunk the turns retrieved. A chunk deleted since (or whose document
        was) has document_name and ordinal None."""
        ids: set[UUID] = set()
        for turn in turns:
            for chunk in (turn.retrieval or {}).get("chunks") or []:
                try:
                    ids.add(UUID(str(chunk.get("chunk_id"))))
                except ValueError:
                    continue
        known: dict[UUID, tuple[str, int]] = {}
        if ids:
            stmt = (
                TenantScopedRepository(self._session, Chunk, self._org_id)
                .select(Chunk.id, Document.filename, Chunk.ordinal)
                .join(Document, (Document.id == Chunk.document_id) & (Document.organisation_id == Chunk.organisation_id))
                .where(Chunk.id.in_(ids))
            )
            known = {cid: (name, ordinal) for cid, name, ordinal in (await self._session.execute(stmt)).all()}
        hydrated: dict[UUID, list[dict[str, Any]]] = {}
        for turn in turns:
            items = []
            for chunk in (turn.retrieval or {}).get("chunks") or []:
                try:
                    cid = UUID(str(chunk.get("chunk_id")))
                except ValueError:
                    continue
                name, ordinal = known.get(cid, (None, None))
                items.append(
                    {
                        "chunk_id": cid,
                        "document_name": name,
                        "ordinal": ordinal,
                        "score": chunk.get("score"),
                        "confidence": chunk.get("confidence"),
                        "rerank_score": chunk.get("rerank_score"),
                    }
                )
            hydrated[turn.id] = items
        return hydrated

    # --- messages ----------------------------------------------------------------------------------

    async def messages(self, conversation_id: UUID, *, after: datetime | None = None, limit: int = 100) -> list[Message]:
        where = [Message.conversation_id == conversation_id]
        if after is not None:
            where.append(Message.created_at > after)
        if after is not None:  # gap fill: what follows the cursor, oldest first
            found = await self._messages.list(*where, order_by=(Message.created_at, Message.id), limit=limit)
        else:  # history: the newest `limit`, in chronological order
            newest = await self._messages.list(
                *where, order_by=(Message.created_at.desc(), Message.id.desc()), limit=limit
            )
            found = newest[::-1]
        await self.attach_author_names(found)
        return found

    async def assignee_name(self, conversation: Conversation) -> str | None:
        """The display name (never the email) of the conversation's current assignee, None when unassigned."""
        if conversation.assignee_membership_id is None:
            return None
        row = await self._session.execute(
            TenantScopedRepository(self._session, Membership, self._org_id)
            .select(User.name)
            .join(User, User.id == Membership.user_id)
            .where(Membership.id == conversation.assignee_membership_id)
        )
        return row.scalar_one_or_none()

    async def attach_author_names(self, messages: Iterable[Message]) -> None:
        """Sets `author_name` (the user's display name, never the email) on the agent messages, for message_payload."""
        agents = [m for m in messages if m.role == "agent" and m.author_membership_id is not None]
        if not agents:
            return
        rows = await self._session.execute(
            TenantScopedRepository(self._session, Membership, self._org_id)
            .select(Membership.id, User.name)
            .join(User, User.id == Membership.user_id)
            .where(Membership.id.in_({m.author_membership_id for m in agents}))
        )
        names = dict(rows.all())
        for m in agents:
            m.author_name = names.get(m.author_membership_id)

    async def unanswered_customer_messages(self, conversation_id: UUID, limit: int = 50) -> list[Message]:
        """The customer messages the bot still has to answer, oldest first: those newer than both the
        newest message a bot turn answered (a turn answers every customer message up to its
        `BotTurn.message_id`) and the last agent or system message (a person took over)."""
        answered = (
            self._messages.select(func.max(Message.created_at))
            .join(BotTurn, BotTurn.message_id == Message.id)
            .where(BotTurn.conversation_id == conversation_id, BotTurn.organisation_id == Message.organisation_id)
        )
        handed_over = self._messages.select(func.max(Message.created_at)).where(
            Message.conversation_id == conversation_id, Message.role.in_(("agent", "system"))
        )
        where = [Message.conversation_id == conversation_id, Message.role == "customer"]
        for stmt in (answered, handed_over):
            cutoff = (await self._session.execute(stmt)).scalar_one_or_none()
            if cutoff is not None:
                where.append(Message.created_at > cutoff)
        return await self._messages.list(*where, order_by=(Message.created_at, Message.id), limit=limit)

    async def add_customer_message(
        self, conversation: Conversation, text: str, client_id: str | None
    ) -> tuple[Message, bool]:
        """(message, created). A resend with the same `client_id` returns the stored message, created=False."""
        text = (text or "").strip()
        if not 1 <= len(text) <= MAX_CUSTOMER_MESSAGE_LENGTH:
            raise InvalidMessageError()
        if client_id is not None:
            existing = await self._by_client_id(conversation.id, client_id)
            if existing is not None:
                return existing, False
        try:
            async with self._session.begin_nested():
                message = await self._insert(conversation, "customer", text, (), client_id)
        except IntegrityError:  # a concurrent resend won the unique index
            existing = client_id is not None and await self._by_client_id(conversation.id, client_id)
            if not existing:
                raise
            return existing, False
        if self._usage_counter is not None and not conversation.is_test and conversation.channel in _METERED_CHANNELS:
            await record_usage(
                self._session, self._org_id, "messages", 1, message.id, counter=self._usage_counter
            )
        return message, True

    async def add_message(
        self, conversation: Conversation, role: str, text: str, citations: Iterable[Mapping[str, Any]] = ()
    ) -> Message:
        if role not in _ROLES:
            raise ValueError(f"Unknown message role {role!r}")
        return await self._insert(conversation, role, text, citations, None)

    async def _insert(
        self,
        conversation: Conversation,
        role: str,
        text: str,
        citations: Iterable[Mapping[str, Any]],
        client_id: str | None,
        *,
        author_membership_id: UUID | None = None,
    ) -> Message:
        # The database clock, read when the row is written: callers that lock the conversation row
        # first (TurnRunner.post_customer_message) get timestamps in commit order across replicas.
        now = (await self._session.execute(select(func.clock_timestamp(type_=DateTime(timezone=True))))).scalar_one()
        message = Message(
            conversation_id=conversation.id,
            role=role,
            text=text,
            citations=[dict(c) for c in citations],
            client_id=client_id,
            author_membership_id=author_membership_id,
            created_at=now,
        )
        await self._messages.add(message)
        conversation.last_message_at = now
        await self._session.flush()
        self._record(conversation, "message", membership_id=author_membership_id, data={"role": role})
        logger.info(
            "message_added",
            conversation_id=str(conversation.id),
            message_id=str(message.id),
            role=role,
            chars=len(text),
        )
        return message

    async def _by_client_id(self, conversation_id: UUID, client_id: str) -> Message | None:
        stmt = self._messages.select().where(
            Message.conversation_id == conversation_id, Message.client_id == client_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    # --- bot turn lease ----------------------------------------------------------------------------

    async def acquire_turn_lease(self, conversation_id: UUID, seconds: int | float = TURN_LEASE_SECONDS) -> datetime | None:
        """One bot turn at a time per conversation. A single atomic UPDATE on the database clock
        (clock_timestamp(), not the frozen transaction time): of two concurrent callers only one matches
        the WHERE clause. An expired lease (a crashed turn) can be taken over.

        Returns the lease expiry, which the holder passes to release_turn_lease(); None = not acquired."""
        now = func.clock_timestamp(type_=DateTime(timezone=True))
        stmt = (
            update(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.id.in_(self._owned_ids(conversation_id)),
                or_(Conversation.turn_lease_until.is_(None), Conversation.turn_lease_until < now),
            )
            .values(turn_lease_until=now + timedelta(seconds=seconds))
            .returning(Conversation.turn_lease_until)
            .execution_options(synchronize_session=False)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def renew_turn_lease(
        self, conversation_id: UUID, lease: datetime, seconds: int | float = TURN_LEASE_SECONDS
    ) -> datetime | None:
        """Extends the caller's lease to now + seconds while it is still the caller's (same value).
        Returns the new value, which later renewals and the release must use; None = lost."""
        now = func.clock_timestamp(type_=DateTime(timezone=True))
        stmt = (
            update(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.id.in_(self._owned_ids(conversation_id)),
                Conversation.turn_lease_until == lease,
            )
            .values(turn_lease_until=now + timedelta(seconds=seconds))
            .returning(Conversation.turn_lease_until)
            .execution_options(synchronize_session=False)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def release_turn_lease(self, conversation_id: UUID, lease: datetime) -> None:
        """Clears the lease only if it is still the caller's: a late release must not free a lease
        that was taken over after this one expired."""
        stmt = (
            update(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.id.in_(self._owned_ids(conversation_id)),
                Conversation.turn_lease_until == lease,
            )
            .values(turn_lease_until=None)
            .execution_options(synchronize_session=False)
        )
        await self._session.execute(stmt)

    # --- internals ---------------------------------------------------------------------------------

    def _owned_ids(self, conversation_id: UUID) -> Any:
        """The organisation scope for a bulk UPDATE, which the repository cannot issue itself. The
        lease conditions stay in the UPDATE's own WHERE so they are re-checked after a row lock wait."""
        return self._conversations.select(Conversation.id).where(Conversation.id == conversation_id)

    def _record(
        self, conversation: Conversation, kind: str, *, membership_id: UUID | None = None, data: Mapping[str, Any] | None = None
    ) -> None:
        """The analytics event for a change just made, in the same transaction."""
        record(
            self._session, self._org_id, conversation.id, kind,
            channel=conversation.channel, membership_id=membership_id, data=data,
        )

    async def _emit(self, event: ConversationEvent, log_event: str, **fields: Any) -> None:
        logger.info(log_event, conversation_id=str(event.conversation_id), customer_id=str(event.customer_id), **fields)
        await self._events.publish(event)
