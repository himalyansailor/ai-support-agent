"""Call recordings: start, fetch (Twilio -> our storage -> delete on Twilio), retries, and the daily purge."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import event, select
from supportcore.db.models import (
    Call,
    CallRecording,
    IntegrationEvent,
    Organisation,
    PhoneNumber,
    TelephonyAccount,
    VoiceSettings,
    Widget,
)
from supportcore.events import EventBus
from supportcore.inbox.publisher import InboxPublisher
from supportcore.secrets.fernet_cipher import FernetCipher
from supportcore.voice.calls import CallService
from supportcore.voice.recordings import (
    MAX_RECORDING_BYTES,
    fetch_call_recording,
    purge_call_recordings,
    start_call_recording,
)
from supportcore.voice.telephony.fake_telephony import FakeTelephony
from supportcore.voice.telephony.ports import TelephonyAuthError, TelephonyError

CALLER = "+14155550123"
PUBLIC_URL = "https://voice.example.com"
TOKEN = "t" * 32


class NullPubSub:
    async def publish(self, channel, payload):
        pass


class RecordingPubSub:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, channel, payload):
        self.published.append((channel, payload))


class MemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.before_put = None
        self.fail_put = False

    async def put(self, key, data, content_type):
        if self.fail_put:
            raise OSError("storage down")
        if self.before_put:
            await self.before_put()
        self.objects[key] = data

    async def get(self, key):
        return self.objects[key]

    async def stat_size(self, key):
        return len(self.objects[key])

    async def get_range(self, key, start, end):
        return self.objects[key][start : end + 1]

    async def delete(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)


class Queue:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, dict, float | None]] = []

    def enqueue(self, name, payload, *, countdown=None):
        self.jobs.append((name, payload, countdown))


class FlakyTelephony(FakeTelephony):
    def __init__(self) -> None:
        super().__init__()
        self.fail_download = False
        self.fail_delete = False
        self.fail_start = False
        self.auth_error = False
        self.before_download = None

    async def download_recording(self, account, recording_sid, max_bytes):
        if self.before_download:
            await self.before_download()
        if self.auth_error:
            raise TelephonyAuthError("rejected")
        if self.fail_download:
            raise TelephonyError("recording_failed", 500)
        return await super().download_recording(account, recording_sid, max_bytes)

    async def delete_recording(self, account, recording_sid):
        if self.fail_delete:
            raise TelephonyError("connection_failed")
        await super().delete_recording(account, recording_sid)

    async def start_recording(self, account, call_sid, status_callback):
        if self.fail_start:
            raise TelephonyError("recording_failed", 400)
        return await super().start_recording(account, call_sid, status_callback)


@pytest.fixture
def runtime(session_factory):
    cipher = FernetCipher(Fernet.generate_key().decode())
    adapters = SimpleNamespace(
        pubsub=RecordingPubSub(), telephony=FlakyTelephony(), storage=MemoryStorage(), job_queue=Queue(), cipher=cipher
    )
    settings = SimpleNamespace(voice_public_url=PUBLIC_URL)
    return SimpleNamespace(
        session_factory=session_factory, events=EventBus(), adapters=adapters, settings=settings
    )


async def seed(runtime, suffix="a", *, retention=90):
    async with runtime.session_factory() as s:
        org = Organisation(name=f"Org {suffix}", slug=f"org-{suffix}")
        s.add(org)
        await s.flush()
        widget = Widget(organisation_id=org.id, name="W", public_key=f"pk-{suffix}")
        account = TelephonyAccount(
            organisation_id=org.id, kind="byo", account_sid="AC" + suffix * 32,
            auth_token_encrypted=runtime.adapters.cipher.encrypt(TOKEN), auth_token_last4=TOKEN[-4:],
        )
        s.add_all([widget, account])
        await s.flush()
        number = PhoneNumber(
            organisation_id=org.id, telephony_account_id=account.id, e164=f"+1500555000{ord(suffix) % 10}",
            provider_sid="PN" + suffix * 32, ownership="byo", widget_id=widget.id,
        )
        s.add(VoiceSettings(organisation_id=org.id, record_calls=True, recording_retention_days=retention))
        s.add(number)
        await s.flush()
        service = CallService(s, org.id, runtime.events, InboxPublisher(NullPubSub(), None))
        call = await service.start_phone_call(number, "CA" + suffix * 32, CALLER)
        await service.mark_stream_connected(call.id)
        await s.commit()
        return org.id, call.id


async def recordings(runtime):
    async with runtime.session_factory() as s:
        return list((await s.execute(select(CallRecording))).scalars())


async def errors(runtime):
    async with runtime.session_factory() as s:
        return list((await s.execute(select(IntegrationEvent))).scalars())


async def test_start_stores_sid_and_passes_status_callback(runtime):
    org, call_id = await seed(runtime)
    await start_call_recording(runtime, org, call_id)
    async with runtime.session_factory() as s:
        call = (await s.execute(select(Call))).scalar_one()
    _, args = next(c for c in runtime.adapters.telephony.calls if c[0] == "start_recording")
    assert call.recording_sid and call.recording_sid.startswith("RE")
    assert args == ("AC" + "a" * 32, call.provider_call_sid, f"{PUBLIC_URL}/twilio/recording/status?call_id={call_id}")


async def test_start_uses_the_status_callback_the_voice_process_built(runtime):
    """The voice process owns the URL Twilio reaches; the worker's own (possibly stale) setting is not used."""
    org, call_id = await seed(runtime)
    runtime.settings.voice_public_url = "https://stale-tunnel.example.com"
    url = f"https://voice-now.example.com/twilio/recording/status?call_id={call_id}"
    await start_call_recording(runtime, org, call_id, status_callback=url)
    _, args = next(c for c in runtime.adapters.telephony.calls if c[0] == "start_recording")
    assert args[2] == url


