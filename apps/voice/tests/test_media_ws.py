"""WS /twilio/media (Phase 7a, Task 9): Starlette's TestClient plays Twilio against the ASGI app, with the fake STT
and TTS (`fake` providers) and the scripted chat model behind the real TurnRunner.

The whole app runs in the TestClient's portal thread (one event loop for the lifespan, the webhooks and the
sockets); the test thread seeds and inspects the database through its own engine. Every socket read is bounded."""

import asyncio
import base64
import json
import threading
import time
import uuid
import xml.etree.ElementTree as ET

import anyio
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, update
from starlette.testclient import TestClient
from supportcore.ai.chat.scripted_chat import CLASSIFIER_MARKER
from supportcore.ai.settings_service import OrganisationAISettingsService
from supportcore.db.models import (
    BotTurn,
    Call,
    Conversation,
    ConversationEvent,
    Message,
    RiskRule,
    User,
    VoiceSettings,
)
from supportcore.events import EventBus
from supportcore.observability.tracing import get_test_span_exporter
from supportcore.pubsub.ports import conversation_channel
from supportcore.risk.service import RiskRuleService
from supportcore.testing import script_chat
from supportcore.voice.strings import strings_for
from supportcore.voice.tokens import issue_stream_token
from supportvoice import media_ws
from supportvoice.main import create_app
from voice_helpers import (
    ACCOUNT_SID,
    CALLER_E164,
    NUMBER_E164,
    PUBLIC_URL,
    RecordingQueue,
    seed_org,
    signed_headers,
)

INBOUND = "/twilio/voice/inbound"
STATUS = "/twilio/voice/status"
AFTER_STREAM = "/twilio/voice/after-stream"
MEDIA = "/twilio/media"
CALL_SID = "CA" + "1" * 32
STREAM_SID = "MZ" + "2" * 32
EN = strings_for("en")
QUESTION = "What are your opening hours?"
REPLY = "We are open from nine to five on weekdays."
READ_TIMEOUT = 10.0


# --- fixtures ------------------------------------------------------------------------------------


@pytest.fixture
def ws_settings(voice_settings):
    return voice_settings.model_copy(update={"checkpointer": "memory"})


@pytest.fixture
async def ws_app(ws_settings, db_engine, session_factory):  # db_engine, session_factory: schema + truncate
    """The app without the shared fixture's teardown: the TestClient's lifespan closes the container (in the loop
    that ran it)."""
    application = create_app(ws_settings)
    application.state.container.adapters.job_queue = RecordingQueue()
    yield application
    await application.state.engine.dispose()


