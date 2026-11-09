"""OpenAI Realtime (GA) speech-to-speech over a raw WebSocket (no SDK).

8 kHz µ-law goes up and down as base64 (`audio/pcmu`). Spoken text, tool arguments and keys are never logged.
"""

import asyncio
import base64
import contextlib
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any
from urllib.parse import urlencode

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from supportcore.config import Settings
from supportcore.voice.realtime.ports import (
    AssistantTranscript,
    AudioOut,
    CallerSpeechStarted,
    CallerTranscript,
    RealtimeAuthError,
    RealtimeConfig,
    RealtimeError,
    RealtimeEvent,
    ResponseDone,
    ResponseStarted,
    SayFailed,
    ToolCall,
)

OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"
CONNECT_TIMEOUT_SECONDS = 10
MAX_MESSAGE_BYTES = 1024 * 1024
MULAW_BYTES_PER_SECOND = 8000
TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"
_END = object()
_RATE_LIMITED = {"rate_limit_exceeded", "insufficient_quota"}


def session_update(config: RealtimeConfig) -> dict[str, Any]:
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "model": config.model,
            "instructions": config.instructions,
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "transcription": {"model": TRANSCRIPTION_MODEL, "language": config.language},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.5,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 500,
                        "create_response": True,
                        "interrupt_response": True,
                    },
                },
                "output": {"format": {"type": "audio/pcmu"}, "voice": config.voice},
            },
            "tools": [
                {"type": "function", "name": t.name, "description": t.description, "parameters": dict(t.parameters)}
                for t in config.tools
            ],
            "tool_choice": "auto",
        },
    }


def _error_for(message: Mapping[str, Any]) -> Exception | None:
    """An `error` event -> the exception to raise (None: not fatal, e.g. cancelling a finished response)."""
    err = message.get("error")
    err = err if isinstance(err, dict) else {}
    code, kind = err.get("code"), err.get("type")
    if code == "invalid_api_key" or kind == "invalid_api_key":
        return RealtimeAuthError("openai rejected the api key")
    if code in _RATE_LIMITED or kind in _RATE_LIMITED:
        return RealtimeError("rate_limited")
    if kind == "server_error":
        return RealtimeError("stream_error")
    return None


def _say_id(response: Mapping[str, Any]) -> tuple[bool, str | None]:
    metadata = response.get("metadata") or {}
    if metadata.get("kind") != "say":
        return False, None
    say_id = metadata.get("say_id")
    return True, say_id if isinstance(say_id, str) else None


def parse_message(raw: str | bytes) -> RealtimeEvent | Exception | None:
    """One server message -> a port event, an exception to raise, or None (ignored)."""
    if isinstance(raw, bytes):
        return None
    try:
        message = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(message, dict):
        return None
    kind = message.get("type")
    try:
        if kind == "input_audio_buffer.speech_started":
            return CallerSpeechStarted()
        if kind == "conversation.item.input_audio_transcription.completed":
            return CallerTranscript(item_id=message["item_id"], text=message.get("transcript") or "")
        if kind == "response.created":
            out_of_band, say_id = _say_id(message["response"])
            return ResponseStarted(response_id=message["response"]["id"], out_of_band=out_of_band, say_id=say_id)
        if kind == "response.output_audio.delta":
            return AudioOut(
                response_id=message["response_id"],
                item_id=message["item_id"],
                audio=base64.b64decode(message["delta"]),
            )
        if kind == "response.output_audio_transcript.done":
            return AssistantTranscript(
                response_id=message["response_id"], item_id=message["item_id"], text=message.get("transcript") or ""
            )
        if kind == "response.function_call_arguments.done":
            try:
                arguments = json.loads(message.get("arguments") or "")
            except ValueError:
                arguments = {}
            return ToolCall(
                response_id=message["response_id"],
                call_id=message["call_id"],
                name=message["name"],
                arguments=arguments if isinstance(arguments, dict) else {},
            )
        if kind == "response.done":
            response = message["response"]
            usage = response.get("usage") or {}
            out_of_band, say_id = _say_id(response)
            return ResponseDone(
                response_id=response["id"],
                status=response.get("status") or "completed",
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                out_of_band=out_of_band,
                say_id=say_id,
            )
        if kind == "error":
            fatal = _error_for(message)
            if fatal is not None:
                return fatal
            # Not fatal. Only fixed lines are sent with an event_id: one naming it means that line was refused.
            event_id = (message.get("error") or {}).get("event_id")
            return SayFailed(say_id=event_id) if isinstance(event_id, str) and event_id else None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None  # a malformed event is ignored, never echoed
    return None