async def test_start_without_a_callback_url_records_error_and_never_calls_twilio(runtime):
    """An old-shape payload (no status_callback) on a worker without SUPPORT_VOICE_PUBLIC_URL must not ask Twilio
    to post to a relative URL."""
    org, call_id = await seed(runtime)
    runtime.settings.voice_public_url = None
    await start_call_recording(runtime, org, call_id)
    assert [c for c in runtime.adapters.telephony.calls if c[0] == "start_recording"] == []
    (event,) = await errors(runtime)
    assert (event.kind, event.provider, event.error_type) == ("voice", "twilio", "recording_failed")
    async with runtime.session_factory() as s:
        assert (await s.execute(select(Call))).scalar_one().recording_sid is None


async def test_start_job_passes_the_payload_status_callback(runtime):
    from supportcore.knowledge.jobs import JOB_HANDLERS

    org, call_id = await seed(runtime)
    runtime.settings.voice_public_url = None
    url = f"https://voice-now.example.com/twilio/recording/status?call_id={call_id}"
    payload = {"organisation_id": str(org), "call_id": str(call_id), "status_callback": url}
    await JOB_HANDLERS["start_call_recording"](runtime, payload)
    _, args = next(c for c in runtime.adapters.telephony.calls if c[0] == "start_recording")
    assert args[2] == url


async def test_start_failure_records_error_without_retry(runtime):
    org, call_id = await seed(runtime)
    runtime.adapters.telephony.fail_start = True
    await start_call_recording(runtime, org, call_id)
    (event,) = await errors(runtime)
    assert (event.kind, event.provider, event.error_type) == ("voice", "twilio", "recording_failed")
    assert runtime.adapters.job_queue.jobs == []


async def test_fetch_stores_then_deletes_on_twilio(runtime):
    org, call_id = await seed(runtime, retention=30)
    runtime.adapters.telephony.recordings["RE" + "1" * 32] = b"MP3-BYTES"
    before = datetime.now(UTC)
    await fetch_call_recording(runtime, org, call_id, "RE" + "1" * 32)
    (rec,) = await recordings(runtime)
    assert rec.storage_key.startswith(f"orgs/{org}/calls/{call_id}/") and rec.storage_key.endswith(".mp3")
    assert runtime.adapters.storage.objects[rec.storage_key] == b"MP3-BYTES"
    assert (rec.size_bytes, rec.content_type) == (9, "audio/mpeg")
    assert timedelta(days=30) - timedelta(minutes=1) < rec.delete_after - before < timedelta(days=30, minutes=1)
    names = [c[0] for c in runtime.adapters.telephony.calls]
    assert names.index("download_recording") < names.index("delete_recording")
    assert runtime.adapters.telephony.calls[names.index("download_recording")][1][2] == MAX_RECORDING_BYTES == 200 * 1024 * 1024


async def test_fetch_publishes_an_inbox_upsert_after_storing(runtime):
    """An agent with the conversation open gets the recording player without reloading."""
    from supportcore.realtime.spawn import drain_pending

    org, call_id = await seed(runtime)
    runtime.adapters.telephony.recordings["RE" + "1" * 32] = b"MP3-BYTES"
    await fetch_call_recording(runtime, org, call_id, "RE" + "1" * 32)
    await drain_pending()
    async with runtime.session_factory() as s:
        conversation_id = (await s.execute(select(Call.conversation_id))).scalar_one()
    upserts = [m for _, m in runtime.adapters.pubsub.published if m.get("type") == "conversation.upsert"]
    assert [m["conversation"]["id"] for m in upserts] == [str(conversation_id)]


