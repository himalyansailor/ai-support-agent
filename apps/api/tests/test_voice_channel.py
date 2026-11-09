import uuid
from uuid import UUID

import pytest
from kb_helpers import create_org, join_org, signup
from pydantic import SecretStr
from sqlalchemy import select
from supportcore.db.models import (
    AuditLog,
    IntegrationEvent,
    ProviderCredential,
    TelephonyAccount,
)

PUBLIC_URL = "https://voice.example.com"
SID = "AC" + "b" * 32
TOKEN = "t" * 32
BAD_TOKEN = "bad-token" + "0" * 23
PLATFORM_TOKEN = "platform-master-token-" + "p" * 10


@pytest.fixture
def test_settings(test_settings):
    return test_settings.model_copy(
        update={
            "telephony": "fake",
            "voice_public_url": PUBLIC_URL,
            "twilio_account_sid": "AC" + "a" * 32,
            "twilio_auth_token": SecretStr(PLATFORM_TOKEN),
        }
    )


@pytest.fixture(autouse=True)
def _truncate(session_factory):
    """Real-commit tests: session_factory truncates every table at teardown."""


@pytest.fixture
async def setup(raw_client):
    headers = await signup(raw_client, "owner@x.com")
    org_id = await create_org(raw_client, headers)
    widget = await raw_client.post(f"/orgs/{org_id}/widgets", json={"name": "Site"}, headers=headers)
    assert widget.status_code == 201, widget.text
    return raw_client, headers, f"/orgs/{org_id}/voice", org_id, widget.json()["id"]


