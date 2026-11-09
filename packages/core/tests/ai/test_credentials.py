import json
import logging
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from supportcore.ai.credentials import (
    CredentialInUseError,
    CredentialInvalidError,
    CredentialLabelTakenError,
    CredentialService,
    sanitize_provider_message,
)
from supportcore.ai.events import AISettingsUpdated, CredentialCreated, CredentialDeleted
from supportcore.ai.catalog import UnknownModelError
from supportcore.ai.settings_service import (
    CredentialProviderMismatchError,
    InvalidModelNameError,
    InvalidTemperatureError,
    OrganisationAISettingsService,
)
from supportcore.audit.subscribers import register_audit_subscribers
from supportcore.db.models import AuditLog, OrganisationAISettings, ProviderCredential, User
from supportcore.errors import NotFoundError
from supportcore.events import DomainEvent, EventBus
from supportcore.orgs.service import OrganisationService
from supportcore.secrets.fernet_cipher import FernetCipher

KEY = "sk-ant-api03-SUPERSECRETKEYVALUE-abcd1234WXYZ"


class StubValidator:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.error = error

    async def validate(self, provider: str, api_key: str, base_url: str | None) -> None:
        self.calls.append((provider, api_key, base_url))
        if self.error is not None:
            raise self.error


@pytest.fixture
async def owner(session) -> User:
    u = User(email="owner@x.com", password_hash="x", name="Owner")
    session.add(u)
    await session.flush()
    return u


@pytest.fixture
def seen() -> list[DomainEvent]:
    return []


@pytest.fixture
def bus(session, seen) -> EventBus:
    b = EventBus()
    register_audit_subscribers(b, lambda: session)

    async def record(e: DomainEvent) -> None:
        seen.append(e)

    b.subscribe(DomainEvent, record)
    return b


@pytest.fixture
async def org(session, owner):
    return await OrganisationService(session, EventBus()).create(owner, "Acme")


@pytest.fixture
async def other_org(session, owner):
    return await OrganisationService(session, EventBus()).create(owner, "Other")


@pytest.fixture
def cipher() -> FernetCipher:
    return FernetCipher(Fernet.generate_key().decode())


@pytest.fixture
def validator() -> StubValidator:
    return StubValidator()


@pytest.fixture
def creds(session, org, cipher, validator, bus) -> CredentialService:
    return CredentialService(session, org.id, cipher, validator, bus, allow_private_base_urls=True)


@pytest.fixture
def ai_settings(session, org, bus) -> OrganisationAISettingsService:
    return OrganisationAISettingsService(session, org.id, bus)


async def _count(session, org_id) -> int:
    rows = (await session.execute(select(ProviderCredential).where(ProviderCredential.organisation_id == org_id))).all()
    return len(rows)


# --- credentials -------------------------------------------------------------------------


async def test_create_credential_stores_ciphertext_and_last4(session, creds, org, owner, validator, seen):
    row = await creds.create("anthropic", "Main", f"  {KEY}\n", None, owner)
    assert row.organisation_id == org.id
    assert row.encrypted_api_key != KEY and KEY not in row.encrypted_api_key
    assert row.last4 == KEY[-4:]
    assert creds.decrypt_key(row) == KEY
    assert row.created_by_user_id == owner.id
    assert row.last_validated_at is not None
    assert validator.calls == [("anthropic", KEY, None)]
    [event] = [e for e in seen if isinstance(e, CredentialCreated)]
    assert (event.credential_id, event.provider, event.label) == (row.id, "anthropic", "Main")
    assert [c.id for c in await creds.list()] == [row.id]


async def test_create_credential_runs_validator_and_maps_error(session, org, owner, cipher, bus):
    error = CredentialInvalidError(f"401 Unauthorized: invalid x-api-key {KEY} (request req_0123456789abcdefghijKLMN)")
    svc = CredentialService(session, org.id, cipher, StubValidator(error), bus)
    with pytest.raises(CredentialInvalidError) as exc:
        await svc.create("anthropic", "Main", KEY, None, owner)
    message = str(exc.value)
    assert KEY not in message
    assert "SUPERSECRET" not in message
    assert "req_0123456789abcdefghijKLMN" not in message
    assert message.startswith("401 Unauthorized")
    assert exc.value.__cause__ is None and exc.value.__context__ is None
    assert await _count(session, org.id) == 0


async def test_unexpected_validator_exception_becomes_sanitised_invalid(session, org, owner, cipher, bus, caplog):
    svc = CredentialService(session, org.id, cipher, StubValidator(RuntimeError(f"boom {KEY}")), bus)
    with caplog.at_level(logging.DEBUG), pytest.raises(CredentialInvalidError) as exc:
        await svc.create("anthropic", "Main", KEY, None, owner)
    assert KEY not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__context__ is None
    assert KEY not in caplog.text
    assert await _count(session, org.id) == 0


