"""Chat model factory, the real credential validator and the openai_compatible base-URL guard.

No test here touches the network: provider calls are patched, HTTP goes through httpx.MockTransport
and DNS through a monkeypatched socket.getaddrinfo.
"""

import socket
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from cryptography.fernet import Fernet

from supportcore.ai.base_url import ensure_public_base_url
from supportcore.ai.credentials import CredentialInvalidError
from supportcore.ai.ports import ChatModelFactory
from supportcore.ai.validation import VALIDATION_TIMEOUT_SECONDS, ProviderCredentialValidator
from supportcore.config import Settings
from supportcore.registry import ProviderRegistry, UnknownProviderError
from supportcore.wiring import build_adapters

KEY = "sk-ant-api03-SUPERSECRETKEYVALUE-abcd1234WXYZ"


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


def _resolve_to(monkeypatch, *ips: str) -> list[str]:
    """Make every DNS lookup return `ips`; returns the list of looked-up hosts."""
    looked_up: list[str] = []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        looked_up.append(host)
        return [
            (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))
            for ip in ips
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return looked_up


# --- factory -------------------------------------------------------------------------------


def test_factory_builds_anthropic_with_key():
    from langchain_anthropic import ChatAnthropic

    chat = build_adapters(_settings()).chat_models.create(
        "anthropic", "claude-sonnet-5-5", api_key=KEY, base_url=None, temperature=0.3
    )
    assert isinstance(chat, ChatAnthropic)
    assert chat.model == "claude-sonnet-5-5"
    assert chat.anthropic_api_key.get_secret_value() == KEY
    assert chat.temperature == 0.3
    assert KEY not in repr(chat)


def test_factory_builds_openai_and_compatible():
    from langchain_openai import ChatOpenAI

    factory = build_adapters(_settings()).chat_models
    chat = factory.create("openai", "gpt-x", api_key="sk-openai-123456", base_url=None, temperature=0.0)
    assert isinstance(chat, ChatOpenAI) and chat.model_name == "gpt-x"
    compat = factory.create(
        "openai_compatible", "llama", api_key="k-123456789", base_url="http://llm.local:8000/v1", temperature=0.5
    )
    assert isinstance(compat, ChatOpenAI)
    assert compat.model_name == "llama" and compat.openai_api_base == "http://llm.local:8000/v1"
    # A custom base_url turns streamed usage off by default; the answer stream must ask for it.
    assert compat.stream_usage is True and compat._should_stream_usage()


def test_factory_rejects_unknown_provider_and_model():
    factory = build_adapters(_settings()).chat_models
    with pytest.raises(UnknownProviderError):
        factory.create("nope", "m", api_key=None, base_url=None, temperature=0.2)
    from supportcore.ai.catalog import UnknownModelError

    with pytest.raises(UnknownModelError):
        factory.create("anthropic", "claude-made-up", api_key=KEY, base_url=None, temperature=0.2)


async def test_fake_chat_only_in_test_env():
    assert "fake" not in build_adapters(_settings("dev")).chat_models.providers()
    chat = build_adapters(_settings()).chat_models.create(
        "fake", "fake-chat", api_key=None, base_url=None, temperature=0.2
    )
    assert (await chat.ainvoke("hello")).content == "Fake answer [1]"


# --- validator -----------------------------------------------------------------------------


