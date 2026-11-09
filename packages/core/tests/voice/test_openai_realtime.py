import asyncio
import base64
import json

import pytest
import websockets
from supportcore.config import Settings
from supportcore.voice.realtime.openai_realtime import OpenAIRealtime
from supportcore.voice.realtime.ports import (
    AssistantTranscript,
    AudioOut,
    CallerSpeechStarted,
    CallerTranscript,
    RealtimeAuthError,
    RealtimeConfig,
    RealtimeError,
    RealtimeTool,
    ResponseDone,
    ResponseStarted,
    SayFailed,
    ToolCall,
)
from websockets.http11 import Response

pytestmark = pytest.mark.asyncio

TOOLS = (
    RealtimeTool("search_knowledge_base", "search", {"type": "object"}),
    RealtimeTool("transfer_to_human", "handoff", {"type": "object"}),
)


def _settings():
    return Settings(_env_file=None, env="test")


def _config(key="k"):
    return RealtimeConfig(
        provider="openai", model="gpt-realtime", voice="marin", language="en", instructions="be nice",
        tools=TOOLS, api_key=key,
    )  # fmt: skip


class Server:
    """A local fake OpenAI Realtime: records requests and client messages, runs `script(srv, ws)` per connection."""

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
                self.received.append(json.loads(m))

    async def __aenter__(self):
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0, process_request=self._process)
        self.url = f"ws://127.0.0.1:{self._server.sockets[0].getsockname()[1]}/v1/realtime"
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()


async def _wait_for(srv, n):
    for _ in range(200):
        if len(srv.received) >= n:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("client messages not received")


async def _open(srv, key="k"):
    return await OpenAIRealtime(_settings(), url=srv.url).open(_config(key))


async def test_session_update_sent_first():
    async with Server() as srv:
        stream = await _open(srv)
        await _wait_for(srv, 1)
        await stream.aclose()
    assert srv.requests[0].headers["Authorization"] == "Bearer k"
    assert srv.requests[0].path.endswith("?model=gpt-realtime")
    msg = srv.received[0]
    assert msg["type"] == "session.update"
    session = msg["session"]
    assert session["type"] == "realtime" and session["output_modalities"] == ["audio"]
    assert session["audio"]["input"]["format"]["type"] == "audio/pcmu"
    assert session["audio"]["input"]["transcription"] == {"model": "gpt-4o-mini-transcribe", "language": "en"}
    assert session["audio"]["input"]["turn_detection"]["interrupt_response"] is True
    assert session["audio"]["output"] == {"format": {"type": "audio/pcmu"}, "voice": "marin"}
    assert [t["name"] for t in session["tools"]] == ["search_knowledge_base", "transfer_to_human"]
    assert session["tool_choice"] == "auto" and session["instructions"] == "be nice"


async def test_audio_appended_base64():
    async with Server() as srv:
        stream = await _open(srv)
        await stream.send_audio(b"\xff" * 160)
        await _wait_for(srv, 2)
        await stream.aclose()
    assert srv.received[1] == {
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(b"\xff" * 160).decode(),
    }
    assert stream.audio_seconds == pytest.approx(0.02)


async def test_events_mapped():
    async def script(srv, ws):
        await ws.recv()
        for message in (
            {"type": "session.updated"},
            {"type": "input_audio_buffer.speech_started"},
            {"type": "response.created", "response": {"id": "r", "metadata": {"kind": "say", "say_id": "s1"}}},
            {"type": "response.created", "response": {"id": "r2", "metadata": None}},
            {"type": "conversation.item.input_audio_transcription.completed", "item_id": "i1", "transcript": "hi"},
            {"type": "response.output_audio.delta", "response_id": "r", "item_id": "i2",
             "delta": base64.b64encode(b"abc").decode()},
            {"type": "response.output_audio_transcript.done", "response_id": "r", "item_id": "i2", "transcript": "yo"},
            {"type": "response.function_call_arguments.done", "response_id": "r", "call_id": "c1",
             "name": "search_knowledge_base", "arguments": '{"query": "x"}'},
            {"type": "response.done", "response": {"id": "r", "status": "completed",
             "usage": {"input_tokens": 5, "output_tokens": 7}, "metadata": {"kind": "say", "say_id": "s1"}}},
            {"type": "response.done", "response": {"id": "r2", "status": "cancelled"}},
        ):  # fmt: skip
            await ws.send(json.dumps(message))
        await ws.wait_closed()

    async with Server(script) as srv:
        stream = await _open(srv)
        got = []
        async for event in stream.events():
            got.append(event)
            if len(got) == 9:
                break
        await stream.aclose()
    assert got == [
        CallerSpeechStarted(),
        ResponseStarted("r", True, "s1"),
        ResponseStarted("r2", False, None),
        CallerTranscript("i1", "hi"),
        AudioOut("r", "i2", b"abc"),
        AssistantTranscript("r", "i2", "yo"),
        ToolCall("r", "c1", "search_knowledge_base", {"query": "x"}),
        ResponseDone("r", "completed", 5, 7, True, "s1"),
        ResponseDone("r2", "cancelled", 0, 0, False),
    ]


