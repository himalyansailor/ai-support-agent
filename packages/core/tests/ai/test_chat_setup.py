"""Shared chat model setup: load what the bot/playground need (short DB reads), build the model with the SSRF guard."""

import pytest

from supportcore.ai.chat_setup import ChatSetup, build_chat_model, load_chat_setup
from supportcore.ai.errors import CredentialInvalidError


@pytest.fixture
async def owner(session):
    from supportcore.db.models import User

    u = User(email="owner@x.com", password_hash="x", name="Owner")
    session.add(u)
    await session.flush()
    return u


@pytest.fixture
async def org(session, owner):
    from supportcore.events import EventBus
    from supportcore.orgs.service import OrganisationService

    return await OrganisationService(session, EventBus()).create(owner, "Acme")


@pytest.fixture
def bus():
    from supportcore.events import EventBus

    return EventBus()


@pytest.fixture
def kb_settings(tmp_path):
    from cryptography.fernet import Fernet

    from supportcore.config import Settings
    from supportcore.testing import TEST_DATABASE_URL

    return Settings(
        _env_file=None,
        env="test",
        database_url=TEST_DATABASE_URL,
        redis_url="redis://localhost:6379/0",
        jwt_secret="test-secret-test-secret-test-secret-123",
        smtp_from="noreply@test.local",
        secrets_key=Fernet.generate_key().decode(),
        file_storage="local",
        local_storage_path=str(tmp_path / "uploads"),
        vector_store="chroma",
    )


@pytest.fixture
def adapters(kb_settings):
    from supportcore.wiring import build_adapters

    return build_adapters(kb_settings)


def _setup(**overrides) -> ChatSetup:
    base = dict(
        provider="fake", model="fake-chat", temperature=0.2, api_key=None, base_url=None,
        organisation_name="Acme", language="en",
    )
    return ChatSetup(**{**base, **overrides})


async def test_load_chat_setup_none_when_not_ready(session, org, adapters, kb_settings, bus):
    # Fresh organisation: the recommended default needs an API key that is not set.
    assert await load_chat_setup(session, org.id, adapters, kb_settings, bus) is None


async def test_load_chat_setup_when_ready(session, org, owner, adapters, kb_settings, bus):
    from supportcore.ai.settings_service import OrganisationAISettingsService

    await OrganisationAISettingsService(session, org.id, bus, include_fake=True).update(
        "fake", "fake-chat", None, 0.3, owner
    )
    setup = await load_chat_setup(session, org.id, adapters, kb_settings, bus)
    assert setup == _setup(temperature=0.3, organisation_name="Acme", language=setup.language)
    assert setup.api_key is None and setup.base_url is None


def test_repr_hides_api_key():
    text = repr(_setup(api_key="sk-secret-123"))
    assert "sk-secret-123" not in text and "fake-chat" in text


async def test_build_chat_model_runs_ssrf_guard(adapters, kb_settings):
    settings = kb_settings.model_copy(update={"allow_private_base_urls": False})
    setup = _setup(provider="openai_compatible", model="m", api_key="k", base_url="http://169.254.169.254")
    with pytest.raises(CredentialInvalidError):
        await build_chat_model(adapters, settings, setup, timeout=5.0)


async def test_build_chat_model_passes_arguments(kb_settings):
    seen = {}

    class _Factory:
        def create(self, provider, model, **kwargs):
            seen.update(provider=provider, model=model, **kwargs)
            return object()

    adapters = type("A", (), {"chat_models": _Factory()})()
    await build_chat_model(adapters, kb_settings, _setup(temperature=0.7), temperature=0.0, timeout=20.0)
    assert seen == {
        "provider": "fake", "model": "fake-chat", "api_key": None, "base_url": None, "temperature": 0.0, "timeout": 20.0
    }


async def test_check_chat_base_url_and_create_are_separate(adapters, kb_settings):
    from supportcore.ai.chat_setup import check_chat_base_url, create_chat_model

    settings = kb_settings.model_copy(update={"allow_private_base_urls": False})
    bad = _setup(provider="openai_compatible", model="m", api_key="k", base_url="http://169.254.169.254")
    with pytest.raises(CredentialInvalidError):
        await check_chat_base_url(settings, bad)
    assert create_chat_model(adapters, _setup(), timeout=5.0) is not None  # no pre-check here