@pytest.fixture
def sessions_built(monkeypatch):
    """Every VoiceSession the media socket builds."""
    built = []
    original = media_ws.VoiceSession

    class Recorded(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            built.append(self)

    monkeypatch.setattr(media_ws, "VoiceSession", Recorded)
    return built


@pytest.fixture
def published(ws_app):
    """Every pub/sub publish (channel, message) the app makes."""
    pubsub = ws_app.state.container.adapters.pubsub
    original = pubsub.publish
    records: list[tuple[str, dict]] = []

    async def publish(channel, message):
        records.append((channel, message))
        await original(channel, message)

    pubsub.publish = publish
    return records


# --- helpers -------------------------------------------------------------------------------------


async def enable_chat(session_factory, organisation_id) -> None:
    """The scripted chat model answers; the widget has no knowledge base here, so the low-confidence rule (which
    would escalate every turn) is switched off."""
    async with session_factory() as s:
        owner = User(email=f"owner-{uuid.uuid4().hex[:8]}@x.com", password_hash="x", name="Owner")
        s.add(owner)
        await s.flush()
        await OrganisationAISettingsService(s, organisation_id, EventBus(), include_fake=True).update(
            "scripted", "scripted-chat", None, 0.2, owner
        )
        await RiskRuleService(s, organisation_id, EventBus()).list()  # seeds the default rules
        await s.execute(
            update(RiskRule)
            .where(RiskRule.organisation_id == organisation_id, RiskRule.builtin_key == "low_confidence")
            .values(enabled=False)
        )
        await s.commit()


async def ready_org(app, session_factory, *, record_calls=False):
    seed = await seed_org(app, session_factory)
    await enable_chat(session_factory, seed.organisation_id)
    if record_calls:
        async with session_factory() as s:
            await s.execute(update(VoiceSettings).values(record_calls=True))
            await s.commit()
    return seed


def place_call(tc: TestClient) -> str:
    """The inbound webhook; returns the stream token from the <Connect><Stream> TwiML."""
    params = {"AccountSid": ACCOUNT_SID, "CallSid": CALL_SID, "From": CALLER_E164, "To": NUMBER_E164}
    response = tc.post(INBOUND, data=params, headers=signed_headers(INBOUND, params))
    assert response.status_code == 200
    parameter = ET.fromstring(response.text).find("Connect/Stream/Parameter")
    assert parameter is not None, response.text
    return parameter.get("value")


def post_status(tc: TestClient, status: str = "completed", duration: str = "42"):
    params = {"AccountSid": ACCOUNT_SID, "CallSid": CALL_SID, "CallStatus": status, "CallDuration": duration}
    return tc.post(STATUS, data=params, headers=signed_headers(STATUS, params))


def recv(ws, timeout: float = READ_TIMEOUT) -> dict:
    """The next raw ASGI message the app sent, or a failure after `timeout` (never blocks forever)."""

    async def receive():
        with anyio.fail_after(timeout):
            return await ws._send_rx.receive()

    return ws.portal.call(receive)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def connected_event() -> dict:
    return {"event": "connected", "protocol": "Call", "version": "1.0.0"}


def start_event(token: str | None) -> dict:
    parameters = {} if token is None else {"token": token}
    return {
        "event": "start",
        "sequenceNumber": "1",
        "streamSid": STREAM_SID,
        "start": {
            "streamSid": STREAM_SID,
            "accountSid": ACCOUNT_SID,
            "callSid": CALL_SID,
            "tracks": ["inbound"],
            "customParameters": parameters,
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
        },
    }


def media_event(data: bytes) -> dict:
    return {
        "event": "media",
        "streamSid": STREAM_SID,
        "media": {"track": "inbound", "chunk": "1", "timestamp": "20", "payload": b64(data)},
    }


def stop_event() -> dict:
    return {"event": "stop", "streamSid": STREAM_SID, "stop": {"accountSid": ACCOUNT_SID, "callSid": CALL_SID}}


def open_stream(ws, token: str | None) -> None:
    ws.send_json(connected_event())
    ws.send_json(start_event(token))


def spoken_until_mark(ws, name: str) -> str:
    """Reads outbound events up to the mark `name` and returns the fake TTS text they carried. Marks are echoed
    back as played, like Twilio once its buffer has played them."""
    audio = b""
    while True:
        message = recv(ws)
        assert message["type"] == "websocket.send", message
        event = json.loads(message["text"])
        assert event["streamSid"] == STREAM_SID
        if event["event"] == "media":
            frame = base64.b64decode(event["media"]["payload"])
            assert len(frame) == 160
            audio += frame
        elif event["event"] == "mark":
            ws.send_json({"event": "mark", "streamSid": STREAM_SID, "mark": {"name": event["mark"]["name"]}})
            if event["mark"]["name"] == name:
                return audio.replace(b"\xff", b"").replace(b"FAKE-TTS:", b" ").decode().strip()


def expect_close(ws) -> int:
    """Reads until the app closes the socket; returns the close code."""
    while True:
        message = recv(ws)
        if message["type"] == "websocket.close":
            return message.get("code", 1000)


async def eventually(check, timeout: float = 10.0):
    """Polls the async `check` until it returns a truthy value."""
    deadline = time.monotonic() + timeout
    while True:
        value = await check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


async def only_call(session_factory) -> Call:
    async with session_factory() as s:
        return (await s.execute(select(Call))).scalar_one()


async def count_events(session_factory, kind: str) -> int:
    async with session_factory() as s:
        return (
            await s.execute(select(func.count()).select_from(ConversationEvent).where(ConversationEvent.kind == kind))
        ).scalar_one()


async def messages_of(session_factory, conversation_id) -> list[tuple[str, str]]:
    async with session_factory() as s:
        rows = (
            await s.execute(
                select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at)
            )
        ).scalars()
        return [(m.role, m.text) for m in rows]


def calls_running(app) -> int:
    return len(app.state.container._calls)


# --- the happy path ------------------------------------------------------------------------------


