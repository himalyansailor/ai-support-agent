"""Scrubbing of provider error text so API keys never reach messages, logs or responses."""

import re

from supportcore.ai.errors import CredentialInvalidError, ProviderResponseError

MAX_PROVIDER_MESSAGE_LENGTH = 200
_TOKEN_LIKE = re.compile(r"[A-Za-z0-9_\-]{20,}")
PROVIDER_UNREACHABLE = "Could not reach the provider"
PROVIDER_TIMED_OUT = "The provider did not respond in time"


def sanitize_provider_message(message: str, api_key: str | None) -> str:
    """Masks the key and any long token-like substring, and truncates to 200 characters."""
    if api_key:
        message = message.replace(api_key, "***")
    message = _TOKEN_LIKE.sub("***", message)
    return message.strip()[:MAX_PROVIDER_MESSAGE_LENGTH]


def _chain(exc: BaseException):
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def compatible_provider_message(exc: BaseException) -> str:
    """A fixed, readable message for a failed call to an org-supplied (openai_compatible) endpoint.

    Never includes the response body (the endpoint may be, or redirect to, something internal): our
    own guard errors keep their message, an HTTP error becomes "Provider returned HTTP <status>",
    anything else a fixed message."""
    for link in _chain(exc):
        if isinstance(link, (CredentialInvalidError, ProviderResponseError)):
            return str(link)
    for link in _chain(exc):
        status = getattr(link, "status_code", None)
        if status is None:
            status = getattr(getattr(link, "response", None), "status_code", None)
        if isinstance(status, int) and not isinstance(status, bool):
            return f"Provider returned HTTP {status}"
    if any("timeout" in type(link).__name__.lower() for link in _chain(exc)):
        return PROVIDER_TIMED_OUT
    return PROVIDER_UNREACHABLE
