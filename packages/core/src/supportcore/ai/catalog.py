"""Static catalog of the chat and embedding models an organisation can pick.

`model="*"` marks a free-text provider: any non-empty model id is accepted and resolves to that spec.
The `fake` and `scripted` providers exist for tests only and are returned only with `include_fake=True`.
"""

from dataclasses import dataclass

from supportcore.errors import DomainError

ANY_MODEL = "*"
ANY_LANGUAGE = "*"

LANGUAGES: list[tuple[str, str]] = [("en", "English")]


class UnknownModelError(DomainError):
    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model
        super().__init__(f"Unknown model {model!r} for provider {provider!r}")


class UnsupportedLanguageError(DomainError):
    def __init__(self, language: str) -> None:
        self.language = language
        super().__init__(f"Unsupported language: {language[:20]!r}")


def ensure_supported_language(language: str) -> str:
    """Returns the language code if it is in LANGUAGES, else raises UnsupportedLanguageError."""
    if language not in {code for code, _label in LANGUAGES}:
        raise UnsupportedLanguageError(language)
    return language


@dataclass(frozen=True)
class EmbeddingModelSpec:
    provider: str
    model: str
    dimensions: int | None  # None: probed when the knowledge base is created
    languages: frozenset[str]  # "*" means any language
    requires_credential: bool
    recommended: bool
    label: str
    # Cosine score at which an answer from this model starts to count as confident; retrieval
    # reports `score - confidence_threshold` so scores of different models are comparable.
    confidence_threshold: float = 0.0

    def supports_language(self, language: str) -> bool:
        return ANY_LANGUAGE in self.languages or language in self.languages


@dataclass(frozen=True)
class ChatModelSpec:
    provider: str
    model: str
    label: str
    recommended: bool
    requires_credential: bool = True


_ANY = frozenset({ANY_LANGUAGE})

_EMBEDDING_MODELS: tuple[EmbeddingModelSpec, ...] = (
    EmbeddingModelSpec("local", "BAAI/bge-small-en-v1.5", 384, frozenset({"en"}), False, True, "BGE Small (English)", 0.60),
    EmbeddingModelSpec("local", "intfloat/multilingual-e5-small", 384, _ANY, False, False, "Multilingual E5 Small", 0.80),
    EmbeddingModelSpec("openai", "text-embedding-3-small", 1536, _ANY, True, False, "OpenAI text-embedding-3-small", 0.35),
    EmbeddingModelSpec("openai", "text-embedding-3-large", 3072, _ANY, True, False, "OpenAI text-embedding-3-large", 0.35),
    EmbeddingModelSpec("openai_compatible", ANY_MODEL, None, _ANY, True, False, "OpenAI-compatible (enter model id)", 0.35),
)

_CHAT_MODELS: tuple[ChatModelSpec, ...] = (
    ChatModelSpec("anthropic", "claude-sonnet-5-5", "Claude Sonnet 5.5", recommended=True),
    ChatModelSpec("openai", ANY_MODEL, "OpenAI (enter model id)", recommended=False),
    ChatModelSpec("openai_compatible", ANY_MODEL, "OpenAI-compatible (enter model id)", recommended=False),
)

_FAKE_EMBEDDINGS = (
    EmbeddingModelSpec("fake", "fake-embedding", 8, _ANY, False, False, "Fake embedding (tests)", 0.0),
    # A second size, so tests can change dimensions (and rebuild) without real models.
    EmbeddingModelSpec("fake", "fake-embedding-16", 16, _ANY, False, False, "Fake embedding 16d (tests)", 0.0),
)
_FAKE_CHAT = ChatModelSpec("fake", "fake-chat", "Fake chat (tests)", recommended=False, requires_credential=False)
# Replies from a script the test sets (supportcore.testing.script_chat), with tool calls and streaming.
_SCRIPTED_CHAT = ChatModelSpec(
    "scripted", "scripted-chat", "Scripted chat (tests)", recommended=False, requires_credential=False
)

# Cross-encoder reranker models (sentence-transformers, run locally). Only these may be loaded: any
# other id would be downloaded from the Hugging Face hub on first use. The first is the default.
RERANKER_MODELS: tuple[str, ...] = ("BAAI/bge-reranker-base", "BAAI/bge-reranker-v2-m3")
DEFAULT_RERANKER_MODEL = RERANKER_MODELS[0]

# Providers an organisation can store an API key for. "deepgram" is voice-only (speech-to-text): it is
# not offered as a chat or embedding provider.
CREDENTIAL_PROVIDERS: frozenset[str] = frozenset(
    {s.provider for s in (*_EMBEDDING_MODELS, *_CHAT_MODELS) if s.requires_credential} | {"deepgram"}
)


def _embedding_specs(include_fake: bool) -> tuple[EmbeddingModelSpec, ...]:
    return (*_EMBEDDING_MODELS, *_FAKE_EMBEDDINGS) if include_fake else _EMBEDDING_MODELS


def _chat_specs(include_fake: bool) -> tuple[ChatModelSpec, ...]:
    return (*_CHAT_MODELS, _FAKE_CHAT, _SCRIPTED_CHAT) if include_fake else _CHAT_MODELS


def embedding_models_for(language: str, *, include_fake: bool = False) -> list[EmbeddingModelSpec]:
    return [s for s in _embedding_specs(include_fake) if s.supports_language(language)]


def chat_models(*, include_fake: bool = False) -> list[ChatModelSpec]:
    return list(_chat_specs(include_fake))


def _resolve[S: (EmbeddingModelSpec, ChatModelSpec)](specs: tuple[S, ...], provider: str, model: str) -> S:
    if model.strip():
        candidates = [s for s in specs if s.provider == provider]
        for spec in candidates:
            if spec.model == model:
                return spec
        for spec in candidates:
            if spec.model == ANY_MODEL:
                return spec
    raise UnknownModelError(provider, model)


def get_embedding_spec(provider: str, model: str, *, include_fake: bool = False) -> EmbeddingModelSpec:
    return _resolve(_embedding_specs(include_fake), provider, model)


def get_chat_spec(provider: str, model: str, *, include_fake: bool = False) -> ChatModelSpec:
    return _resolve(_chat_specs(include_fake), provider, model)
