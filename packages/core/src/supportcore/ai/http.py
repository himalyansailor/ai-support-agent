"""Hardened HTTP clients for org-supplied (openai_compatible) endpoints.

Every outbound call to a tenant's base URL (embeddings, chat, the validator's /models check) goes
through `guarded_async_client()`:

- Redirects are never followed (`follow_redirects=False`) and proxy environment variables are
  ignored (`trust_env=False`).
- Each new connection resolves the host once, refuses it unless every address passes the base-URL
  rules (supportcore.ai.base_url; skipped when private addresses are allowed), and opens the socket
  to a vetted IP. The URL is not rewritten, so the Host header, TLS SNI and certificate checks still
  use the hostname, and a DNS answer that changes after the check (rebinding) cannot move the
  connection elsewhere. Should a redirect ever be followed, its host is vetted the same way.
- Response bodies are capped (`max_response_bytes`, 10 MB by default) and compression is not
  accepted, so the cap is on what is actually decoded.
- Keep-alive is off: a client is built per embedder or chat model and is never closed explicitly,
  so no idle socket may outlive the request that opened it.

`refusing_sync_client()` is handed to SDKs that insist on a sync client too; it sends nothing.
"""

from collections.abc import AsyncIterator, Iterable

import httpcore
import httpx

from supportcore.ai.base_url import public_addresses
from supportcore.ai.errors import ProviderResponseError

DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
RESPONSE_TOO_LARGE = "The provider's response was too large"
RESPONSE_COMPRESSED = "The provider sent a compressed response, which is not accepted"


class _VettedBackend(httpcore.AsyncNetworkBackend):
    """Resolves and vets the host itself, then connects to the vetted IP (never re-resolving)."""

    def __init__(self, *, allow_private: bool) -> None:
        self._inner = httpcore.AnyIOBackend()
        self._allow_private = allow_private

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if self._allow_private:
            return await self._inner.connect_tcp(host, port, timeout, local_address, socket_options)
        last_error: Exception | None = None
        for address in await public_addresses(host, port):  # CredentialInvalidError if any is private
            try:
                return await self._inner.connect_tcp(address, port, timeout, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        assert last_error is not None  # public_addresses() never returns an empty list
        raise last_error

    async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options=None):
        raise httpcore.ConnectError("Unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class _VettedTransport(httpx.AsyncHTTPTransport):
    """httpx's transport over a connection pool that uses _VettedBackend, without keep-alive."""

    def __init__(self, *, allow_private: bool) -> None:
        super().__init__(trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            max_connections=10,
            max_keepalive_connections=0,
            network_backend=_VettedBackend(allow_private=allow_private),
        )


class _CappedStream(httpx.AsyncByteStream):
    def __init__(self, inner: httpx.AsyncByteStream, limit: int) -> None:
        self._inner = inner
        self._limit = limit

    async def __aiter__(self) -> AsyncIterator[bytes]:
        received = 0
        async for chunk in self._inner:
            received += len(chunk)
            if received > self._limit:
                raise ProviderResponseError(RESPONSE_TOO_LARGE)
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class _CappedTransport(httpx.AsyncBaseTransport):
    """Asks for an uncompressed body, refuses a compressed or oversized one."""

    def __init__(self, inner: httpx.AsyncBaseTransport, limit: int) -> None:
        self._inner = inner
        self._limit = limit

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.headers["Accept-Encoding"] = "identity"
        response = await self._inner.handle_async_request(request)
        encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
        length = response.headers.get("Content-Length", "")
        refusal = None
        if encoding not in ("", "identity"):
            refusal = RESPONSE_COMPRESSED
        elif length.isdigit() and int(length) > self._limit:
            refusal = RESPONSE_TOO_LARGE
        if refusal is not None:
            await response.aclose()
            raise ProviderResponseError(refusal)
        assert isinstance(response.stream, httpx.AsyncByteStream)
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_CappedStream(response.stream, self._limit),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


def guarded_async_client(
    *,
    allow_private: bool,
    timeout: float | None = None,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """The client for one openai_compatible endpoint. `allow_private` is
    Settings.effective_allow_private_base_urls(). `transport` replaces the vetted network transport
    (tests pass httpx.MockTransport); the body cap still applies."""
    inner = transport if transport is not None else _VettedTransport(allow_private=allow_private)
    kwargs = {} if timeout is None else {"timeout": timeout}
    return httpx.AsyncClient(
        transport=_CappedTransport(inner, max_response_bytes),
        follow_redirects=False,
        trust_env=False,
        **kwargs,
    )


class _RefusingTransport(httpx.BaseTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise RuntimeError("Synchronous calls to OpenAI-compatible providers are not supported")


def refusing_sync_client() -> httpx.Client:
    """A sync client that sends nothing: only the async SDK paths may reach a tenant's endpoint."""
    return httpx.Client(transport=_RefusingTransport(), follow_redirects=False, trust_env=False)