async def test_fetch_is_idempotent(runtime):
    org, call_id = await seed(runtime)
    await fetch_call_recording(runtime, org, call_id, "RE" + "1" * 32)
    await fetch_call_recording(runtime, org, call_id, "RE" + "1" * 32)
    assert len(await recordings(runtime)) == 1
    assert [c[0] for c in runtime.adapters.telephony.calls].count("download_recording") == 1
    assert len(runtime.adapters.storage.objects) == 1


async def test_fetch_twilio_delete_failure_is_not_retried(runtime):
    org, call_id = await seed(runtime)
    runtime.adapters.telephony.fail_delete = True
    await fetch_call_recording(runtime, org, call_id, "RE" + "1" * 32)
    assert len(await recordings(runtime)) == 1
    assert runtime.adapters.job_queue.jobs == []


async def test_fetch_retries_then_records_error(runtime):
    org, call_id = await seed(runtime)
    runtime.adapters.telephony.fail_download = True
    sid = "RE" + "1" * 32
    for attempt, countdown in ((1, 60), (2, 300), (3, 900)):
        runtime.adapters.job_queue.jobs.clear()
        await fetch_call_recording(runtime, org, call_id, sid, attempt)
        (name, payload, delay) = runtime.adapters.job_queue.jobs[0]
        assert name == "fetch_call_recording" and delay == countdown and payload["attempt"] == attempt + 1
        assert payload == {
            "organisation_id": str(org), "call_id": str(call_id), "recording_sid": sid, "attempt": attempt + 1,
        }
        assert await errors(runtime) == []
    runtime.adapters.job_queue.jobs.clear()
    await fetch_call_recording(runtime, org, call_id, sid, 4)
    assert runtime.adapters.job_queue.jobs == []
    (event,) = await errors(runtime)
    assert (event.kind, event.provider, event.error_type) == ("voice", "twilio", "recording_failed")
    assert await recordings(runtime) == []


async def test_storage_put_outside_session(runtime, db_engine):
    org, call_id = await seed(runtime)
    out = {"n": 0}

    def on_checkout(*_):
        out["n"] += 1

    def on_checkin(*_):
        out["n"] -= 1

    event.listen(db_engine.sync_engine, "checkout", on_checkout)
    event.listen(db_engine.sync_engine, "checkin", on_checkin)
    baseline = out["n"]
    seen: list[int] = []

    async def probe():
        seen.append(out["n"])

    runtime.adapters.telephony.before_download = probe
    runtime.adapters.storage.before_put = probe
    try:
        await fetch_call_recording(runtime, org, call_id, "RE" + "1" * 32)
    finally:
        event.remove(db_engine.sync_engine, "checkout", on_checkout)
        event.remove(db_engine.sync_engine, "checkin", on_checkin)
    assert seen == [baseline, baseline]


async def add_recording(runtime, org, call_id, *, expired: bool):
    key = f"orgs/{org}/calls/{call_id}/{'old' if expired else 'new'}.mp3"
    runtime.adapters.storage.objects[key] = b"x"
    delta = timedelta(days=-1) if expired else timedelta(days=1)
    async with runtime.session_factory() as s:
        s.add(
            CallRecording(
                organisation_id=org, call_id=call_id, storage_key=key, size_bytes=1, duration_seconds=3,
                delete_after=datetime.now(UTC) + delta,
            )
        )
        await s.commit()
    return key


async def test_purge_deletes_expired_only(runtime):
    org1, call1 = await seed(runtime, "a")
    org2, call2 = await seed(runtime, "b")
    old = await add_recording(runtime, org1, call1, expired=True)
    new = await add_recording(runtime, org2, call2, expired=False)
    assert await purge_call_recordings(runtime) == 1
    rows = await recordings(runtime)
    assert [r.storage_key for r in rows] == [new]
    assert old not in runtime.adapters.storage.objects and new in runtime.adapters.storage.objects
    assert runtime.adapters.storage.deleted == [old]


async def test_purge_keeps_row_when_storage_delete_fails(runtime):
    org, call_id = await seed(runtime)
    await add_recording(runtime, org, call_id, expired=True)

    async def boom(key):
        raise OSError("down")

    runtime.adapters.storage.delete = boom
    assert await purge_call_recordings(runtime) == 0
    assert len(await recordings(runtime)) == 1


