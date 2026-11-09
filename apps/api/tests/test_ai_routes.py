from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from supportcore.ai import catalog
from supportcore.db.models import AuditLog, Membership, ProviderCredential, Role, User
from supportcore.secrets.fernet_cipher import FernetCipher

KEY = "sk-ant-api03-SUPERSECRETKEYVALUE-abcd1234WXYZ"
PASSWORD = "correct-horse-1"
CREDENTIAL_VIEW = {"id", "provider", "label", "base_url", "last4", "created_at", "last_validated_at"}


class StubValidator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    async def validate(self, provider: str, api_key: str, base_url: str | None) -> None:
        self.calls.append((provider, api_key, base_url))


@pytest.fixture
def validator(app) -> StubValidator:
    """Route tests never call real providers: the container's validator is swapped for a stub."""
    stub = StubValidator()
    app.state.container.adapters.credential_validator = stub
    return stub


@pytest.fixture
async def org(client, auth_headers):
    return (await client.post("/orgs", json={"name": "Acme"}, headers=auth_headers)).json()


async def _signup(client, email: str) -> dict[str, str]:
    r = await client.post("/auth/signup", json={"email": email, "password": PASSWORD, "name": email})
    assert r.status_code == 201
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _join(client, session, org_id, email, role_name) -> dict[str, str]:
    headers = await _signup(client, email)
    user = (await session.execute(select(User).where(User.email == email))).scalar_one()
    role = (
        await session.execute(select(Role).where(Role.organisation_id == org_id, Role.name == role_name))
    ).scalar_one()
    session.add(Membership(organisation_id=org_id, user_id=user.id, role_id=role.id))
    await session.flush()
    return headers


async def _create_credential(client, org_id, headers, **overrides):
    body = {"provider": "anthropic", "label": "Main", "api_key": KEY, **overrides}
    return await client.post(f"/orgs/{org_id}/ai/credentials", json=body, headers=headers)


# --- catalog -------------------------------------------------------------------------------


async def test_catalog_requires_auth(client):
    assert (await client.get("/ai/catalog?language=en")).status_code == 401


async def test_catalog_filters_by_language(client, auth_headers, monkeypatch):
    r = await client.get("/ai/catalog?language=en", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"chat_models", "embedding_models", "languages"}
    assert body["languages"] == [{"code": "en", "label": "English"}]
    recommended = [m for m in body["chat_models"] if m["recommended"]]
    assert [(m["provider"], m["model"]) for m in recommended] == [("anthropic", "claude-sonnet-5-5")]
    assert set(body["chat_models"][0]) == {"provider", "model", "label", "recommended", "requires_credential"}
    en_models = {m["model"] for m in body["embedding_models"]}
    assert {"BAAI/bge-small-en-v1.5", "intfloat/multilingual-e5-small", "text-embedding-3-small"} <= en_models
    bge = next(m for m in body["embedding_models"] if m["model"] == "BAAI/bge-small-en-v1.5")
    assert bge == {
        "provider": "local",
        "model": "BAAI/bge-small-en-v1.5",
        "label": "BGE Small (English)",
        "dimensions": 384,
        "requires_credential": False,
        "recommended": True,
    }
    # the test-only fake providers are listed in the test env
    assert any(m["provider"] == "fake" for m in body["chat_models"])

    monkeypatch.setattr(catalog, "LANGUAGES", [("en", "English"), ("de", "German")])
    de = (await client.get("/ai/catalog?language=de", headers=auth_headers)).json()
    de_models = {m["model"] for m in de["embedding_models"]}
    assert "BAAI/bge-small-en-v1.5" not in de_models  # English-only model filtered out
    assert "intfloat/multilingual-e5-small" in de_models
    assert de["languages"] == [{"code": "en", "label": "English"}, {"code": "de", "label": "German"}]


async def test_catalog_rejects_unsupported_language(client, auth_headers):
    r = await client.get("/ai/catalog?language=xx", headers=auth_headers)
    assert r.status_code == 422 and r.json()["code"] == "unsupported_language"


