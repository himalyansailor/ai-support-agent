"""The speech-to-text port: one streaming recognition session per call (8 kHz µ-law in, transcript events out)."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol

from supportcore.registry import ProviderRegistry, UnknownProviderError


@dataclass(frozen=True)
class SttConfig:
    provider: str
    model: str
    language: str
    api_key: str | None = field(repr=False, default=None)


@dataclass(frozen=True)
class Transcript:
    # is_final: this text is settled; speech_final: the speaker paused (end of utterance)
    text: str = field(repr=False)
    is_final: bool
    speech_final: bool


@dataclass(frozen=True)
class SpeechStarted: ...


@dataclass(frozen=True)
class UtteranceEnd: ...


SttEvent = Transcript | SpeechStarted | UtteranceEnd


class SttAuthError(Exception):
    """The provider rejected the API key."""


class SttError(Exception):
    """Anything else that failed; `error_type` is one of the voice integration error types."""

    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type


class SttStream(Protocol):
    async def send(self, mulaw_8k: bytes) -> None: ...

    def events(self) -> AsyncIterator[SttEvent]: ...  # ends when the stream closes; raises SttError / SttAuthError

    async def aclose(self) -> None: ...

    @property
    def audio_seconds(self) -> float: ...  # audio sent so far, for metering


class SpeechToText(Protocol):
    async def open(self, config: SttConfig) -> SttStream: ...


class SttFactory:
    """Builds the SpeechToText for a provider id from the wiring registry; an unknown provider is a ValueError."""

    def __init__(self, registry: ProviderRegistry[SpeechToText]) -> None:
        self._registry = registry

    def create(self, provider: str) -> SpeechToText:
        try:
            return self._registry.create(provider)
        except UnknownProviderError as exc:
            raise ValueError(str(exc)) from None


__all__ = [
    "SpeechStarted",
    "SpeechToText",
    "SttAuthError",
    "SttConfig",
    "SttError",
    "SttEvent",
    "SttFactory",
    "SttStream",
    "Transcript",
    "UtteranceEnd",
]
