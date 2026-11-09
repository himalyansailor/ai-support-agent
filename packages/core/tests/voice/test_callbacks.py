"""CallbackService (Task 11): calling a customer back from the platform. Callback tokens are bound to one agent and
one conversation, a callback is a new outbound `Call` row on the same conversation, and an answered callback marks
the conversation's callback done."""

import uuid
from datetime import UTC, datetime

import jwt
import pytest
from sqlalchemy import select
from supportcore.db.models import (
    Call,
    Conversation,
    Membership,
    PhoneNumber,
    TelephonyAccount,
    VoiceSettings,
)
from supportcore.errors import NotFoundError
from supportcore.events import EventBus
from supportcore.inbox.publisher import InboxPublisher
from supportcore.inbox.summary import inbox_summary
from supportcore.orgs.service import OrganisationService
from supportcore.voice.callbacks import CallbackService
from supportcore.voice.calls import CallService
from supportcore.voice.errors import (
    InvalidVoiceTokenError,
    NoCallbackNumberError,
    VoiceNotAvailableError,
)
from supportcore.voice.tokens import (
    issue_call_ticket,
    issue_callback_ticket,
    issue_stream_token,
    verify_call_ticket,
    verify_callback_ticket,
)

CUSTOMER = "+14155550123"
NUMBER = "+15005550006"
OTHER_NUMBER = "+15005550007"


class NullPubSub:
    async def publish(self, channel, payload):
        pass


@pytest.fixture
def publisher():
    return InboxPublisher(NullPubSub(), None)


@pytest.fixture
async def membership(session, org, owner):
    return (await session.execute(select(Membership).where(Membership.organisation_id == org.id))).scalars().first()


@pytest.fixture
async def account(session, org, adapters):
    a = TelephonyAccount(
        organisation_id=org.id,
        kind="byo",
        account_sid="AC" + "f" * 32,
        auth_token_encrypted=adapters.cipher.encrypt("t" * 32),
        auth_token_last4="tttt",
    )
    session.add(a)
    await session.flush()
    return a


async def add_number(session, org, account, widget, e164=NUMBER, enabled=True):
    n = PhoneNumber(
        organisation_id=org.id,
        telephony_account_id=account.id,
        e164=e164,
        provider_sid="PN" + uuid.uuid4().hex,
        ownership="byo",
        widget_id=widget.id,
        enabled=enabled,
    )
    session.add(n)
    await session.flush()
    return n


@pytest.fixture
async def number(session, org, account, widget):
    return await add_number(session, org, account, widget)


@pytest.fixture
async def conversation(session, org, number, bus, publisher):
    """A conversation the customer phoned in on `number`, with a callback waiting."""
    calls = CallService(session, org.id, bus, publisher)
    call = await calls.start_phone_call(number, "CA" + "1" * 32, CUSTOMER)
    conv = (await session.execute(select(Conversation).where(Conversation.id == call.conversation_id))).scalar_one()
    conv.callback_status, conv.callback_phone = "requested", CUSTOMER
    await session.flush()
    return conv


@pytest.fixture
def service(session, org, bus, publisher, adapters, settings):
    return CallbackService(session, org.id, bus, publisher, adapters, settings)


# --- the ticket ----------------------------------------------------------------------------------------------


def test_ticket_bound_to_agent_and_conversation(settings):
    org_id, mid, conv_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    token, jti = issue_callback_ticket(settings, org_id, mid, conv_id)
    ticket = verify_callback_ticket(settings, token)
    assert (ticket.organisation_id, ticket.membership_id, ticket.conversation_id, ticket.jti) == (
        org_id, mid, conv_id, jti,
    )
    # Told apart from the other voice tokens by purpose, in both directions.
    call_token, _ = issue_call_ticket(settings, org_id, uuid.uuid4(), uuid.uuid4())
    for other in (call_token, issue_stream_token(settings, uuid.uuid4(), org_id), token + "x"):
        with pytest.raises(InvalidVoiceTokenError):
            verify_callback_ticket(settings, other)
    with pytest.raises(InvalidVoiceTokenError):
        verify_call_ticket(settings, token)


