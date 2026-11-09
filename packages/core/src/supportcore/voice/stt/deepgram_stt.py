"""Deepgram live streaming speech-to-text over a raw WebSocket (no SDK).

8 kHz µ-law audio goes up as binary frames; JSON messages come back. Transcript text is never logged.
"""

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from urllib.parse import urlencode

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from supportcore.config import Settings
from supportcore.voice.stt.ports import (
    SpeechStarted,
    SttAuthError,
    SttConfig,
    SttError,
    SttEvent,
    Transcript,
    UtteranceEnd,
)

DEEPGRAM_LIVE_URL = "wss://api.deepgram.com/v1/listen"
CONNECT_TIMEOUT_SECONDS = 10
MAX_MESSAGE_BYTES = 1024 * 1024  # a transcript message is a few KB; a bigger one is a misbehaving provider
KEEPALIVE_SECONDS = 5.0
MULAW_BYTES_PER_SECOND = 8000
_END = object()


def _query(config: SttConfig) -> str:
    return urlencode(
        {
            "model": config.model,
            "language": config.language,
            "encoding": "mulaw",
            "sample_rate": 8000,
            "channels": 1,
            "interim_results": "true",
            "endpointing": 300,
            "utterance_end_ms": 1000,
            "vad_events": "true",
            "smart_format": "true",
            "punctuate": "true",
        }
    )


def parse_message(raw: str | bytes) -> SttEvent | None:
    """One Deepgram JSON message -> a port event (None: ignored, including empty non-final text)."""
    if isinstance(raw, bytes):
        return None
    try:
        message = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(message, dict):
        return None
    kind = message.get("type")
    if kind == "Results":
        try:
            text = message["channel"]["alternatives"][0].get("transcript", "") or ""
        except (KeyError, IndexError, TypeError, AttributeError):
            return None
        speech_final = bool(message.get("speech_final"))
        if not text and not speech_final:
            return None
        return Transcript(text=text, is_final=bool(message.get("is_final")), speech_final=speech_final)
    if kind == "SpeechStarted":
        return SpeechStarted()
    if kind == "UtteranceEnd":
        return UtteranceEnd()
    return None  # Metadata and anything else


class DeepgramSttStream:
    def __init__(self, ws: ClientConnection) -> None:
        self._ws = ws
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self._audio_bytes = 0
        self._last_sent = time.monotonic()
        self._closing = False
        self._failed: SttError | None = None
        self._reader = asyncio.create_task(self._read())
        self._keepalive = asyncio.create_task(self._keep_alive())

    @property
    def audio_seconds(self) -> float:
        return self._audio_bytes / MULAW_BYTES_PER_SECOND

    async def send(self, mulaw_8k: bytes) -> None:
        if self._failed is not None:
            raise self._failed
        try:
            await self._ws.send(mulaw_8k)
        except ConnectionClosed:
            raise SttError("stream_error") from None
        self._audio_bytes += len(mulaw_8k)
        self._last_sent = time.monotonic()

    async def events(self) -> AsyncIterator[SttEvent]:
        while True:
            item = await self._queue.get()
            if item is _END:
                self._queue.put_nowait(_END)  # later calls end immediately too
                return
            if isinstance(item, BaseException):
                self._queue.put_nowait(_END)
                raise item
            yield item  # type: ignore[misc]

    async def aclose(self) -> None:
        if not self._closing:
            self._closing = True
            self._keepalive.cancel()
            with contextlib.suppress(ConnectionClosed, WebSocketException, OSError):
                await self._ws.send(json.dumps({"type": "CloseStream"}))
            # Deepgram flushes what it has and closes the socket; give the reader a moment to drain.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(self._reader), timeout=2)
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._reader.cancel()
        self._keepalive.cancel()
        await asyncio.gather(self._reader, self._keepalive, return_exceptions=True)
        self._queue.put_nowait(_END)

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                event = parse_message(raw)
                if event is not None:
                    self._queue.put_nowait(event)
        except ConnectionClosed:
            pass
        except Exception:  # noqa: BLE001 - any read failure is a stream error; no message text kept
            self._fail()
            return
        # The loop ends on a normal close too, so a close nobody asked for is still an error.
        if self._closing:
            self._queue.put_nowait(_END)
        else:
            self._fail()

    def _fail(self) -> None:
        self._failed = SttError("stream_error")
        self._queue.put_nowait(self._failed)

    async def _keep_alive(self) -> None:
        try:
            while True:
                idle = time.monotonic() - self._last_sent
                await asyncio.sleep(max(KEEPALIVE_SECONDS - idle, 0.0) or KEEPALIVE_SECONDS)
                if time.monotonic() - self._last_sent >= KEEPALIVE_SECONDS:
                    await self._ws.send(json.dumps({"type": "KeepAlive"}))
                    self._last_sent = time.monotonic()
        except (ConnectionClosed, asyncio.CancelledError):
            return


class DeepgramSpeechToText:
    def __init__(self, settings: Settings, *, url: str = DEEPGRAM_LIVE_URL) -> None:
        self._settings = settings
        self._url = url

    async def open(self, config: SttConfig) -> DeepgramSttStream:
        if not config.api_key:
            raise SttAuthError("missing api key")
        try:
            ws = await connect(
                f"{self._url}?{_query(config)}",
                additional_headers={"Authorization": f"Token {config.api_key}"},
                open_timeout=CONNECT_TIMEOUT_SECONDS,
                max_size=MAX_MESSAGE_BYTES,
            )
        except InvalidStatus as exc:
            if exc.response.status_code in (401, 403):
                raise SttAuthError("deepgram rejected the api key") from None
            raise SttError("connection_failed") from None
        except (WebSocketException, OSError, TimeoutError):
            raise SttError("connection_failed") from None
        return DeepgramSttStream(ws)