# --- credentials ---------------------------------------------------------------------------


async def test_credentials_crud_never_returns_key(client, auth_headers, org, validator, session):
    r = await _create_credential(client, org["id"], auth_headers)
    assert r.status_code == 201, r.text
    created = r.json()
    assert set(created) == CREDENTIAL_VIEW
    assert created["last4"] == "WXYZ" and created["provider"] == "anthropic" and created["base_url"] is None
    assert created["last_validated_at"] is not None
    assert validator.calls == [("anthropic", KEY, None)]
    assert KEY not in r.text and "SUPERSECRET" not in r.text

    listed = await client.get(f"/orgs/{org['id']}/ai/credentials", headers=auth_headers)
    assert listed.status_code == 200 and listed.json() == [created]
    assert KEY not in listed.text

    row = (await session.execute(select(ProviderCredential))).scalar_one()
    assert KEY not in row.encrypted_api_key
    audit = (await session.execute(select(AuditLog))).scalars().all()
    assert all(KEY not in str(a.metadata_) for a in audit)

    r = await client.delete(f"/orgs/{org['id']}/ai/credentials/{created['id']}", headers=auth_headers)
    assert r.status_code == 204
    assert (await client.get(f"/orgs/{org['id']}/ai/credentials", headers=auth_headers)).json() == []
    r = await client.delete(f"/orgs/{org['id']}/ai/credentials/{created['id']}", headers=auth_headers)
    assert r.status_code == 404 and r.json()["code"] == "not_found"


async def test_create_credential_errors_have_codes(client, auth_headers, org, validator):
    assert (await _create_credential(client, org["id"], auth_headers)).status_code == 201
    r = await _create_credential(client, org["id"], auth_headers, label="main")
    assert r.status_code == 409 and r.json()["code"] == "credential_label_taken"
    r = await _create_credential(client, org["id"], auth_headers, label="Other", provider="nope")
    assert r.status_code == 422 and r.json()["code"] == "credential_invalid"
    r = await _create_credential(client, org["id"], auth_headers, label="Other", api_key="short")
    assert r.status_code == 422 and r.json()["code"] == "credential_invalid"


async def test_compatible_credential_private_base_url_allowed_in_test_env(client, auth_headers, org, validator):
    r = await _create_credential(
        client, org["id"], auth_headers,
        provider="openai_compatible", label="Ollama", base_url="http://localhost:11434/v1",
    )
    assert r.status_code == 201 and r.json()["base_url"] == "http://localhost:11434/v1"


async def test_compatible_credential_private_base_url_rejected_when_disallowed(
    client, app, auth_headers, org, validator
):
    app.state.container.settings.allow_private_base_urls = False
    r = await _create_credential(
        client, org["id"], auth_headers,
        provider="openai_compatible", label="Ollama", base_url="http://127.0.0.1:11434/v1",
    )
    assert r.status_code == 422
    assert r.json() == {"detail": "Base URL must be a public address", "code": "credential_invalid"}
    assert validator.calls == []


async def test_agent_cannot_list_credentials_403(client, auth_headers, org, session, validator):
    created = (await _create_credential(client, org["id"], auth_headers)).json()
    # Admin (bot.configure) may list but not create/delete; billing.manage is Owner-only by default.
    for role in ("Agent", "Admin", "Viewer"):
        headers = await _join(client, session, org["id"], f"{role.lower()}@x.com", role)
        listed = await client.get(f"/orgs/{org['id']}/ai/credentials", headers=headers)
        assert listed.status_code == (200 if role == "Admin" else 403)
        if role == "Admin":
            assert listed.json() == [created] and KEY not in listed.text
        assert (await _create_credential(client, org["id"], headers, label=role)).status_code == 403
        r = await client.delete(f"/orgs/{org['id']}/ai/credentials/{created['id']}", headers=headers)
        assert r.status_code == 403
    assert validator.calls == [("anthropic", KEY, None)]