async def test_tool_result_then_response_create():
    async with Server() as srv:
        stream = await _open(srv)
        await stream.send_tool_result("c1", {"ok": True})
        await stream.send_tool_result("c2", {"ok": True}, respond=False)
        await _wait_for(srv, 4)
        await stream.aclose()
    assert srv.received[1] == {
        "type": "conversation.item.create",
        "item": {"type": "function_call_output", "call_id": "c1", "output": '{"ok": true}'},
    }
    assert srv.received[2] == {"type": "response.create"}
    assert srv.received[3]["item"]["call_id"] == "c2"
    assert len(srv.received) == 4


async def test_say_is_out_of_band():
    async with Server() as srv:
        stream = await _open(srv)
        await stream.say("Hello there", "say-1")
        await _wait_for(srv, 2)
        await stream.aclose()
    response = srv.received[1]["response"]
    assert srv.received[1]["type"] == "response.create" and srv.received[1]["event_id"] == "say-1"
    assert response["conversation"] == "none" and response["metadata"] == {"kind": "say", "say_id": "say-1"}
    assert response["output_modalities"] == ["audio"]
    assert response["instructions"] == "Say exactly this, and nothing else: Hello there"


async def test_truncate_and_cancel_messages():
    async with Server() as srv:
        stream = await _open(srv)
        await stream.cancel_response()
        await stream.truncate("i1", 1200)
        await _wait_for(srv, 3)
        await stream.aclose()
    assert srv.received[1] == {"type": "response.cancel"}
    assert srv.received[2] == {
        "type": "conversation.item.truncate",
        "item_id": "i1",
        "content_index": 0,
        "audio_end_ms": 1200,
    }


@pytest.mark.parametrize("status", [401, 403])
async def test_handshake_401_raises_auth_error(status):
    async with Server(reject_status=status) as srv:
        with pytest.raises(RealtimeAuthError):
            await _open(srv)


async def test_other_handshake_failure_and_no_listener_are_connection_failed():
    async with Server(reject_status=500) as srv:
        with pytest.raises(RealtimeError) as err:
            await _open(srv)
    assert err.value.error_type == "connection_failed"
    with pytest.raises(RealtimeError) as err:
        await _open(srv)  # nothing listening any more
    assert err.value.error_type == "connection_failed"


async def _error_script(error):
    async def script(srv, ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "error", "error": error}))
        await ws.wait_closed()

    return script


async def test_error_event_invalid_api_key_raises_auth_error():
    async with Server(await _error_script({"type": "invalid_request_error", "code": "invalid_api_key"})) as srv:
        stream = await _open(srv)
        with pytest.raises(RealtimeAuthError):
            async for _ in stream.events():
                pass
        await stream.aclose()


@pytest.mark.parametrize("code", ["rate_limit_exceeded", "insufficient_quota"])
async def test_rate_limit_error_mapped(code):
    async with Server(await _error_script({"type": "error", "code": code})) as srv:
        stream = await _open(srv)
        with pytest.raises(RealtimeError) as err:
            async for _ in stream.events():
                pass
        await stream.aclose()
    assert err.value.error_type == "rate_limited"


async def test_benign_error_event_is_ignored():
    async def script(srv, ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                                                              "code": "response_cancel_not_active"}}))
        await ws.send(json.dumps({"type": "input_audio_buffer.speech_started"}))
        await ws.wait_closed()  # fmt: skip

    async with Server(script) as srv:
        stream = await _open(srv)
        assert await anext(stream.events()) == CallerSpeechStarted()
        await stream.aclose()


async def test_refused_say_is_reported():
    async def script(srv, ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                                                              "code": "conversation_already_has_active_response",
                                                              "event_id": "say-7"}}))
        await ws.wait_closed()  # fmt: skip

    async with Server(script) as srv:
        stream = await _open(srv)
        assert await anext(stream.events()) == SayFailed("say-7")
        await stream.aclose()


async def test_unexpected_close_raises_stream_error():
    async def script(srv, ws):
        await ws.recv()
        await ws.close()

    async with Server(script) as srv:
        stream = await _open(srv)
        with pytest.raises(RealtimeError) as err:
            async for _ in stream.events():
                pass
        assert err.value.error_type == "stream_error"
        with pytest.raises(RealtimeError):
            await stream.send_audio(b"x")
        await stream.aclose()


@pytest.mark.parametrize("arguments", ["not json", "", "[1, 2]", None])
async def test_bad_tool_arguments_become_empty_dict(arguments):
    async def script(srv, ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "response.function_call_arguments.done", "response_id": "r",
                                  "call_id": "c", "name": "transfer_to_human", "arguments": arguments}))
        await ws.wait_closed()  # fmt: skip

    async with Server(script) as srv:
        stream = await _open(srv)
        event = await anext(stream.events())
        await stream.aclose()
    assert event == ToolCall("r", "c", "transfer_to_human", {})


async def test_missing_key_is_auth_error():
    with pytest.raises(RealtimeAuthError):
        await OpenAIRealtime(_settings()).open(_config(key=None))