def test_ticket_expires_after_120_seconds(settings):
    token, _ = issue_callback_ticket(settings, uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    claims = jwt.decode(token, options={"verify_signature": False})
    assert 115 <= claims["exp"] - datetime.now(UTC).timestamp() <= 120


async def test_ticket_single_use(service, session, settings, conversation, membership, number):
    _, jti = issue_callback_ticket(settings, uuid.uuid4(), membership.id, conversation.id)
    first = await service.start_outbound(conversation.id, membership, call_sid="CA" + "2" * 32, ticket_jti=jti)
    assert first.ticket_jti == jti
    with pytest.raises(InvalidVoiceTokenError):
        await service.start_outbound(conversation.id, membership, call_sid="CA" + "3" * 32, ticket_jti=jti)
    assert len((await session.execute(select(Call).where(Call.direction == "outbound"))).scalars().all()) == 1


async def test_retried_webhook_returns_the_same_call(service, conversation, membership):
    a = await service.start_outbound(conversation.id, membership, call_sid="CA" + "2" * 32, ticket_jti="j1")
    b = await service.start_outbound(conversation.id, membership, call_sid="CA" + "2" * 32, ticket_jti="j1")
    assert a.id == b.id


# --- prepare ------------------------------------------------------------------------------------------------


async def test_prepare_uses_the_number_the_customer_called(service, session, org, account, widget, conversation, membership):
    await add_number(session, org, account, widget, e164=OTHER_NUMBER)
    plan = await service.prepare(conversation.id, membership)
    assert plan.customer_e164 == CUSTOMER and plan.from_e164 == NUMBER and plan.record is False
    assert CUSTOMER not in repr(plan) and NUMBER not in repr(plan)


async def test_prepare_falls_back_to_first_enabled_number_and_reports_recording(
    service, session, org, account, widget, conversation, membership, number
):
    await session.execute(Call.__table__.update().values(phone_number_id=None))
    await session.execute(PhoneNumber.__table__.update().where(PhoneNumber.id == number.id).values(enabled=False))
    await add_number(session, org, account, widget, e164=OTHER_NUMBER)
    session.add(VoiceSettings(organisation_id=org.id, record_calls=True))
    await session.flush()
    plan = await service.prepare(conversation.id, membership)
    assert plan.from_e164 == OTHER_NUMBER and plan.record is True


async def test_no_callback_phone_409(service, session, conversation, membership):
    conversation.callback_phone = None
    await session.flush()
    with pytest.raises(NoCallbackNumberError):
        await service.prepare(conversation.id, membership)


async def test_no_from_number_409(service, session, conversation, membership, number):
    await session.execute(Call.__table__.update().values(phone_number_id=None))
    await session.execute(PhoneNumber.__table__.update().values(enabled=False))
    with pytest.raises(VoiceNotAvailableError):
        await service.prepare(conversation.id, membership)


async def test_no_telephony_account_409(session, org, bus, publisher, adapters, settings, conversation, membership):
    await session.execute(PhoneNumber.__table__.delete())
    await session.execute(TelephonyAccount.__table__.delete())
    service = CallbackService(session, org.id, bus, publisher, adapters, settings)
    with pytest.raises(VoiceNotAvailableError):
        await service.prepare(conversation.id, membership)


async def test_cross_org_404(session, owner, conversation, membership, bus, publisher, adapters, settings):
    other = await OrganisationService(session, EventBus()).create(owner, "Other")
    service = CallbackService(session, other.id, bus, publisher, adapters, settings)
    with pytest.raises(NotFoundError):
        await service.prepare(conversation.id, membership)
    with pytest.raises(NotFoundError):
        await service.start_outbound(conversation.id, membership, call_sid="CA" + "9" * 32, ticket_jti="j9")


# --- the call ------------------------------------------------------------------------------------------------


async def test_callback_call_is_listed_on_conversation(service, session, org, conversation, membership, number):
    call = await service.start_outbound(conversation.id, membership, call_sid=None, ticket_jti=None)
    assert (call.direction, call.source, call.end_reason, call.status) == ("outbound", "callback", "callback", "ringing")
    assert (call.agent_membership_id, call.phone_number_id, call.conversation_id) == (
        membership.id, number.id, conversation.id,
    )
    assert call.provider_call_sid is None and call.caller_e164 is None and call.mode is None
    calls = (await session.execute(select(Call).where(Call.conversation_id == conversation.id))).scalars().all()
    assert sorted(c.direction for c in calls) == ["inbound", "outbound"]
    # The inbox list keeps showing the inbound call.
    summary = await inbox_summary(session, org.id, conversation.id)
    assert summary["call"]["source"] == "phone"
    await service.set_sid(call.id, "CA" + "4" * 32)
    await session.refresh(call)
    assert call.provider_call_sid == "CA" + "4" * 32


async def test_answered_callback_marks_done(service, session, conversation, membership):
    call = await service.start_outbound(conversation.id, membership, call_sid="CA" + "2" * 32, ticket_jti="j1")
    await service.dial_result(call.id, "completed")
    await session.refresh(conversation)
    assert conversation.callback_status == "done"


@pytest.mark.parametrize("dial_status", ["no-answer", "busy", "failed", "canceled", ""])
async def test_unanswered_callback_leaves_requested(service, session, conversation, membership, dial_status):
    call = await service.start_outbound(conversation.id, membership, call_sid="CA" + "2" * 32, ticket_jti="j1")
    await service.dial_result(call.id, dial_status)
    await session.refresh(conversation)
    assert conversation.callback_status == "requested"


async def test_answered_callback_without_a_waiting_callback_changes_nothing(service, session, conversation, membership):
    call = await service.start_outbound(conversation.id, membership, call_sid="CA" + "2" * 32, ticket_jti="j1")
    conversation.callback_status = "done"
    await session.flush()
    await service.dial_result(call.id, "completed")
    await session.refresh(conversation)
    assert conversation.callback_status == "done"


async def test_callback_allowed_at_voice_minutes_limit_when_not_locked(
    service, session, org, adapters, conversation, membership
):
    """I5: callers were promised a callback when the minutes ran out, so the team may make it (still metered)."""
    from supportcore.billing.fake_billing import FakeBilling
    from supportcore.testing import start_trial_plan

    await start_trial_plan(session, org.id, voice_minutes=0)
    adapters.billing = FakeBilling()
    await service.check_voice_minutes()
    call = await service.start_outbound(conversation.id, membership, call_sid=None, ticket_jti=None)
    assert call.direction == "outbound"


async def test_callback_refused_when_account_locked(service, session, org, adapters, conversation, membership):
    from datetime import UTC, datetime, timedelta

    from supportcore.billing.errors import PlanLimitError
    from supportcore.billing.fake_billing import FakeBilling
    from supportcore.testing import start_trial_plan

    await start_trial_plan(session, org.id, voice_minutes=100, started_at=datetime.now(UTC) - timedelta(days=20))
    adapters.billing = FakeBilling()  # the 14-day trial has ended: locked
    with pytest.raises(PlanLimitError) as raised:
        await service.start_outbound(conversation.id, membership, call_sid=None, ticket_jti=None)
    assert (raised.value.resource, raised.value.limit) == ("voice_minutes", 0)
    assert (await session.execute(select(Call).where(Call.direction == "outbound"))).scalars().all() == []
