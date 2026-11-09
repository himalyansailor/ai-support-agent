"""In-memory SpeechToText for tests (registered only when env == "test").

A frame starting with b"FAKE-STT:" makes the stream emit SpeechStarted and then a final Transcript of the UTF-8 text
after the prefix; b"FAKE-NOISE" emits only SpeechStarted; b"FAKE-STT-AUTHFAIL" makes events() raise SttAuthError.
Any other frame is counted as 20 ms of audio.
"""

import asyncio
from collections.abc import AsyncIterator

from supportcore.voice.stt.ports import (
    SpeechStarted,
    SttAuthError,
    SttConfig,
    SttEvent,
    SttStream,
    Transcript,
)

FRAME_SECONDS = 0.02
_CLOSED = object()


class FakeSttStream:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[SttEvent | BaseException | object] = asyncio.Queue()
        self._audio_seconds = 0.0
        self._closed = False

    async def send(self, mulaw_8k: bytes) -> None:
        if self._closed:
            return
        if mulaw_8k.startswith(b"FAKE-STT-AUTHFAIL"):
            self._queue.put_nowait(SttAuthError("fake stt auth failure"))
        elif mulaw_8k.startswith(b"FAKE-STT:"):
            text = mulaw_8k[len(b"FAKE-STT:") :].decode("utf-8")
            self._queue.put_nowait(SpeechStarted())
            self._queue.put_nowait(Transcript(text, True, True))
        elif mulaw_8k.startswith(b"FAKE-NOISE"):
            self._queue.put_nowait(SpeechStarted())
        else:
            self._audio_seconds += FRAME_SECONDS

    async def events(self) -> AsyncIterator[SttEvent]:
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


class FakeSpeechToText:
    def __init__(self) -> None:
        self.opened: list[SttConfig] = []

    async def open(self, config: SttConfig) -> SttStream:
        self.opened.append(config)
        return FakeSttStream()
