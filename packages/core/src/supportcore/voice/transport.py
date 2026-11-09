"""The media transport port: one call's audio in both directions (Twilio Media Streams in production).

Inbound: caller audio (8 kHz µ-law frames), playback marks echoed back, and the end of the stream. Outbound: audio
frames, named marks (echoed back once the audio before them has played), and `clear` (drop buffered audio).
"""

import asyncio
import base64
import binascii
import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = [
    "MAX_FRAME_BYTES",
    "MAX_MESSAGE_BYTES",
    "MESSAGE_TOO_BIG",
    "SEND_TIMEOUT_SECONDS",
    "AudioIn",
    "FakeTransport",
    "MarkPlayed",
    "MediaTransport",
    "Stop",
    "StreamStart",
    "TransportClosed",
    "TransportEvent",
    "TwilioMediaTransport",
]


@dataclass(frozen=True)
class AudioIn:
    frame: bytes = field(repr=False)


@dataclass(frozen=True)
class MarkPlayed:
    name: str


@dataclass(frozen=True)
class Stop: ...


TransportEvent = AudioIn | MarkPlayed | Stop


class TransportClosed(Exception):
    """The other side went away: sending is no longer possible."""


class MediaTransport(Protocol):
    def events(self) -> AsyncIterator[TransportEvent]: ...  # ends when the stream closes

    async def send_audio(self, frame: bytes) -> None: ...  # raises TransportClosed once closed

    async def send_mark(self, name: str) -> None: ...

    async def clear(self) -> None: ...

    async def close(self) -> None: ...  # idempotent


_END = object()


class FakeTransport:
    """In-memory transport for tests: `push()` scripts inbound events, `sent` records outbound ones as
    ("audio", frame) / ("mark", name) / ("clear",) / ("close",).

    With `auto_marks` every mark is echoed back as played at once (instant playback). With `echo_marks_on_clear`
    a `clear` echoes every mark not yet played back, and a mark sent while no audio is buffered (nothing sent
    since the last `clear`) is echoed at once, as Twilio does. `hold_audio()` makes `send_audio` record the
    frame and then wait until `release_audio()`, so a test can catch the bot mid-reply. `disconnect()` is the
    other side going away: the inbound stream ends and further sends raise TransportClosed.
    """

    def __init__(self, *, auto_marks: bool = True, echo_marks_on_clear: bool = False) -> None:
        self.auto_marks = auto_marks
        self.echo_marks_on_clear = echo_marks_on_clear
        self._unplayed: list[str] = []
        self._buffered = False  # audio sent since the last clear (echo_marks_on_clear only)
        self.sent: list[tuple] = []
        self.closed = False
        self._inbound: asyncio.Queue[TransportEvent | object] = asyncio.Queue()
        self._changed = asyncio.Event()
        self._released = asyncio.Event()
        self._released.set()
        self.on_send: Callable[[tuple], None] | None = None  # test hook, called before each outbound record

    # --- scripting ---------------------------------------------------------------------------------

    def push(self, event: TransportEvent) -> None:
        if isinstance(event, MarkPlayed) and event.name in self._unplayed:
            self._unplayed.remove(event.name)
        self._inbound.put_nowait(event)

    def disconnect(self) -> None:
        if not self.closed:
            self.closed = True
            self._inbound.put_nowait(_END)
            self._changed.set()

    def hold_audio(self) -> None:
        self._released.clear()

    def release_audio(self) -> None:
        self._released.set()

    async def wait_until(self, predicate: Callable[["FakeTransport"], bool], timeout: float = 5.0) -> None:
        """Waits for `predicate`, re-checked on every outbound event (and every 10 ms, for state elsewhere)."""
        async with asyncio.timeout(timeout):
            while not predicate(self):
                self._changed.clear()
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._changed.wait(), 0.01)

    # --- MediaTransport ----------------------------------------------------------------------------

    async def events(self) -> AsyncIterator[TransportEvent]:
        while True:
            item = await self._inbound.get()
            if item is _END:
                return
            yield item  # type: ignore[misc]

    async def send_audio(self, frame: bytes) -> None:
        self._record(("audio", frame))
        self._buffered = True
        await self._released.wait()

    async def send_mark(self, name: str) -> None:
        self._record(("mark", name))
        self._unplayed.append(name)
        if self.auto_marks or (self.echo_marks_on_clear and not self._buffered):
            self.push(MarkPlayed(name))

    async def clear(self) -> None:
        self._record(("clear",))
        self._buffered = False
        if self.echo_marks_on_clear:
            for name in list(self._unplayed):
                self.push(MarkPlayed(name))

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.sent.append(("close",))
        self._inbound.put_nowait(_END)
        self._changed.set()

    def _record(self, entry: tuple) -> None:
        if self.closed:
            raise TransportClosed()
        if self.on_send is not None:
            self.on_send(entry)
        self.sent.append(entry)
        self._changed.set()


# --- Twilio Media Streams --------------------------------------------------------------------------

