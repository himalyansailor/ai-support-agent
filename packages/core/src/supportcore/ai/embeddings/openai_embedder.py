"""OpenAI and OpenAI-compatible embeddings through langchain-openai.

OpenAI-compatible endpoints are org-supplied: they are called through the hardened HTTP client
(supportcore.ai.http) and their errors are reported without the response body.
"""

from supportcore.ai.http import guarded_async_client, refusing_sync_client
from supportcore.ai.ports import EmbeddingError
from supportcore.ai.sanitize import compatible_provider_message, sanitize_provider_message
from supportcore.config import Settings


class OpenAIEmbedder:
    """Embeds via `OpenAIEmbeddings`, sending at most `settings.embed_batch_size` texts per request.

    `dimensions` comes from the catalog. For models whose size is unknown (openai_compatible) call
    `await probe_dimensions()` once first; reading `dimensions` before that raises.
    """

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None,
        base_url: str | None,
        settings: Settings,
        dimensions: int | None,
        compatible: bool = False,
    ) -> None:
        from langchain_openai import OpenAIEmbeddings  # lazy: heavy import

        self.model_id = model
        self._api_key = api_key
        self._batch_size = max(1, settings.embed_batch_size)
        self._dimensions = dimensions
        self._compatible = compatible
        kwargs: dict = {}
        if compatible:
            kwargs = {
                "check_embedding_ctx_length": False,
                "http_async_client": guarded_async_client(
                    allow_private=settings.effective_allow_private_base_urls()
                ),
                "http_client": refusing_sync_client(),
            }
        self._client = OpenAIEmbeddings(model=model, api_key=api_key, base_url=base_url, **kwargs)

    @property
    def dimensions(self) -> int:
        if self._dimensions is None:
            raise EmbeddingError(
                f"Embedding size of model {self.model_id!r} is unknown; call probe_dimensions() first"
            )
        return self._dimensions

    async def probe_dimensions(self) -> int:
        """Embeds one short text to discover the vector size (and validates the key/endpoint)."""
        if self._dimensions is None:
            self._dimensions = len(await self.embed_query("dimension probe"))
        return self._dimensions

    def _error(self, exc: Exception) -> EmbeddingError:
        if self._compatible:
            detail = compatible_provider_message(exc)
        else:
            detail = sanitize_provider_message(str(exc), self._api_key) or type(exc).__name__
        return EmbeddingError(f"Embedding request failed: {detail}")

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        try:
            for start in range(0, len(texts), self._batch_size):
                vectors.extend(await self._client.aembed_documents(texts[start : start + self._batch_size]))
        except Exception as exc:
            raise self._error(exc) from None
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        try:
            return await self._client.aembed_query(text)
        except Exception as exc:
            raise self._error(exc) from None
