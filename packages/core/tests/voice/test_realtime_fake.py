import asyncio

import pytest
from supportcore.config import Settings
from supportcore.voice.catalog import (
    credential_provider_for,
    get_realtime_spec,
    realtime_models,
)
from supportcore.voice.realtime.fake_realtime import FakeSpeechToSpeech
from supportcore.voice.realtime.ports import (
    AssistantTranscript,
    AudioOut,
    CallerSpeechStarted,
    CallerTranscript,
    RealtimeAuthError,
    RealtimeConfig,
    RealtimeError,
    ResponseDone,
    ResponseStarted,
    SayFailed,
    ToolCall,
)
from supportcore.wiring import build_adapters

CONFIG = RealtimeConfig(provider="fake", model="fake-realtime", voice="fake", language="en", instructions="SECRET")


async def _take(stream, n):
    events = []
    it = stream.events()
    for _ in range(n):
        events.append(await asyncio.wait_for(anext(it), 1))
    return events


def test_realtime_catalog():
    models = realtime_models("fr")
    assert [(m.provider, m.model) for m in models] == [("openai", "gpt-realtime"), ("openai", "gpt-realtime-mini")]
    recommended = [m for m in models if m.recommended]
    assert [m.model for m in recommended] == ["gpt-realtime"]
    spec = get_realtime_spec("openai", "gpt-realtime")
    assert spec.default_voice == "marin" and "cedar" in spec.voices and spec.requires_credential
    assert get_realtime_spec("openai", "gpt-realtime-mini").default_voice == "marin"
    assert all(m.provider != "fake" for m in models)
    assert [m.model for m in realtime_models("en", include_fake=True)][-1] == "fake-realtime"
    with pytest.raises(ValueError):
        get_realtime_spec("fake", "fake-realtime")
    fake = get_realtime_spec("fake", "fake-realtime", include_fake=True)
    assert (fake.requires_credential, fake.default_voice) == (False, "fake")
    assert credential_provider_for("openai") == "openai"


async def test_fake_realtime_reply_and_tool_call():
    stream = await FakeSpeechToSpeech().open(CONFIG)
    await stream.send_audio(b"FAKE-RT:hello there")
    events = await _take(stream, 6)
    assert isinstance(events[0], CallerSpeechStarted)
    assert isinstance(events[1], CallerTranscript) and events[1].text == "hello there"
    started = events[2]
    assert started == ResponseStarted(started.response_id, False, None)
    audio = b"".join(e.audio for e in events if isinstance(e, AudioOut))
    assert audio == b"FAKE-RT-REPLY:hello there"
    assert all(e.response_id == started.response_id for e in events if isinstance(e, AudioOut))
    assert events[-2] == AssistantTranscript(started.response_id, events[-2].item_id, "Reply: hello there")
    assert events[-1] == ResponseDone(started.response_id, "completed", 10, 20, False)

    await stream.send_audio(b"FAKE-RT:let me talk to someone [tool:transfer_to_human {\"reason\": \"asked\"}]")
    events = await _take(stream, 5)
    assert [type(e) for e in events[2:]] == [ResponseStarted, ToolCall, ResponseDone]
    assert (events[3].name, events[3].arguments) == ("transfer_to_human", {"reason": "asked"})
    assert events[2].response_id == events[3].response_id == events[4].response_id
    await stream.send_tool_result(events[3].call_id, {"ok": True})
    events = await _take(stream, 4)
    assert [type(e) for e in events] == [ResponseStarted, AudioOut, AssistantTranscript, ResponseDone]
    assert events[2].text == 'Tool said: {"ok": true}'

    await stream.send_audio(b"\xff" * 160)  # plain audio is only counted
    assert stream.audio_seconds == pytest.approx(0.02)
    await stream.cancel_response()
    await stream.truncate("item1", 800)
    assert (stream.cancelled, stream.truncations) == (1, [("item1", 800)])
    await stream.aclose()
    assert [e async for e in stream.events()] == []


async def test_fake_realtime_say_and_authfail():
    provider = FakeSpeechToSpeech()
    stream = await provider.open(CONFIG)
    assert provider.opened == [CONFIG] and "SECRET" not in repr(CONFIG)
    await stream.say("Please hold", "say-1")
    events = await _take(stream, 4)
    assert events[0] == ResponseStarted(events[0].response_id, True, "say-1")
    assert events[1] == AudioOut(events[0].response_id, events[1].item_id, b"FAKE-RT-SAY:Please hold")
    assert (events[3].out_of_band, events[3].say_id) == (True, "say-1")
    stream.reject_says = True
    await stream.say("Goodbye", "say-2")
    assert await _take(stream, 1) == [SayFailed("say-2")]
    await stream.send_audio(b"FAKE-RT-AUTHFAIL")
    with pytest.raises(RealtimeAuthError):
        await _take(stream, 1)

    dropped = await provider.open(CONFIG)
    await dropped.send_audio(b"FAKE-RT-DROP")
    with pytest.raises(RealtimeError) as exc:
        await _take(dropped, 1)
    assert exc.value.error_type == "stream_error"


def test_realtime_fakes_only_in_test_env():
    def settings(env):
        return Settings(
            _env_file=None,
            env=env,
            database_url="postgresql+asyncpg://u:p@h/db",
            redis_url="redis://h:6379/0",
            jwt_secret="x" * 32,
            smtp_from="noreply@example.com",
        )

    test = build_adapters(settings("test"))
    assert test.speech_to_speech.create("fake") is test.speech_to_speech.create("fake")
    assert isinstance(test.speech_to_speech.create("fake"), FakeSpeechToSpeech)
    with pytest.raises(ValueError):
        build_adapters(settings("dev")).speech_to_speech.create("fake")