def test_sanitize_provider_message():
    msg = sanitize_provider_message(f"bad key {KEY} token=abcdefghijklmnopqrstuvwxyz0123 ok", KEY)
    assert KEY not in msg and "abcdefghijklmnopqrstuvwxyz0123" not in msg
    assert msg == "bad key *** token=*** ok"
    assert len(sanitize_provider_message("x" * 50 + " " + "word " * 100, "irrelevant-key")) <= 200
    assert sanitize_provider_message("", "k") == ""


async def test_create_rejects_empty_and_short_keys(session, creds, org, owner, validator):
    with pytest.raises(CredentialInvalidError, match="API key is required"):
        await creds.create("anthropic", "Main", "   ", None, owner)
    with pytest.raises(CredentialInvalidError, match="API key looks too short"):
        await creds.create("anthropic", "Main", " abc1234 ", None, owner)
    assert validator.calls == []
    assert await _count(session, org.id) == 0


async def test_create_rejects_unknown_provider_and_blank_label(creds, owner, validator):
    with pytest.raises(CredentialInvalidError, match="Unsupported provider"):
        await creds.create("fake", "Main", KEY, None, owner)
    with pytest.raises(CredentialInvalidError, match="Label is required"):
        await creds.create("anthropic", "  ", KEY, None, owner)
    assert validator.calls == []


async def test_openai_compatible_requires_base_url(session, creds, org, owner, validator):
    for bad in (None, "", "ftp://models.local", "not a url", "http://"):
        with pytest.raises(CredentialInvalidError, match="base URL"):
            await creds.create("openai_compatible", "Local", KEY, bad, owner)
    assert validator.calls == []
    row = await creds.create("openai_compatible", "Local", KEY, " http://llm.local:8000/v1 ", owner)
    assert row.base_url == "http://llm.local:8000/v1"
    assert validator.calls == [("openai_compatible", KEY, "http://llm.local:8000/v1")]


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ("http://user:pass@llm.local:8000/v1", "must not contain a user name or password"),
        ("http://user@llm.local/v1", "must not contain a user name or password"),
        ("http://llm.local/v1?key=abc", "must not contain a query string or fragment"),
        ("http://llm.local/v1#frag", "must not contain a query string or fragment"),
        ("http://llm.local/v1?", "must not contain a query string or fragment"),
        ("http://llm.local:99999/v1", "invalid port"),
    ],
)
async def test_base_url_rejects_userinfo_query_and_fragment(creds, owner, validator, bad, message):
    with pytest.raises(CredentialInvalidError, match=message):
        await creds.create("openai_compatible", "Local", KEY, bad, owner)
    assert validator.calls == []


async def test_base_url_ignored_for_hosted_providers(creds, owner):
    row = await creds.create("openai", "OpenAI", KEY, "https://evil.example.com", owner)
    assert row.base_url is None


async def test_label_unique_case_insensitive_within_org(session, creds, owner, other_org, cipher, validator, bus):
    await creds.create("anthropic", "Main", KEY, None, owner)
    with pytest.raises(CredentialLabelTakenError):
        await creds.create("openai", " MAIN ", KEY, None, owner)
    other = CredentialService(session, other_org.id, cipher, validator, bus)
    assert (await other.create("anthropic", "Main", KEY, None, owner)).label == "Main"


async def test_label_unique_constraint_guard(session, creds, org, owner, cipher):
    session.add(
        ProviderCredential(
            organisation_id=org.id, provider="openai", label="Race", encrypted_api_key=cipher.encrypt(KEY), last4="WXYZ"
        )
    )
    await session.flush()

    async def no_check(*args, **kwargs) -> None:
        return None

    creds._ensure_label_free = no_check  # simulate losing the race after the check
    with pytest.raises(CredentialLabelTakenError):
        await creds.create("anthropic", "RACE", KEY, None, owner)  # DB index is case-insensitive too
    assert await _count(session, org.id) == 1  # session still usable


async def test_list_is_tenant_scoped(session, creds, owner, other_org, cipher, validator, bus):
    await creds.create("anthropic", "Main", KEY, None, owner)
    other = CredentialService(session, other_org.id, cipher, validator, bus)
    assert await other.list() == []


async def test_delete_credential(session, creds, org, owner, seen):
    row = await creds.create("anthropic", "Main", KEY, None, owner)
    await creds.delete(row.id, owner)
    assert await _count(session, org.id) == 0
    [event] = [e for e in seen if isinstance(e, CredentialDeleted)]
    assert (event.credential_id, event.provider, event.label) == (row.id, "anthropic", "Main")