async def test_full_call_transcript_persisted(ws_app, session_factory, sessions_built, capsys):
    seed = await ready_org(ws_app, session_factory)
    script_chat([REPLY])
    exporter = get_test_span_exporter()
    exporter.clear()
    with TestClient(ws_app) as tc:
        token = place_call(tc)
        with tc.websocket_connect(MEDIA) as ws:
            open_stream(ws, token)
            greeting = spoken_until_mark(ws, "turn-0")
            assert greeting == EN["voice_greeting"].format(organisation="Acme a")
            assert ws_app.state.container.active_calls.count == 1
            ws.send_json(media_event(f"FAKE-STT:{QUESTION}".encode()))
            assert spoken_until_mark(ws, "turn-1") == REPLY
            ws.send_json(stop_event())
            assert expect_close(ws) == 1000
        ended = await eventually(lambda: _stream_ended_call(session_factory))
        assert not ended.finalised  # Twilio's status callback has not come yet
        await eventually(lambda: _true(calls_running(ws_app) == 0))
        assert ws_app.state.container.active_calls.count == 0
        assert post_status(tc).status_code == 204
        call = await only_call(session_factory)
    assert len(sessions_built) == 1
    assert (call.finalised, call.status, call.end_reason, call.duration_seconds) == (
        True, "completed", "caller_hangup", 42,
    )
    assert call.tts_characters > 0
    transcript = await messages_of(session_factory, call.conversation_id)
    assert ("customer", QUESTION) in transcript
    assert ("bot", REPLY) in transcript
    async with session_factory() as s:
        conversation = await s.get(Conversation, call.conversation_id)
    assert conversation.channel == "voice" and conversation.organisation_id == seed.organisation_id
    assert await count_events(session_factory, "call_ended") == 1
    assert ws_app.state.container.adapters.job_queue.jobs == []  # recording is off
    logs = capsys.readouterr().out
    for secret in (token, CALLER_E164, QUESTION, REPLY):
        assert secret not in logs
    connected_logs = [line for line in logs.splitlines() if '"media_stream_connected"' in line]
    assert connected_logs and all(str(seed.organisation_id) in line for line in connected_logs)
    spans = [span for span in exporter.get_finished_spans() if span.name == "voice.call"]
    assert len(spans) == 1
    assert dict(spans[0].attributes) == {
        "organisation.id": str(seed.organisation_id),
        "conversation.id": str(call.conversation_id),
        "call.id": str(call.id),
        "call.source": "phone",
    }


async def _finalised_call(session_factory):
    call = await only_call(session_factory)
    return call if call.finalised else None


async def _stream_ended_call(session_factory):
    call = await only_call(session_factory)
    return call if call.stream_ended_at is not None else None


async def call_ended_data(session_factory) -> list[dict]:
    async with session_factory() as s:
        return list(
            (await s.execute(select(ConversationEvent.data).where(ConversationEvent.kind == "call_ended"))).scalars()
        )


async def test_recording_job_enqueued_when_enabled(ws_app, session_factory):
    seed = await ready_org(ws_app, session_factory, record_calls=True)
    script_chat([])
    with TestClient(ws_app) as tc:
        token = place_call(tc)
        with tc.websocket_connect(MEDIA) as ws:
            open_stream(ws, token)
            spoken = spoken_until_mark(ws, "turn-0")
            assert spoken.endswith(EN["voice_recording_notice"])
            call = await only_call(session_factory)
            assert ws_app.state.container.adapters.job_queue.jobs == [
                (
                    "start_call_recording",
                    {
                        "organisation_id": str(seed.organisation_id),
                        "call_id": str(call.id),
                        # built here, where the public URL Twilio reaches is known (the worker may lack it)
                        "status_callback": f"{PUBLIC_URL}/twilio/recording/status?call_id={call.id}",
                    },
                )
            ]
            ws.send_json(stop_event())
            expect_close(ws)


async def test_inbox_receives_live_message_events(ws_app, session_factory, published):
    await ready_org(ws_app, session_factory)
    script_chat([REPLY])
    with TestClient(ws_app) as tc:
        token = place_call(tc)
        with tc.websocket_connect(MEDIA) as ws:
            open_stream(ws, token)
            spoken_until_mark(ws, "turn-0")
            ws.send_json(media_event(f"FAKE-STT:{QUESTION}".encode()))
            spoken_until_mark(ws, "turn-1")
            ws.send_json(stop_event())
            expect_close(ws)
        call = await eventually(lambda: _stream_ended_call(session_factory))
    channel = conversation_channel(call.conversation_id)
    bodies = [
        message["message"]["text"]
        for name, message in published
        if name == channel and message.get("type") == "message"
    ]
    assert QUESTION in bodies and REPLY in bodies


# --- refusals ------------------------------------------------------------------------------------


async def test_stream_token_single_use(ws_app, session_factory, sessions_built):
    await ready_org(ws_app, session_factory)
    script_chat([])
    with TestClient(ws_app) as tc:
        token = place_call(tc)
        with tc.websocket_connect(MEDIA) as ws:
            open_stream(ws, token)
            spoken_until_mark(ws, "turn-0")
            with tc.websocket_connect(MEDIA) as replay:  # the same token while the call is live
                open_stream(replay, token)
                assert expect_close(replay) == 1000
            ws.send_json(stop_event())
            expect_close(ws)
        await eventually(lambda: _stream_ended_call(session_factory))
        with tc.websocket_connect(MEDIA) as replay:  # and after it
            open_stream(replay, token)
            assert expect_close(replay) == 1000
        assert ws_app.state.container.active_calls.count == 0
    assert len(sessions_built) == 1


