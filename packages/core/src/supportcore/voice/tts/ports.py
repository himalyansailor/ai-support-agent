"""The text-to-speech port: text in, 8 kHz µ-law audio frames out."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol

from supportcore.registry import ProviderRegistry, UnknownProviderError


@dataclass(frozen=True)
class TtsConfig:
    provider: str
    model: str
    voice: str
    language: str
    api_key: str | None = field(repr=False, default=None)


class TtsAuthError(Exception):
    """The provider rejected the API key."""


class TtsError(Exception):
    """Anything else that failed; `error_type` is one of the voice integration error types."""

    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type


class TextToSpeech(Protocol):
    def synthesize(self, text: str, config: TtsConfig) -> AsyncIterator[bytes]:
        """8 kHz µ-law, 160-byte frames. Callers must `aclose()` (or cancel) the iterator when they stop early."""
        ...


class TtsFactory:
    """Builds the TextToSpeech for a provider id from the wiring registry; an unknown provider is a ValueError."""

    def __init__(self, registry: ProviderRegistry[TextToSpeech]) -> None:
        self._registry = registry

    def create(self, provider: str) -> TextToSpeech:
        try:
            return self._registry.create(provider)
        except UnknownProviderError as exc:
            raise ValueError(str(exc)) from None