async def connect(client, headers, base):
    r = await client.put(f"{base}/telephony", json={"account_sid": SID, "auth_token": TOKEN}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def test_connect_byo_encrypts_token_and_sets_up_browser_calling(setup, session_factory):
    client, headers, base, _, _ = setup
    assert (await client.get(f"{base}/telephony", headers=headers)).json() == {
        "connected": False, "platform_available": True, "public_url_set": True,
    }
    body = await connect(client, headers, base)
    assert body == {
        "connected": True, "kind": "byo", "account_sid": SID, "auth_token_last4": TOKEN[-4:], "status": "active",
        "last_error_type": None, "browser_calling_ready": True, "platform_available": True, "public_url_set": True,
    }
    async with session_factory() as s:
        account = (await s.execute(select(TelephonyAccount))).scalar_one()
    assert TOKEN not in account.auth_token_encrypted and account.api_key_secret_encrypted
    assert (await client.get(f"{base}/telephony", headers=headers)).json() == body
    assert (await client.delete(f"{base}/telephony", headers=headers)).status_code == 204
    assert (await client.get(f"{base}/telephony", headers=headers)).json()["connected"] is False
    assert (await client.delete(f"{base}/telephony", headers=headers)).status_code == 404


async def test_connect_byo_bad_token_is_422_and_nothing_stored(setup, session_factory):
    client, headers, base, _, _ = setup
    r = await client.put(f"{base}/telephony", json={"account_sid": SID, "auth_token": BAD_TOKEN}, headers=headers)
    assert r.status_code == 422 and r.json()["code"] == "telephony_credentials_invalid"
    r = await client.put(f"{base}/telephony", json={"account_sid": "nope", "auth_token": TOKEN}, headers=headers)
    assert r.status_code == 422 and r.json()["code"] == "telephony_credentials_invalid"
    async with session_factory() as s:
        assert (await s.execute(select(TelephonyAccount))).scalars().all() == []


async def test_auth_failure_marks_account_and_records_error(setup, session_factory, app):
    client, headers, base, _, _ = setup
    await connect(client, headers, base)
    cipher = app.state.container.adapters.cipher
    async with session_factory() as s:
        account = (await s.execute(select(TelephonyAccount))).scalar_one()
        account.auth_token_encrypted = cipher.encrypt(BAD_TOKEN)
        await s.commit()
    r = await client.get(f"{base}/numbers/search?country=US", headers=headers)
    assert r.status_code == 422 and r.json()["code"] == "telephony_credentials_invalid"
    body = (await client.get(f"{base}/telephony", headers=headers)).json()
    assert (body["status"], body["last_error_type"]) == ("auth_failed", "auth_failed")
    async with session_factory() as s:
        event = (await s.execute(select(IntegrationEvent))).scalar_one()
    assert (event.kind, event.provider, event.error_type) == ("voice", "twilio", "auth_failed")


async def test_buy_flow_and_numbers_list(setup):
    client, headers, base, _, widget_id = setup
    found = await client.get(f"{base}/numbers/search?country=US&area_code=415", headers=headers)
    assert found.status_code == 200 and found.json()[0]["e164"] == "+15005550007"
    assert set(found.json()[0]) == {"e164", "locality", "region", "monthly_price", "price_unit"}
    r = await client.post(f"{base}/numbers/buy", json={"e164": "+15005550007", "widget_id": widget_id}, headers=headers)
    assert r.status_code == 201, r.text
    number = r.json()
    assert set(number) == {"id", "e164", "label", "widget_id", "enabled", "ownership", "webhooks_out_of_date"}
    assert (number["ownership"], number["webhooks_out_of_date"]) == ("platform", False)
    listed = (await client.get(f"{base}/numbers", headers=headers)).json()
    assert [n["id"] for n in listed] == [number["id"]]
    tel = (await client.get(f"{base}/telephony", headers=headers)).json()
    assert tel["kind"] == "platform" and tel["browser_calling_ready"] is True
    patched = await client.patch(
        f"{base}/numbers/{number['id']}", json={"label": "Sales", "enabled": False, "widget_id": None}, headers=headers
    )
    assert patched.status_code == 200
    assert (patched.json()["label"], patched.json()["enabled"], patched.json()["widget_id"]) == ("Sales", False, None)
    assert (await client.delete(f"{base}/numbers/{number['id']}", headers=headers)).status_code == 204
    assert (await client.get(f"{base}/numbers", headers=headers)).json() == []


async def test_buy_hidden_without_platform_credentials(setup, app):
    client, headers, base, _, widget_id = setup
    app.state.container.settings.twilio_account_sid = None
    app.state.container.settings.twilio_auth_token = None
    assert (await client.get(f"{base}/telephony", headers=headers)).json()["platform_available"] is False
    r = await client.post(f"{base}/numbers/buy", json={"e164": "+15005550007", "widget_id": widget_id}, headers=headers)
    assert r.status_code == 409 and r.json()["code"] == "telephony_not_configured"
    r = await client.get(f"{base}/numbers/search?country=US", headers=headers)
    assert r.status_code == 409 and r.json()["code"] == "telephony_not_configured"


async def test_connect_number_and_sync_webhooks(setup, app):
    client, headers, base, _, widget_id = setup
    await connect(client, headers, base)
    available = (await client.get(f"{base}/numbers/available", headers=headers)).json()
    assert available == [{"provider_sid": "PN" + "0" * 32, "e164": "+15005550006", "connected": False}]
    r = await client.post(
        f"{base}/numbers/connect", json={"provider_sid": available[0]["provider_sid"], "widget_id": widget_id},
        headers=headers,
    )
    assert r.status_code == 201 and r.json()["ownership"] == "byo" and r.json()["webhooks_out_of_date"] is False
    assert (await client.get(f"{base}/numbers/available", headers=headers)).json()[0]["connected"] is True
    app.state.container.settings.voice_public_url = "https://new-tunnel.example.com"
    assert (await client.get(f"{base}/numbers", headers=headers)).json()[0]["webhooks_out_of_date"] is True
    r = await client.post(f"{base}/numbers/sync-webhooks", headers=headers)
    assert r.status_code == 200 and r.json() == {"updated": 1}
    assert (await client.get(f"{base}/numbers", headers=headers)).json()[0]["webhooks_out_of_date"] is False


async def test_settings_catalog_and_readiness(setup):
    client, headers, base, _, _ = setup
    s = (await client.get(f"{base}/settings", headers=headers)).json()
    assert s["phone_enabled"] is False and s["max_call_minutes"] == 30 and s["stt_model"] is None
    r = await client.put(
        f"{base}/settings",
        json={"phone_enabled": True, "stt_provider": "fake", "stt_model": "fake-stt", "tts_provider": "fake",
              "tts_model": "fake-tts", "max_call_minutes": 12},
        headers=headers,
    )
    assert r.status_code == 200 and r.json()["max_call_minutes"] == 12 and r.json()["phone_enabled"] is True
    bad = await client.put(f"{base}/settings", json={"max_call_minutes": 500}, headers=headers)
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_voice_settings"
    bad = await client.put(f"{base}/settings", json={"stt_provider": "deepgram", "stt_model": "x"}, headers=headers)
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_voice_settings"
    ready = (await client.get(f"{base}/readiness", headers=headers)).json()
    assert ready == {"phone": False, "browser": False, "reasons": ["telephony_not_connected"]}
    cat = (await client.get(f"{base}/catalog", headers=headers)).json()
    assert {m["model"] for m in cat["stt"]} >= {"nova-3", "nova-2", "fake-stt"}
    tts = next(m for m in cat["tts"] if m["model"] == "gpt-4o-mini-tts")
    assert tts["default_voice"] == "coral" and "sage" in tts["voices"] and tts["credential_provider"] == "openai"


async def test_settings_reject_credential_of_wrong_provider(setup, session_factory, app):
    client, headers, base, org_id, _ = setup
    cipher = app.state.container.adapters.cipher
    async with session_factory() as s:
        row = ProviderCredential(
            organisation_id=UUID(org_id), provider="openai", label="oa", encrypted_api_key=cipher.encrypt("sk-x-1234"),
            last4="1234",
        )
        s.add(row)
        await s.commit()
        cid = str(row.id)
    r = await client.put(
        f"{base}/settings", json={"stt_provider": "deepgram", "stt_model": "nova-3", "stt_credential_id": cid},
        headers=headers,
    )
    assert r.status_code == 422 and r.json()["code"] == "invalid_voice_settings"


async def test_requires_channels_manage(setup, session_factory):
    client, headers, base, org_id, widget_id = setup
    agent = await join_org(client, session_factory, UUID(org_id), "agent@x.com", role_name="Agent")
    n = str(uuid.uuid4())
    calls = [
        ("get", "/settings", None), ("put", "/settings", {}), ("get", "/readiness", None), ("get", "/catalog", None),
        ("get", "/telephony", None), ("put", "/telephony", {"account_sid": SID, "auth_token": TOKEN}),
        ("delete", "/telephony", None), ("get", "/numbers", None), ("get", "/numbers/search?country=US", None),
        ("post", "/numbers/buy", {"e164": "+15005550007", "widget_id": widget_id}), ("get", "/numbers/available", None),
        ("post", "/numbers/connect", {"provider_sid": "PN" + "0" * 32, "widget_id": widget_id}),
        ("patch", f"/numbers/{n}", {"label": "x"}), ("delete", f"/numbers/{n}", None),
        ("post", "/numbers/sync-webhooks", None),
    ]
    for method, path, body in calls:
        kwargs = {"json": body} if body is not None else {}
        r = await getattr(client, method)(base + path, headers=agent, **kwargs)
        assert r.status_code == 403, (method, path, r.status_code)
    role = await client.post(
        f"/orgs/{org_id}/roles", json={"name": "Chan", "permissions": ["channels.manage"]}, headers=headers
    )
    member = await join_org(client, session_factory, UUID(org_id), "chan@x.com", role_id=UUID(role.json()["id"]))
    assert (await client.get(f"{base}/settings", headers=member)).status_code == 200


async def test_cross_org_number_is_404(setup):
    client, headers, base, _, widget_id = setup
    number = (
        await client.post(f"{base}/numbers/buy", json={"e164": "+15005550007", "widget_id": widget_id}, headers=headers)
    ).json()
    other = await signup(client, "other@x.com")
    other_org = await create_org(client, other, "Other")
    other_base = f"/orgs/{other_org}/voice"
    assert (await client.patch(f"{other_base}/numbers/{number['id']}", json={"label": "x"}, headers=other)).status_code == 404
    assert (await client.delete(f"{other_base}/numbers/{number['id']}", headers=other)).status_code == 404
    assert (await client.get(f"{other_base}/numbers", headers=other)).json() == []
    # the other org cannot reach this org's routes either
    assert (await client.get(f"{base}/numbers", headers=other)).status_code == 404
    assert (await client.get(f"{base}/numbers", headers=headers)).json()[0]["id"] == number["id"]


async def test_responses_never_contain_secrets(setup, session_factory, app):
    client, headers, base, _, widget_id = setup
    cipher = app.state.container.adapters.cipher
    texts: list[str] = []
    secrets = {TOKEN, BAD_TOKEN, PLATFORM_TOKEN}

    async def call(method, path, **kw):
        r = await getattr(client, method)(base + path, headers=headers, **kw)
        texts.append(r.text)
        return r

    async def remember_account_secrets():
        async with session_factory() as s:
            account = (await s.execute(select(TelephonyAccount))).scalar_one()
        secrets.update({
            account.auth_token_encrypted, account.api_key_secret_encrypted,
            cipher.decrypt(account.auth_token_encrypted), cipher.decrypt(account.api_key_secret_encrypted),
        })

    # bring your own
    await call("put", "/telephony", json={"account_sid": SID, "auth_token": TOKEN})
    await remember_account_secrets()
    await call("get", "/telephony")
    await call("get", "/numbers/available")
    number = (await call("post", "/numbers/connect", json={"provider_sid": "PN" + "0" * 32, "widget_id": widget_id})).json()
    await call("get", "/numbers")
    await call("get", "/numbers/search?country=US&area_code=415")
    await call("patch", f"/numbers/{number['id']}", json={"label": "Main"})
    await call("post", "/numbers/sync-webhooks")
    await call("put", "/settings", json={"phone_enabled": True})
    await call("get", "/settings")
    await call("get", "/readiness")
    await call("get", "/catalog")
    await call("delete", f"/numbers/{number['id']}")
    await call("put", "/telephony", json={"account_sid": SID, "auth_token": BAD_TOKEN})
    await call("delete", "/telephony")
    # platform subaccount
    await call("get", "/numbers/search?country=US")
    bought = (await call("post", "/numbers/buy", json={"e164": "+15005550007", "widget_id": widget_id})).json()
    await remember_account_secrets()
    await call("get", "/telephony")
    await call("get", "/numbers")
    await call("patch", f"/numbers/{bought['id']}", json={"enabled": False})
    await call("delete", f"/numbers/{bought['id']}")
    async with session_factory() as s:
        audit = (await s.execute(select(AuditLog))).scalars().all()
    texts.append(repr([(a.action, a.target_type, a.target_id, a.metadata_) for a in audit]))
    blob = "\n".join(texts)
    assert len(secrets) >= 9
    for secret in secrets:
        assert secret not in blob
    assert "auth_token\"" not in blob and "api_key_secret" not in blob


async def test_account_in_use_conflicts_are_409(setup, session_factory):
    client, headers, base, _, widget_id = setup
    await connect(client, headers, base)
    await client.post(
        f"{base}/numbers/connect", json={"provider_sid": "PN" + "0" * 32, "widget_id": widget_id}, headers=headers
    )
    r = await client.put(f"{base}/telephony", json={"account_sid": "AC" + "e" * 32, "auth_token": TOKEN}, headers=headers)
    assert r.status_code == 409 and r.json()["code"] == "telephony_account_in_use"
    other = await signup(client, "other2@x.com")
    other_org = await create_org(client, other, "Other2")
    r = await client.put(
        f"/orgs/{other_org}/voice/telephony", json={"account_sid": SID, "auth_token": TOKEN}, headers=other
    )
    assert r.status_code == 409 and r.json()["code"] == "telephony_account_in_use"


async def test_catalog_lists_realtime_models(setup):
    client, headers, base, _, _ = setup
    cat = (await client.get(f"{base}/catalog", headers=headers)).json()
    models = {m["model"]: m for m in cat["realtime"]}
    assert set(models) == {"gpt-realtime", "gpt-realtime-mini", "fake-realtime"}
    rt = models["gpt-realtime"]
    assert rt["recommended"] is True and rt["default_voice"] == "marin" and "cedar" in rt["voices"]
    assert rt["credential_provider"] == "openai" and rt["requires_credential"] is True
    assert models["fake-realtime"]["credential_provider"] is None


async def test_realtime_and_transfer_settings_round_trip(setup):
    client, headers, base, _, _ = setup
    s = (await client.get(f"{base}/settings", headers=headers)).json()
    assert s["mode"] == "chained" and s["realtime_model"] is None
    assert (s["transfer_browser_enabled"], s["transfer_phone_enabled"]) == (True, False)
    r = await client.put(
        f"{base}/settings",
        json={"mode": "speech_to_speech", "realtime_provider": "fake", "realtime_model": "fake-realtime",
              "transfer_browser_enabled": False, "transfer_phone_enabled": True},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "speech_to_speech" and body["realtime_voice"] == "fake"
    assert (body["transfer_browser_enabled"], body["transfer_phone_enabled"]) == (False, True)
    ready = (await client.get(f"{base}/readiness", headers=headers)).json()
    assert "realtime_not_configured" not in ready["reasons"] and "stt_not_configured" not in ready["reasons"]
    bad = await client.put(f"{base}/settings", json={"mode": "telepathy"}, headers=headers)
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_voice_settings"
    bad = await client.put(f"{base}/settings", json={"realtime_provider": "openai", "realtime_model": "x"}, headers=headers)
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_voice_settings"
