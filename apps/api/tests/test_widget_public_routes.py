"""Public widget API (/widget/v1): config, frame policy, sessions and a customer's own conversations."""

import time

import jwt
import pytest
import structlog
from pydantic import SecretStr
from sqlalchemy import select

from kb_helpers import create_org, signup
from supportcore.config import Settings
from supportcore.conversations.service import ConversationService
from supportcore.db.models import Conversation

ORIGIN = "https://shop.example.com"


@pytest.fixture
async def dash(client):
    headers = await signup(client, "owner@x.com")
    return headers, await create_org(client, headers)


async def _widget(client, dash, *, domains=("shop.example.com",), **patch) -> dict:
    headers, org_id = dash
    r = await client.post(f"/orgs/{org_id}/widgets", json={"name": "Site"}, headers=headers)
    assert r.status_code == 201, r.text
    w = r.json()
    r = await client.patch(
        f"/orgs/{org_id}/widgets/{w['id']}", json={"allowed_domains": list(domains), **patch}, headers=headers
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _session(client, key, *, origin=ORIGIN, visitor_id=None, preview=False, token=None, ticket=None):
    body = {"key": key, "parent_origin": origin, "preview": preview}
    if ticket:
        body["preview_ticket"] = ticket
    if visitor_id:
        body["visitor_id"] = visitor_id
    if token:
        body["customer_token"] = token
    return await client.post("/widget/v1/sessions", json=body)


def _bearer(r) -> dict:
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def test_config_returns_public_view(client, dash):
    w = await _widget(client, dash, appearance={"greeting": "Welcome!"})
    r = await client.get("/widget/v1/config", params={"key": w["public_key"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {
        "appearance", "strings", "language", "csat_enabled", "organisation_name", "voice_call_enabled",
    }
    assert body["strings"]["greeting"] == "Welcome!" and body["organisation_name"] == "Acme"
    assert "allowed_domains" not in str(body) and "identity" not in str(body)


async def test_config_unknown_key_404(client):
    r = await client.get("/widget/v1/config", params={"key": "wk_unknown"})
    assert r.status_code == 404 and r.json()["code"] == "not_found"


async def test_disabled_widget_404(client, dash):
    w = await _widget(client, dash, enabled=False)
    assert (await client.get("/widget/v1/config", params={"key": w["public_key"]})).status_code == 404
    assert (await _session(client, w["public_key"])).status_code == 404
    r = await client.get("/widget/v1/frame-policy", params={"key": w["public_key"]})
    assert r.json() == {"frame_ancestors": "'none'"}


async def test_frame_policy(client, dash):
    w = await _widget(client, dash, domains=("shop.example.com", "localhost:5500"))
    r = await client.get("/widget/v1/frame-policy", params={"key": w["public_key"]})
    assert r.status_code == 200
    assert r.json() == {
        "frame_ancestors": "http://localhost:3000 https://shop.example.com https://localhost:5500 http://localhost:5500"
    }
    r = await client.get("/widget/v1/frame-policy", params={"key": "wk_unknown"})
    assert r.status_code == 200 and r.json() == {"frame_ancestors": "'none'"}


@pytest.fixture
def limiter_keys(app):
    keys: list[str] = []
    limiter = app.state.container.adapters.rate_limiter
    real_hit = limiter.hit

    async def hit(key, limit, window):
        keys.append(key)
        return await real_hit(key, limit, window)

    limiter.hit = hit
    return keys


async def _policy(client, **headers):
    r = await client.get("/widget/v1/frame-policy", params={"key": "wk_unknown"}, headers=headers)
    assert r.status_code == 200
    return r


async def test_frame_policy_limit_keyed_by_forwarded_ip_with_internal_token(app, client, limiter_keys):
    app.state.container.settings.internal_api_token = SecretStr("internal-secret")
    ok = {"X-Support-Internal-Token": "internal-secret"}
    await _policy(client, **ok, **{"X-Support-Client-IP": "203.0.113.7"})
    await _policy(client, **ok, **{"X-Support-Client-IP": "203.0.113.8"})
    assert limiter_keys == ["ip:frame-policy:203.0.113.7", "ip:frame-policy:203.0.113.8"]


async def test_frame_policy_ignores_forwarded_ip_without_valid_token(app, client, limiter_keys):
    app.state.container.settings.internal_api_token = SecretStr("internal-secret")
    await _policy(client, **{"X-Support-Internal-Token": "wrong", "X-Support-Client-IP": "203.0.113.7"})
    await _policy(client, **{"X-Support-Client-IP": "203.0.113.7"})
    await _policy(client, **{"X-Support-Internal-Token": "internal-secret"})  # token but no forwarded ip
    assert len(limiter_keys) == 3
    assert all("203.0.113.7" not in k for k in limiter_keys)
    assert len(set(limiter_keys)) == 1  # all share the peer-IP bucket


async def test_frame_policy_ignores_forwarded_ip_when_token_unset(app, client, limiter_keys):
    assert app.state.container.settings.internal_api_token is None
    await _policy(client, **{"X-Support-Internal-Token": "x", "X-Support-Client-IP": "203.0.113.7"})
    assert "203.0.113.7" not in limiter_keys[0]


async def test_session_rejects_unlisted_parent_origin(client, dash):
    w = await _widget(client, dash)
    r = await _session(client, w["public_key"], origin="https://evil.example.com")
    assert r.status_code == 403 and r.json()["code"] == "origin_not_allowed"


async def test_session_start_returns_token_and_visitor(client, dash):
    w = await _widget(client, dash)
    r = await _session(client, w["public_key"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) >= {"token", "customer_id", "visitor_id", "conversation"} and body["conversation"] is None
    again = await _session(client, w["public_key"], visitor_id=body["visitor_id"])
    assert again.json()["customer_id"] == body["customer_id"]


async def test_session_identity_required_and_bad_token(client, dash):
    headers, org_id = dash
    w = await _widget(client, dash)
    r = await client.post(f"/orgs/{org_id}/widgets/{w['id']}/identity-secret", headers=headers)
    secret = r.json()["secret"]
    await client.patch(f"/orgs/{org_id}/widgets/{w['id']}", json={"identity_required": True}, headers=headers)
    r = await _session(client, w["public_key"])
    assert r.status_code == 401 and r.json()["code"] == "identity_required"
    r = await _session(client, w["public_key"], token="garbage")
    assert r.status_code == 401 and r.json()["code"] == "invalid_customer_token"
    now = int(time.time())
    good = jwt.encode({"sub": "u1", "iat": now, "exp": now + 600}, secret, algorithm="HS256")
    assert (await _session(client, w["public_key"], token=good)).status_code == 200


async def test_session_rate_limited_429(client, dash):
    w = await _widget(client, dash)
    for _ in range(20):
        assert (await _session(client, w["public_key"])).status_code == 200
    r = await _session(client, w["public_key"])
    assert r.status_code == 429 and r.json()["code"] == "rate_limited"
    assert int(r.headers["Retry-After"]) >= 1


async def test_config_rate_limited_429(client):
    for _ in range(120):
        assert (await client.get("/widget/v1/config", params={"key": "wk_x"})).status_code == 404
    assert (await client.get("/widget/v1/config", params={"key": "wk_x"})).status_code == 429


async def test_conversation_routes_require_a_widget_token(client, dash, auth_headers):
    cid = "00000000-0000-0000-0000-000000000001"
    calls = [
        ("GET", "/widget/v1/conversations"),
        ("POST", "/widget/v1/conversations"),
        ("GET", f"/widget/v1/conversations/{cid}/messages"),
        ("POST", f"/widget/v1/conversations/{cid}/end"),
        ("POST", f"/widget/v1/conversations/{cid}/csat"),
        ("POST", f"/widget/v1/conversations/{cid}/contact"),
    ]
    for method, url in calls:
        r = await client.request(method, url)
        assert r.status_code == 401 and r.json()["code"] == "invalid_widget_token", url
        # A dashboard access token is not a widget token.
        r = await client.request(method, url, headers=auth_headers)
        assert r.status_code == 401 and r.json()["code"] == "invalid_widget_token", url


async def test_widget_token_rejected_on_dashboard_route(client, dash):
    w = await _widget(client, dash)
    s = await _session(client, w["public_key"])
    assert (await client.get("/me", headers=_bearer(s))).status_code == 401


async def test_conversation_lifecycle(client, dash):
    headers, org_id = dash
    w = await _widget(client, dash)
    s = await _session(client, w["public_key"])
    auth = _bearer(s)
    r = await client.post("/widget/v1/conversations", headers=auth)
    assert r.status_code == 201, r.text
    conv = r.json()
    assert conv["status"] == "bot" and conv["language"] == "en" and conv["is_test"] is False
    assert [c["id"] for c in (await client.get("/widget/v1/conversations", headers=auth)).json()] == [conv["id"]]
    assert (await client.get(f"/widget/v1/conversations/{conv['id']}/messages", headers=auth)).json() == []
    # The session resumes the open conversation.
    again = await _session(client, w["public_key"], visitor_id=s.json()["visitor_id"])
    assert again.json()["conversation"]["id"] == conv["id"]
    # contact only while open, csat only after the chat ended.
    r = await client.post(f"/widget/v1/conversations/{conv['id']}/contact", json={"email": "a@x.com"}, headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "invalid_conversation_state"
    r = await client.post(f"/widget/v1/conversations/{conv['id']}/csat", json={"score": 5}, headers=auth)
    assert r.status_code == 409 and r.json()["code"] == "invalid_conversation_state"
    r = await client.post(f"/widget/v1/conversations/{conv['id']}/end", headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "resolved"
    r = await client.post(
        f"/widget/v1/conversations/{conv['id']}/csat", json={"score": 5, "comment": "great"}, headers=auth
    )
    assert r.status_code == 200 and r.json()["csat_score"] == 5
    r = await client.post(f"/widget/v1/conversations/{conv['id']}/csat", json={"score": 9}, headers=auth)
    assert r.status_code == 422


async def test_conversation_create_rate_limited_per_customer(client, dash):
    w = await _widget(client, dash)
    auth = _bearer(await _session(client, w["public_key"]))
    for _ in range(10):
        assert (await client.post("/widget/v1/conversations", headers=auth)).status_code == 201
    r = await client.post("/widget/v1/conversations", headers=auth)
    assert r.status_code == 429 and "Retry-After" in r.headers
    other = _bearer(await _session(client, w["public_key"]))  # another customer is unaffected
    assert (await client.post("/widget/v1/conversations", headers=other)).status_code == 201


async def test_customer_cannot_read_other_customers_conversation_404(client, dash):
    w = await _widget(client, dash)
    a = _bearer(await _session(client, w["public_key"]))
    b = _bearer(await _session(client, w["public_key"]))
    conv = (await client.post("/widget/v1/conversations", headers=a)).json()
    for method, suffix, body in [
        ("GET", "messages", None),
        ("POST", "end", None),
        ("POST", "csat", {"score": 3}),
        ("POST", "contact", {"email": "a@x.com"}),
    ]:
        r = await client.request(method, f"/widget/v1/conversations/{conv['id']}/{suffix}", headers=b, json=body)
        assert r.status_code == 404, suffix
    assert (await client.get("/widget/v1/conversations", headers=b)).json() == []
    # A conversation of another organisation is equally invisible.
    other_headers = await signup(client, "other@x.com")
    other_org = await create_org(client, other_headers, "Other")
    ow = await _widget(client, (other_headers, other_org))
    o = _bearer(await _session(client, ow["public_key"]))
    assert (await client.get(f"/widget/v1/conversations/{conv['id']}/messages", headers=o)).status_code == 404


async def _ticket(client, dash, widget_id, headers=None):
    h, org_id = dash
    r = await client.post(f"/orgs/{org_id}/widgets/{widget_id}/preview-ticket", headers=headers or h)
    return r


async def test_preview_requires_a_dashboard_ticket(client, dash, test_settings):
    w = await _widget(client, dash)
    other = await _widget(client, dash)
    key, web = w["public_key"], test_settings.web_origin
    # No ticket: 403, even from the dashboard origin.
    r = await _session(client, key, origin=web, preview=True)
    assert r.status_code == 403 and r.json()["code"] == "origin_not_allowed"
    # A ticket for another widget: 403.
    wrong = (await _ticket(client, dash, other["id"])).json()["ticket"]
    assert (await _session(client, key, origin=web, preview=True, ticket=wrong)).status_code == 403
    # Garbage and non-ticket tokens: 403.
    assert (await _session(client, key, origin=web, preview=True, ticket="junk")).status_code == 403
    # Valid ticket but not the dashboard origin: 403.
    ticket = (await _ticket(client, dash, w["id"])).json()["ticket"]
    assert (await _session(client, key, origin=ORIGIN, preview=True, ticket=ticket)).status_code == 403
    # Valid ticket from the dashboard origin: test conversations.
    s = await _session(client, key, origin=web, preview=True, ticket=ticket)
    assert s.status_code == 200, s.text
    assert (await client.post("/widget/v1/conversations", headers=_bearer(s))).json()["is_test"] is True
    # An embedding site session is a real one.
    s = await _session(client, key, origin=ORIGIN)
    assert (await client.post("/widget/v1/conversations", headers=_bearer(s))).json()["is_test"] is False
    # The dashboard origin is not an embedding site: without preview it is refused.
    assert (await _session(client, key, origin=web, preview=False)).status_code == 403


async def test_expired_preview_ticket_403(client, dash, test_settings):
    import jwt as pyjwt
    from datetime import UTC, datetime, timedelta

    w = await _widget(client, dash)
    _, org_id = dash
    now = datetime.now(UTC)
    old = pyjwt.encode(
        {"type": "widget_preview", "org": org_id, "wid": w["id"], "iat": now - timedelta(seconds=400), "exp": now - timedelta(seconds=100)},
        test_settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
    assert (await _session(client, w["public_key"], origin=test_settings.web_origin, preview=True, ticket=old)).status_code == 403


async def test_dashboard_tokens_are_not_preview_tickets(client, dash, test_settings, auth_headers):
    w = await _widget(client, dash)
    access = dash[0]["Authorization"].split()[1]
    widget_token = (await _session(client, w["public_key"])).json()["token"]
    for bad in (access, widget_token):
        r = await _session(client, w["public_key"], origin=test_settings.web_origin, preview=True, ticket=bad)
        assert r.status_code == 403


async def test_preview_ticket_route_permissions(client, dash):
    headers, org_id = dash
    w = await _widget(client, dash)
    r = await _ticket(client, dash, w["id"])
    assert r.status_code == 200 and r.json()["ticket"]
    # Another organisation's user: 404.
    other_headers = await signup(client, "other@x.com")
    other_org = await create_org(client, other_headers, "Other")
    assert (await client.post(f"/orgs/{other_org}/widgets/{w['id']}/preview-ticket", headers=other_headers)).status_code == 404
    assert (await client.post(f"/orgs/{org_id}/widgets/{w['id']}/preview-ticket", headers=other_headers)).status_code == 404
    # No token: 401.
    assert (await client.post(f"/orgs/{org_id}/widgets/{w['id']}/preview-ticket")).status_code == 401


async def test_preview_ticket_agent_role_forbidden(kb_client, session_factory):
    from uuid import UUID

    from kb_helpers import join_org

    headers = await signup(kb_client, "owner@x.com")
    org_id = await create_org(kb_client, headers)
    w = (await kb_client.post(f"/orgs/{org_id}/widgets", json={"name": "S"}, headers=headers)).json()
    agent = await join_org(kb_client, session_factory, UUID(org_id), "agent@x.com", role_name="Agent")
    url = f"/orgs/{org_id}/widgets/{w['id']}/preview-ticket"
    assert (await kb_client.post(url, headers=agent)).status_code == 403
    bot = await kb_client.post(
        f"/orgs/{org_id}/roles", json={"name": "Botter", "permissions": ["bot.configure"]}, headers=headers
    )
    member = await join_org(kb_client, session_factory, UUID(org_id), "bot@x.com", role_id=UUID(bot.json()["id"]))
    assert (await kb_client.post(url, headers=member)).status_code == 200


async def test_disabled_or_deleted_widget_invalidates_session_tokens(client, dash):
    headers, org_id = dash
    w = await _widget(client, dash)
    auth = _bearer(await _session(client, w["public_key"]))
    assert (await client.get("/widget/v1/conversations", headers=auth)).status_code == 200
    await client.patch(f"/orgs/{org_id}/widgets/{w['id']}", json={"enabled": False}, headers=headers)
    for method, url in [("GET", "/widget/v1/conversations"), ("POST", "/widget/v1/conversations")]:
        r = await client.request(method, url, headers=auth)
        assert r.status_code == 401 and r.json()["code"] == "invalid_widget_token"
    await client.patch(f"/orgs/{org_id}/widgets/{w['id']}", json={"enabled": True}, headers=headers)
    assert (await client.post("/widget/v1/conversations", headers=auth)).status_code == 201
    assert (await client.delete(f"/orgs/{org_id}/widgets/{w['id']}", headers=headers)).status_code == 204
    for method, url in [("GET", "/widget/v1/conversations"), ("POST", "/widget/v1/conversations")]:
        r = await client.request(method, url, headers=auth)
        assert r.status_code == 401 and r.json()["code"] == "invalid_widget_token"


async def test_message_text_never_logged(client, dash, session, app):
    w = await _widget(client, dash)
    s = await _session(client, w["public_key"])
    auth = _bearer(s)
    conv = (await client.post("/widget/v1/conversations", headers=auth)).json()
    row = (await session.execute(select(Conversation).where(Conversation.id == conv["id"]))).scalar_one()
    text = "please refund order 99 SECRET-MESSAGE-TEXT"
    with structlog.testing.capture_logs() as logs:
        service = ConversationService(session, row.organisation_id, app.state.container.events)
        await service.add_customer_message(row, text, "c-1")
        await service.add_message(row, "bot", text, citations=[])
        r = await client.get(f"/widget/v1/conversations/{conv['id']}/messages", headers=auth)
    assert r.status_code == 200 and [m["text"] for m in r.json()] == [text, text]
    assert r.json()[0]["role"] == "customer" and r.json()[0]["client_id"] == "c-1"
    assert "SECRET-MESSAGE-TEXT" not in repr(logs)
    assert auth["Authorization"].split()[1] not in repr(logs)


async def test_message_text_never_in_stdlib_logs(client, dash, session, app, caplog):
    w = await _widget(client, dash)
    auth = _bearer(await _session(client, w["public_key"]))
    conv = (await client.post("/widget/v1/conversations", headers=auth)).json()
    row = (await session.execute(select(Conversation).where(Conversation.id == conv["id"]))).scalar_one()
    caplog.set_level("DEBUG")
    await ConversationService(session, row.organisation_id, app.state.container.events).add_customer_message(
        row, "SECRET-MESSAGE-TEXT", None
    )
    await client.get(f"/widget/v1/conversations/{conv['id']}/messages", headers=auth)
    assert "SECRET-MESSAGE-TEXT" not in caplog.text
