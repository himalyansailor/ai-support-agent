"""Widget voice: the config flag and POST /widget/v1/calls/token (Phase 7a, Task 11)."""

import jwt
import pytest
from sqlalchemy import select
from kb_helpers import create_org, signup
from pydantic import SecretStr
from supportcore.db.models import VoiceSettings
from supportcore.voice.tokens import verify_call_ticket

ORIGIN = "https://shop.example.com"
SID = "AC" + "b" * 32
TOKEN = "t" * 32


@pytest.fixture
def test_settings(test_settings):
    return test_settings.model_copy(
        update={
            "telephony": "fake",
            "voice_public_url": "https://voice.example.com",
            "twilio_account_sid": "AC" + "a" * 32,
            "twilio_auth_token": SecretStr("platform-master-token-" + "p" * 10),
        }
    )


async def _widget(client, headers, org_id, name="Site") -> dict:
    r = await client.post(f"/orgs/{org_id}/widgets", json={"name": name}, headers=headers)
    assert r.status_code == 201, r.text
    r = await client.patch(
        f"/orgs/{org_id}/widgets/{r.json()['id']}", json={"allowed_domains": ["shop.example.com"]}, headers=headers
    )
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
async def env(client):
    headers = await signup(client, "owner@x.com")
    org_id = await create_org(client, headers)
    return client, headers, org_id, await _widget(client, headers, org_id)


async def _enable_voice(client, headers, org_id, **settings):
    base = f"/orgs/{org_id}/voice"
    r = await client.put(f"{base}/telephony", json={"account_sid": SID, "auth_token": TOKEN}, headers=headers)
    assert r.status_code == 200, r.text
    body = {
        "browser_calls_enabled": True, "stt_provider": "fake", "stt_model": "fake-stt", "tts_provider": "fake",
        "tts_model": "fake-tts", **settings,
    }
    r = await client.put(f"{base}/settings", json=body, headers=headers)
    assert r.status_code == 200, r.text
    assert (await client.get(f"{base}/readiness", headers=headers)).json()["browser"] is True


async def _session(client, key):
    r = await client.post("/widget/v1/sessions", json={"key": key, "parent_origin": ORIGIN})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}, r.json()["customer_id"]


async def _config(client, key, **params):
    r = await client.get("/widget/v1/config", params={"key": key, **params})
    assert r.status_code == 200, r.text
    return r.json()


async def test_widget_config_voice_flag(env):
    client, headers, org_id, widget = env
    assert (await _config(client, widget["public_key"]))["voice_call_enabled"] is False
    await _enable_voice(client, headers, org_id)
    assert (await _config(client, widget["public_key"]))["voice_call_enabled"] is True
    other = await _widget(client, headers, org_id, "Other")
    assert (await _config(client, other["public_key"]))["voice_call_enabled"] is True  # NULL = all widgets
    await _enable_voice(client, headers, org_id, browser_widget_id=widget["id"])
    assert (await _config(client, widget["public_key"]))["voice_call_enabled"] is True
    assert (await _config(client, other["public_key"]))["voice_call_enabled"] is False


async def test_widget_config_hides_calls_in_the_preview(env):
    """`/calls/token` refuses preview sessions, so the preview must not offer a Call button that always fails."""
    client, headers, org_id, widget = env
    await _enable_voice(client, headers, org_id)
    assert (await _config(client, widget["public_key"]))["voice_call_enabled"] is True
    assert (await _config(client, widget["public_key"], preview="1"))["voice_call_enabled"] is False


async def test_call_token_issues_ticket_bound_to_customer(env, test_settings):
    client, headers, org_id, widget = env
    await _enable_voice(client, headers, org_id)
    auth, customer_id = await _session(client, widget["public_key"])
    r = await client.post("/widget/v1/calls/token", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"token", "ticket", "expires_in"} and body["expires_in"] == 600
    assert body["token"] == f"fake-token:customer-{customer_id}"  # the fake telephony's token carries the identity
    ticket = verify_call_ticket(test_settings, body["ticket"])
    assert (str(ticket.organisation_id), str(ticket.widget_id), str(ticket.customer_id)) == (
        org_id, widget["id"], customer_id,
    )
    assert jwt.decode(body["ticket"], options={"verify_signature": False})["exp"]
    again = (await client.post("/widget/v1/calls/token", headers=auth)).json()
    assert verify_call_ticket(test_settings, again["ticket"]).jti != ticket.jti  # a fresh single-use ticket each time


async def test_call_token_requires_session(env):
    client, *_ = env
    assert (await client.post("/widget/v1/calls/token")).status_code == 401


async def test_call_token_rate_limited(env):
    client, headers, org_id, widget = env
    await _enable_voice(client, headers, org_id)
    auth, _ = await _session(client, widget["public_key"])
    for _i in range(5):
        assert (await client.post("/widget/v1/calls/token", headers=auth)).status_code == 200
    r = await client.post("/widget/v1/calls/token", headers=auth)
    assert r.status_code == 429 and r.json()["code"] == "rate_limited" and "retry-after" in r.headers
    other, _ = await _session(client, widget["public_key"])  # another session (another visitor) has its own budget
    assert (await client.post("/widget/v1/calls/token", headers=other)).status_code == 200


async def test_call_token_409_when_not_ready(env):
    client, headers, org_id, widget = env
    auth, _ = await _session(client, widget["public_key"])
    r = await client.post("/widget/v1/calls/token", headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "voice_not_available"
    await _enable_voice(client, headers, org_id)
    other = await _widget(client, headers, org_id, "Other")
    await _enable_voice(client, headers, org_id, browser_widget_id=other["id"])  # browser calls limited to another widget
    r = await client.post("/widget/v1/calls/token", headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "voice_not_available"


async def test_call_token_refused_for_preview_session(env, test_settings):
    client, headers, org_id, widget = env
    await _enable_voice(client, headers, org_id)
    r = await client.post(f"/orgs/{org_id}/widgets/{widget['id']}/preview-ticket", headers=headers)
    assert r.status_code == 200, r.text
    body = {"key": widget["public_key"], "parent_origin": test_settings.web_origin, "preview": True,
            "preview_ticket": r.json()["ticket"]}
    session = await client.post("/widget/v1/sessions", json=body)
    assert session.status_code == 200, session.text
    auth = {"Authorization": f"Bearer {session.json()['token']}"}
    r = await client.post("/widget/v1/calls/token", headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "voice_not_available"


async def test_public_routes_do_not_create_voice_settings(env, session):
    client, _headers, _org_id, widget = env
    auth, _ = await _session(client, widget["public_key"])
    assert (await _config(client, widget["public_key"]))["voice_call_enabled"] is False
    assert (await client.post("/widget/v1/calls/token", headers=auth)).status_code == 409
    assert (await session.execute(select(VoiceSettings))).scalars().all() == []