class _ExplodingChat:
    """Stands in for a provider SDK whose error message echoes the key."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def ainvoke(self, *args, **kwargs):
        raise RuntimeError(f"Error code: 401 - invalid x-api-key {self.kwargs['api_key']}")


def _validator(settings, *, chat_registry=None, transport=None) -> ProviderCredentialValidator:
    adapters = build_adapters(settings)
    chat_factory = ChatModelFactory(chat_registry) if chat_registry is not None else adapters.chat_models
    return ProviderCredentialValidator(adapters.embedders, chat_factory, settings=settings, http_transport=transport)


async def test_validator_sanitises_provider_error():
    created: list[dict] = []
    registry: ProviderRegistry = ProviderRegistry("chat_model")

    @registry.register("anthropic")
    def _anthropic(**kwargs):
        created.append(kwargs)
        return _ExplodingChat(**kwargs)

    with pytest.raises(CredentialInvalidError) as exc:
        await _validator(_settings(), chat_registry=registry).validate("anthropic", KEY, None)
    assert KEY not in str(exc.value)
    assert "SUPERSECRET" not in str(exc.value)
    assert "invalid x-api-key" in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__context__ is None
    # One ping against the recommended model, capped to a single token, with the 10 s timeout.
    [kwargs] = created
    assert kwargs["model"] == "claude-sonnet-5-5"
    assert kwargs["max_tokens"] == 1 and kwargs["timeout"] == VALIDATION_TIMEOUT_SECONDS == 10


async def test_validator_anthropic_success_pings_once():
    from langchain_anthropic import ChatAnthropic

    with patch.object(ChatAnthropic, "ainvoke", AsyncMock(return_value=None)) as ping:
        await _validator(_settings()).validate("anthropic", KEY, None)
    ping.assert_awaited_once()
    assert ping.call_args.args[0] == "ping"


async def test_validator_openai_embeds_with_small_model():
    from langchain_openai import OpenAIEmbeddings

    key = "sk-openai-SECRETSECRETSECRET-1234"
    with patch.object(OpenAIEmbeddings, "aembed_query", AsyncMock(return_value=[0.0] * 3)) as embed:
        await _validator(_settings()).validate("openai", key, None)
    embed.assert_awaited_once_with("ping")

    boom = RuntimeError(f"Incorrect API key provided: {key}")
    with patch.object(OpenAIEmbeddings, "aembed_query", AsyncMock(side_effect=boom)):
        with pytest.raises(CredentialInvalidError) as exc:
            await _validator(_settings()).validate("openai", key, None)
    assert key not in str(exc.value) and "Incorrect API key" in str(exc.value)


async def test_validator_openai_compatible_get_models():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers["Authorization"] == "Bearer good-key-123":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(401, json={"error": "bad key bad-key-123"})

    v = _validator(_settings(), transport=httpx.MockTransport(handler))
    await v.validate("openai_compatible", "good-key-123", "http://llm.local:8000/v1/")
    assert str(seen[0].url) == "http://llm.local:8000/v1/models"
    with pytest.raises(CredentialInvalidError) as exc:
        await v.validate("openai_compatible", "bad-key-123", "http://llm.local:8000/v1")
    assert "401" in str(exc.value) and "bad-key-123" not in str(exc.value)


async def test_validator_openai_compatible_does_not_follow_redirects():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data"})
        raise AssertionError(f"redirect followed to {request.url}")

    v = _validator(_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(CredentialInvalidError) as exc:
        await v.validate("openai_compatible", "good-key-123", "http://llm.local:8000/v1")
    assert "302" in str(exc.value)


async def test_validator_timeout_becomes_invalid():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    v = _validator(_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(CredentialInvalidError):
        await v.validate("openai_compatible", "good-key-123", "http://llm.local:8000/v1")


async def test_validator_rejects_unknown_provider():
    with pytest.raises(CredentialInvalidError):
        await _validator(_settings()).validate("fake", KEY, None)


async def test_validator_rejects_private_base_url_in_prod(monkeypatch):
    looked_up = _resolve_to(monkeypatch, "10.0.0.5")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent to a private address")

    v = _validator(_settings("prod"), transport=httpx.MockTransport(handler))
    with pytest.raises(CredentialInvalidError) as exc:
        await v.validate("openai_compatible", "good-key-123", "https://internal.example.com/v1")
    assert str(exc.value) == "Base URL must be a public address"
    assert looked_up == ["internal.example.com"]


async def test_validator_allows_public_base_url_in_prod(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    v = _validator(_settings("prod"), transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    await v.validate("openai_compatible", "good-key-123", "https://llm.example.com/v1")


# --- base URL guard ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/v1",
        "http://api.localhost/v1",
        "http://127.0.0.1:8000/v1",
        "http://[::1]:8000/v1",
        "http://10.1.2.3/v1",
        "http://192.168.1.10/v1",
        "http://172.16.0.1/v1",
        "http://169.254.169.254/latest",
        "http://0.0.0.0/v1",
        "http://224.0.0.1/v1",
        "http://[::ffff:127.0.0.1]/v1",
        "http://[fe80::1]/v1",
        "http://100.64.0.1/v1",
    ],
)
async def test_guard_rejects_private_literals(url, monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")  # a literal must be rejected without trusting DNS
    with pytest.raises(CredentialInvalidError, match="^Base URL must be a public address$"):
        await ensure_public_base_url(url, allow_private=False)


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fd00::1"])
async def test_guard_rejects_hosts_resolving_to_private(ip, monkeypatch):
    _resolve_to(monkeypatch, ip)
    with pytest.raises(CredentialInvalidError, match="^Base URL must be a public address$"):
        await ensure_public_base_url("https://sneaky.example.com/v1", allow_private=False)


async def test_guard_rejects_if_any_resolved_address_is_private(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34", "127.0.0.1")
    with pytest.raises(CredentialInvalidError):
        await ensure_public_base_url("https://mixed.example.com/v1", allow_private=False)


async def test_guard_unresolvable_host(monkeypatch):
    def fail(*args, **kwargs):
        raise socket.gaierror("nodename nor servname provided")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(CredentialInvalidError, match="could not be resolved"):
        await ensure_public_base_url("https://nope.example.com/v1", allow_private=False)


async def test_guard_allows_public_and_skips_when_private_allowed(monkeypatch):
    looked_up = _resolve_to(monkeypatch, "93.184.216.34")
    await ensure_public_base_url("https://llm.example.com/v1", allow_private=False)
    await ensure_public_base_url("http://localhost:11434/v1", allow_private=True)
    await ensure_public_base_url("http://10.0.0.5/v1", allow_private=True)
    assert looked_up == ["llm.example.com"]


def test_allow_private_base_urls_effective_default():
    assert _settings("dev").effective_allow_private_base_urls() is True
    assert _settings("test").effective_allow_private_base_urls() is True
    assert _settings("prod").effective_allow_private_base_urls() is False
    assert _settings("prod", allow_private_base_urls=True).effective_allow_private_base_urls() is True
    assert _settings("dev", allow_private_base_urls=False).effective_allow_private_base_urls() is False
