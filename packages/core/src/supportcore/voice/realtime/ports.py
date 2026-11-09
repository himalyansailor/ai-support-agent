"""The speech-to-speech port: one Realtime session per call (8 kHz µ-law in, audio, transcripts and tool calls out)."""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from supportcore.registry import ProviderRegistry, UnknownProviderError


@dataclass(frozen=True)
class RealtimeTool:
    name: str
    description: str
    parameters: Mapping[str, Any]  # JSON schema


@dataclass(frozen=True)
class RealtimeConfig:
    provider: str
    model: str
    voice: str
    language: str
    instructions: str = field(repr=False)
    tools: tuple[RealtimeTool, ...] = ()
    api_key: str | None = field(repr=False, default=None)


@dataclass(frozen=True)
class ResponseStarted:
    response_id: str
    out_of_band: bool  # a fixed line (metadata {"kind": "say"})
    say_id: str | None = None  # the fixed line's id, as passed to say()


@dataclass(frozen=True)
class AudioOut:  # 8 kHz µ-law, any length
    response_id: str
    item_id: str
    audio: bytes = field(repr=False)


@dataclass(frozen=True)
class CallerSpeechStarted: ...


@dataclass(frozen=True)
class CallerTranscript:
    item_id: str
    text: str = field(repr=False)


@dataclass(frozen=True)
class AssistantTranscript:
    response_id: str
    item_id: str
    text: str = field(repr=False)


@dataclass(frozen=True)
class ToolCall:
    response_id: str
    call_id: str
    name: str
    arguments: Mapping[str, Any] = field(repr=False)  # {} when not valid JSON


@dataclass(frozen=True)
class ResponseDone:
    response_id: str
    status: str  # completed | cancelled | failed | incomplete
    input_tokens: int
    output_tokens: int
    out_of_band: bool  # a fixed line (metadata {"kind": "say"})
    say_id: str | None = None  # the fixed line's id, as passed to say()


@dataclass(frozen=True)
class SayFailed:
    """The provider refused a fixed line (a non-fatal error naming its request): no response will come for it."""

    say_id: str


RealtimeEvent = (
    ResponseStarted
    | AudioOut
    | CallerSpeechStarted
    | CallerTranscript
    | AssistantTranscript
    | ToolCall
    | ResponseDone
    | SayFailed
)


class RealtimeAuthError(Exception):
    """The provider rejected the API key."""


class RealtimeError(Exception):
    """Anything else that failed; `error_type` is one of the voice integration error types."""

    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type


class RealtimeStream(Protocol):
    async def send_audio(self, mulaw_8k: bytes) -> None: ...

    async def send_tool_result(self, call_id: str, output: Mapping[str, Any], *, respond: bool = True) -> None: ...

    async def say(self, text: str, say_id: str) -> None: ...  # out-of-band fixed line; its events carry say_id

    async def cancel_response(self) -> None: ...

    async def truncate(self, item_id: str, audio_end_ms: int) -> None: ...

    def events(self) -> AsyncIterator[RealtimeEvent]: ...  # raises RealtimeAuthError / RealtimeError

    async def aclose(self) -> None: ...

    @property
    def audio_seconds(self) -> float: ...  # caller audio sent


class SpeechToSpeech(Protocol):
    async def open(self, config: RealtimeConfig) -> RealtimeStream: ...


class RealtimeFactory:
    """Builds the SpeechToSpeech for a provider id from the wiring registry; an unknown provider is a ValueError."""

    def __init__(self, registry: ProviderRegistry[SpeechToSpeech]) -> None:
        self._registry = registry

    def create(self, provider: str) -> SpeechToSpeech:
        try:
            return self._registry.create(provider)
        except UnknownProviderError as exc:
            raise ValueError(str(exc)) from None


__all__ = [
    "AssistantTranscript",
    "AudioOut",
    "CallerSpeechStarted",
    "CallerTranscript",
    "RealtimeAuthError",
    "RealtimeConfig",
    "RealtimeError",
    "RealtimeEvent",
    "RealtimeFactory",
    "RealtimeStream",
    "RealtimeTool",
    "ResponseDone",
    "ResponseStarted",
    "SayFailed",
    "SpeechToSpeech",
    "ToolCall",
]
