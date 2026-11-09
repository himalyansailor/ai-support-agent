"""The speech-to-text and text-to-speech models an organisation can pick for voice calls."""

from dataclasses import dataclass

DEEPGRAM_LANGUAGES = frozenset({"en", "es", "fr", "de", "it", "pt", "nl", "ja", "ru", "hi"})
REALTIME_VOICES = ("marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse")
OPENAI_VOICES = ("alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse")


@dataclass(frozen=True)
class SttModelSpec:
    provider: str
    model: str
    label: str
    languages: frozenset[str] | None  # None: any language
    recommended: bool
    requires_credential: bool


@dataclass(frozen=True)
class TtsModelSpec:
    provider: str
    model: str
    label: str
    languages: frozenset[str] | None  # None: any language
    recommended: bool
    requires_credential: bool
    voices: tuple[str, ...]
    default_voice: str


@dataclass(frozen=True)
class RealtimeModelSpec:
    provider: str
    model: str
    label: str
    languages: frozenset[str] | None  # None: any language
    recommended: bool
    requires_credential: bool
    voices: tuple[str, ...]
    default_voice: str


_STT = (
    SttModelSpec("deepgram", "nova-3", "Deepgram Nova-3", DEEPGRAM_LANGUAGES, True, True),
    SttModelSpec("deepgram", "nova-2", "Deepgram Nova-2", DEEPGRAM_LANGUAGES, False, True),
)
_FAKE_STT = (SttModelSpec("fake", "fake-stt", "Fake STT", None, False, False),)
_TTS = (
    TtsModelSpec("openai", "gpt-4o-mini-tts", "OpenAI GPT-4o mini TTS", None, True, True, OPENAI_VOICES, "coral"),
    TtsModelSpec("openai", "tts-1", "OpenAI TTS-1", None, False, True, OPENAI_VOICES, "coral"),
)
_FAKE_TTS = (TtsModelSpec("fake", "fake-tts", "Fake TTS", None, False, False, ("fake",), "fake"),)

_REALTIME = (
    RealtimeModelSpec(
        "openai", "gpt-realtime", "OpenAI GPT Realtime", None, True, True, REALTIME_VOICES, "marin"
    ),
    RealtimeModelSpec(
        "openai", "gpt-realtime-mini", "OpenAI GPT Realtime mini", None, False, True, REALTIME_VOICES, "marin"
    ),
)
_FAKE_REALTIME = (
    RealtimeModelSpec("fake", "fake-realtime", "Fake Realtime", None, False, False, ("fake",), "fake"),
)

_CREDENTIAL_PROVIDERS: dict[str, str | None] = {"deepgram": "deepgram", "openai": "openai", "fake": None}


def _speaks(languages: frozenset[str] | None, language: str) -> bool:
    return languages is None or language.split("-")[0].lower() in languages


def stt_models(language: str, include_fake: bool = False) -> list[SttModelSpec]:
    return [s for s in (*_STT, *(_FAKE_STT if include_fake else ())) if _speaks(s.languages, language)]


def tts_models(language: str, include_fake: bool = False) -> list[TtsModelSpec]:
    return [s for s in (*_TTS, *(_FAKE_TTS if include_fake else ())) if _speaks(s.languages, language)]


def realtime_models(language: str, include_fake: bool = False) -> list[RealtimeModelSpec]:
    return [s for s in (*_REALTIME, *(_FAKE_REALTIME if include_fake else ())) if _speaks(s.languages, language)]


def get_stt_spec(provider: str, model: str, include_fake: bool = False) -> SttModelSpec:
    for spec in (*_STT, *(_FAKE_STT if include_fake else ())):
        if spec.provider == provider and spec.model == model:
            return spec
    raise ValueError(f"Unknown speech-to-text model {provider}/{model}")


def get_tts_spec(provider: str, model: str, include_fake: bool = False) -> TtsModelSpec:
    for spec in (*_TTS, *(_FAKE_TTS if include_fake else ())):
        if spec.provider == provider and spec.model == model:
            return spec
    raise ValueError(f"Unknown text-to-speech model {provider}/{model}")


def get_realtime_spec(provider: str, model: str, include_fake: bool = False) -> RealtimeModelSpec:
    for spec in (*_REALTIME, *(_FAKE_REALTIME if include_fake else ())):
        if spec.provider == provider and spec.model == model:
            return spec
    raise ValueError(f"Unknown speech-to-speech model {provider}/{model}")


def credential_provider_for(provider: str) -> str | None:
    """The ProviderCredential provider that holds the key for a voice provider (None: needs no key)."""
    try:
        return _CREDENTIAL_PROVIDERS[provider]
    except KeyError:
        raise ValueError(f"Unknown voice provider {provider!r}") from None
