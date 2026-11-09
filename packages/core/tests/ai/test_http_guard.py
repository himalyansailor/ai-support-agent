"""The hardened HTTP client for openai_compatible endpoints (supportcore.ai.http).

Real loopback HTTP servers stand in for a tenant's endpoint and an internal service; DNS goes
through a stubbed socket.getaddrinfo that only answers for the made-up test hostnames.
"""

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpcore
import httpx
import pytest
from cryptography.fernet import Fernet

from supportcore.ai.base_url import NOT_PUBLIC_MESSAGE, ensure_public_base_url
from supportcore.ai.errors import CredentialInvalidError, ProviderResponseError
from supportcore.ai.http import guarded_async_client
from supportcore.ai.ports import EmbeddingError
from supportcore.ai.validation import ProviderCredentialValidator
from supportcore.config import Settings
from supportcore.wiring import build_adapters

SECRET_BODY = b"INTERNAL-METADATA-SECRET-VALUE-ABC"
PUBLIC_IP = "93.184.216.34"


def _settings(env="test", **overrides):
    if env == "prod":
        overrides.setdefault("secrets_key", Fernet.generate_key().decode())
        overrides.setdefault("billing", "stripe")  # prod refuses the dev billing adapter
        overrides.setdefault("stripe_secret_key", "sk_x")
        overrides.setdefault("stripe_webhook_secret", "whsec_x")
    return Settings(
        _env_file=None,
        env=env,
        database_url="postgresql+asyncpg://u:p@h/db",
        redis_url="redis://h:6379/0",
        jwt_secret="x" * 32,
        smtp_from="noreply@example.com",
        **overrides,
    )


class _QuietServer(HTTPServer):
    def handle_error(self, request, client_address):
        pass  # the client hanging up on a refused body is expected


class _Server:
    """A loopback HTTP server answering every request with `respond(handler)`; records the paths hit."""

    def __init__(self, respond):
        self.hits: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _any(self):
                outer.hits.append(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                respond(self)

            do_GET = do_POST = _any

            def log_message(self, *args):
                pass

        self._httpd = _QuietServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, args=(0.05,), daemon=True)
        self._thread.start()

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join()


def _send(handler, status, body=b"", headers=None):
    handler.send_response(status)
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


@pytest.fixture
def servers():
    started: list[_Server] = []

    def start(respond):
        server = _Server(respond)
        started.append(server)
        return server

    yield start
    for server in started:
        server.close()


@pytest.fixture
def internal(servers):
    """The internal service a redirect points at; it must never be reached."""
    return servers(lambda h: _send(h, 500, SECRET_BODY))


def _redirector(servers, internal):
    location = f"http://127.0.0.1:{internal.port}/internal/secret"
    return servers(lambda h: _send(h, 307, headers={"Location": location}))


def _stub_dns(monkeypatch, answers):
    """Answers lookups of hostnames in `answers` (a list per name, consumed one call at a time; the
    last one repeats); everything else (IP literals) goes to the real resolver."""
    real = socket.getaddrinfo
    calls: list[str] = []

    def fake(host, port, *args, **kwargs):
        if host in answers:
            calls.append(host)
            queue = answers[host]
            ip = queue.pop(0) if len(queue) > 1 else queue[0]
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            return [(family, socket.SOCK_STREAM, 6, "", (ip, port or 0))]
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return calls


# --- redirects ---------------------------------------------------------------------------------


async def test_compatible_embedder_does_not_follow_redirects(servers, internal):
    redirector = _redirector(servers, internal)
    settings = _settings()  # dev/test: private addresses allowed, so only the redirect rule applies
    embedder = build_adapters(settings).embedders.create(
        "openai_compatible", "m", api_key="sk-test-key-123456", base_url=f"http://127.0.0.1:{redirector.port}/v1",
        settings=settings,
    )
    with pytest.raises(EmbeddingError) as exc:
        await embedder.embed_query("hi")
    assert internal.hits == []
    assert redirector.hits  # the endpoint itself was called
    assert str(exc.value) == "Embedding request failed: Provider returned HTTP 307"