async def test_kb_manage_only_role_can_list_but_not_create_credentials(client, auth_headers, org, session, validator):
    created = (await _create_credential(client, org["id"], auth_headers)).json()
    role = await client.post(
        f"/orgs/{org['id']}/roles", json={"name": "KB editor", "permissions": ["kb.manage"]}, headers=auth_headers
    )
    assert role.status_code == 201
    headers = await _signup(client, "kbeditor@x.com")
    user = (await session.execute(select(User).where(User.email == "kbeditor@x.com"))).scalar_one()
    session.add(Membership(organisation_id=UUID(org["id"]), user_id=user.id, role_id=UUID(role.json()["id"])))
    await session.flush()
    listed = await client.get(f"/orgs/{org['id']}/ai/credentials", headers=headers)
    assert listed.status_code == 200 and listed.json() == [created] and KEY not in listed.text
    assert (await _create_credential(client, org["id"], headers, label="x")).status_code == 403
    r = await client.delete(f"/orgs/{org['id']}/ai/credentials/{created['id']}", headers=headers)
    assert r.status_code == 403


async def test_credentials_cross_tenant_404(client, auth_headers, org, validator):
    created = (await _create_credential(client, org["id"], auth_headers)).json()
    other_headers = await _signup(client, "b@x.com")
    other_org = (await client.post("/orgs", json={"name": "Other"}, headers=other_headers)).json()
    # outsider on our org
    assert (await client.get(f"/orgs/{org['id']}/ai/credentials", headers=other_headers)).status_code == 404
    # our credential through their org
    r = await client.delete(f"/orgs/{other_org['id']}/ai/credentials/{created['id']}", headers=other_headers)
    assert r.status_code == 404
    r = await client.delete(f"/orgs/{org['id']}/ai/credentials/{uuid4()}", headers=auth_headers)
    assert r.status_code == 404


async def test_delete_credential_in_use_409(client, auth_headers, org, validator):
    created = (await _create_credential(client, org["id"], auth_headers)).json()
    settings = {
        "chat_provider": "anthropic",
        "chat_model": "claude-sonnet-5-5",
        "chat_credential_id": created["id"],
        "temperature": 0.2,
    }
    assert (await client.put(f"/orgs/{org['id']}/ai/settings", json=settings, headers=auth_headers)).status_code == 200
    r = await client.delete(f"/orgs/{org['id']}/ai/credentials/{created['id']}", headers=auth_headers)
    assert r.status_code == 409 and r.json()["code"] == "credential_in_use"


# --- AI settings ---------------------------------------------------------------------------


