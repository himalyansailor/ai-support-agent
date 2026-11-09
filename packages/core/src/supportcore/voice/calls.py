"""One voice call's lifecycle: created by a Twilio webhook, connected by the media stream, finalised exactly once.

Never log or record a phone number, spoken text or token here: ids, counts and reason codes only.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.analytics.events import record
from supportcore.assignment.service import AssignmentService
from supportcore.billing.usage import record_usage
from supportcore.bot.turns import store_bot_turn
from supportcore.config import Settings
from supportcore.conversations.service import ConversationService
from supportcore.db.models import (
    Call,
    CallTransfer,
    Customer,
    Message,
    Organisation,
    PhoneNumber,
)
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import NotFoundError
from supportcore.events import EventBus
from supportcore.inbox.customers import find_or_create_customer_by_phone
from supportcore.inbox.events import message_payload
from supportcore.inbox.publisher import InboxPublisher
from supportcore.voice.errors import InvalidVoiceTokenError
from supportcore.voice.tokens import CallTicket

if TYPE_CHECKING:
    from supportcore.wiring import Adapters

__all__ = ["VOICE_CALLBACK_DECISION", "CallService"]

logger = structlog.get_logger()

VOICE_CALLBACK_DECISION = {
    "action": "escalate",
    "rule_key": "voice_callback",
    "rule_name": "Voice callback",
    "reason": None,
    "source": "voice",
}


class CallService:
    """`adapters` and `settings` are needed only by `request_callback` and `route_to_team` (they route the
    conversation to the team)."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        events: EventBus,
        publisher: InboxPublisher,
        *,
        adapters: Adapters | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._org_id = organisation_id
        self._events = events
        self._publisher = publisher
        self._adapters = adapters
        self._settings = settings
        self._calls = TenantScopedRepository(session, Call, organisation_id)
        self._conversations = ConversationService(
            session,
            organisation_id,
            events,
            publisher=publisher,
            usage_counter=adapters.usage_counter if adapters is not None else None,
        )

    # --- start -----------------------------------------------------------------------------------

    async def start_phone_call(self, number: PhoneNumber, call_sid: str, caller_e164: str | None) -> Call:
        """A repeated `call_sid` (Twilio retries webhooks) returns the existing call."""
        existing = await self._by_sid(call_sid)
        if existing is not None:
            return existing
        try:
            async with self._session.begin_nested():
                if caller_e164:
                    customer = await find_or_create_customer_by_phone(self._session, self._org_id, caller_e164)
                else:  # withheld caller id
                    customer = Customer(last_seen_at=datetime.now(UTC))
                    await TenantScopedRepository(self._session, Customer, self._org_id).add(customer)
                    await self._session.flush()
                return await self._create(
                    customer, number.widget_id, "phone", call_sid, caller_e164=caller_e164, phone_number_id=number.id
                )
        except IntegrityError:  # a concurrent retry of the same CallSid won
            existing = await self._by_sid(call_sid)
            if existing is None:
                raise
            return existing

    async def start_browser_call(self, ticket: CallTicket, call_sid: str) -> Call:
        """The ticket's `jti` is stored on the call (unique), so a ticket starts one call only."""
        existing = await self._by_sid(call_sid)
        if existing is not None:
            if existing.ticket_jti != ticket.jti:
                raise InvalidVoiceTokenError()
            return existing
        # Checked before anything is created: a reused ticket must not leave a conversation behind (the unique
        # ticket_jti below stays as the race backstop).
        if await self._by_ticket(ticket.jti) is not None:
            raise InvalidVoiceTokenError()
        customer = await TenantScopedRepository(self._session, Customer, self._org_id).get(ticket.customer_id)
        if customer is None:
            raise InvalidVoiceTokenError()
        try:
            async with self._session.begin_nested():
                return await self._create(
                    customer, ticket.widget_id, "browser", call_sid, ticket_jti=ticket.jti
                )
        except IntegrityError:
            existing = await self._by_sid(call_sid)
            if existing is not None and existing.ticket_jti == ticket.jti:
                return existing
            raise InvalidVoiceTokenError() from None

    async def _create(
        self,
        customer: Customer,
        widget_id: UUID | None,
        source: str,
        call_sid: str,
        *,
        caller_e164: str | None = None,
        phone_number_id: UUID | None = None,
        ticket_jti: str | None = None,
    ) -> Call:
        organisation = (
            await self._session.execute(select(Organisation).where(Organisation.id == self._org_id))
        ).scalar_one_or_none()
        if organisation is None:
            raise NotFoundError("Organisation not found")
        conversation = await self._conversations.create(
            customer.id, widget_id, organisation.language, is_test=False, channel="voice"
        )
        call = Call(
            conversation_id=conversation.id,
            phone_number_id=phone_number_id,
            source=source,
            provider_call_sid=call_sid,
            caller_e164=caller_e164,
            ticket_jti=ticket_jti,
            status="ringing",
            started_at=datetime.now(UTC),
        )
        await self._calls.add(call)
        await self._session.flush()
        record(self._session, self._org_id, conversation.id, "call_started", channel="voice", data={"source": source})
        logger.info("call_started", call_id=str(call.id), conversation_id=str(conversation.id), source=source)
        return call

    # --- during the call ---------------------------------------------------------------------------

    async def mark_stream_connected(self, call_id: UUID, mode: str | None = None) -> Call:
        """The stream connects (once). `mode`: the organisation's voice mode now, stored as the call's engine."""
        call = await self._locked(call_id)
        if call.stream_connected_at is not None or call.finalised:
            raise InvalidVoiceTokenError()
        if mode is not None:
            call.mode = mode
        now = datetime.now(UTC)
        call.stream_connected_at = now
        call.answered_at = call.answered_at or now
        call.status = "in_progress"
        await self._session.flush()
        return call

    async def add_caller_utterance(self, call_id: UUID, text: str) -> Message:
        call = await self._get(call_id)
        conversation = await self._conversations.locked(call.conversation_id)
        message, created = await self._conversations.add_customer_message(conversation, text, None)
        if created:
            self._publisher.conversation_changed(self._session, self._org_id, conversation.id)
            self._publisher.widget(
                self._session, conversation.id, [{"type": "message", "message": message_payload(message)}]
            )
        return message

    async def add_bot_reply(
        self,
        call_id: UUID,
        text: str,
        *,
        decision: Mapping[str, Any] | None,
        retrieval: Mapping[str, Any] | None,
        model: Mapping[str, Any],
        latency_ms: int | None,
        routed: bool = False,
    ) -> Message | None:
        """What the speech-to-speech model said, stored as the bot's reply with its BotTurn (outcome `answered`, or
        `escalated` when the risk decision escalated) and the `bot_turn` event, in the shapes the turn runner stores.
        The conversation left `bot` meanwhile (a person took over): the BotTurn is kept as the trace but no message
        is stored, and None is returned. `routed`: the session itself handed the conversation to the team during
        this reply, so leaving `bot` does not supersede it. The widget and the inbox are told after the commit."""
        call = await self._get(call_id)
        conversation = await self._conversations.locked(call.conversation_id)
        superseded = conversation.status != "bot" and not routed
        message_id = (
            await self._session.execute(
                TenantScopedRepository(self._session, Message, self._org_id)
                .select(Message.id)
                .where(Message.conversation_id == conversation.id, Message.role == "customer")
                .order_by(Message.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        reply = None if superseded else await self._conversations.add_message(conversation, "bot", text)
        await store_bot_turn(
            self._session,
            self._org_id,
            conversation,
            message_id=message_id,
            reply_message_id=reply.id if reply is not None else None,
            outcome="escalated" if (decision or {}).get("action") == "escalate" else "answered",
            no_answer=False,
            decision=decision,
            retrieval=retrieval,
            tool_calls=[],
            model=model,
            latency_ms=latency_ms,
            trace_id=None,
        )
        if reply is not None:
            self._publisher.conversation_changed(self._session, self._org_id, conversation.id)
            self._publisher.widget(
                self._session, conversation.id, [{"type": "message", "message": message_payload(reply)}]
            )
        return reply

    async def route_to_team(self, call_id: UUID, decision: Mapping[str, Any]) -> None:
        """Hands the call's conversation to the team with `decision` (no announcement: nobody is watching a widget),
        unless it already left `bot`."""
        call = await self._get(call_id)
        conversation = await self._conversations.locked(call.conversation_id)
        if conversation.status == "bot":
            await self._assignment().route_to_team(conversation, decision, announce=False)

    async def request_callback(self, call_id: UUID, end_reason: str) -> None:
        """Hands a conversation the bot still has to the team as a callback ticket (no announcement: nobody is
        watching a widget). Phone calls also record the number to call back. The first end reason wins:
        `handoff_callback`, `voice_unavailable`, `error`, ... and `transfer_no_answer` (a live transfer nobody took, or
        the caller hung up while agents were rung; set by TransferService, never over `transferred`). A phone
        call with no caller number and nothing said is not routed: there is nothing for an agent to act on (the
        call still finalises as usual)."""
        call = await self._locked(call_id)
        conversation = await self._conversations.locked(call.conversation_id)
        if call.end_reason is None:
            call.end_reason = end_reason
        if call.source == "phone" and call.caller_e164 and conversation.callback_status is None:
            conversation.callback_status = "requested"
            conversation.callback_phone = call.caller_e164
        unreachable = call.source == "phone" and not call.caller_e164 and not await self._caller_spoke(conversation.id)
        if conversation.status == "bot" and not unreachable:
            await self._assignment().route_to_team(conversation, VOICE_CALLBACK_DECISION, announce=False)
        await self._session.flush()
        self._publisher.conversation_changed(self._session, self._org_id, conversation.id)

    async def set_end_reason(self, call_id: UUID, reason: str) -> None:
        call = await self._locked(call_id)
        if call.end_reason is None:
            call.end_reason = reason
            await self._session.flush()

    async def add_usage(self, call_id: UUID, stt_seconds: float, tts_characters: int) -> None:
        await self._session.execute(
            update(Call)
            .where(Call.id == call_id, Call.id.in_(self._calls.select(Call.id)))  # tenant-scoped via the repository
            .values(stt_seconds=Call.stt_seconds + stt_seconds, tts_characters=Call.tts_characters + tts_characters)
        )

    # --- end ---------------------------------------------------------------------------------------
    #
    # A call that connected a media stream is finalised by the later of two events: Twilio's terminal status
    # callback (`provider_ended`) and the end of the stream's session (`stream_ended`, once its last turn has
    # persisted and its usage is stored). Each records its side under the call's row lock, so exactly one of them
    # sees both and finalises. A call that never connected a stream is finalised by the status callback alone.
    # Calls neither side finishes (a crash before the stream end was recorded, no status callback) are finalised by
    # the stale-call sweep (supportcore.voice.jobs.finalise_stale_calls).

    async def provider_ended(self, call_id: UUID, status: str, duration_seconds: int | None) -> bool:
        """Twilio's terminal status: stored, then the call is finalised unless its stream's session is still
        running. True when this finalised it."""
        call = await self._locked(call_id)
        if call.finalised:
            return False
        call.status = status
        call.duration_seconds = duration_seconds
        call.provider_ended_at = call.provider_ended_at or datetime.now(UTC)
        if call.stream_connected_at is not None and call.stream_ended_at is None:
            await self._session.flush()
            return False  # the session finalises once it is done
        return await self.finalise(call_id, status, duration_seconds)

    async def stream_ended(self, call_id: UUID) -> bool:
        """The stream's session is done: recorded, then the call is finalised if Twilio's terminal status is
        already known. True when this finalised it."""
        call = await self._locked(call_id)
        if call.finalised or call.stream_ended_at is not None:
            return False
        call.stream_ended_at = datetime.now(UTC)
        if call.provider_ended_at is None:
            await self._session.flush()
            return False  # the status callback finalises
        return await self.finalise(call_id, call.status, call.duration_seconds)

    async def finalise(self, call_id: UUID, status: str, duration_seconds: int | None) -> bool:
        """The end bookkeeping, once per call (through `provider_ended` / `stream_ended`, whichever comes last).
        False when it already ran."""
        call = await self._locked(call_id)
        if call.finalised:
            return False
        now = datetime.now(UTC)
        call.ended_at = now
        call.status = status
        if duration_seconds is None and call.answered_at is not None:
            duration_seconds = max(0, round((now - call.answered_at).total_seconds()))
        call.duration_seconds = duration_seconds
        call.end_reason = call.end_reason or "caller_hangup"
        call.finalised = True
        await self._session.flush()
        if self._adapters is not None and duration_seconds:
            await record_usage(
                self._session,
                self._org_id,
                "voice_minutes",
                math.ceil(duration_seconds / 60),
                call.id,
                counter=self._adapters.usage_counter,
            )
        await self._end_connected_transfer(call.id, now)
        conversation = await self._conversations.locked(call.conversation_id)
        if conversation.status == "bot":
            await self._conversations.end(conversation)
        self._publisher.conversation_changed(self._session, self._org_id, conversation.id)
        record(
            self._session,
            self._org_id,
            conversation.id,
            "call_ended",
            channel="voice",
            data={
                "source": call.source,
                "mode": call.mode,
                "duration_seconds": duration_seconds,
                "end_reason": call.end_reason,
                "stt_seconds": call.stt_seconds,
                "tts_characters": call.tts_characters,
            },
        )
        logger.info(
            "call_ended",
            call_id=str(call.id),
            conversation_id=str(conversation.id),
            status=status,
            end_reason=call.end_reason,
            duration_seconds=duration_seconds,
        )
        return True

    # --- internals ---------------------------------------------------------------------------------

    async def _end_connected_transfer(self, call_id: UUID, now: datetime) -> None:
        """Records the end of the call's connected transfer when nothing did (the caller's status callback was lost,
        or `caller_left` failed): the agent is free again. The lock order is transfer → call, and the call's lock is
        held here, so the transfer row is never waited for (SKIP LOCKED): a transfer locked right now belongs to a
        TransferService writer, and the busy check ignores a finalised call's transfer anyway."""
        transfers = TenantScopedRepository(self._session, CallTransfer, self._org_id)
        unlocked = (
            transfers.select(CallTransfer.id)
            .where(
                CallTransfer.call_id == call_id,
                CallTransfer.status == "connected",
                CallTransfer.ended_at.is_(None),
            )
            .with_for_update(skip_locked=True)
        )
        await self._session.execute(
            update(CallTransfer)
            .where(CallTransfer.id.in_(unlocked.scalar_subquery()))
            .values(ended_at=now)
            .execution_options(synchronize_session=False)
        )

    def _assignment(self) -> AssignmentService:
        if self._adapters is None or self._settings is None:
            raise RuntimeError("routing a call's conversation needs the adapters and settings")
        return AssignmentService(
            self._session, self._org_id, self._events, self._adapters, self._settings, publisher=self._publisher
        )

    async def _caller_spoke(self, conversation_id: UUID) -> bool:
        stmt = (
            TenantScopedRepository(self._session, Message, self._org_id)
            .select(Message.id)
            .where(Message.conversation_id == conversation_id, Message.role == "customer")
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None

    async def _by_sid(self, call_sid: str) -> Call | None:
        stmt = self._calls.select().where(Call.provider_call_sid == call_sid)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _by_ticket(self, jti: str) -> Call | None:
        stmt = self._calls.select().where(Call.ticket_jti == jti)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _get(self, call_id: UUID) -> Call:
        call = await self._calls.get(call_id)
        if call is None:
            raise NotFoundError("Call not found")
        return call

    async def _locked(self, call_id: UUID) -> Call:
        """The call, its row locked (FOR UPDATE) and refreshed as of the lock."""
        stmt = self._calls.select(for_update=True).where(Call.id == call_id).execution_options(populate_existing=True)
        call = (await self._session.execute(stmt)).scalar_one_or_none()
        if call is None:
            raise NotFoundError("Call not found")
        return call