async def test_compatible_chat_does_not_follow_redirects(servers, internal):
    redirector = _redirector(servers, internal)
    chat = build_adapters(_settings()).chat_models.create(
        "openai_compatible", "m", api_key="sk-test-key-123456", base_url=f"http://127.0.0.1:{redirector.port}/v1",
        temperature=0.0,
    )
    with pytest.raises(Exception) as exc:
        await chat.ainvoke("hi")
    assert internal.hits == []
    assert redirector.hits
    assert getattr(exc.value, "status_code", None) == 307


async def test_redirect_to_private_host_is_refused_even_if_followed(servers, internal, monkeypatch):
    """Defence in depth: should a redirect be followed anyway, the new host is vetted at connect time."""
    redirector = _redirector(servers, internal)
    _stub_dns(monkeypatch, {"public.example.com": [PUBLIC_IP]})
    real_connect = httpcore.AnyIOBackend.connect_tcp

    async def connect(self, host, port, *args, **kwargs):  # the "public" endpoint lives on loopback
        return await real_connect(self, "127.0.0.1" if host == PUBLIC_IP else host, port, *args, **kwargs)

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    client = guarded_async_client(allow_private=False)
    async with client:
        with pytest.raises(CredentialInvalidError, match=f"^{NOT_PUBLIC_MESSAGE}$"):
            await client.get(f"http://public.example.com:{redirector.port}/v1/models", follow_redirects=True)
    assert redirector.hits == ["/v1/models"]
    assert internal.hits == []


# --- DNS rebinding -----------------------------------------------------------------------------


async def test_dns_answer_changing_after_the_check_cannot_reach_a_private_ip(servers, internal, monkeypatch):
    # First lookup (the ensure_public_base_url gate) says public; the next one (connect) says loopback.
    calls = _stub_dns(monkeypatch, {"rebind.example.com": [PUBLIC_IP, "127.0.0.1"]})
    settings = _settings("prod")
    base_url = f"http://rebind.example.com:{internal.port}/v1"
    await ensure_public_base_url(base_url, allow_private=False)
    embedder = build_adapters(settings).embedders.create(
        "openai_compatible", "m", api_key="sk-test-key-123456", base_url=base_url, settings=settings
    )
    with pytest.raises(EmbeddingError) as exc:
        await embedder.embed_query("hi")
    assert str(exc.value) == f"Embedding request failed: {NOT_PUBLIC_MESSAGE}"
    assert internal.hits == []
    assert calls == ["rebind.example.com", "rebind.example.com"]


async def test_connection_goes_to_the_vetted_address(servers, monkeypatch):
    """The host is resolved once per connection and the socket opened to that exact IP; the Host
    header still names the host."""
    seen: list[str] = []
    endpoint = servers(lambda h: (seen.append(h.headers["Host"]), _send(h, 200, b"{}")))
    _stub_dns(monkeypatch, {"llm.example.com": [PUBLIC_IP]})
    connected: list[str] = []
    real_connect = httpcore.AnyIOBackend.connect_tcp

    async def connect(self, host, port, *args, **kwargs):
        connected.append(host)
        return await real_connect(self, "127.0.0.1" if host == PUBLIC_IP else host, port, *args, **kwargs)

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    async with guarded_async_client(allow_private=False) as client:
        response = await client.get(f"http://llm.example.com:{endpoint.port}/v1/models")
    assert response.status_code == 200
    assert connected == [PUBLIC_IP]
    assert seen == [f"llm.example.com:{endpoint.port}"]


async def test_private_literal_refused_at_connect_in_prod(internal):
    async with guarded_async_client(allow_private=False) as client:
        with pytest.raises(CredentialInvalidError, match=f"^{NOT_PUBLIC_MESSAGE}$"):
            await client.get(f"http://127.0.0.1:{internal.port}/v1/models")
    assert internal.hits == []