async def test_bad_token_closes_without_session(ws_app, session_factory, sessions_built, ws_settings):
    seed = await ready_org(ws_app, session_factory)
    with TestClient(ws_app) as tc:
        place_call(tc)
        call = await only_call(session_factory)
        forged = issue_stream_token(
            ws_settings.model_copy(update={"jwt_secret": SecretStr("another-secret-another-secret-12345")}),
            call.id,
            seed.organisation_id,
        )
        for token in (None, "not-a-token", forged):
            with tc.websocket_connect(MEDIA) as ws:
                open_stream(ws, token)
                assert expect_close(ws) == 1000
        assert ws_app.state.container.active_calls.count == 0
    assert sessions_built == []
    call = await only_call(session_factory)
    assert call.stream_connected_at is None and call.status == "ringing"


async def test_no_start_within_timeout_closes(ws_app, session_factory, sessions_built, monkeypatch):
    monkeypatch.setattr(media_ws, "START_TIMEOUT_SECONDS", 0.2)
    await ready_org(ws_app, session_factory)
    with TestClient(ws_app) as tc:
        token = place_call(tc)
        with tc.websocket_connect(MEDIA) as ws:
            ws.send_json(connected_event())
            started = time.monotonic()
            assert expect_close(ws) == 1000
            assert time.monotonic() - started < 3
        # the token was never used: it still connects
        with tc.websocket_connect(MEDIA) as ws:
            open_stream(ws, token)
            spoken_until_mark(ws, "turn-0")
            ws.send_json(stop_event())
            expect_close(ws)
    assert len(sessions_built) == 1


async def test_oversized_frame_closes(ws_app, session_factory, sessions_built):
    await ready_org(ws_app, session_factory)
    script_chat([])
    with TestClient(ws_app) as tc:
        # A message over 64 KiB, before the start event.
        with tc.websocket_connect(MEDIA) as ws:
            ws.send_json(connected_event())
            ws.send_text('{"event": "connected", "pad": "' + "x" * (64 * 1024) + '"}')
            assert expect_close(ws) == 1009
        assert sessions_built == []
        # An audio frame over 4 KiB, mid-call.
        token = place_call(tc)
        with tc.websocket_connect(MEDIA) as ws:
            open_stream(ws, token)
            spoken_until_mark(ws, "turn-0")
            ws.send_json(media_event(b"\x7f" * (4 * 1024 + 1)))
            assert expect_close(ws) == 1009
        await eventually(lambda: _stream_ended_call(session_factory))
        await eventually(lambda: _true(ws_app.state.container.active_calls.count == 0))


async def test_capacity_requests_callback_and_closes(ws_app, session_factory, sessions_built):
    await ready_org(ws_app, session_factory)
    active = ws_app.state.container.active_calls
    with TestClient(ws_app) as tc:
        token = place_call(tc)  # the webhook saw room; a burst fills the process before the stream connects
        assert active.acquire() and active.acquire()
        try:
            with tc.websocket_connect(MEDIA) as ws:
                open_stream(ws, token)
                assert expect_close(ws) == 1000
            assert active.count == 2
        finally:
            active.release()
            active.release()
        call = await only_call(session_factory)
        params = {"AccountSid": ACCOUNT_SID, "CallSid": CALL_SID}
        query = f"call_id={call.id}"
        after = tc.post(f"{AFTER_STREAM}?{query}", data=params, headers=signed_headers(AFTER_STREAM, params, query=query))
        assert post_status(tc).status_code == 204  # no session ran: the status callback alone finalises
    assert sessions_built == []
    assert call.end_reason == "capacity"
    assert (await only_call(session_factory)).finalised
    assert await count_events(session_factory, "call_ended") == 1
    async with session_factory() as s:
        conversation = await s.get(Conversation, call.conversation_id)
    assert (conversation.callback_status, conversation.status) == ("requested", "open")
    assert ET.fromstring(after.text).find("Say").text == EN["voice_unavailable"]


async def test_not_ready_at_stream_time_requests_callback(ws_app, session_factory, sessions_built):
    await ready_org(ws_app, session_factory)
    with TestClient(ws_app) as tc:
        token = place_call(tc)
        async with session_factory() as s:  # voice turned off between the webhook and the stream
            await s.execute(update(VoiceSettings).values(phone_enabled=False))
            await s.commit()
        with tc.websocket_connect(MEDIA) as ws:
            open_stream(ws, token)
            assert expect_close(ws) == 1000
        assert ws_app.state.container.active_calls.count == 0
        assert post_status(tc).status_code == 204
    assert sessions_built == []
    call = await only_call(session_factory)
    assert call.end_reason == "voice_unavailable"
    assert call.finalised and await count_events(session_factory, "call_ended") == 1


# --- the mode picks the engine (Phase 7b, Task 10) -------------------------------------------------