async def test_ai_settings_ready_flag(client, auth_headers, org, session, validator):
    url = f"/orgs/{org['id']}/ai/settings"
    r = await client.get(url, headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {
        "chat_provider": "anthropic",
        "chat_model": "claude-sonnet-5-5",
        "chat_credential_id": None,
        "temperature": 0.2,
        "ready": False,
    }
    cred = (await _create_credential(client, org["id"], auth_headers)).json()
    body = {
        "chat_provider": "anthropic",
        "chat_model": "claude-sonnet-5-5",
        "chat_credential_id": cred["id"],
        "temperature": 0.5,
    }
    r = await client.put(url, json=body, headers=auth_headers)
    assert r.status_code == 200 and r.json() == {**body, "ready": True}

    # the fake chat provider (test env only) needs no credential
    fake = {"chat_provider": "fake", "chat_model": "fake-chat", "chat_credential_id": None, "temperature": 0.0}
    r = await client.put(url, json=fake, headers=auth_headers)
    assert r.status_code == 200 and r.json()["ready"] is True

    # any member may read; only bot.configure may write
    agent = await _join(client, session, org["id"], "agent@x.com", "Agent")
    assert (await client.get(url, headers=agent)).json()["chat_provider"] == "fake"
    assert (await client.put(url, json=body, headers=agent)).status_code == 403
    admin = await _join(client, session, org["id"], "admin@x.com", "Admin")
    assert (await client.put(url, json=body, headers=admin)).status_code == 200


async def test_ai_settings_errors_have_codes(client, auth_headers, org, validator):
    url = f"/orgs/{org['id']}/ai/settings"
    base = {"chat_provider": "openai", "chat_model": "gpt-x", "chat_credential_id": None, "temperature": 0.2}
    r = await client.put(url, json={**base, "chat_model": "m" * 201}, headers=auth_headers)
    assert r.status_code == 422 and r.json()["code"] == "invalid_model_name"
    r = await client.put(url, json={**base, "temperature": 1.5}, headers=auth_headers)
    assert r.status_code == 422 and r.json()["code"] == "invalid_temperature"
    unknown = {**base, "chat_provider": "anthropic", "chat_model": "claude-x"}
    r = await client.put(url, json=unknown, headers=auth_headers)
    assert r.status_code == 422 and r.json()["code"] == "unknown_model"
    cred = (await _create_credential(client, org["id"], auth_headers)).json()
    r = await client.put(url, json={**base, "chat_credential_id": cred["id"]}, headers=auth_headers)
    assert r.status_code == 422 and r.json()["code"] == "credential_provider_mismatch"
    r = await client.put(url, json={**base, "chat_credential_id": str(uuid4())}, headers=auth_headers)
    assert r.status_code == 404


async def test_ai_settings_outsider_404(client, auth_headers, org):
    outsider = await _signup(client, "out@x.com")
    assert (await client.get(f"/orgs/{org['id']}/ai/settings", headers=outsider)).status_code == 404


# --- org language --------------------------------------------------------------------------


async def test_patch_org_language(client, auth_headers, org, session, monkeypatch):
    url = f"/orgs/{org['id']}"
    r = await client.patch(url, json={"language": "en"}, headers=auth_headers)
    assert r.status_code == 200 and r.json() == org

    r = await client.patch(url, json={"language": "xx"}, headers=auth_headers)
    assert r.status_code == 422 and r.json()["code"] == "unsupported_language"

    monkeypatch.setattr(catalog, "LANGUAGES", [("en", "English"), ("de", "German")])
    r = await client.patch(url, json={"language": "de"}, headers=auth_headers)
    assert r.status_code == 200 and r.json() == {**org, "language": "de"}
    assert (await client.get(url, headers=auth_headers)).json()["language"] == "de"
    actions = (await session.execute(select(AuditLog.action))).scalars().all()
    assert "organisation.updated" in actions

    admin = await _join(client, session, org["id"], "admin@x.com", "Admin")  # no org.manage
    assert (await client.patch(url, json={"language": "en"}, headers=admin)).status_code == 403
    outsider = await _signup(client, "out@x.com")
    assert (await client.patch(url, json={"language": "en"}, headers=outsider)).status_code == 404


# --- unreadable keys -----------------------------------------------------------------------


async def test_unreadable_credential_maps_to_422(client, app, auth_headers, org, session, validator):
    """A key encrypted under an old SUPPORT_SECRETS_KEY surfaces as 422 credential_unreadable."""
    from fastapi import APIRouter, Depends

    from supportapi.deps import OrgContext, SessionDep, build_credential_service, get_container, get_org_context

    created = (await _create_credential(client, org["id"], auth_headers)).json()
    app.state.container.adapters.cipher = FernetCipher(Fernet.generate_key().decode())  # key rotated

    router = APIRouter()

    @router.get("/_test/orgs/{org_id}/decrypt/{credential_id}")
    async def decrypt(
        session: SessionDep,
        credential_id: UUID,
        ctx: OrgContext = Depends(get_org_context),
        container=Depends(get_container),
    ):
        service = build_credential_service(session, ctx.organisation.id, container)
        service.decrypt_key(await service.get(credential_id))
        return {}

    app.include_router(router)
    r = await client.get(f"/_test/orgs/{org['id']}/decrypt/{created['id']}", headers=auth_headers)
    assert r.status_code == 422
    assert r.json() == {
        "detail": "This API key can't be decrypted; delete it and add it again.",
        "code": "credential_unreadable",
    }