class OpenAIRealtimeStream:
    def __init__(self, ws: ClientConnection) -> None:
        self._ws = ws
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self._audio_bytes = 0
        self._closing = False
        self._failed: Exception | None = None
        self._reader = asyncio.create_task(self._read())

    @property
    def audio_seconds(self) -> float:
        return self._audio_bytes / MULAW_BYTES_PER_SECOND

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._failed is not None:
            raise self._failed
        try:
            await self._ws.send(json.dumps(payload))
        except ConnectionClosed:
            raise RealtimeError("stream_error") from None

    async def send_audio(self, mulaw_8k: bytes) -> None:
        await self._send({"type": "input_audio_buffer.append", "audio": base64.b64encode(mulaw_8k).decode("ascii")})
        self._audio_bytes += len(mulaw_8k)

    async def send_tool_result(self, call_id: str, output: Mapping[str, Any], *, respond: bool = True) -> None:
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {"type": "function_call_output", "call_id": call_id, "output": json.dumps(output)},
            }
        )
        if respond:
            await self._send({"type": "response.create"})

    async def say(self, text: str, say_id: str) -> None:
        await self._send(
            {
                "type": "response.create",
                "event_id": say_id,  # named by an error event should the line be refused
                "response": {
                    "conversation": "none",
                    "output_modalities": ["audio"],
                    "metadata": {"kind": "say", "say_id": say_id},
                    "instructions": f"Say exactly this, and nothing else: {text}",
                },
            }
        )

    async def cancel_response(self) -> None:
        await self._send({"type": "response.cancel"})

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        await self._send(
            {
                "type": "conversation.item.truncate",
                "item_id": item_id,
                "content_index": 0,
                "audio_end_ms": audio_end_ms,
            }
        )

    async def events(self) -> AsyncIterator[RealtimeEvent]:
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
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._reader.cancel()
        await asyncio.gather(self._reader, return_exceptions=True)
        self._queue.put_nowait(_END)

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                parsed = parse_message(raw)
                if isinstance(parsed, Exception):
                    self._fail(parsed)
                    return
                if parsed is not None:
                    self._queue.put_nowait(parsed)
        except ConnectionClosed:
            pass
        except Exception:  # noqa: BLE001 - any read failure is a stream error; no message text kept
            self._fail(RealtimeError("stream_error"))
            return
        if self._closing:
            self._queue.put_nowait(_END)
        else:
            self._fail(RealtimeError("stream_error"))  # a close nobody asked for

    def _fail(self, exc: Exception) -> None:
        self._failed = exc
        self._queue.put_nowait(exc)


class OpenAIRealtime:
    def __init__(self, settings: Settings, *, url: str = OPENAI_REALTIME_URL) -> None:
        self._settings = settings
        self._url = url

    async def open(self, config: RealtimeConfig) -> OpenAIRealtimeStream:
        if not config.api_key:
            raise RealtimeAuthError("missing api key")
        try:
            ws = await connect(
                f"{self._url}?{urlencode({'model': config.model})}",
                additional_headers={"Authorization": f"Bearer {config.api_key}"},
                open_timeout=CONNECT_TIMEOUT_SECONDS,
                max_size=MAX_MESSAGE_BYTES,
            )
        except InvalidStatus as exc:
            if exc.response.status_code in (401, 403):
                raise RealtimeAuthError("openai rejected the api key") from None
            raise RealtimeError("connection_failed") from None
        except (WebSocketException, OSError, TimeoutError):
            raise RealtimeError("connection_failed") from None
        stream = OpenAIRealtimeStream(ws)
        try:
            await stream._send(session_update(config))
        except BaseException:
            await stream.aclose()
            raise
        return stream
