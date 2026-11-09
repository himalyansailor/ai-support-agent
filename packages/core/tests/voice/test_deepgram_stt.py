import asyncio
import json
from urllib.parse import parse_qs, urlparse

import pytest
import websockets
from supportcore.config import Settings
from supportcore.voice.stt import deepgram_stt
from supportcore.voice.stt.deepgram_stt import DeepgramSpeechToText
from supportcore.voice.stt.ports import (
    SpeechStarted,
    SttAuthError,
    SttConfig,
    SttError,
    Transcript,
    UtteranceEnd,
)
from websockets.http11 import Response

pytestmark = pytest.mark.asyncio


def _settings():
    return Settings(_env_file=None, env="test")


def _config(key="k"):
    return SttConfig(provider="deepgram", model="nova-3", language="en", api_key=key)


def _results(text, *, is_final=True, speech_final=False):
    return json.dumps(
        {
            "type": "Results",
            "is_final": is_final,
            "speech_final": speech_final,
            "channel": {"alternatives": [{"transcript": text}]},
        }
    )


class Server:
    """A local fake Deepgram: records what it received and runs `script(ws)` per connection."""

    def __init__(self, script=None, reject_status=None):
        self.script = script
        self.reject_status = reject_status
        self.requests = []
        self.received = []

    async def _process(self, connection, request):
        self.requests.append(request)
        if self.reject_status:
            return Response(self.reject_status, "Rejected", websockets.Headers(), b"")
        return None

    async def _handler(self, ws):
        if self.script:
            await self.script(self, ws)
        else:
            async for m in ws:
                self.received.append(m)

    async def __aenter__(self):
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0, process_request=self._process)
        self.url = f"ws://127.0.0.1:{self._server.sockets[0].getsockname()[1]}/v1/listen"
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()


async def test_deepgram_sends_audio_and_maps_results():
    async def script(srv, ws):
        await ws.send(json.dumps({"type": "Metadata", "request_id": "x"}))
        await ws.send(json.dumps({"type": "SpeechStarted"}))
        await ws.send(_results("", is_final=False))  # empty, dropped
        await ws.send(_results("hello", is_final=False))
        await ws.send(_results("hello there", is_final=True, speech_final=True))
        await ws.send(_results("", is_final=True, speech_final=True))  # empty but speech_final, kept
        await ws.send(json.dumps({"type": "UtteranceEnd"}))
        async for m in ws:
            srv.received.append(m)

    async with Server(script) as srv:
        stream = await DeepgramSpeechToText(_settings(), url=srv.url).open(_config())
        await stream.send(b"\xff" * 160)
        got = []
        async for event in stream.events():
            got.append(event)
            if isinstance(event, UtteranceEnd):
                break
        await stream.aclose()
        assert got == [
            SpeechStarted(),
            Transcript("hello", False, False),
            Transcript("hello there", True, True),
            Transcript("", True, True),
            UtteranceEnd(),
        ]
        assert srv.received[0] == b"\xff" * 160
        assert stream.audio_seconds == pytest.approx(0.02)


async def test_deepgram_query_and_auth_header():
    async with Server() as srv:
        stream = await DeepgramSpeechToText(_settings(), url=srv.url).open(_config("k"))
        await stream.aclose()
    request = srv.requests[0]
    assert request.headers["Authorization"] == "Token k"
    query = {k: v[0] for k, v in parse_qs(urlparse(request.path).query).items()}
    assert query == {
        "model": "nova-3",
        "language": "en",
        "encoding": "mulaw",
        "sample_rate": "8000",
        "channels": "1",
        "interim_results": "true",
        "endpointing": "300",
        "utterance_end_ms": "1000",
        "vad_events": "true",
        "smart_format": "true",
        "punctuate": "true",
    }


@pytest.mark.parametrize("status", [401, 403])
async def test_deepgram_401_raises_auth_error(status):
    async with Server(reject_status=status) as srv:
        with pytest.raises(SttAuthError):
            await DeepgramSpeechToText(_settings(), url=srv.url).open(_config())


async def test_deepgram_other_handshake_failure_is_connection_failed():
    async with Server(reject_status=500) as srv:
        with pytest.raises(SttError) as err:
            await DeepgramSpeechToText(_settings(), url=srv.url).open(_config())
    assert err.value.error_type == "connection_failed"
    with pytest.raises(SttError) as err:  # nothing listening
        await DeepgramSpeechToText(_settings(), url="ws://127.0.0.1:1/v1/listen").open(_config())
    assert err.value.error_type == "connection_failed"


async def test_deepgram_unexpected_close_raises_stream_error():
    async def script(srv, ws):
        await ws.send(json.dumps({"type": "SpeechStarted"}))
        await ws.close()

    async with Server(script) as srv:
        stream = await DeepgramSpeechToText(_settings(), url=srv.url).open(_config())
        got = []
        with pytest.raises(SttError) as err:
            async for event in stream.events():
                got.append(event)
        assert got == [SpeechStarted()]
        assert err.value.error_type == "stream_error"
        await stream.aclose()


async def test_deepgram_keepalive_when_idle(monkeypatch):
    monkeypatch.setattr(deepgram_stt, "KEEPALIVE_SECONDS", 0.05)
    async with Server() as srv:
        stream = await DeepgramSpeechToText(_settings(), url=srv.url).open(_config())
        await asyncio.sleep(0.2)
        await stream.aclose()
    assert json.loads(srv.received[0]) == {"type": "KeepAlive"}


async def test_deepgram_no_keepalive_while_audio_flows(monkeypatch):
    monkeypatch.setattr(deepgram_stt, "KEEPALIVE_SECONDS", 0.2)
    async with Server() as srv:
        stream = await DeepgramSpeechToText(_settings(), url=srv.url).open(_config())
        for _ in range(8):
            await stream.send(b"\xff" * 160)
            await asyncio.sleep(0.05)
        await stream.aclose()
    assert all(isinstance(m, bytes) or json.loads(m)["type"] != "KeepAlive" for m in srv.received)


async def test_deepgram_close_sends_closestream():
    async def script(srv, ws):
        async for m in ws:
            srv.received.append(m)
            if isinstance(m, str) and json.loads(m)["type"] == "CloseStream":
                await ws.close()

    async with Server(script) as srv:
        stream = await DeepgramSpeechToText(_settings(), url=srv.url).open(_config())
        await stream.aclose()
        assert [json.loads(m) for m in srv.received] == [{"type": "CloseStream"}]
        assert [e async for e in stream.events()] == []  # ends cleanly, no error after CloseStream


async def test_deepgram_events_after_end_returns_immediately():
    async with Server() as srv:
        stream = await DeepgramSpeechToText(_settings(), url=srv.url).open(_config())
        await stream.aclose()
        assert [e async for e in stream.events()] == []
        assert await asyncio.wait_for(_drain(stream), timeout=1) == []


async def _drain(stream):
    return [e async for e in stream.events()]


async def test_deepgram_oversized_message_is_a_stream_error():
    """A misbehaving provider cannot make us buffer an unbounded message (cap: 1 MiB)."""

    async def script(srv, ws):
        await ws.send(json.dumps({"type": "Metadata", "padding": "x" * (2 * 1024 * 1024)}))
        await ws.send(_results("after", is_final=True, speech_final=True))
        async for m in ws:
            srv.received.append(m)

    async with Server(script) as srv:
        stream = await DeepgramSpeechToText(_settings(), url=srv.url).open(_config())
        with pytest.raises(SttError) as err:
            async with asyncio.timeout(5):
                async for _ in stream.events():
                    pass
        assert err.value.error_type == "stream_error"
        await stream.aclose()