async def test_delete_other_org_credential_is_not_found(session, creds, owner, other_org, cipher, validator, bus):
    row = await creds.create("anthropic", "Main", KEY, None, owner)
    other = CredentialService(session, other_org.id, cipher, validator, bus)
    with pytest.raises(NotFoundError):
        await other.delete(row.id, owner)
    with pytest.raises(NotFoundError):
        await other.delete(uuid4(), owner)
    with pytest.raises(NotFoundError):
        other.decrypt_key(row)


async def test_delete_credential_in_use_by_ai_settings_409(session, creds, ai_settings, org, owner):
    row = await creds.create("anthropic", "Main", KEY, None, owner)
    await ai_settings.update("anthropic", "claude-sonnet-5-5", row.id, 0.2, owner)
    with pytest.raises(CredentialInUseError, match="Credential is in use by AI settings"):
        await creds.delete(row.id, owner)
    assert await _count(session, org.id) == 1


async def test_delete_runs_extra_in_use_checks(session, org, owner, cipher, validator, bus):
    calls = []

    async def kb_check(s, organisation_id, credential_id):
        calls.append((organisation_id, credential_id))
        return "knowledge base \"Docs\""

    svc = CredentialService(session, org.id, cipher, validator, bus, in_use_checks=[kb_check])
    row = await svc.create("openai", "Main", KEY, None, owner)
    with pytest.raises(CredentialInUseError, match='Credential is in use by knowledge base "Docs"'):
        await svc.delete(row.id, owner)
    assert calls == [(org.id, row.id)]


async def test_audit_metadata_never_contains_key(session, creds, ai_settings, org, owner, seen, caplog):
    with caplog.at_level(logging.DEBUG):
        row = await creds.create("openai", "Main", KEY, None, owner)
        await ai_settings.update("openai", "gpt-4.1", row.id, 0.5, owner)
        await ai_settings.update("anthropic", "claude-sonnet-5-5", None, 0.5, owner)
        await creds.delete(row.id, owner)

    rows = list(
        (await session.execute(select(AuditLog).where(AuditLog.organisation_id == org.id))).scalars()
    )
    actions = [r.action for r in rows]
    assert actions.count("credential.created") == 1
    assert actions.count("credential.deleted") == 1
    assert actions.count("ai_settings.updated") == 2
    created = next(r for r in rows if r.action == "credential.created")
    assert (created.target_type, created.target_id) == ("credential", str(row.id))
    assert created.metadata_ == {"provider": "openai", "label": "Main"}
    updated = next(r for r in rows if r.action == "ai_settings.updated")
    assert (updated.target_type, updated.target_id) == ("organisation", str(org.id))

    blob = json.dumps([r.metadata_ for r in rows]) + repr(seen) + caplog.text
    assert KEY not in blob
    assert KEY[-12:] not in blob
    assert row.encrypted_api_key not in blob
    assert KEY not in repr(row)


# --- AI settings -------------------------------------------------------------------------


async def test_ai_settings_defaults_created_lazily(session, ai_settings, org):
    s = await ai_settings.get()
    assert (s.organisation_id, s.chat_provider, s.chat_model, s.chat_credential_id, s.temperature) == (
        org.id,
        "anthropic",
        "claude-sonnet-5-5",
        None,
        0.2,
    )
    again = await ai_settings.get()
    assert again is s
    rows = (await session.execute(select(OrganisationAISettings))).scalars().all()
    assert len(rows) == 1


async def test_ai_settings_update_and_event(session, creds, ai_settings, owner, seen):
    cred = await creds.create("openai", "OpenAI", KEY, None, owner)
    s = await ai_settings.update(" openai ", " gpt-4.1-mini ", cred.id, 0.7, owner)
    assert (s.chat_provider, s.chat_model, s.chat_credential_id, s.temperature) == (
        "openai",
        "gpt-4.1-mini",
        cred.id,
        0.7,
    )
    [event] = [e for e in seen if isinstance(e, AISettingsUpdated)]
    assert event.changes == {
        "chat_provider": {"from": "anthropic", "to": "openai"},
        "chat_model": {"from": "claude-sonnet-5-5", "to": "gpt-4.1-mini"},
        "chat_credential_id": {"from": None, "to": str(cred.id)},
        "temperature": {"from": 0.2, "to": 0.7},
    }
    seen.clear()
    await ai_settings.update("openai", "gpt-4.1-mini", cred.id, 0.7, owner)
    assert [e for e in seen if isinstance(e, AISettingsUpdated)] == []


async def test_ai_settings_rejects_credential_of_other_provider(creds, ai_settings, owner):
    cred = await creds.create("openai", "OpenAI", KEY, None, owner)
    with pytest.raises(CredentialProviderMismatchError):
        await ai_settings.update("anthropic", "claude-sonnet-5-5", cred.id, 0.2, owner)
    s = await ai_settings.get()
    assert s.chat_credential_id is None


