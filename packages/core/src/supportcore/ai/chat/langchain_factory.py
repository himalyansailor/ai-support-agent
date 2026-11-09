"""LangChain chat models for the hosted and OpenAI-compatible providers.

Provider SDKs are imported inside each builder so importing wiring stays cheap. OpenAI-compatible
endpoints are org-supplied and are only called through the hardened HTTP client (supportcore.ai.http).
"""

from typing import TYPE_CHECKING, Any

from supportcore.ai.catalog import get_chat_spec
from supportcore.ai.http import guarded_async_client, refusing_sync_client

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel


def _optional(max_tokens: int | None, timeout: float | None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if timeout is not None:
        kwargs["timeout"] = timeout
    return kwargs


def anthropic_chat_model(
    *,
    model: str,
    api_key: str | None,
    base_url: str | None,
    temperature: float,
    max_tokens: int | None = None,
    timeout: float | None = None,
) -> "BaseChatModel":
    if base_url is not None:
        raise ValueError("The anthropic provider always uses its official endpoint; base_url is not allowed")
    get_chat_spec("anthropic", model)  # raises UnknownModelError
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(
        model=model, api_key=api_key, temperature=temperature, **_optional(max_tokens, timeout)
    )


def openai_chat_model(
    *,
    model: str,
    api_key: str | None,
    base_url: str | None,
    temperature: float,
    max_tokens: int | None = None,
    timeout: float | None = None,
) -> "BaseChatModel":
    if base_url is not None:
        raise ValueError("The openai provider always uses its official endpoint; base_url is not allowed")
    get_chat_spec("openai", model)
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model=model, api_key=api_key, temperature=temperature, **_optional(max_tokens, timeout))


def openai_compatible_chat_model(
    *,
    model: str,
    api_key: str | None,
    base_url: str | None,
    temperature: float,
    max_tokens: int | None = None,
    timeout: float | None = None,
    allow_private_base_urls: bool,
) -> "BaseChatModel":
    """`allow_private_base_urls` is Settings.effective_allow_private_base_urls() (bound in wiring)."""
    if not base_url:
        raise ValueError("The openai_compatible provider needs a base_url")
    get_chat_spec("openai_compatible", model)
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        http_async_client=guarded_async_client(allow_private=allow_private_base_urls),
        http_client=refusing_sync_client(),
        # A custom base_url turns ChatOpenAI's streamed usage off by default, so the answer stream would report
        # no tokens; ask for it (stream_options.include_usage). A server that ignores it leaves usage unknown.
        stream_usage=True,
        **_optional(max_tokens, timeout),
    )
