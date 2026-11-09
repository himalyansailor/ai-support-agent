import json
import re
from uuid import UUID, uuid4

from sqlalchemy import select

from kb_helpers import create_kb, create_org, join_org, signup
from supportcore.db.models import AuditLog

WIDGET_VIEW = {
    "id", "name", "public_key", "enabled", "allowed_domains", "appearance", "csat_enabled", "identity_required",
    "identity_secret_last4", "knowledge_base_ids", "snippet", "created_at",
}


async def _setup(kb_client):
    headers = await signup(kb_client, "owner@x.com")
    return headers, await create_org(kb_client, headers)


async def _widget(client, org_id, headers, name="Site") -> dict:
    r = await client.post(f"/orgs/{org_id}/widgets", json={"name": name}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


async def test_widget_crud_requires_channels_manage(kb_client, session_factory):
    headers, org_id = await _setup(kb_client)
    w = await _widget(kb_client, org_id, headers)
    agent = await join_org(kb_client, session_factory, UUID(org_id), "agent@x.com", role_name="Agent")
    base = f"/orgs/{org_id}/widgets"
    assert (await kb_client.get(base, headers=agent)).status_code == 403
    assert (await kb_client.post(base, json={"name": "N"}, headers=agent)).status_code == 403
    assert (await kb_client.get(f"{base}/{w['id']}", headers=agent)).status_code == 403
    assert (await kb_client.patch(f"{base}/{w['id']}", json={"name": "N"}, headers=agent)).status_code == 403
    assert (await kb_client.delete(f"{base}/{w['id']}", headers=agent)).status_code == 403
    assert (await kb_client.post(f"{base}/{w['id']}/identity-secret", headers=agent)).status_code == 403
    # A custom role with channels.manage works.
    role = await kb_client.post(
        f"/orgs/{org_id}/roles", json={"name": "Chan", "permissions": ["channels.manage"]}, headers=headers
    )
    member = await join_org(kb_client, session_factory, UUID(org_id), "chan@x.com", role_id=UUID(role.json()["id"]))
    assert (await kb_client.get(base, headers=member)).status_code == 200


async def test_widget_lifecycle(kb_client):
    headers, org_id = await _setup(kb_client)
    kb = await create_kb(kb_client, org_id, headers)
    w = await _widget(kb_client, org_id, headers)
    url = f"/orgs/{org_id}/widgets/{w['id']}"
    r = await kb_client.patch(
        url,
        json={
            "name": "Shop", "allowed_domains": ["Shop.Example.com"], "knowledge_base_ids": [kb["id"]],
            "appearance": {"primary_color": "#112233"}, "csat_enabled": False,
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["name"], body["allowed_domains"], body["knowledge_base_ids"]) == ("Shop", ["shop.example.com"], [kb["id"]])
    assert body["appearance"]["primary_color"] == "#112233" and body["csat_enabled"] is False
    listed = (await kb_client.get(f"/orgs/{org_id}/widgets", headers=headers)).json()
    assert [x["id"] for x in listed] == [w["id"]]
    assert (await kb_client.delete(url, headers=headers)).status_code == 204
    assert (await kb_client.get(url, headers=headers)).status_code == 404


async def test_widget_errors_have_stable_codes(kb_client):
    headers, org_id = await _setup(kb_client)
    w = await _widget(kb_client, org_id, headers)
    url = f"/orgs/{org_id}/widgets/{w['id']}"

    async def code(body):
        r = await kb_client.patch(url, json=body, headers=headers)
        return r.status_code, r.json()["code"]

    assert await code({"allowed_domains": ["*"]}) == (422, "invalid_domain")
    assert await code({"allowed_domains": [f"s{i}.example.com" for i in range(21)]}) == (422, "too_many_domains")
    assert await code({"appearance": {"position": "top"}}) == (422, "invalid_appearance")
    assert await code({"identity_required": True}) == (422, "identity_secret_required")
    assert await code({"knowledge_base_ids": [str(uuid4())]}) == (404, "not_found")


async def test_cross_tenant_widget_404(kb_client):
    headers, org_id = await _setup(kb_client)
    w = await _widget(kb_client, org_id, headers)
    other = await signup(kb_client, "other@x.com")
    other_org = await create_org(kb_client, other, "Other")
    foreign_kb = await create_kb(kb_client, other_org, other)
    url = f"/orgs/{other_org}/widgets/{w['id']}"  # right widget id, wrong org
    assert (await kb_client.get(url, headers=other)).status_code == 404
    assert (await kb_client.patch(url, json={"name": "x"}, headers=other)).status_code == 404
    assert (await kb_client.delete(url, headers=other)).status_code == 404
    assert (await kb_client.post(f"{url}/identity-secret", headers=other)).status_code == 404
    # The owner's org path with someone else's token is not even a member.
    assert (await kb_client.get(f"/orgs/{org_id}/widgets/{w['id']}", headers=other)).status_code in (403, 404)
    # Linking another org's KB is a 404 and leaves the widget untouched.
    r = await kb_client.patch(
        f"/orgs/{org_id}/widgets/{w['id']}", json={"knowledge_base_ids": [foreign_kb["id"]]}, headers=headers
    )
    assert r.status_code == 404
    assert (await kb_client.get(f"/orgs/{other_org}/widgets", headers=other)).json() == []
    assert (await kb_client.get(f"/orgs/{org_id}/widgets/{w['id']}", headers=headers)).json()["knowledge_base_ids"] == []


async def test_widget_out_has_snippet_and_no_secret(kb_client, session_factory):
    headers, org_id = await _setup(kb_client)
    w = await _widget(kb_client, org_id, headers)
    assert set(w) == WIDGET_VIEW
    assert w["snippet"] == (
        f'<script src="http://localhost:3000/widget/v1.js" data-key="{w["public_key"]}" async></script>'
    )
    assert w["identity_secret_last4"] is None and w["enabled"] is True and w["knowledge_base_ids"] == []

    r = await kb_client.post(f"/orgs/{org_id}/widgets/{w['id']}/identity-secret", headers=headers)
    assert r.status_code == 200
    assert set(r.json()) == {"secret"} and re.fullmatch(r"wsec_[A-Za-z0-9_-]{43}", r.json()["secret"])
    secret = r.json()["secret"]

    for path in (f"/orgs/{org_id}/widgets", f"/orgs/{org_id}/widgets/{w['id']}"):
        text = (await kb_client.get(path, headers=headers)).text
        assert secret not in text and "encrypted" not in text
    got = (await kb_client.get(f"/orgs/{org_id}/widgets/{w['id']}", headers=headers)).json()
    assert got["identity_secret_last4"] == secret[-4:]
    patched = await kb_client.patch(
        f"/orgs/{org_id}/widgets/{w['id']}", json={"identity_required": True}, headers=headers
    )
    assert patched.status_code == 200 and patched.json()["identity_required"] is True
    assert secret not in patched.text

    async with session_factory() as s:
        rows = (await s.execute(select(AuditLog).where(AuditLog.action.like("widget.%")))).scalars().all()
    assert {r.action for r in rows} >= {"widget.created", "widget.identity_secret_rotated", "widget.updated"}
    assert all(secret not in json.dumps(r.metadata_) for r in rows)
