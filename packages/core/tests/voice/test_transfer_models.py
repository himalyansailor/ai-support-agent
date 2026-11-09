import uuid
from datetime import UTC, datetime, timedelta

import pytest
import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from supportcore.config import Settings
from supportcore.db.models import (
    Call,
    CallTransfer,
    Conversation,
    Customer,
    Membership,
    VoiceSettings,
)
from supportcore.presence.memory_presence import MemoryAgentPresence
from supportcore.presence.redis_presence import RedisAgentPresence
from supportcore.voice.strings import STRINGS
from supportcore.voice.telephony.fake_telephony import FakeTelephony
from supportcore.voice.telephony.ports import (
    BrowserCalling,
    CallWebhooks,
    TelephonyAccountRef,
    TelephonyError,
)
from supportcore.wiring import build_adapters

ACCOUNT = TelephonyAccountRef("AC" + "a" * 32, "t" * 32)
CALLING = BrowserCalling("SK" + "1" * 32, "secret", "AP" + "2" * 32)
REDIS_URL = "redis://localhost:6380/0"


async def _conversation(session, org) -> Conversation:
    customer = Customer(organisation_id=org.id, phone="+15551230000")
    session.add(customer)
    await session.flush()
    conv = Conversation(organisation_id=org.id, customer_id=customer.id, channel="voice")
    session.add(conv)
    await session.flush()
    return conv


def _call(org, conv, *, sid, direction="inbound", source="phone") -> Call:
    return Call(
        organisation_id=org.id, conversation_id=conv.id, source=source, direction=direction, provider_call_sid=sid
    )


async def _membership(session, org) -> Membership:
    return (await session.execute(select(Membership).where(Membership.organisation_id == org.id))).scalars().first()


async def test_call_transfer_round_trip(session, org):
    conv = await _conversation(session, org)
    call = _call(org, conv, sid="CA" + "1" * 32)
    session.add(call)
    await session.flush()
    transfer = CallTransfer(
        organisation_id=org.id,
        call_id=call.id,
        conversation_id=conv.id,
        deadline_at=datetime.now(UTC) + timedelta(seconds=60),
    )
    session.add(transfer)
    await session.flush()
    await session.refresh(transfer)
    assert (transfer.status, transfer.attempt, transfer.tried_membership_ids) == ("ringing", 0, [])
    assert (transfer.current_membership_id, transfer.current_target, transfer.current_leg_sid) == (None, None, None)
    assert (transfer.connected_membership_id, transfer.connected_at, transfer.ended_at) == (None, None, None)
    transfer.tried_membership_ids = [str(uuid.uuid4())]
    transfer.current_target = "phone"
    await session.flush()
    session.add(
        CallTransfer(
            organisation_id=org.id, call_id=call.id, conversation_id=conv.id, deadline_at=datetime.now(UTC)
        )
    )
    with pytest.raises(IntegrityError):  # one transfer per call
        await session.flush()


