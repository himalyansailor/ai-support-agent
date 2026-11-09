"""AI ports: the Embedder interface and the factories that build embedders and chat models from
provider registries (see wiring.embedder_registry / wiring.chat_registry)."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from supportcore.config import Settings
from supportcore.errors import DomainError
from supportcore.registry import ProviderRegistry

if TYPE_CHECKING:  # langchain_core is only needed by the adapters that build chat models
    from langchain_core.language_models import BaseChatModel


class EmbeddingError(DomainError):
    """Embedding failed. The message is readable and never contains an API key."""


class Embedder(Protocol):
    model_id: str
    dimensions: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class EmbedderFactory:
    """Creates embedders by provider key from a registry (see wiring.embedder_registry)."""

    def __init__(self, registry: ProviderRegistry[Embedder]) -> None:
        self._registry = registry

    def providers(self) -> list[str]:
        return self._registry.keys()

    def create(
        self, provider: str, model: str, *, api_key: str | None, base_url: str | None, settings: Settings
    ) -> Embedder:
        """Raises UnknownProviderError (listing available keys) or UnknownModelError for bad input."""
        return self._registry.create(provider, model=model, api_key=api_key, base_url=base_url, settings=settings)


class ChatModelFactory:
    """Creates LangChain chat models by provider key from a registry (see wiring.chat_registry).

    openai_compatible base URLs are not checked here: callers that send requests to one first run
    supportcore.ai.base_url.ensure_public_base_url (it needs an async DNS lookup). The model itself
    only talks to the endpoint through the hardened HTTP client (supportcore.ai.http), which vets
    the address of every connection and never follows redirects.
    """

    def __init__(self, registry: "ProviderRegistry[BaseChatModel]") -> None:
        self._registry = registry

    def providers(self) -> list[str]:
        return self._registry.keys()

    def create(
        self,
        provider: str,
        model: str,
        *,
        api_key: str | None,
        base_url: str | None,
        temperature: float,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> "BaseChatModel":
        """Raises UnknownProviderError (listing available keys) or UnknownModelError for bad input."""
        return self._registry.create(
            provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )


@dataclass(frozen=True)
class RerankCandidate:
    chunk_id: UUID
    text: str
    score: float


@dataclass(frozen=True)
class RerankResult:
    chunk_id: UUID
    score: float  # the candidate's original (vector similarity) score
    rerank_score: float | None  # None when no reranking happened


class Reranker(Protocol):
    """Reorders retrieval candidates. Returns at most `top_n` results (none when top_n <= 0 or
    there are no candidates), best first."""

    async def rerank(self, query: str, candidates: list[RerankCandidate], top_n: int) -> list[RerankResult]: ...


class RerankerFactory:
    """Creates rerankers by name from a registry (see wiring.reranker_registry)."""

    def __init__(self, registry: ProviderRegistry[Reranker], settings: Settings) -> None:
        self._registry = registry
        self._settings = settings

    def providers(self) -> list[str]:
        return self._registry.keys()

    def create(self, name: str, params: dict) -> Reranker:
        """Raises UnknownProviderError for an unknown name and ValueError for unknown/invalid params."""
        return self._registry.create(name, params=params, settings=self._settings)