async def test_env_proxies_are_ignored(monkeypatch, servers):
    endpoint = servers(lambda h: _send(h, 200, b"{}"))
    monkeypatch.setenv("HTTP_PROXY", "http://10.255.255.1:3128")
    monkeypatch.setenv("ALL_PROXY", "http://10.255.255.1:3128")
    async with guarded_async_client(allow_private=True) as client:
        response = await client.get(f"http://127.0.0.1:{endpoint.port}/v1/models")
    assert response.status_code == 200


# --- body cap ----------------------------------------------------------------------------------


async def test_body_cap_with_content_length(servers):
    endpoint = servers(lambda h: _send(h, 200, b"x" * 4096))
    async with guarded_async_client(allow_private=True, max_response_bytes=1024) as client:
        with pytest.raises(ProviderResponseError, match="too large"):
            await client.get(f"http://127.0.0.1:{endpoint.port}/v1/models")


async def test_body_cap_without_content_length(servers):
    def stream(handler):  # HTTP/1.0, no Content-Length: the body runs until the connection closes
        handler.send_response(200)
        handler.end_headers()
        for _ in range(8):
            handler.wfile.write(b"y" * 512)

    endpoint = servers(stream)
    async with guarded_async_client(allow_private=True, max_response_bytes=1024) as client:
        with pytest.raises(ProviderResponseError, match="too large"):
            await client.get(f"http://127.0.0.1:{endpoint.port}/v1/models")


async def test_body_under_cap_is_returned_and_compression_is_not_requested(servers):
    encodings: list[str | None] = []
    endpoint = servers(lambda h: (encodings.append(h.headers.get("Accept-Encoding")), _send(h, 200, b"ok")))
    async with guarded_async_client(allow_private=True, max_response_bytes=1024) as client:
        response = await client.get(f"http://127.0.0.1:{endpoint.port}/v1/models")
    assert response.content == b"ok"
    assert encodings == ["identity"]


async def test_compressed_response_is_refused(servers):
    endpoint = servers(lambda h: _send(h, 200, b"\x1f\x8b", headers={"Content-Encoding": "gzip"}))
    async with guarded_async_client(allow_private=True) as client:
        with pytest.raises(ProviderResponseError, match="compressed"):
            await client.get(f"http://127.0.0.1:{endpoint.port}/v1/models")


async def test_embedder_reports_too_large_response(servers):
    endpoint = servers(lambda h: _send(h, 200, b"z" * (11 * 1024 * 1024)))
    settings = _settings()
    embedder = build_adapters(settings).embedders.create(
        "openai_compatible", "m", api_key="sk-test-key-123456", base_url=f"http://127.0.0.1:{endpoint.port}/v1",
        settings=settings,
    )
    with pytest.raises(EmbeddingError, match="too large"):
        await embedder.embed_query("hi")


async def test_validator_models_read_is_capped_at_1mb():
    big = httpx.Response(200, content=b"m" * (1024 * 1024 + 1))
    adapters = build_adapters(_settings())
    v = ProviderCredentialValidator(
        adapters.embedders, adapters.chat_models, settings=_settings(), http_transport=httpx.MockTransport(lambda r: big)
    )
    with pytest.raises(CredentialInvalidError, match="too large"):
        await v.validate("openai_compatible", "good-key-123", "http://llm.local:8000/v1")


# --- error text --------------------------------------------------------------------------------


async def test_compatible_errors_never_include_the_response_body(servers):
    endpoint = servers(lambda h: _send(h, 502, b'{"error": {"message": "' + SECRET_BODY + b'"}}'))
    settings = _settings()
    embedder = build_adapters(settings).embedders.create(
        "openai_compatible", "m", api_key="sk-test-key-123456", base_url=f"http://127.0.0.1:{endpoint.port}/v1",
        settings=settings,
    )
    with pytest.raises(EmbeddingError) as exc:
        await embedder.embed_query("hi")
    assert str(exc.value) == "Embedding request failed: Provider returned HTTP 502"
