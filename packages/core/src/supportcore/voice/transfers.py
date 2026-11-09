"""Live transfer of a call to a person: the agents who can take it are rung one at a time until one accepts, the
attempts run out or the deadline passes; then the caller hears that someone will follow up, and the conversation
stays with the team as a callback ticket.

`TransferService` never calls Twilio. Each method is one unit of work by its caller and returns the Twilio work to do
after the commit as `TransferAction`s (place a leg, end a leg, pull the caller out of the conference). State changes
happen under the transfer's row lock (then the call's, then the conversation's), so of two agents accepting at once
exactly one joins, and nothing accepts after the transfer gave up.

Never log or record a phone number here (the agent's, the caller's or ours): ids, attempts, statuses and reason codes
only. `Ring.to` / `Ring.from_` are kept out of reprs for that reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import and_, exists, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from supportcore.analytics.integration_errors import record_integration_error
from supportcore.assignment.availability import available_agents
from supportcore.assignment.service import AssigneeNotEligibleError, AssignmentService
from supportcore.config import Settings
from supportcore.conversations.service import InvalidConversationStateError
from supportcore.db.models import (
    Call,
    CallTransfer,
    Conversation,
    Membership,
    PhoneNumber,
    VoiceSettings,
)
from supportcore.db.session import after_commit
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import NotFoundError
from supportcore.events import EventBus
from supportcore.inbox.events import (
    call_offer_ended_event,
    call_offer_event,
    inbox_channel,
)
from supportcore.inbox.publisher import InboxPublisher
from supportcore.observability.logging import get_logger
from supportcore.realtime.spawn import spawn_publish
from supportcore.team.service import E164_PATTERN
from supportcore.voice.calls import CallService
from supportcore.voice.settings_service import VoiceSettingsService
from supportcore.voice.softphone import agent_identity
from supportcore.voice.strings import unavailable_key

if TYPE_CHECKING:
    from supportcore.wiring import Adapters

__all__ = [
    "ACCEPT_GATHER_SECONDS",
    "AGENT_RING_SECONDS",
    "MAX_TRANSFERRED_CALL_HOURS",
    "MAX_TRANSFER_ATTEMPTS",
    "TRANSFER_DEADLINE_SECONDS",
    "TRANSFER_WATCHDOG_GRACE_SECONDS",
    "EndLeg",
    "RedirectCaller",
    "Ring",
    "TransferAction",
    "TransferService",
    "caller_id_number",
    "transfer_candidates",
]

logger = get_logger(__name__)

AGENT_RING_SECONDS = 20
MAX_TRANSFER_ATTEMPTS = 3
TRANSFER_DEADLINE_SECONDS = 60
ACCEPT_GATHER_SECONDS = 10
TRANSFER_WATCHDOG_GRACE_SECONDS = 5
MAX_TRANSFERRED_CALL_HOURS = 4

# The From of a browser leg when the organisation has no number: Twilio accepts a client identity string there.
_BROWSER_FROM_FALLBACK = "support"
_FAILED_KEY_BASE = "voice_transfer_failed"


@dataclass(frozen=True)
class Ring:
    """Place an agent leg."""

    transfer_id: UUID
    attempt: int
    membership_id: UUID
    target: str  # browser | phone
    to: str = field(repr=False)
    from_: str = field(repr=False)


@dataclass(frozen=True)
class EndLeg:
    """Stop a ringing agent leg."""

    call_sid: str


@dataclass(frozen=True)
class RedirectCaller:
    """Pull the caller out of the conference: say this strings key and hang up."""

    call_sid: str
    key: str  # voice_transfer_failed_*
    language: str


TransferAction = Ring | EndLeg | RedirectCaller

# (the agent, the target "browser" | "phone", the leg's To)
Candidate = tuple[Membership, str, str]


# --- who can take the call ---------------------------------------------------------------------------------------


async def caller_id_number(session: AsyncSession, organisation_id: UUID, conversation_id: UUID) -> str | None:
    """The number agent legs are placed from: the one the conversation's inbound call came in on when known, else
    the organisation's first enabled number (by created_at). None when it has neither."""
    numbers = TenantScopedRepository(session, PhoneNumber, organisation_id)
    called = (
        numbers.select(PhoneNumber.e164)
        .join(Call, Call.phone_number_id == PhoneNumber.id)
        .where(
            Call.organisation_id == PhoneNumber.organisation_id,
            Call.conversation_id == conversation_id,
            Call.direction == "inbound",
        )
        .limit(1)
    )
    e164 = (await session.execute(called)).scalar_one_or_none()
    if e164 is not None:
        return e164
    first = numbers.select(PhoneNumber.e164).where(PhoneNumber.enabled.is_(True))
    first = first.order_by(PhoneNumber.created_at, PhoneNumber.id).limit(1)
    return (await session.execute(first)).scalar_one_or_none()


