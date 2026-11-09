import uuid

import pytest
from sqlalchemy import func, select
from supportcore.db.models import (
    Call,
    ConversationEvent,
    Customer,
    PhoneNumber,
    TelephonyAccount,
)
from supportcore.inbox.publisher import InboxPublisher
from supportcore.voice.calls import CallService
from supportcore.voice.errors import InvalidVoiceTokenError
from supportcore.voice.tokens import CallTicket

CALLER = "+14155550123"


class NullPubSub:
    async def publish(self, channel, payload):
        pass


@pytest.fixture
async def number(session, org, widget):
    account = TelephonyAccount(
        organisation_id=org.id, kind="byo", account_sid="AC" + "f" * 32, auth_token_encrypted="x", auth_token_last4="xxxx"
    )
    session.add(account)
    await session.flush()
    n = PhoneNumber(
        organisation_id=org.id, telephony_account_id=account.id, e164="+15005550006", provider_sid="PN" + "0" * 32,
        ownership="byo", widget_id=widget.id,
    )
    session.add(n)
    await session.flush()
    return n


@pytest.fixture
def service(session, org, bus):
    return CallService(session, org.id, bus, InboxPublisher(NullPubSub(), None))


async def kinds(session):
    return list((await session.execute(select(ConversationEvent.kind).order_by(ConversationEvent.occurred_at))).scalars())


async def test_start_phone_call_creates_conversation_and_is_idempotent(service, session, number):
    call = await service.start_phone_call(number, "CA" + "1" * 32, CALLER)
    assert (call.source, call.status, call.caller_e164, call.phone_number_id) == ("phone", "ringing", CALLER, number.id)
    again = await service.start_phone_call(number, "CA" + "1" * 32, CALLER)
    assert again.id == call.id
    assert (await session.execute(select(func.count()).select_from(Call))).scalar_one() == 1
    assert (await kinds(session)).count("call_started") == 1


async def test_withheld_caller_id_still_creates_a_call(service, session, number):
    call = await service.start_phone_call(number, "CA" + "2" * 32, None)
    assert call.caller_e164 is None
    assert (await session.execute(select(func.count()).select_from(Customer))).scalar_one() == 1


async def test_stream_connects_once(service, session, number):
    call = await service.start_phone_call(number, "CA" + "1" * 32, CALLER)
    connected = await service.mark_stream_connected(call.id)
    assert (connected.status, connected.stream_connected_at is not None, connected.answered_at is not None) == (
        "in_progress", True, True,
    )
    with pytest.raises(InvalidVoiceTokenError):
        await service.mark_stream_connected(call.id)


async def test_utterance_usage_and_finalise_once(service, session, number):
    call = await service.start_phone_call(number, "CA" + "1" * 32, CALLER)
    await service.mark_stream_connected(call.id)
    message = await service.add_caller_utterance(call.id, "my order is late")
    assert (message.role, message.text) == ("customer", "my order is late")
    await service.add_usage(call.id, 2.5, 40)
    await service.add_usage(call.id, 1.0, 10)
    await session.refresh(call)
    assert (call.stt_seconds, call.tts_characters) == (3.5, 50)

    await service.set_end_reason(call.id, "max_duration")
    await service.set_end_reason(call.id, "error")  # first one wins
    assert await service.finalise(call.id, "completed", 30) is True
    assert await service.finalise(call.id, "completed", 99) is False
    await session.refresh(call)
    assert (call.status, call.duration_seconds, call.end_reason, call.finalised) == ("completed", 30, "max_duration", True)
    assert (await kinds(session)).count("call_ended") == 1


async def test_finalise_derives_duration_from_answer_time(service, session, number):
    call = await service.start_phone_call(number, "CA" + "1" * 32, CALLER)
    await service.mark_stream_connected(call.id)
    assert await service.finalise(call.id, "completed", None)
    await session.refresh(call)
    assert call.duration_seconds == 0 and call.end_reason == "caller_hangup"


async def test_browser_ticket_is_single_use(service, session, org, widget):
    customer = Customer(organisation_id=org.id, visitor_id="v1")
    session.add(customer)
    await session.flush()
    ticket = CallTicket(org.id, widget.id, customer.id, "jti-1")
    call = await service.start_browser_call(ticket, "CA" + "1" * 32)
    assert call.source == "browser"
    assert (await service.start_browser_call(ticket, "CA" + "1" * 32)).id == call.id  # Twilio's retry
    with pytest.raises(InvalidVoiceTokenError):
        await service.start_browser_call(ticket, "CA" + "2" * 32)
    with pytest.raises(InvalidVoiceTokenError):
        await service.start_browser_call(CallTicket(org.id, widget.id, customer.id, "jti-2"), "CA" + "1" * 32)
    with pytest.raises(InvalidVoiceTokenError):  # a customer of nobody
        await service.start_browser_call(CallTicket(org.id, widget.id, uuid.uuid4(), "jti-3"), "CA" + "3" * 32)


async def test_upsert_for_a_rolled_back_conversation_publishes_nothing(session_factory):
    published = []

    class Recording:
        async def publish(self, channel, payload):
            published.append(payload)

    publisher = InboxPublisher(Recording(), session_factory)
    await publisher._publish_upsert(uuid.uuid4(), uuid.uuid4())
    assert published == []


# --- finalised by the last of: Twilio's status callback, the stream's end ----------------------------


async def test_status_before_stream_end_waits_for_the_stream(service, session, number):
    call = await service.start_phone_call(number, "CA" + "1" * 32, CALLER)
    await service.mark_stream_connected(call.id)
    assert await service.provider_ended(call.id, "completed", 42) is False  # the session is still running
    await session.refresh(call)
    assert (call.finalised, call.status, call.duration_seconds) == (False, "completed", 42)
    assert "call_ended" not in await kinds(session)
    await service.add_usage(call.id, 3.0, 25)
    assert await service.stream_ended(call.id) is True
    assert await service.stream_ended(call.id) is False
    assert await service.provider_ended(call.id, "completed", 42) is False
    await session.refresh(call)
    assert (call.finalised, call.status, call.duration_seconds, call.stream_ended_at is not None) == (
        True, "completed", 42, True,
    )
    ended = (await session.execute(select(ConversationEvent).where(ConversationEvent.kind == "call_ended"))).scalars().all()
    assert len(ended) == 1
    assert (ended[0].data["stt_seconds"], ended[0].data["tts_characters"], ended[0].data["duration_seconds"]) == (
        3.0, 25, 42,
    )


async def test_stream_end_before_status_waits_for_the_status(service, session, number):
    call = await service.start_phone_call(number, "CA" + "1" * 32, CALLER)
    await service.mark_stream_connected(call.id)
    assert await service.stream_ended(call.id) is False  # Twilio's terminal status is not known yet
    await session.refresh(call)
    assert (call.finalised, call.status) == (False, "in_progress")
    assert await service.provider_ended(call.id, "no_answer", 7) is True
    await session.refresh(call)
    assert (call.finalised, call.status, call.duration_seconds) == (True, "no_answer", 7)
    assert (await kinds(session)).count("call_ended") == 1


async def test_call_without_a_stream_finalises_on_the_status_alone(service, session, number):
    call = await service.start_phone_call(number, "CA" + "1" * 32, CALLER)
    assert await service.provider_ended(call.id, "busy", 0) is True
    await session.refresh(call)
    assert (call.finalised, call.status) == (True, "busy")