async def test_call_transfer_status_and_target_checked(session, org):
    conv = await _conversation(session, org)
    call = _call(org, conv, sid="CA" + "1" * 32)
    session.add(call)
    await session.flush()
    session.add(
        CallTransfer(
            organisation_id=org.id, call_id=call.id, conversation_id=conv.id, deadline_at=datetime.now(UTC),
            status="bogus",
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_conversation_allows_one_inbound_and_many_outbound_calls(session, org):
    conv = await _conversation(session, org)
    session.add(_call(org, conv, sid="CA" + "1" * 32))
    session.add(_call(org, conv, sid="CA" + "2" * 32, direction="outbound", source="callback"))
    session.add(_call(org, conv, sid=None, direction="outbound", source="callback"))
    await session.flush()
    session.add(_call(org, conv, sid="CA" + "3" * 32))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_call_provider_sid_nullable_but_unique(session, org):
    conv = await _conversation(session, org)
    session.add_all(
        [
            _call(org, conv, sid=None, direction="outbound", source="callback"),
            _call(org, conv, sid=None, direction="outbound", source="callback"),
        ]
    )
    await session.flush()  # NULLs do not collide
    session.add(_call(org, conv, sid="CA" + "9" * 32, direction="outbound", source="callback"))
    await session.flush()
    session.add(_call(org, conv, sid="CA" + "9" * 32, direction="outbound", source="callback"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_call_new_columns_and_checks(session, org):
    conv = await _conversation(session, org)
    member = await _membership(session, org)
    call = _call(org, conv, sid="CA" + "1" * 32, direction="outbound", source="callback")
    call.mode, call.agent_membership_id, call.end_reason = "speech_to_speech", member.id, "transfer_no_answer"
    session.add(call)
    await session.flush()
    bad = _call(org, conv, sid="CA" + "2" * 32, direction="outbound", source="callback")
    bad.mode = "bogus"
    session.add(bad)
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_voice_settings_transfer_defaults(session, org):
    settings = VoiceSettings(organisation_id=org.id)
    session.add(settings)
    await session.flush()
    await session.refresh(settings)
    assert (settings.transfer_browser_enabled, settings.transfer_phone_enabled) == (True, False)
    assert (settings.realtime_provider, settings.realtime_model, settings.realtime_voice) == (None, None, None)
    assert settings.realtime_credential_id is None


async def test_membership_call_preference_check(session, org):
    member = await _membership(session, org)
    await session.refresh(member)
    assert member.call_preference == "browser"
    member.call_preference = "phone"
    await session.flush()
    member.call_preference = "pigeon"
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_fake_telephony_create_and_update_call():
    fake = FakeTelephony()
    hooks = CallWebhooks("https://v/answer", "https://v/status")
    sid = await fake.create_call(ACCOUNT, "+15551230000", "+15005550006", hooks, 20)
    assert sid.startswith("CA") and len(sid) == 34
    assert await fake.create_call(ACCOUNT, "+15551230000", "+1", hooks, 20) != sid
    with pytest.raises(TelephonyError) as exc:
        await fake.create_call(ACCOUNT, "client:refuse-me", "+1", hooks, 20)
    assert exc.value.error_type == "connection_failed"
    await fake.update_call(ACCOUNT, sid, twiml="<Response/>")
    await fake.update_call(ACCOUNT, sid, end=True)
    updates = [c for c in fake.calls if c[0] == "update_call"]
    assert [u[1][2:] for u in updates] == [("<Response/>", False), (None, True)]


def test_fake_incoming_token():
    fake = FakeTelephony()
    assert fake.browser_access_token(ACCOUNT.account_sid, CALLING, "agent-1", 60) == "fake-token:agent-1"
    token = fake.browser_access_token(ACCOUNT.account_sid, CALLING, "agent-1", 60, incoming=True)
    assert token == "fake-token:agent-1:in"


def _settings(**kw) -> Settings:
    return Settings(
        _env_file=None,
        env=kw.pop("env", "test"),
        database_url="postgresql+asyncpg://u:p@h/db",
        redis_url="redis://h:6379/0",
        jwt_secret="x" * 32,
        smtp_from="noreply@example.com",
        **kw,
    )


async def test_softphone_namespace_separate_from_presence():
    adapters = build_adapters(_settings(presence="memory"))
    org, member = uuid.uuid4(), uuid.uuid4()
    await adapters.softphones.beat(org, member)
    assert await adapters.softphones.present(org) == {member}
    assert await adapters.presence.present(org) == set()


@pytest.mark.integration
async def test_redis_softphone_key_is_namespaced_and_default_key_unchanged():
    client = aioredis.from_url(REDIS_URL)
    try:
        await client.ping()
    except Exception:  # noqa: BLE001
        await client.aclose()
        pytest.skip("Redis is not running")
    org, member = uuid.uuid4(), uuid.uuid4()
    try:
        await RedisAgentPresence(client, namespace="softphone").beat(org, member)
        assert await client.exists(f"softphone:{org.hex}") == 1
        assert await client.exists(f"presence:{org.hex}") == 0
        await RedisAgentPresence(client).beat(org, member)
        assert await client.exists(f"presence:{org.hex}") == 1
    finally:
        await client.delete(f"softphone:{org.hex}", f"presence:{org.hex}")
        await client.aclose()


async def test_memory_presence_default_namespace():
    assert MemoryAgentPresence().namespace == "presence"
    assert MemoryAgentPresence(namespace="softphone").namespace == "softphone"


def test_strings_complete():
    new_keys = {
        "voice_transferring",
        "voice_transfer_failed_phone",
        "voice_transfer_failed_browser",
        "voice_transfer_failed_unknown",
        "voice_agent_whisper",
        "voice_agent_taken",
        "voice_callback_connecting",
    }
    assert STRINGS
    for language, strings in STRINGS.items():
        assert new_keys <= set(strings), language
        assert "{organisation}" in strings["voice_agent_whisper"]