async def test_fetch_storage_put_failure_follows_retry_ladder(runtime):
    org, call_id = await seed(runtime)
    runtime.adapters.storage.fail_put = True
    sid = "RE" + "1" * 32
    await fetch_call_recording(runtime, org, call_id, sid, 1)
    assert [(j[0], j[2]) for j in runtime.adapters.job_queue.jobs] == [("fetch_call_recording", 60)]
    runtime.adapters.job_queue.jobs.clear()
    await fetch_call_recording(runtime, org, call_id, sid, 4)
    assert runtime.adapters.job_queue.jobs == []
    (event_row,) = await errors(runtime)
    assert event_row.error_type == "recording_failed"
    assert await recordings(runtime) == []


async def test_fetch_auth_error_not_retried_records_auth_failed(runtime):
    org, call_id = await seed(runtime)
    runtime.adapters.telephony.auth_error = True
    await fetch_call_recording(runtime, org, call_id, "RE" + "1" * 32)
    assert runtime.adapters.job_queue.jobs == []
    (event_row,) = await errors(runtime)
    assert (event_row.kind, event_row.provider, event_row.error_type) == ("voice", "twilio", "auth_failed")


async def test_fetch_without_account_records_recording_failed(runtime):
    org, call_id = await seed(runtime)
    async with runtime.session_factory() as s:
        account = (await s.execute(select(TelephonyAccount))).scalar_one()
        account.auth_token_encrypted = "garbage"
        await s.commit()
    await fetch_call_recording(runtime, org, call_id, "RE" + "1" * 32)
    (event_row,) = await errors(runtime)
    assert event_row.error_type == "recording_failed"
    assert runtime.adapters.job_queue.jobs == []


async def test_fetch_stores_twilio_duration_with_fallback(runtime):
    org, call_id = await seed(runtime)
    await fetch_call_recording(runtime, org, call_id, "RE" + "1" * 32, recording_duration=37)
    (rec,) = await recordings(runtime)
    assert rec.duration_seconds == 37
    org2, call2 = await seed(runtime, "b")
    async with runtime.session_factory() as s:
        call = (await s.execute(select(Call).where(Call.id == call2))).scalar_one()
        call.duration_seconds = 55
        await s.commit()
    await fetch_call_recording(runtime, org2, call2, "RE" + "2" * 32)
    rows = {r.call_id: r for r in await recordings(runtime)}
    assert rows[call2].duration_seconds == 55


async def test_retry_payload_keeps_duration(runtime):
    org, call_id = await seed(runtime)
    runtime.adapters.telephony.fail_download = True
    await fetch_call_recording(runtime, org, call_id, "RE" + "1" * 32, 1, recording_duration=9)
    assert runtime.adapters.job_queue.jobs[0][1]["recording_duration"] == 9


async def test_fetch_concurrent_duplicate_deletes_loser_object(runtime):
    org, call_id = await seed(runtime)
    sid = "RE" + "1" * 32

    async def other_wins():  # a duplicate callback stores its recording while this one is uploading
        async with runtime.session_factory() as s:
            s.add(
                CallRecording(
                    organisation_id=org, call_id=call_id, storage_key="orgs/other.mp3", size_bytes=1,
                    duration_seconds=1, delete_after=datetime.now(UTC) + timedelta(days=1),
                )
            )
            await s.commit()

    runtime.adapters.storage.before_put = other_wins
    await fetch_call_recording(runtime, org, call_id, sid)
    (rec,) = await recordings(runtime)
    assert rec.storage_key == "orgs/other.mp3"
    assert len(runtime.adapters.storage.deleted) == 1  # the loser removed its own object
    assert [c[0] for c in runtime.adapters.telephony.calls].count("delete_recording") == 0


async def test_start_is_noop_when_already_started(runtime):
    org, call_id = await seed(runtime)
    await start_call_recording(runtime, org, call_id)
    await start_call_recording(runtime, org, call_id)
    assert [c[0] for c in runtime.adapters.telephony.calls].count("start_recording") == 1


async def test_purge_handles_more_than_one_batch(runtime, monkeypatch):
    from supportcore.voice import recordings as module

    monkeypatch.setattr(module, "PURGE_BATCH", 2)
    org, first = await seed(runtime)
    async with runtime.session_factory() as s:
        number = (await s.execute(select(PhoneNumber))).scalar_one()
        service = CallService(s, org, runtime.events, InboxPublisher(NullPubSub(), None))
        calls = [first]
        for i in range(4):
            calls.append((await service.start_phone_call(number, f"CA{i:032d}", CALLER)).id)
        for i, cid in enumerate(calls):
            s.add(
                CallRecording(
                    organisation_id=org, call_id=cid, storage_key=f"k/{i}.mp3", size_bytes=1, duration_seconds=1,
                    delete_after=datetime.now(UTC) - timedelta(days=1),
                )
            )
        await s.commit()
    assert await purge_call_recordings(runtime) == 5
    assert await recordings(runtime) == []
