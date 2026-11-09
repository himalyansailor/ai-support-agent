"""Calling a customer back from the platform: an agent's browser (or the agent's phone, bridged) dials the number the
conversation asked to be called back on, from the organisation's own number. Each callback is a new outbound `Call`
row on the conversation (`source="callback"`, `end_reason="callback"`); an answered one marks the callback done.

`CallbackService` never calls Twilio, and the caller opens no session across a Twilio call (see the API route).
Never log or record a phone number here (the customer's, the agent's or ours): `CallbackPlan` keeps them out of its
repr, and only ids and statuses are logged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.analytics.events import record
from supportcore.billing.entitlements import EntitlementService
from supportcore.billing.errors import PlanLimitError
from supportcore.config import Settings
from supportcore.conversations.service import ConversationService
from supportcore.db.models import (
    Call,
    Conversation,
    Membership,
    PhoneNumber,
    TelephonyAccount,
    VoiceSettings,
)
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import NotFoundError
from supportcore.events import EventBus
from supportcore.inbox.publisher import InboxPublisher
from supportcore.observability.logging import get_logger
from supportcore.voice.calls import CallService
from supportcore.voice.errors import (
    InvalidVoiceTokenError,
    NoCallbackNumberError,
    VoiceNotAvailableError,
)
from supportcore.voice.telephony.ports import TelephonyAccountRef
from supportcore.voice.transfer_actions import load_account_ref
from supportcore.voice.transfers import caller_id_number

if TYPE_CHECKING:
    from supportcore.wiring import Adapters

__all__ = ["CALLBACK_RING_SECONDS", "CallbackPlan", "CallbackService", "browser_calling_ready"]

logger = get_logger(__name__)

CALLBACK_RING_SECONDS = 30  # how long the agent's phone (and then the customer's) may ring


@dataclass(frozen=True)
class CallbackPlan:
    """What a callback needs, validated. The numbers and the account are kept out of reprs."""

    customer_e164: str = field(repr=False)
    from_e164: str = field(repr=False)
    account_ref: TelephonyAccountRef = field(repr=False)
    record: bool  # Dial with `record` (the organisation records its calls)


async def browser_calling_ready(session: AsyncSession, organisation_id: UUID) -> bool:
    """The organisation's Twilio account has browser calling set up (API key and TwiML App)."""
    account = (
        await session.execute(TenantScopedRepository(session, TelephonyAccount, organisation_id).select())
    ).scalar_one_or_none()
    return account is not None and bool(
        account.api_key_sid and account.api_key_secret_encrypted and account.twiml_app_sid
    )