async def test_ai_settings_rejects_other_org_credential(session, ai_settings, owner, other_org, cipher, validator, bus):
    other = CredentialService(session, other_org.id, cipher, validator, bus)
    cred = await other.create("anthropic", "Theirs", KEY, None, owner)
    with pytest.raises(NotFoundError):
        await ai_settings.update("anthropic", "claude-sonnet-5-5", cred.id, 0.2, owner)


async def test_ai_settings_validates_model_and_temperature(ai_settings, owner):
    with pytest.raises(UnknownModelError):
        await ai_settings.update("anthropic", "claude-unknown", None, 0.2, owner)
    with pytest.raises(UnknownModelError):
        await ai_settings.update("fake", "fake-chat", None, 0.2, owner)
    for bad in (-0.01, 1.01, float("nan")):
        with pytest.raises(InvalidTemperatureError):
            await ai_settings.update("anthropic", "claude-sonnet-5-5", None, bad, owner)
    s = await ai_settings.update("anthropic", "claude-sonnet-5-5", None, 1.0, owner)
    assert s.temperature == 1.0


async def test_ai_settings_bounds_free_text_model_name(ai_settings, owner):
    for bad in ("", "   ", "m" * 201):
        with pytest.raises(InvalidModelNameError):
            await ai_settings.update("openai", bad, None, 0.2, owner)
    s = await ai_settings.update("openai", "  " + "m" * 200 + "  ", None, 0.2, owner)
    assert s.chat_model == "m" * 200


async def test_ai_settings_fake_provider_when_enabled(session, org, owner, bus):
    svc = OrganisationAISettingsService(session, org.id, bus, include_fake=True)
    s = await svc.update("fake", "fake-chat", None, 0.0, owner)
    assert (s.chat_provider, s.chat_model) == ("fake", "fake-chat")


async def test_deleting_org_cascades(session, org_a, owner, cipher, validator, bus):
    await CredentialService(session, org_a.id, cipher, validator, bus).create("anthropic", "Main", KEY, None, owner)
    await OrganisationAISettingsService(session, org_a.id, bus).get()
    await session.delete(org_a)
    await session.flush()
    assert (await session.execute(select(ProviderCredential))).first() is None
    assert (await session.execute(select(OrganisationAISettings))).first() is None


# --- base URL guard at creation, unreadable keys, readiness ---------------------------------


async def test_create_rejects_private_base_url_when_not_allowed(
    session, org, owner, cipher, validator, bus, monkeypatch
):
    import socket

    def resolve_private(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve_private)
    svc = CredentialService(session, org.id, cipher, validator, bus)  # default: private addresses refused
    for url in ("http://localhost:11434/v1", "http://127.0.0.1:8000/v1", "https://internal.example.com/v1"):
        with pytest.raises(CredentialInvalidError, match="^Base URL must be a public address$"):
            await svc.create("openai_compatible", "Local", KEY, url, owner)
    assert validator.calls == []  # rejected before any outbound validation call
    assert await _count(session, org.id) == 0


async def test_create_allows_private_base_url_when_allowed(session, org, owner, cipher, validator, bus):
    svc = CredentialService(session, org.id, cipher, validator, bus, allow_private_base_urls=True)
    row = await svc.create("openai_compatible", "Ollama", KEY, "http://localhost:11434/v1", owner)
    assert row.base_url == "http://localhost:11434/v1"


async def test_decrypt_with_changed_secrets_key_is_unreadable(session, creds, org, owner, validator, bus):
    from supportcore.ai.credentials import CredentialUnreadableError

    row = await creds.create("anthropic", "Main", KEY, None, owner)
    rotated = CredentialService(session, org.id, FernetCipher(Fernet.generate_key().decode()), validator, bus)
    with pytest.raises(CredentialUnreadableError) as exc:
        rotated.decrypt_key(row)
    assert str(exc.value) == "This API key can't be decrypted; delete it and add it again."
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


async def test_ai_settings_is_ready(session, creds, ai_settings, org, owner, bus):
    row = await ai_settings.get()
    assert await ai_settings.is_ready(row) is False  # anthropic needs a credential
    cred = await creds.create("anthropic", "Main", KEY, None, owner)
    row = await ai_settings.update("anthropic", "claude-sonnet-5-5", cred.id, 0.2, owner)
    assert await ai_settings.is_ready(row) is True
    row = await ai_settings.update("openai", "gpt-x", None, 0.2, owner)
    assert await ai_settings.is_ready(row) is False
    fake = OrganisationAISettingsService(session, org.id, bus, include_fake=True)
    row = await fake.update("fake", "fake-chat", None, 0.2, owner)
    assert await fake.is_ready(row) is True  # the fake chat provider needs no credential
    assert await ai_settings.is_ready(row) is False  # ...and is unknown outside the test env