async def transfer_candidates(
    session: AsyncSession,
    organisation_id: UUID,
    adapters: Adapters,
    conversation: Conversation,
    voice_settings: VoiceSettings,
    now: datetime,
    *,
    present: set[UUID] | None = None,
    softphones: set[UUID] | None = None,
) -> list[Candidate]:
    """The agents who can take a transferred call now (Assumption 4), each with how to reach them.

    `available_agents()` minus those busy on another call's transfer (`_busy_agents`); each reached by their
    preference when the organisation allows it and they are reachable that way, else by the other allowed target.
    A browser needs a registered softphone (`adapters.softphones`); a phone needs an E.164 profile number and a
    number to call from. The conversation's assignee comes first, then the rest in `available_agents` order.

    A caller holding a row lock passes `present` / `softphones` read before taking it."""
    allowed = {
        target
        for target, on in (
            ("browser", voice_settings.transfer_browser_enabled),
            ("phone", voice_settings.transfer_phone_enabled),
        )
        if on
    }
    if not allowed:
        return []
    if present is None:
        present = await adapters.presence.present(organisation_id)
    if softphones is None:
        softphones = await adapters.softphones.present(organisation_id) if "browser" in allowed else set()
    agents = await available_agents(session, organisation_id, adapters.presence, now, present=present)
    if not agents:
        return []
    busy = await _busy_agents(session, organisation_id, [m.id for m in agents], now)
    from_number = await caller_id_number(session, organisation_id, conversation.id) if "phone" in allowed else None
    found: list[Candidate] = []
    for membership in agents:
        if membership.id in busy:
            continue
        preferred = membership.call_preference
        for target in (preferred, "phone" if preferred == "browser" else "browser"):
            if target not in allowed:
                continue
            if target == "browser" and membership.id in softphones:
                found.append((membership, "browser", f"client:{agent_identity(membership.id)}"))
                break
            phone = membership.phone_number
            if target == "phone" and from_number and phone and E164_PATTERN.fullmatch(phone):
                found.append((membership, "phone", phone))
                break
    assignee = conversation.assignee_membership_id
    found.sort(key=lambda candidate: candidate[0].id != assignee)  # stable: the rest keep their order
    return found


async def _busy_agents(session: AsyncSession, organisation_id: UUID, ids: list[UUID], now: datetime) -> set[UUID]:
    """Of `ids`, the agents on another call's transfer whose call is not finalised: being rung for it (the
    current agent of a `ringing` transfer), or talking on it (`connected`, its end not recorded, connected within
    MAX_TRANSFERRED_CALL_HOURS: a transfer whose end was never recorded stops counting then, like the sweep)."""
    call_open = exists().where(
        Call.id == CallTransfer.call_id,
        Call.organisation_id == CallTransfer.organisation_id,  # the transfer's own call (the transfer is scoped)
        Call.finalised.is_(False),
    )
    talking = and_(
        CallTransfer.status == "connected",
        CallTransfer.ended_at.is_(None),
        CallTransfer.connected_at > now - timedelta(hours=MAX_TRANSFERRED_CALL_HOURS),
        CallTransfer.connected_membership_id.in_(ids),
    )
    rung = and_(CallTransfer.status == "ringing", CallTransfer.current_membership_id.in_(ids))
    stmt = TenantScopedRepository(session, CallTransfer, organisation_id).select(
        CallTransfer.status, CallTransfer.connected_membership_id, CallTransfer.current_membership_id
    )
    rows = await session.execute(stmt.where(call_open, or_(talking, rung)))
    return {connected if status == "connected" else current for status, connected, current in rows}