@pytest.fixture
def realtime_built(monkeypatch):
    """Every RealtimeVoiceSession the media socket builds."""
    built = []
    original = media_ws.RealtimeVoiceSession

    class Recorded(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            built.append(self)

    monkeypatch.setattr(media_ws, "RealtimeVoiceSession", Recorded)
    return built


async def set_speech_to_speech(session_factory, *, configured=True) -> None:
    values = {"mode": "speech_to_speech"}
    if configured:
        values |= {"realtime_provider": "fake", "realtime_model": "fake-realtime", "realtime_voice": "fake"}
    async with session_factory() as s:
        await s.execute(update(VoiceSettings).values(**values))
        await s.commit()


def realtime_audio_until(ws, needle: bytes, mark: str | None = None) -> bytes:
    """Reads outbound events (echoing marks as played) until the audio so far contains `needle` and, when given,
    the mark `mark` has come (so the line has played out and the caller's next words are not a barge-in)."""
    audio, marked = b"", mark is None
    while needle not in audio or not marked:
        message = recv(ws)
        assert message["type"] == "websocket.send", message
        event = json.loads(message["text"])
        if event["event"] == "media":
            audio += base64.b64decode(event["media"]["payload"])
        elif event["event"] == "mark":
            ws.send_json({"event": "mark", "streamSid": STREAM_SID, "mark": {"name": event["mark"]["name"]}})
            marked = marked or event["mark"]["name"] == mark
    return audio


async def test_speech_to_speech_call_end_to_end(ws_app, session_factory, sessions_built, realtime_built):
    seed = await ready_org(ws_app, session_factory)
    await set_speech_to_speech(session_factory)
    with TestClient(ws_app) as tc:
        token = place_call(tc)
        with tc.websocket_connect(MEDIA) as ws:
            open_stream(ws, token)
            realtime_audio_until(ws, b"FAKE-RT-SAY:", "turn-0")  # the greeting, spoken by the model
            ws.send_json(media_event(f"FAKE-RT:{QUESTION}".encode()))
            realtime_audio_until(ws, b"FAKE-RT-REPLY:" + QUESTION.encode())  # the model's reply, as audio frames
            ws.send_json(stop_event())
            assert expect_close(ws) == 1000
        await eventually(lambda: _stream_ended_call(session_factory))
        await eventually(lambda: _true(calls_running(ws_app) == 0))
        assert post_status(tc).status_code == 204
        call = await only_call(session_factory)
    assert sessions_built == [] and len(realtime_built) == 1
    assert call.mode == "speech_to_speech" and call.finalised
    transcript = await messages_of(session_factory, call.conversation_id)
    assert ("customer", QUESTION) in transcript
    assert ("bot", "Reply: " + QUESTION) in transcript
    assert [d["mode"] for d in await call_ended_data(session_factory)] == ["speech_to_speech"]
    async with session_factory() as s:
        conversation = await s.get(Conversation, call.conversation_id)
    assert conversation.organisation_id == seed.organisation_id


async def test_chained_still_default(ws_app, session_factory, sessions_built, realtime_built):
    await ready_org(ws_app, session_factory)
    script_chat([REPLY])
    with TestClient(ws_app) as tc:
        token = place_call(tc)
        with tc.websocket_connect(MEDIA) as ws:
            open_stream(ws, token)
            spoken_until_mark(ws, "turn-0")
            ws.send_json(media_event(f"FAKE-STT:{QUESTION}".encode()))
            assert spoken_until_mark(ws, "turn-1") == REPLY
            ws.send_json(stop_event())
            expect_close(ws)
        await eventually(lambda: _stream_ended_call(session_factory))
        await eventually(lambda: _true(calls_running(ws_app) == 0))
        assert post_status(tc).status_code == 204
        call = await only_call(session_factory)
    assert len(sessions_built) == 1 and realtime_built == []
    assert call.mode == "chained"
    assert [d["mode"] for d in await call_ended_data(session_factory)] == ["chained"]


async def test_realtime_not_configured_refuses_with_callback(ws_app, session_factory, sessions_built, realtime_built):
    await ready_org(ws_app, session_factory)
    with TestClient(ws_app) as tc:
        token = place_call(tc)
        await set_speech_to_speech(session_factory, configured=False)  # switched between the webhook and the stream
        with tc.websocket_connect(MEDIA) as ws:
            open_stream(ws, token)
            assert expect_close(ws) == 1000
        assert ws_app.state.container.active_calls.count == 0
        assert post_status(tc).status_code == 204
    assert sessions_built == [] and realtime_built == []
    call = await only_call(session_factory)
    assert call.end_reason == "voice_unavailable"
    assert call.finalised and await count_events(session_factory, "call_ended") == 1
    async with session_factory() as s:
        conversation = await s.get(Conversation, call.conversation_id)
    assert conversation.callback_status == "requested"


# --- hangup mid-turn -----------------------------------------------------------------------------


@pytest.mark.parametrize("status_first", [True, False], ids=["status-before-stream-end", "stream-end-before-status"])
async def test_hangup_mid_turn_persists_turn_and_finalises_once(ws_app, session_factory, status_first):
    """The caller hangs up while the bot is mid-reply. Whichever of Twilio's status callback and the end of the
    session comes last finalises the call: the reply is stored either way, and `call_ended` (once) has the usage."""
    await ready_org(ws_app, session_factory)
    entered, gate = threading.Event(), threading.Event()

    async def hold_the_reply(messages):
        if messages and CLASSIFIER_MARKER in str(messages[0].content):
            return
        entered.set()
        while not gate.is_set():
            await asyncio.sleep(0.01)

    script_chat([REPLY], hook=hold_the_reply)
    active = ws_app.state.container.active_calls
    try:
        with TestClient(ws_app) as tc:
            token = place_call(tc)
            with tc.websocket_connect(MEDIA) as ws:
                open_stream(ws, token)
                spoken_until_mark(ws, "turn-0")
                ws.send_json(media_event(f"FAKE-STT:{QUESTION}".encode()))
                assert entered.wait(READ_TIMEOUT)  # the model is mid-reply
                ws.send_json(stop_event())
                assert expect_close(ws) == 1000
            # The line is closed: the slot is free while the turn is still running.
            await eventually(lambda: _true(active.count == 0))
            assert calls_running(ws_app) == 1
            if status_first:
                assert post_status(tc).status_code == 204
                held = await only_call(session_factory)
                assert not held.finalised and held.stream_ended_at is None  # waits for the session
                assert await count_events(session_factory, "call_ended") == 0
                gate.set()
                await eventually(lambda: _finalised_call(session_factory))
            else:
                gate.set()
                ended = await eventually(lambda: _stream_ended_call(session_factory))
                assert not ended.finalised  # waits for Twilio's status
                assert post_status(tc).status_code == 204
            await eventually(lambda: _true(calls_running(ws_app) == 0))
            assert post_status(tc).status_code == 204  # a retried callback changes nothing
    finally:
        gate.set()
    call = await only_call(session_factory)
    assert (call.finalised, call.status, call.duration_seconds) == (True, "completed", 42)
    ended_events = await call_ended_data(session_factory)
    assert len(ended_events) == 1
    assert ended_events[0]["tts_characters"] > 0 and ended_events[0]["duration_seconds"] == 42
    async with session_factory() as s:
        turns = (await s.execute(select(BotTurn).where(BotTurn.conversation_id == call.conversation_id))).scalars().all()
        conversation = await s.get(Conversation, call.conversation_id)
    assert len(turns) == 1 and turns[0].reply_message_id is not None
    assert ("bot", REPLY) in await messages_of(session_factory, call.conversation_id)
    assert conversation.status == "resolved"


async def _true(value: bool) -> bool:
    return value


async def test_shutdown_cancels_running_sessions(ws_app, session_factory):
    await ready_org(ws_app, session_factory)
    entered = threading.Event()

    async def never_finish(messages):
        if messages and CLASSIFIER_MARKER in str(messages[0].content):
            return
        entered.set()
        await asyncio.sleep(3600)

    script_chat([REPLY], hook=never_finish)
    tc = TestClient(ws_app)
    with tc:
        token = place_call(tc)
        with tc.websocket_connect(MEDIA) as ws:
            open_stream(ws, token)
            spoken_until_mark(ws, "turn-0")
            ws.send_json(media_event(f"FAKE-STT:{QUESTION}".encode()))
            assert entered.wait(READ_TIMEOUT)
            ws.send_json(stop_event())
            expect_close(ws)
        assert calls_running(ws_app) == 1
        started = time.monotonic()
    assert time.monotonic() - started < 15  # not TURN_FINISH_TIMEOUT_SECONDS (360 s)
    assert calls_running(ws_app) == 0
    assert ws_app.state.container.active_calls.count == 0


async def test_shutdown_ends_a_live_call_at_once(ws_app, session_factory):
    await ready_org(ws_app, session_factory)
    script_chat([])
    container = ws_app.state.container
    with TestClient(ws_app) as tc:
        token = place_call(tc)
        with tc.websocket_connect(MEDIA) as ws:
            open_stream(ws, token)
            spoken_until_mark(ws, "turn-0")
            assert calls_running(ws_app) == 1
            started = time.monotonic()
            tc.portal.call(container.aclose)  # what the lifespan runs on SIGTERM
            elapsed = time.monotonic() - started
            assert expect_close(ws) == 1000
        container.aclose = _closed_already
    assert elapsed < 2  # cancelled, not left to time out
    assert calls_running(ws_app) == 0
    assert container.active_calls.count == 0


async def _closed_already() -> None:
    return None


# --- failures between connecting and finalising ----------------------------------------------------


async def test_stream_end_is_retried_once_after_a_database_error(ws_app, session_factory, monkeypatch):
    from supportcore.voice.calls import CallService

    monkeypatch.setattr(media_ws, "STREAM_END_RETRY_SECONDS", 0.01)
    original = CallService.stream_ended
    attempts = []

    async def flaky(self, call_id):
        attempts.append(call_id)
        if len(attempts) == 1:
            raise ConnectionError("database blip")
        return await original(self, call_id)

    monkeypatch.setattr(CallService, "stream_ended", flaky)
    await ready_org(ws_app, session_factory)
    script_chat([])
    with TestClient(ws_app) as tc:
        token = place_call(tc)
        with tc.websocket_connect(MEDIA) as ws:
            open_stream(ws, token)
            spoken_until_mark(ws, "turn-0")
            ws.send_json(stop_event())
            expect_close(ws)
        await eventually(lambda: _stream_ended_call(session_factory))
        assert post_status(tc).status_code == 204
    assert len(attempts) == 2
    assert (await only_call(session_factory)).finalised
    assert await count_events(session_factory, "call_ended") == 1


async def test_failure_before_the_session_starts_records_the_stream_end(ws_app, session_factory, monkeypatch):
    class Broken:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("cannot build the session")

    monkeypatch.setattr(media_ws, "VoiceSession", Broken)
    await ready_org(ws_app, session_factory)
    with TestClient(ws_app) as tc:
        token = place_call(tc)
        with tc.websocket_connect(MEDIA) as ws:
            open_stream(ws, token)
            assert expect_close(ws) == 1000
        assert ws_app.state.container.active_calls.count == 0
        call = await only_call(session_factory)
        assert call.stream_ended_at is not None and call.end_reason == "error"
        params = {"AccountSid": ACCOUNT_SID, "CallSid": CALL_SID}
        query = f"call_id={call.id}"
        after = tc.post(f"{AFTER_STREAM}?{query}", data=params, headers=signed_headers(AFTER_STREAM, params, query=query))
        assert ET.fromstring(after.text).find("Say").text == EN["voice_trouble"]
        assert post_status(tc).status_code == 204
    assert (await only_call(session_factory)).finalised
    assert await count_events(session_factory, "call_ended") == 1


async def test_recording_not_enqueued_when_the_connect_rolls_back(ws_app, session_factory, monkeypatch):
    from supportcore.db.uow import UnitOfWork

    class FailingCommit(UnitOfWork):
        async def commit(self):
            raise ConnectionError("commit failed")

    monkeypatch.setattr(media_ws, "UnitOfWork", FailingCommit)
    await ready_org(ws_app, session_factory, record_calls=True)
    with TestClient(ws_app) as tc:
        token = place_call(tc)
        with tc.websocket_connect(MEDIA) as ws:
            open_stream(ws, token)
            assert expect_close(ws) == 1000
        assert ws_app.state.container.active_calls.count == 0
    assert ws_app.state.container.adapters.job_queue.jobs == []
    assert (await only_call(session_factory)).stream_connected_at is None


# --- protocol edges over the real socket -----------------------------------------------------------


async def test_media_before_start_and_odd_messages_are_ignored(ws_app, session_factory, sessions_built):
    await ready_org(ws_app, session_factory)
    script_chat([REPLY])
    with TestClient(ws_app) as tc:
        token = place_call(tc)
        with tc.websocket_connect(MEDIA) as ws:
            ws.send_json(connected_event())
            ws.send_json(media_event(b"FAKE-STT:heard before the start"))  # nothing is listening yet
            ws.send_text("{not json")
            ws.send_bytes(b"\x00\xff binary")
            ws.send_json(start_event(token))
            spoken_until_mark(ws, "turn-0")
            ws.send_json(start_event("another-token"))  # a second start mid-call changes nothing
            ws.send_text("[" * 10000)
            ws.send_bytes(b"\x80\x81")
            ws.send_json(media_event(f"FAKE-STT:{QUESTION}".encode()))
            assert spoken_until_mark(ws, "turn-1") == REPLY
            ws.send_json(stop_event())
            assert expect_close(ws) == 1000
        call = await eventually(lambda: _stream_ended_call(session_factory))
    assert len(sessions_built) == 1
    transcript = await messages_of(session_factory, call.conversation_id)
    assert [text for role, text in transcript if role == "customer"] == [QUESTION]


# --- plan limits (Phase 8, Task 5) -----------------------------------------------------------------


async def trial_with_voice_minutes(app, session_factory, organisation_id, minutes: int) -> None:
    """A trial plan with `minutes` voice minutes, enforced (the fake provider grants nothing for free)."""
    from supportcore.billing.fake_billing import FakeBilling
    from supportcore.testing import start_trial_plan

    async with session_factory() as s:
        await start_trial_plan(s, organisation_id, voice_minutes=minutes)
        await s.commit()
    app.state.container.adapters.billing = FakeBilling()


async def test_media_connect_plan_limit_opens_callback(ws_app, session_factory, sessions_built):
    seed = await ready_org(ws_app, session_factory)
    await trial_with_voice_minutes(ws_app, session_factory, seed.organisation_id, 0)
    with TestClient(ws_app) as tc:
        token = place_call(tc)
        with tc.websocket_connect(MEDIA) as ws:
            open_stream(ws, token)
            assert expect_close(ws) == 1000
        assert ws_app.state.container.active_calls.count == 0
        call = await only_call(session_factory)
        params = {"AccountSid": ACCOUNT_SID, "CallSid": CALL_SID}
        query = f"call_id={call.id}"
        after = tc.post(f"{AFTER_STREAM}?{query}", data=params, headers=signed_headers(AFTER_STREAM, params, query=query))
        assert post_status(tc).status_code == 204  # no session ran: the status callback alone finalises
    assert sessions_built == []
    assert call.end_reason == "plan_limit"
    assert (await only_call(session_factory)).finalised
    async with session_factory() as s:
        conversation = await s.get(Conversation, call.conversation_id)
    assert (conversation.callback_status, conversation.status) == ("requested", "open")
    assert ET.fromstring(after.text).find("Say").text == EN["voice_unavailable"]


async def test_media_connect_locked_account_promises_no_callback(ws_app, session_factory, sessions_built):
    """I5: a locked account (its trial has ended) cannot call back, so the caller hears "follow up", not "call you
    back"."""
    from datetime import UTC, datetime, timedelta

    from supportcore.billing.fake_billing import FakeBilling
    from supportcore.testing import start_trial_plan

    seed = await ready_org(ws_app, session_factory)
    async with session_factory() as s:
        await start_trial_plan(
            s, seed.organisation_id, voice_minutes=100, started_at=datetime.now(UTC) - timedelta(days=20)
        )
        await s.commit()
    ws_app.state.container.adapters.billing = FakeBilling()
    with TestClient(ws_app) as tc:
        token = place_call(tc)
        with tc.websocket_connect(MEDIA) as ws:
            open_stream(ws, token)
            assert expect_close(ws) == 1000
        call = await only_call(session_factory)
        params = {"AccountSid": ACCOUNT_SID, "CallSid": CALL_SID}
        query = f"call_id={call.id}"
        after = tc.post(f"{AFTER_STREAM}?{query}", data=params, headers=signed_headers(AFTER_STREAM, params, query=query))
        assert post_status(tc).status_code == 204
    assert sessions_built == []
    assert call.end_reason == "plan_limit"
    said = ET.fromstring(after.text).find("Say").text
    assert said == EN["voice_unavailable_unknown"] and "call you back" not in said


async def test_connected_call_not_cut_when_limit_reached_mid_call(ws_app, session_factory, sessions_built):
    from types import SimpleNamespace

    from supportcore.billing.entitlements import EntitlementService
    from supportcore.billing.fake_billing import FakeBilling
    from supportcore.billing.memory_counter import MemoryUsageCounter
    from supportcore.billing.usage import record_usage

    seed = await ready_org(ws_app, session_factory)
    await trial_with_voice_minutes(ws_app, session_factory, seed.organisation_id, 1)
    script_chat([REPLY])
    with TestClient(ws_app) as tc:
        token = place_call(tc)
        with tc.websocket_connect(MEDIA) as ws:
            open_stream(ws, token)
            spoken_until_mark(ws, "turn-0")  # connected: 0 of 1 minute used
            async with session_factory() as s:  # another call's minutes land while this one runs
                await record_usage(s, seed.organisation_id, "voice_minutes", 5, uuid.uuid4(), counter=MemoryUsageCounter())
                await s.commit()
            async with session_factory() as s:  # the ledger says the limit is reached
                adapters = SimpleNamespace(billing=FakeBilling(), usage_counter=MemoryUsageCounter())
                assert (await EntitlementService(s, seed.organisation_id, adapters).meter("voice_minutes")).reached
            ws.send_json(media_event(f"FAKE-STT:{QUESTION}".encode()))
            assert spoken_until_mark(ws, "turn-1") == REPLY  # the session keeps running
            ws.send_json(stop_event())
            assert expect_close(ws) == 1000
        await eventually(lambda: _true(calls_running(ws_app) == 0))
        assert post_status(tc).status_code == 204
    assert len(sessions_built) == 1
    call = await only_call(session_factory)
    assert call.end_reason == "caller_hangup" and call.finalised