MAX_FRAME_BYTES = 4 * 1024  # one inbound audio frame (Twilio sends 160 bytes)
# One inbound WebSocket message. The outer limit is uvicorn's `--ws-max-size` (16 KiB in `make dev-voice`), which
# closes bigger messages with 1009 before they reach the app; this check holds even if a run command drops the flag.
MAX_MESSAGE_BYTES = 64 * 1024
MESSAGE_TOO_BIG = 1009  # the WebSocket close code for both
_MAX_PAYLOAD_CHARS = -(-MAX_FRAME_BYTES // 3) * 4  # base64 length of MAX_FRAME_BYTES
SEND_TIMEOUT_SECONDS = 5.0  # an outbound message Twilio does not take within this means the socket is gone


class WebSocketPort(Protocol):
    """The slice of an ASGI WebSocket the transport needs (Starlette's `WebSocket` fits; supportcore stays
    framework-free). `receive()` returns raw ASGI messages."""

    async def receive(self) -> Mapping[str, Any]: ...

    async def send_text(self, data: str) -> None: ...

    async def close(self, code: int = 1000) -> None: ...


@dataclass(frozen=True)
class StreamStart:
    stream_sid: str
    call_sid: str
    parameters: dict[str, str] = field(repr=False)  # the <Stream> <Parameter>s: the stream token, never logged


class _Closed(Exception):
    """The socket is gone (disconnect) or was closed by us (oversized input)."""


class _FrameTooBig(Exception):
    pass


class TwilioMediaTransport:
    """One Twilio Media Streams WebSocket as a MediaTransport.

    Inbound `connected`, `start`, `media`, `mark` and `stop` are understood; other events and malformed messages
    are ignored. Outbound `media` (base64 µ-law), `mark` and `clear` carry the `streamSid` from `start`. A message
    over MAX_MESSAGE_BYTES or an audio frame over MAX_FRAME_BYTES closes the socket with 1009. Nothing here logs.
    """

    def __init__(self, websocket: WebSocketPort) -> None:
        self._ws = websocket
        self._stream_sid = ""
        self._closed = False

    async def wait_for_start(self, timeout: float) -> StreamStart | None:
        """Reads up to the `start` event. None when it does not come within `timeout`, the socket goes away, or
        an oversized message closed it."""
        try:
            async with asyncio.timeout(timeout):
                while True:
                    event = await self._next()
                    if event.get("event") != "start":
                        continue
                    start = event.get("start")
                    if not isinstance(start, dict):
                        continue
                    raw = start.get("customParameters")
                    parameters = (
                        {str(k): str(v) for k, v in raw.items() if isinstance(v, str)} if isinstance(raw, dict) else {}
                    )
                    self._stream_sid = str(start.get("streamSid") or event.get("streamSid") or "")
                    return StreamStart(self._stream_sid, str(start.get("callSid") or ""), parameters)
        except (TimeoutError, _Closed):
            return None

    async def events(self) -> AsyncIterator[TransportEvent]:
        try:
            while True:
                event = await self._next()
                kind = event.get("event")
                if kind == "media":
                    try:
                        frame = self._frame(event.get("media"))
                    except _FrameTooBig:
                        await self.close(MESSAGE_TOO_BIG)
                        return
                    if frame is not None:
                        yield AudioIn(frame)
                elif kind == "mark":
                    mark = event.get("mark")
                    if isinstance(mark, dict) and isinstance(mark.get("name"), str):
                        yield MarkPlayed(mark["name"])
                elif kind == "stop":
                    yield Stop()
                    return
        except _Closed:
            return

    async def send_audio(self, frame: bytes) -> None:
        await self._send({"event": "media", "streamSid": self._stream_sid, "media": {"payload": _b64(frame)}})

    async def send_mark(self, name: str) -> None:
        await self._send({"event": "mark", "streamSid": self._stream_sid, "mark": {"name": name}})

    async def clear(self) -> None:
        await self._send({"event": "clear", "streamSid": self._stream_sid})

    async def close(self, code: int = 1000) -> None:
        if self._closed:
            return
        self._closed = True
        with suppress(Exception):  # the other side may already be gone
            await self._ws.close(code)

    # --- internals ---------------------------------------------------------------------------------

    async def _next(self) -> dict:
        """The next inbound JSON object (malformed messages skipped)."""
        while True:
            if self._closed:
                raise _Closed()
            try:
                message = await self._ws.receive()
            except RuntimeError:  # receiving after a disconnect
                self._closed = True
                raise _Closed() from None
            if message.get("type") == "websocket.disconnect":
                self._closed = True
                raise _Closed()
            data = message.get("text")
            if data is None:
                data = message.get("bytes")
            if data is None:
                continue
            if len(data) > MAX_MESSAGE_BYTES or (isinstance(data, str) and len(data.encode()) > MAX_MESSAGE_BYTES):
                await self._too_big()
            try:
                event = json.loads(data)
            except (ValueError, UnicodeDecodeError, RecursionError):  # RecursionError: absurdly nested JSON
                continue
            if isinstance(event, dict):
                return event

    def _frame(self, media: object) -> bytes | None:
        if not isinstance(media, dict) or media.get("track", "inbound") != "inbound":
            return None
        payload = media.get("payload")
        if not isinstance(payload, str):
            return None
        if len(payload) > _MAX_PAYLOAD_CHARS:
            raise _FrameTooBig()
        try:
            frame = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            return None
        if len(frame) > MAX_FRAME_BYTES:
            raise _FrameTooBig()
        return frame

    async def _too_big(self) -> None:
        await self.close(MESSAGE_TOO_BIG)
        raise _Closed()

    async def _send(self, event: dict) -> None:
        if self._closed:
            raise TransportClosed()
        try:
            await asyncio.wait_for(self._ws.send_text(json.dumps(event, separators=(",", ":"))), SEND_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - any send failure (or a peer that stopped reading) means the other side is gone
            self._closed = True
            raise TransportClosed() from None


def _b64(frame: bytes) -> str:
    return base64.b64encode(frame).decode("ascii")