class CallbackService:
    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        events: EventBus,
        publisher: InboxPublisher,
        adapters: Adapters,
        settings: Settings,
    ) -> None:
        self._session = session
        self._org_id = organisation_id
        self._events = events
        self._publisher = publisher
        self._adapters = adapters
        self._settings = settings
        self._calls = TenantScopedRepository(session, Call, organisation_id)

    async def prepare(self, conversation_id: UUID, membership: Membership) -> CallbackPlan:
        """NotFoundError for another organisation's conversation; NoCallbackNumberError without a number to call;
        VoiceNotAvailableError without a Twilio account or an organisation number to call from (Assumption 10)."""
        conversation = await self._conversation(conversation_id)
        if not conversation.callback_phone:
            raise NoCallbackNumberError("This conversation has no number to call back")
        ref = await load_account_ref(self._session, self._adapters, self._org_id)
        from_e164 = await caller_id_number(self._session, self._org_id, conversation.id)
        if ref is None or from_e164 is None:
            raise VoiceNotAvailableError("Calls are not available right now")
        recording = (
            await self._session.execute(
                TenantScopedRepository(self._session, VoiceSettings, self._org_id).select(VoiceSettings.record_calls)
            )
        ).scalar_one_or_none()
        return CallbackPlan(conversation.callback_phone, from_e164, ref, bool(recording))

    async def check_voice_minutes(self) -> None:
        """PlanLimitError when the account is locked (no plan, trial ended, subscription ended): a new callback is
        not started. An account that is not locked may always call back, even with its voice minutes used up: the
        caller was promised a callback when the minutes ran out (the call is still metered). A call already
        connected is never cut off."""
        entitlements = EntitlementService(self._session, self._org_id, self._adapters)
        if not (await entitlements.state()).locked:
            return
        usage = await entitlements.meter("voice_minutes")
        if usage.reached:
            raise PlanLimitError("voice_minutes", usage.limit)

    async def start_outbound(
        self, conversation_id: UUID, membership: Membership, *, call_sid: str | None, ticket_jti: str | None
    ) -> Call:
        """The callback's `Call` (`ringing`). `ticket_jti` is stored (unique): a ticket starts one call only
        (InvalidVoiceTokenError), though a retried webhook for the same `call_sid` returns the same call.
        PlanLimitError when the plan's voice minutes are used up."""
        if call_sid is not None:
            existing = await self._by_sid(call_sid)
            if existing is not None:
                if ticket_jti is None or existing.ticket_jti != ticket_jti:
                    raise InvalidVoiceTokenError()
                return existing
        if ticket_jti is not None and await self._by_ticket(ticket_jti) is not None:
            raise InvalidVoiceTokenError()
        plan = await self.prepare(conversation_id, membership)
        await self.check_voice_minutes()
        number_id = (
            await self._session.execute(
                TenantScopedRepository(self._session, PhoneNumber, self._org_id)
                .select(PhoneNumber.id)
                .where(PhoneNumber.e164 == plan.from_e164)
            )
        ).scalar_one_or_none()
        call = Call(
            conversation_id=conversation_id,
            phone_number_id=number_id,
            source="callback",
            direction="outbound",
            end_reason="callback",
            agent_membership_id=membership.id,
            provider_call_sid=call_sid,
            ticket_jti=ticket_jti,
            status="ringing",
            started_at=datetime.now(UTC),
        )
        try:
            async with self._session.begin_nested():
                await self._calls.add(call)
                await self._session.flush()
        except IntegrityError:  # a concurrent use of the same ticket (or retry of the same sid) won
            raise InvalidVoiceTokenError() from None
        record(self._session, self._org_id, conversation_id, "call_started", channel="voice", data={"source": "callback"})
        logger.info("callback_started", call_id=str(call.id), conversation_id=str(conversation_id))
        return call

    async def set_sid(self, call_id: UUID, sid: str) -> None:
        """Stores the agent leg's Twilio sid once Twilio returned it."""
        call = await self._callback_call(call_id, for_update=True)
        call.provider_call_sid = sid
        await self._session.flush()

    async def dial_result(self, call_id: UUID, dial_status: str) -> None:
        """The `<Dial action>`: `completed` (the customer answered) marks the conversation's callback done while it
        is still `requested`; anything else leaves it for another try."""
        call = await self._callback_call(call_id)
        if dial_status != "completed":
            return
        conversations = ConversationService(self._session, self._org_id, self._events, publisher=self._publisher)
        conversation = await conversations.locked(call.conversation_id)
        if conversation.callback_status == "requested":
            await conversations.mark_callback_done(conversation)

    async def failed(self, call_id: UUID) -> None:
        """Twilio refused to place the agent leg: the call is over, as `failed`."""
        service = CallService(
            self._session,
            self._org_id,
            self._events,
            self._publisher,
            adapters=self._adapters,
            settings=self._settings,
        )
        await service.finalise(call_id, "failed", None)

    # --- internals ---------------------------------------------------------------------------------

    async def _conversation(self, conversation_id: UUID) -> Conversation:
        conversation = await TenantScopedRepository(self._session, Conversation, self._org_id).get(conversation_id)
        if conversation is None:
            raise NotFoundError("Conversation not found")
        return conversation

    async def _callback_call(self, call_id: UUID, *, for_update: bool = False) -> Call:
        call = await self._calls.get(call_id, for_update=for_update)
        if call is None or call.source != "callback":
            raise NotFoundError("Call not found")
        return call

    async def _by_sid(self, call_sid: str) -> Call | None:
        stmt = self._calls.select().where(Call.provider_call_sid == call_sid)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _by_ticket(self, jti: str) -> Call | None:
        stmt = self._calls.select().where(Call.ticket_jti == jti)
        return (await self._session.execute(stmt)).scalar_one_or_none()
