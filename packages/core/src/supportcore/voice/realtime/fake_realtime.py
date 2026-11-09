"""In-memory SpeechToSpeech for tests (registered only when env == "test").

A frame starting with b"FAKE-RT:" makes the stream emit CallerSpeechStarted and a CallerTranscript of the UTF-8 text
after the prefix. If the text holds "[tool:NAME {json}]" a response with a ToolCall follows (ResponseStarted, ToolCall,
ResponseDone); otherwise the reply: ResponseStarted, AudioOut (b"FAKE-RT-REPLY:" + text, in 160-byte chunks),
AssistantTranscript("Reply: " + text) and ResponseDone. `say()` answers the same way (out of band, with its say_id),
or with SayFailed while `reject_says` is set.
b"FAKE-RT-AUTHFAIL" makes events() raise RealtimeAuthError, b"FAKE-RT-DROP" RealtimeError("stream_error").
Any other frame is counted as 20 ms of audio.
"""

import asyncio
import json
import re
from collections.abc import AsyncIterator, Mapping
from typing import Any

from supportcore.voice.realtime.ports import (
    AssistantTranscript,
    AudioOut,
    CallerSpeechStarted,
    CallerTranscript,
    RealtimeAuthError,
    RealtimeConfig,
    RealtimeError,
    RealtimeEvent,
    RealtimeStream,
    ResponseDone,
    ResponseStarted,
    SayFailed,
    ToolCall,
)

FRAME_SECONDS = 0.02
CHUNK_BYTES = 160
_CLOSED = object()
_TOOL = re.compile(r"\[tool:(\w+) (\{.*?\})\]")


class FakeRealtimeStream:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[RealtimeEvent | BaseException | object] = asyncio.Queue()
        self._audio_seconds = 0.0
        self._closed = False
        self._counter = 0
        self.cancelled = 0
        self.truncations: list[tuple[str, int]] = []
        self.said: list[str] = []
        self.reject_says = False
        self.tool_results: list[tuple[str, Mapping[str, Any], bool]] = []

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    def _audio(self, response_id: str, item_id: str, payload: bytes) -> None:
        for start in range(0, len(payload), CHUNK_BYTES):
            self._queue.put_nowait(AudioOut(response_id, item_id, payload[start : start + CHUNK_BYTES]))

    def _reply(
        self, text: str, audio_prefix: bytes, transcript: str, *, out_of_band: bool, say_id: str | None = None
    ) -> None:
        response_id, item_id = self._next_id("resp"), self._next_id("item")
        self._queue.put_nowait(ResponseStarted(response_id, out_of_band, say_id))
        self._audio(response_id, item_id, audio_prefix + text.encode("utf-8"))
        self._queue.put_nowait(AssistantTranscript(response_id, item_id, transcript))
        self._queue.put_nowait(ResponseDone(response_id, "completed", 10, 20, out_of_band, say_id))

    async def send_audio(self, mulaw_8k: bytes) -> None:
        if self._closed:
            return
        if mulaw_8k.startswith(b"FAKE-RT-AUTHFAIL"):
            self._queue.put_nowait(RealtimeAuthError("fake realtime auth failure"))
        elif mulaw_8k.startswith(b"FAKE-RT-DROP"):
            self._queue.put_nowait(RealtimeError("stream_error"))
        elif mulaw_8k.startswith(b"FAKE-RT:"):
            text = mulaw_8k[len(b"FAKE-RT:") :].decode("utf-8")
            self._queue.put_nowait(CallerSpeechStarted())
            self._queue.put_nowait(CallerTranscript(self._next_id("caller"), text))
            if match := _TOOL.search(text):
                try:
                    arguments = json.loads(match.group(2))
                except ValueError:
                    arguments = {}
                response_id = self._next_id("resp")
                self._queue.put_nowait(ResponseStarted(response_id, False))
                self._queue.put_nowait(ToolCall(response_id, self._next_id("call"), match.group(1), arguments))
                self._queue.put_nowait(ResponseDone(response_id, "completed", 5, 5, False))
            else:
                self._reply(text, b"FAKE-RT-REPLY:", "Reply: " + text, out_of_band=False)
        else:
            self._audio_seconds += FRAME_SECONDS

    async def send_tool_result(self, call_id: str, output: Mapping[str, Any], *, respond: bool = True) -> None:
        self.tool_results.append((call_id, output, respond))
        if respond and not self._closed:
            text = "Tool said: " + json.dumps(output, sort_keys=True)
            self._reply(text, b"FAKE-RT-REPLY:", text, out_of_band=False)

    async def say(self, text: str, say_id: str) -> None:
        self.said.append(text)
        if self._closed:
            return
        if self.reject_says:
            self._queue.put_nowait(SayFailed(say_id))
        else:
            self._reply(text, b"FAKE-RT-SAY:", text, out_of_band=True, say_id=say_id)

    async def cancel_response(self) -> None:
        self.cancelled += 1

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        self.truncations.append((item_id, audio_end_ms))

    async def events(self) -> AsyncIterator[RealtimeEvent]:
        while True:
            item = await self._queue.get()
            if item is _CLOSED:
                return
            if isinstance(item, BaseException):
                raise item
            yield item  # type: ignore[misc]

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            self._queue.put_nowait(_CLOSED)

    @property
    def audio_seconds(self) -> float:
        return self._audio_seconds


class FakeSpeechToSpeech:
    def __init__(self) -> None:
        self.opened: list[RealtimeConfig] = []
        self.streams: list[FakeRealtimeStream] = []

    async def open(self, config: RealtimeConfig) -> RealtimeStream:
        self.opened.append(config)
        stream = FakeRealtimeStream()
        self.streams.append(stream)
        return stream