# --- the state machine -------------------------------------------------------------------------------------------


class TransferService:
    """`session_factory` is for the integration error row (its own unit of work); without it one is built on the
    session's bind."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        events: EventBus,
        publisher: InboxPublisher,
        adapters: Adapters,
        settings: Settings,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._session = session
        self._org_id = organisation_id
        self._events = events
        self._publisher = publisher
        self._adapters = adapters
        self._settings = settings
        self._session_factory = session_factory
        self._transfers = TenantScopedRepository(session, CallTransfer, organisation_id)
        self._calls = TenantScopedRepository(session, Call, organisation_id)

    # --- starting --------------------------------------------------------------------------------------------

    async def can_transfer(self, call_id: UUID) -> bool:
        """An inbound call, a transfer target the organisation allows, and someone who can take it now. Takes no
        row lock (presence is read freely)."""
        call = await self._call(call_id)
        if call.source == "callback" or call.direction != "inbound":
            return False
        voice = await self._voice_settings()
        if not (voice.transfer_browser_enabled or voice.transfer_phone_enabled):
            return False
        conversation = await self._conversation(call.conversation_id)
        return bool(
            await transfer_candidates(self._session, self._org_id, self._adapters, conversation, voice, _now())
        )

    async def begin(self, call_id: UUID) -> CallTransfer:
        """The call's transfer, `ringing` with nobody rung yet (idempotent per call). Leaves Call.end_reason alone:
        how the call ends is decided by what the transfer does."""
        call = await self._call(call_id)
        existing = await self._by_call(call.id)
        if existing is not None:
            return existing
        transfer = CallTransfer(
            call_id=call.id,
            conversation_id=call.conversation_id,
            status="ringing",
            attempt=0,
            tried_membership_ids=[],
            deadline_at=_now() + timedelta(seconds=TRANSFER_DEADLINE_SECONDS),
        )
        try:
            async with self._session.begin_nested():
                await self._transfers.add(transfer)
                await self._session.flush()
        except IntegrityError:  # a concurrent begin for the same call won
            existing = await self._by_call(call.id)
            if existing is None:
                raise
            return existing
        logger.info("transfer_begun", transfer_id=str(transfer.id), call_id=str(call.id))
        return transfer

    async def start(self, transfer_id: UUID) -> list[TransferAction]:
        """Rings the first agent (once: a repeated start does nothing)."""
        present, softphones = await self._presence()
        transfer = await self._locked(transfer_id)
        if transfer.status != "ringing" or transfer.attempt != 0:
            return []
        return await self._next_ring(transfer, present, softphones)

    # --- an agent leg ----------------------------------------------------------------------------------------

    async def set_leg_sid(self, transfer_id: UUID, attempt: int, sid: str) -> list[TransferAction]:
        """Stores the placed leg's sid. A leg whose transfer has moved on meanwhile (given up, abandoned) is ended."""
        transfer = await self._locked(transfer_id)
        if attempt == transfer.attempt and transfer.status in ("ringing", "connected"):
            transfer.current_leg_sid = sid
            await self._session.flush()
            return []
        return [EndLeg(sid)]

    async def agent_answered(self, transfer_id: UUID, attempt: int) -> str:
        """The leg was picked up: a browser leg of the current attempt connects at once (`join`); a phone leg must
        press 1 first (`gather`: voicemail must not accept); the leg that already connected joins again (`join`);
        anything else is `taken`."""
        transfer = await self._locked(transfer_id)
        if _connected_by(transfer, attempt):
            return "join"  # a repeat for the leg that connected: it joins (again)
        if not _is_current(transfer, attempt) or transfer.current_membership_id is None:
            return "taken"
        if transfer.current_target == "phone":
            return "gather"
        await self._connect(transfer)
        return "join"

    async def accept(self, transfer_id: UUID, attempt: int) -> tuple[bool, list[TransferAction]]:
        """The agent of the current attempt takes the call: True (again for a repeat by the leg that connected);
        False when stale (another attempt, connected by another leg, given up, abandoned or over)."""
        transfer = await self._locked(transfer_id)
        if _connected_by(transfer, attempt):
            return True, []  # a repeat for the leg that connected: it still holds the call
        if not _is_current(transfer, attempt) or transfer.current_membership_id is None:
            logger.info("transfer_accept_refused", transfer_id=str(transfer.id), attempt=attempt, status=transfer.status)
            return False, []
        await self._connect(transfer)
        return True, []

    async def leg_ended(
        self, transfer_id: UUID, attempt: int, call_status: str, *, refused: bool = False
    ) -> list[TransferAction]:
        """The current leg ended without an accept (no answer, busy, declined, voicemail without "1", refused):
        ring the next agent, or give up. Any other leg's end is ignored."""
        present, softphones = await self._presence()
        transfer = await self._locked(transfer_id)
        if _dropped_before_join(transfer, attempt):
            # The agent took the call but their leg ended before it entered the room: the caller would otherwise
            # wait on hold until they hang up. Given up as at the deadline.
            logger.info("transfer_agent_dropped", transfer_id=str(transfer.id), attempt=attempt, call_status=call_status)
            # `transferred` was set at the accept, before anyone joined: the one exception to first-wins (the inbox
            # must not say "transferred" for a caller who got a callback). Locks: transfer (held), call.
            call = await self._call(transfer.call_id, for_update=True)
            if call.end_reason == "transferred":
                call.end_reason = "transfer_no_answer"
            return await self._give_up(transfer)
        if not _is_current(transfer, attempt):
            return []
        logger.info("transfer_leg_ended", transfer_id=str(transfer.id), attempt=attempt, call_status=call_status)
        if transfer.current_target == "browser":
            self._offer_ended(transfer, "missed")
        return await self._next_ring(transfer, present, softphones, refused=refused)

    async def agent_joined(self, transfer_id: UUID, call_sid: str) -> None:
        """The conference reports a participant joined (the caller's TwiML sets the room's status callback, so every
        join arrives here, the caller's own included). It counts as the agent entering the room when the transfer is
        connected and `call_sid` is the stored agent leg; while that sid has not been stored yet, any participant
        other than the caller is the agent (once connected, the agent who accepted is the only other one)."""
        transfer = await self._locked(transfer_id)
        if transfer.status != "connected" or transfer.agent_joined_at is not None or not call_sid:
            return
        call = await self._call(transfer.call_id)
        if call_sid == call.provider_call_sid:
            return
        if transfer.current_leg_sid is not None and call_sid != transfer.current_leg_sid:
            return
        transfer.agent_joined_at = _now()
        await self._session.flush()
        logger.info("transfer_agent_joined", transfer_id=str(transfer.id), attempt=transfer.attempt)

    async def ring_failed(self, transfer_id: UUID, attempt: int) -> list[TransferAction]:
        """Twilio refused to place the leg: recorded (`voice/twilio/transfer_failed`), then handled as its end."""
        actions = await self.leg_ended(transfer_id, attempt, "failed", refused=True)
        factory = self._session_factory or async_sessionmaker(bind=self._session.bind, expire_on_commit=False)
        await record_integration_error(factory, self._org_id, "voice", "twilio", "transfer_failed")
        return actions

    # --- the caller and the clock ------------------------------------------------------------------------------

    async def caller_left(self, call_id: UUID) -> list[TransferAction]:
        """The caller hung up. While ringing: the transfer is abandoned, the ringing leg ended and the conversation
        becomes a callback ticket. Once connected: only the end is recorded. No transfer: nothing."""
        transfer = await self._by_call(call_id, for_update=True)
        if transfer is None:
            return []
        now = _now()
        if transfer.status == "connected" and transfer.ended_at is None:
            transfer.ended_at = now
            await self._session.flush()
            return []
        if transfer.status != "ringing":
            return []
        transfer.status = "abandoned"
        transfer.ended_at = now
        actions: list[TransferAction] = [EndLeg(transfer.current_leg_sid)] if transfer.current_leg_sid else []
        if transfer.current_target == "browser":
            self._offer_ended(transfer, "cancelled")
        await self._session.flush()
        await self._call_service().request_callback(transfer.call_id, "transfer_no_answer")
        logger.info("transfer_abandoned", transfer_id=str(transfer.id), attempt=transfer.attempt)
        return actions

    async def deadline_passed(self, transfer_id: UUID) -> list[TransferAction]:
        """The watchdog (and the stale sweep): a transfer still ringing at its deadline gives up."""
        transfer = await self._locked(transfer_id)
        if transfer.status != "ringing" or _now() < transfer.deadline_at:
            return []
        return await self._give_up(transfer, leg_live=True)

    # --- internals -----------------------------------------------------------------------------------------------

    async def _next_ring(
        self, transfer: CallTransfer, present: set[UUID], softphones: set[UUID], *, refused: bool = False
    ) -> list[TransferAction]:
        """Rings the next agent not tried yet, or gives up (attempts used, deadline passed, nobody left). Any
        previous leg is over by now."""
        now = _now()
        if transfer.attempt >= MAX_TRANSFER_ATTEMPTS or now >= transfer.deadline_at:
            return await self._give_up(transfer, refused=refused)
        conversation = await self._conversation(transfer.conversation_id)
        voice = await self._voice_settings()
        tried = set(transfer.tried_membership_ids)
        candidates = [
            candidate
            for candidate in await transfer_candidates(
                self._session,
                self._org_id,
                self._adapters,
                conversation,
                voice,
                now,
                present=present,
                softphones=softphones,
            )
            if str(candidate[0].id) not in tried
        ]
        if not candidates:
            return await self._give_up(transfer, refused=refused)
        membership, target, to = candidates[0]
        from_ = await caller_id_number(self._session, self._org_id, transfer.conversation_id)
        transfer.attempt += 1
        transfer.current_membership_id = membership.id
        transfer.current_target = target
        transfer.current_leg_sid = None
        transfer.tried_membership_ids = [*transfer.tried_membership_ids, str(membership.id)]
        await self._session.flush()
        if target == "browser":
            self._publish(call_offer_event(transfer.id, transfer.conversation_id, membership.id))
        logger.info(
            "transfer_ringing",
            transfer_id=str(transfer.id),
            attempt=transfer.attempt,
            membership_id=str(membership.id),
            target=target,
        )
        return [Ring(transfer.id, transfer.attempt, membership.id, target, to, from_ or _BROWSER_FROM_FALLBACK)]

    async def _give_up(
        self, transfer: CallTransfer, *, refused: bool = False, leg_live: bool = False
    ) -> list[TransferAction]:
        """Nobody took the call: `no_answer` (`failed` when Twilio refused the last leg), the callback ticket, and
        the caller pulled out of the conference to hear that someone will follow up. `leg_live`: a leg may still be
        ringing (the deadline), so it is ended."""
        transfer.status = "failed" if refused else "no_answer"
        transfer.ended_at = _now()
        actions: list[TransferAction] = []
        if leg_live and transfer.current_leg_sid:
            actions.append(EndLeg(transfer.current_leg_sid))
        if leg_live and transfer.current_target == "browser":
            self._offer_ended(transfer, "missed")
        await self._session.flush()
        await self._call_service().request_callback(transfer.call_id, "transfer_no_answer")
        call = await self._call(transfer.call_id)
        conversation = await self._conversation(transfer.conversation_id)
        if call.provider_call_sid:
            key = unavailable_key(_FAILED_KEY_BASE, call.source, call.caller_e164)
            actions.append(RedirectCaller(call.provider_call_sid, key, conversation.language or "en"))
        logger.info(
            "transfer_given_up", transfer_id=str(transfer.id), status=transfer.status, attempts=transfer.attempt
        )
        return actions

    async def _connect(self, transfer: CallTransfer) -> None:
        """The current agent joins: the transfer is connected, the call ends as `transferred`, and the agent takes
        the conversation (a claim, with the agent as actor). Locks: transfer (held), call, conversation."""
        membership_id = transfer.current_membership_id
        assert membership_id is not None
        call = await self._call(transfer.call_id, for_update=True)
        call.end_reason = "transferred"
        transfer.status = "connected"
        transfer.connected_membership_id = membership_id
        transfer.connected_at = _now()
        await self._session.flush()
        membership = await TenantScopedRepository(self._session, Membership, self._org_id).get(membership_id)
        if membership is not None:
            try:
                await AssignmentService(
                    self._session, self._org_id, self._events, self._adapters, self._settings, publisher=self._publisher
                ).claim(transfer.conversation_id, membership)
            except (AssigneeNotEligibleError, InvalidConversationStateError) as exc:
                # The agent is already on the call: the connection stands, the inbox shows who has it otherwise.
                logger.warning(
                    "transfer_not_assigned",
                    transfer_id=str(transfer.id),
                    membership_id=str(membership_id),
                    error_type=type(exc).__name__,
                )
        self._publisher.conversation_changed(self._session, self._org_id, transfer.conversation_id)
        if transfer.current_target == "browser":
            self._offer_ended(transfer, "accepted")
        logger.info(
            "transfer_connected",
            transfer_id=str(transfer.id),
            attempt=transfer.attempt,
            membership_id=str(membership_id),
            target=transfer.current_target,
        )

    def _offer_ended(self, transfer: CallTransfer, outcome: str) -> None:
        if transfer.current_membership_id is not None:
            self._publish(call_offer_ended_event(transfer.id, transfer.current_membership_id, outcome))

    def _publish(self, payload: dict[str, Any]) -> None:
        """After the commit, on the org's inbox channel (nothing goes out on rollback)."""
        pubsub = self._adapters.pubsub
        channel = inbox_channel(self._org_id)
        after_commit(
            self._session,
            lambda: spawn_publish(pubsub.publish(channel, payload), failure_event="inbox_publish_failed", channel=channel),
        )

    def _call_service(self) -> CallService:
        return CallService(
            self._session, self._org_id, self._events, self._publisher, adapters=self._adapters, settings=self._settings
        )

    async def _presence(self) -> tuple[set[UUID], set[UUID]]:
        """Online presence and registered softphones, read before any row lock is taken."""
        return (
            await self._adapters.presence.present(self._org_id),
            await self._adapters.softphones.present(self._org_id),
        )

    async def _voice_settings(self) -> VoiceSettings:
        return await VoiceSettingsService(self._session, self._org_id, self._events).get()

    async def _locked(self, transfer_id: UUID) -> CallTransfer:
        """The transfer, its row locked (FOR UPDATE) and refreshed as of the lock; NotFoundError otherwise (also for
        another organisation's transfer)."""
        stmt = (
            self._transfers.select(for_update=True)
            .where(CallTransfer.id == transfer_id)
            .execution_options(populate_existing=True)
        )
        transfer = (await self._session.execute(stmt)).scalar_one_or_none()
        if transfer is None:
            raise NotFoundError("Transfer not found")
        return transfer

    async def _by_call(self, call_id: UUID, *, for_update: bool = False) -> CallTransfer | None:
        stmt = self._transfers.select(for_update=for_update).where(CallTransfer.call_id == call_id)
        if for_update:
            stmt = stmt.execution_options(populate_existing=True)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _call(self, call_id: UUID, *, for_update: bool = False) -> Call:
        stmt = self._calls.select(for_update=for_update).where(Call.id == call_id)
        if for_update:
            stmt = stmt.execution_options(populate_existing=True)
        call = (await self._session.execute(stmt)).scalar_one_or_none()
        if call is None:
            raise NotFoundError("Call not found")
        return call

    async def _conversation(self, conversation_id: UUID) -> Conversation:
        conversation = await TenantScopedRepository(self._session, Conversation, self._org_id).get(conversation_id)
        if conversation is None:
            raise NotFoundError("Conversation not found")
        return conversation


def _dropped_before_join(transfer: CallTransfer, attempt: int) -> bool:
    """The connected attempt's agent leg is over while the agent never entered the conference."""
    return (
        transfer.status == "connected"
        and attempt == transfer.attempt
        and transfer.agent_joined_at is None
        and transfer.ended_at is None
    )


def _connected_by(transfer: CallTransfer, attempt: int) -> bool:
    """The transfer is connected by this attempt's leg and the call is not over (an answer or accept repeated by a
    Twilio retry)."""
    return transfer.status == "connected" and attempt == transfer.attempt and attempt > 0 and transfer.ended_at is None


def _is_current(transfer: CallTransfer, attempt: int) -> bool:
    """The attempt is the one ringing now (a leg from an earlier attempt, or any leg once the transfer stopped
    ringing, is stale)."""
    return transfer.status == "ringing" and attempt == transfer.attempt and attempt > 0


def _now() -> datetime:
    return datetime.now(UTC)
