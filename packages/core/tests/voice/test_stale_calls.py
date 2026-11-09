"""finalise_stale_calls: the beat sweep that finalises calls neither the stream nor the status callback finished."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update
from supportcore.billing.memory_counter import MemoryUsageCounter
from supportcore.db.models import (
    Call,
    CallTransfer,
    Conversation,
    ConversationEvent,
    Organisation,
    PhoneNumber,
    TelephonyAccount,
    VoiceSettings,
    Widget,
)
from supportcore.events import EventBus
from supportcore.inbox.publisher import InboxPublisher
from supportcore.realtime.spawn import drain_pending
from supportcore.voice.calls import CallService
from supportcore.voice.jobs import finalise_stale_calls

CALLER = "+14155550123"


class NullPubSub:
    async def publish(self, channel, payload):
        pass


@pytest.fixture
def runtime(session_factory):
    return SimpleNamespace(
        session_factory=session_factory, events=EventBus(), adapters=SimpleNamespace(pubsub=NullPubSub(), usage_counter=MemoryUsageCounter())
    )


async def seed(session_factory, suffix: str, *, max_call_minutes: int | None = None):
    async with session_factory() as s:
        org = Organisation(name=f"Org {suffix}", slug=f"org-{suffix}")
        s.add(org)
        await s.flush()
        widget = Widget(organisation_id=org.id, name="W", public_key=f"pk-{suffix}")
        account = TelephonyAccount(
            organisation_id=org.id, kind="byo", account_sid="AC" + suffix * 32, auth_token_encrypted="x",
            auth_token_last4="xxxx",
        )
        s.add_all([widget, account])
        await s.flush()
        number = PhoneNumber(
            organisation_id=org.id, telephony_account_id=account.id, e164=f"+1500555000{len(suffix)}{ord(suffix) % 10}",
            provider_sid="PN" + suffix * 32, ownership="byo", widget_id=widget.id,
        )
        s.add(number)
        if max_call_minutes is not None:
            s.add(VoiceSettings(organisation_id=org.id, max_call_minutes=max_call_minutes))
        await s.flush()
        await s.commit()
        return org.id, number


async def start_call(runtime, org_id, number, sid: str, *, connect=True) -> Call:
    async with runtime.session_factory() as s:
        service = CallService(s, org_id, runtime.events, InboxPublisher(NullPubSub(), None))
        call = await service.start_phone_call(number, sid, CALLER)
        if connect:
            await service.mark_stream_connected(call.id)
        await s.commit()
        return call


async def provider_ended(runtime, org_id, call_id, status="completed", duration=42) -> bool:
    async with runtime.session_factory() as s:
        result = await CallService(s, org_id, runtime.events, InboxPublisher(NullPubSub(), None)).provider_ended(
            call_id, status, duration
        )
        await s.commit()
        return result


async def age(runtime, call_id, **columns_minutes_ago):
    now = datetime.now(UTC)
    values = {name: now - timedelta(minutes=minutes) for name, minutes in columns_minutes_ago.items()}
    async with runtime.session_factory() as s:
        await s.execute(update(Call).where(Call.id == call_id).values(**values))
        await s.commit()


async def get(runtime, call_id) -> Call:
    async with runtime.session_factory() as s:
        return await s.get(Call, call_id)


async def ended_count(runtime) -> int:
    async with runtime.session_factory() as s:
        return (
            await s.execute(select(func.count()).select_from(ConversationEvent).where(ConversationEvent.kind == "call_ended"))
        ).scalar_one()


async def test_status_stored_but_stream_never_ended_is_finalised_after_the_grace(runtime):
    org_id, number = await seed(runtime.session_factory, "a")
    fresh = await start_call(runtime, org_id, number, "CA" + "1" * 32)
    stale = await start_call(runtime, org_id, number, "CA" + "2" * 32)
    assert await provider_ended(runtime, org_id, fresh.id) is False  # deferred to the stream
    assert await provider_ended(runtime, org_id, stale.id, "completed", 37) is False
    assert (await get(runtime, stale.id)).provider_ended_at is not None
    await age(runtime, fresh.id, provider_ended_at=7)  # within STREAM_END_GRACE (8 min: a last turn may still be storing)
    await age(runtime, stale.id, provider_ended_at=9)

    assert await finalise_stale_calls(runtime) == 1
    await drain_pending()
    done, waiting = await get(runtime, stale.id), await get(runtime, fresh.id)
    assert (done.finalised, done.status, done.duration_seconds, done.end_reason) == (True, "completed", 37, "caller_hangup")
    assert waiting.finalised is False
    assert await ended_count(runtime) == 1
    assert await finalise_stale_calls(runtime) == 0  # idempotent


async def test_call_without_status_callback_is_finalised_as_failed_after_its_limit(runtime):
    org_id, number = await seed(runtime.session_factory, "b", max_call_minutes=5)
    old = await start_call(runtime, org_id, number, "CA" + "3" * 32)
    recent = await start_call(runtime, org_id, number, "CA" + "4" * 32, connect=False)
    await age(runtime, old.id, started_at=16)  # 5 + 10 minutes have passed
    await age(runtime, recent.id, started_at=14)

    assert await finalise_stale_calls(runtime) == 1
    await drain_pending()
    done = await get(runtime, old.id)
    assert (done.finalised, done.status, done.end_reason) == (True, "failed", "caller_hangup")
    assert (await get(runtime, recent.id)).finalised is False


async def test_default_limit_and_organisations_are_swept_separately(runtime):
    org_a, number_a = await seed(runtime.session_factory, "c")  # no voice settings: the 30-minute default
    org_b, number_b = await seed(runtime.session_factory, "d", max_call_minutes=120)
    a = await start_call(runtime, org_a, number_a, "CA" + "5" * 32)
    b = await start_call(runtime, org_b, number_b, "CA" + "6" * 32)
    await age(runtime, a.id, started_at=41)
    await age(runtime, b.id, started_at=41)

    assert await finalise_stale_calls(runtime) == 1
    await drain_pending()
    assert (await get(runtime, a.id)).finalised is True
    assert (await get(runtime, b.id)).finalised is False


async def test_dispatched_as_a_job(runtime, monkeypatch):
    from supportcore.knowledge import jobs as dispatch_module
    from supportcore.voice import jobs as voice_jobs

    seen = []

    async def sweep(rt):
        seen.append(rt)
        return 0

    monkeypatch.setattr(voice_jobs, "finalise_stale_calls", sweep)
    await dispatch_module.dispatch(runtime, "finalise_stale_calls", {})
    assert seen == [runtime]


# --- live transfers (Phase 7b, Task 7) ----------------------------------------------------------------------------


@pytest.fixture
def transfer_settings(tmp_path):
    from cryptography.fernet import Fernet
    from supportcore.config import Settings
    from supportcore.testing import TEST_DATABASE_URL

    return Settings(
        _env_file=None,
        env="test",
        database_url=TEST_DATABASE_URL,
        redis_url="redis://localhost:6379/0",
        jwt_secret="test-secret-test-secret-test-secret-123",
        smtp_from="noreply@test.local",
        secrets_key=Fernet.generate_key().decode(),
        file_storage="local",
        local_storage_path=str(tmp_path / "uploads"),
        vector_store="chroma",
        rate_limiter="memory",
        pubsub="memory",
        presence="memory",
        checkpointer="memory",
        voice_public_url="https://voice.example.com",
    )


@pytest.fixture
async def transfer_runtime(session_factory, transfer_settings):
    """A runtime with real (test) adapters: fake telephony, memory presence and pub/sub."""
    from dataclasses import replace

    from supportcore.voice.telephony.fake_telephony import FakeTelephony
    from supportcore.wiring import build_adapters

    adapters = replace(build_adapters(transfer_settings), telephony=FakeTelephony())
    yield SimpleNamespace(
        session_factory=session_factory, events=EventBus(), adapters=adapters, settings=transfer_settings
    )
    await drain_pending()
    await adapters.aclose()


async def seed_with_token(runtime, suffix: str, **kwargs):
    """`seed`, with an account token the sweep can decrypt (it runs the Twilio work)."""
    org_id, number = await seed(runtime.session_factory, suffix, **kwargs)
    async with runtime.session_factory() as s:
        await s.execute(
            update(TelephonyAccount)
            .where(TelephonyAccount.organisation_id == org_id)
            .values(auth_token_encrypted=runtime.adapters.cipher.encrypt("t" * 32))
        )
        await s.commit()
    return org_id, number


async def add_transfer(runtime, org_id, call: Call, **values) -> CallTransfer:
    async with runtime.session_factory() as s:
        transfer = CallTransfer(
            organisation_id=org_id,
            call_id=call.id,
            conversation_id=call.conversation_id,
            tried_membership_ids=[],
            **{"status": "ringing", "deadline_at": datetime.now(UTC) + timedelta(seconds=60), **values},
        )
        s.add(transfer)
        await s.commit()
        return transfer


async def get_transfer(runtime, transfer_id) -> CallTransfer:
    async with runtime.session_factory() as s:
        return await s.get(CallTransfer, transfer_id)


def twilio_updates(runtime) -> list[tuple]:
    """(call sid, twiml given, end) per update_call the sweep made."""
    return [(args[1], args[2] is not None, args[3]) for name, args in runtime.adapters.telephony.calls if name == "update_call"]


async def test_sweep_gives_up_stuck_transfer(transfer_runtime):
    runtime = transfer_runtime
    org_id, number = await seed_with_token(runtime, "e")
    stuck = await start_call(runtime, org_id, number, "CA" + "7" * 32)
    recent = await start_call(runtime, org_id, number, "CA" + "8" * 32)
    over = await start_call(runtime, org_id, number, "CA" + "9" * 32)
    now = datetime.now(UTC)
    leg = "CA" + "e" * 32
    stuck_t = await add_transfer(
        runtime, org_id, stuck, attempt=1, current_target="phone", current_leg_sid=leg,
        deadline_at=now - timedelta(seconds=61 + 5),
    )
    recent_t = await add_transfer(runtime, org_id, recent, attempt=1, deadline_at=now - timedelta(seconds=30))
    over_t = await add_transfer(runtime, org_id, over, attempt=1, deadline_at=now - timedelta(minutes=5))
    assert await provider_ended(runtime, org_id, over.id) is False  # the caller's leg is over (stream not ended)

    assert await finalise_stale_calls(runtime) == 0  # no call is finalised: the transfers are given up
    await drain_pending()

    assert (await get_transfer(runtime, stuck_t.id)).status == "no_answer"
    assert (await get_transfer(runtime, over_t.id)).status == "no_answer"
    assert (await get_transfer(runtime, recent_t.id)).status == "ringing"  # within deadline + 60 s: the watchdog's
    stuck_call = await get(runtime, stuck.id)
    assert stuck_call.end_reason == "transfer_no_answer"
    async with runtime.session_factory() as s:
        conversation = await s.get(Conversation, stuck.conversation_id)
    assert (conversation.callback_status, conversation.callback_phone) == ("requested", CALLER)
    # The ringing leg ended and the live caller redirected to the failure line; the caller already gone is not.
    assert sorted(twilio_updates(runtime)) == sorted([(leg, False, True), (stuck.provider_call_sid, True, False)])
    assert await finalise_stale_calls(runtime) == 0
    assert len(twilio_updates(runtime)) == 2  # idempotent


async def test_sweep_spares_connected_transfer_until_limit(transfer_runtime):
    runtime = transfer_runtime
    org_id, number = await seed_with_token(runtime, "f", max_call_minutes=5)
    live = await start_call(runtime, org_id, number, "CA" + "a" * 32)
    done = await start_call(runtime, org_id, number, "CA" + "b" * 32)
    plain = await start_call(runtime, org_id, number, "CA" + "c" * 32)
    now = datetime.now(UTC)
    connected = {"status": "connected", "attempt": 1, "deadline_at": now - timedelta(hours=6)}
    await add_transfer(runtime, org_id, live, **connected, connected_at=now - timedelta(hours=3, minutes=59))
    await add_transfer(runtime, org_id, done, **connected, connected_at=now - timedelta(hours=4, minutes=1))
    for call in (live, done, plain):
        await age(runtime, call.id, started_at=5 * 60)  # far past the 5-minute limit and its grace

    assert await finalise_stale_calls(runtime) == 2
    await drain_pending()
    assert (await get(runtime, live.id)).finalised is False  # an agent may still be talking to the caller
    assert (await get(runtime, done.id)).finalised is True
    assert (await get(runtime, plain.id)).finalised is True
    assert twilio_updates(runtime) == []


async def test_sweep_gives_callback_calls_the_transferred_call_limit(transfer_runtime):
    """A callback call is an agent talking to a customer: the bot's limit never applies, MAX_TRANSFERRED_CALL_HOURS
    does."""
    runtime = transfer_runtime
    org_id, number = await seed_with_token(runtime, "g", max_call_minutes=30)
    long_callback = await start_call(runtime, org_id, number, "CA" + "d" * 32, connect=False)
    lost_callback = await start_call(runtime, org_id, number, "CA" + "e" * 32, connect=False)
    for call in (long_callback, lost_callback):
        async with runtime.session_factory() as s:
            await s.execute(update(Call).where(Call.id == call.id).values(source="callback", direction="outbound"))
            await s.commit()
    await age(runtime, long_callback.id, started_at=45)  # past the bot's 30 + 10 minutes
    await age(runtime, lost_callback.id, started_at=4 * 60 + 1)

    assert await finalise_stale_calls(runtime) == 1
    await drain_pending()
    assert (await get(runtime, long_callback.id)).finalised is False
    assert (await get(runtime, lost_callback.id)).finalised is True
