"""Checks an org-provided API key against its provider with one cheap call.

- anthropic: one `ainvoke("ping")` with max_tokens=1 against the recommended Anthropic model.
- openai: one `embed_query("ping")` with text-embedding-3-small.
- openai_compatible: GET {base_url}/models with a Bearer token; any 2xx is valid. The base URL passes
  the SSRF guard right before the request, which goes through the hardened HTTP client
  (supportcore.ai.http: no redirects, vetted addresses) and reads at most 1 MB of the answer. Errors
  never include the response body.

Every check is bounded by VALIDATION_TIMEOUT_SECONDS. Any failure becomes CredentialInvalidError with
a sanitised message; the key is never logged and never kept in an exception chain.
"""

import asyncio
from typing import TYPE_CHECKING

from supportcore.ai.base_url import ensure_public_base_url
from supportcore.ai.catalog import chat_models
from supportcore.ai.errors import CredentialInvalidError
from supportcore.ai.http import guarded_async_client
from supportcore.ai.ports import ChatModelFactory, EmbedderFactory
from supportcore.ai.sanitize import compatible_provider_message, sanitize_provider_message
from supportcore.config import Settings

if TYPE_CHECKING:
    import httpx

VALIDATION_TIMEOUT_SECONDS = 10
VALIDATION_EMBEDDING_MODEL = "text-embedding-3-small"
MAX_MODELS_RESPONSE_BYTES = 1024 * 1024
DEEPGRAM_PROJECTS_URL = "https://api.deepgram.com/v1/projects"


def _recommended_model(provider: str) -> str:
    return next(m.model for m in chat_models() if m.provider == provider and m.recommended)


class ProviderCredentialValidator:
    """The real CredentialValidator (supportcore.ai.credentials)."""

    def __init__(
        self,
        embedder_factory: EmbedderFactory,
        chat_factory: ChatModelFactory,
        *,
        settings: Settings,
        http_transport: "httpx.AsyncBaseTransport | None" = None,  # tests inject httpx.MockTransport
    ) -> None:
        self._embedders = embedder_factory
        self._chat = chat_factory
        self._settings = settings
        self._http_transport = http_transport

    async def validate(self, provider: str, api_key: str, base_url: str | None) -> None:
        try:
            async with asyncio.timeout(VALIDATION_TIMEOUT_SECONDS):
                await self._check(provider, api_key, base_url)
        except TimeoutError:
            message = f"The provider did not respond within {VALIDATION_TIMEOUT_SECONDS} seconds"
        except CredentialInvalidError as exc:  # our own message (may still be sanitised for safety)
            message = sanitize_provider_message(str(exc), api_key) or "The provider rejected this API key"
        except Exception as exc:  # provider SDK errors may echo the key: sanitise everything
            if provider == "openai_compatible":
                message = compatible_provider_message(exc)
            else:
                message = sanitize_provider_message(str(exc), api_key) or "The provider rejected this API key"
        else:
            return
        # Raised outside the except blocks so the provider exception is not kept as __context__.
        raise CredentialInvalidError(message)

    async def _check(self, provider: str, api_key: str, base_url: str | None) -> None:
        if provider == "anthropic":
            chat = self._chat.create(
                "anthropic",
                _recommended_model("anthropic"),
                api_key=api_key,
                base_url=None,
                temperature=0.0,
                max_tokens=1,
                timeout=VALIDATION_TIMEOUT_SECONDS,
            )
            await chat.ainvoke("ping")
        elif provider == "openai":
            embedder = self._embedders.create(
                "openai", VALIDATION_EMBEDDING_MODEL, api_key=api_key, base_url=None, settings=self._settings
            )
            await embedder.embed_query("ping")
        elif provider == "openai_compatible":
            await self._check_models_endpoint(api_key, base_url)
        elif provider == "deepgram":
            await self._check_deepgram(api_key)
        else:
            raise CredentialInvalidError(f"Unsupported provider: {provider[:50]}")

    async def _check_models_endpoint(self, api_key: str, base_url: str | None) -> None:
        if not base_url:
            raise CredentialInvalidError("A base URL is required for OpenAI-compatible providers")
        allow_private = self._settings.effective_allow_private_base_urls()
        await ensure_public_base_url(base_url, allow_private=allow_private)
        async with guarded_async_client(
            allow_private=allow_private,
            timeout=VALIDATION_TIMEOUT_SECONDS,
            max_response_bytes=MAX_MODELS_RESPONSE_BYTES,
            transport=self._http_transport,
        ) as client:
            response = await client.get(
                base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {api_key}"}
            )
        if not response.is_success:
            raise CredentialInvalidError(f"The provider answered HTTP {response.status_code} for /models")

    async def _check_deepgram(self, api_key: str) -> None:
        # A fixed, public host: no SSRF guard needed. Never echo the response body.
        import httpx

        async with httpx.AsyncClient(
            timeout=VALIDATION_TIMEOUT_SECONDS, transport=self._http_transport, trust_env=False
        ) as client:
            response = await client.get(DEEPGRAM_PROJECTS_URL, headers={"Authorization": f"Token {api_key}"})
        if response.status_code == 200:
            return
        if response.status_code in (401, 403):
            raise CredentialInvalidError("The provider rejected this API key")
        raise CredentialInvalidError("Could not validate the API key with the provider")
