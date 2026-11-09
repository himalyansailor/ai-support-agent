from uuid import UUID, uuid4

import httpx
from sqlalchemy import select

from kb_helpers import create_org, join_org, signup
from supportcore.db.models import AuditLog
from supportcore.tools.http_executor import HttpToolExecutor

TOOL_VIEW = {
    "id", "name", "description", "method", "url_template", "auth_header_name", "auth_header_last4",
    "input_schema", "read_only", "enabled", "created_at", "updated_at",
}
BODY = {
    "name": "get_order",
    "description": "Look up an order",
    "method": "GET",
    "url_template": "https://api.shop.com/orders/{order_id}",
    "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
    "auth_header_name": "X-Api-Key",
    "auth_header_value": "super-secret-9876",
}


async def _setup(kb_client):
    headers = await signup(kb_client, "owner@x.com")
    return headers, await create_org(kb_client, headers)


async def test_tool_lifecycle_never_returns_the_secret(kb_client, session_factory):
    headers, org_id = await _setup(kb_client)
    base = f"/orgs/{org_id}/tools"
    r = await kb_client.post(base, json=BODY, headers=headers)
    assert r.status_code == 201, r.text
    tool = r.json()
    assert set(tool) == TOOL_VIEW and tool["auth_header_last4"] == "9876" and tool["read_only"] is True
    assert "super-secret" not in r.text
    assert (await kb_client.get(f"{base}/{tool['id']}", headers=headers)).json() == tool
    assert [t["id"] for t in (await kb_client.get(base, headers=headers)).json()] == [tool["id"]]
    r = await kb_client.patch(
        f"{base}/{tool['id']}", json={"enabled": False, "auth_header_value": "rotated-0001"}, headers=headers
    )
    assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["auth_header_last4"] == "0001"
    r = await kb_client.patch(f"{base}/{tool['id']}", json={"auth_header_name": None}, headers=headers)
    assert r.json()["auth_header_name"] is None and r.json()["auth_header_last4"] is None
    assert (await kb_client.delete(f"{base}/{tool['id']}", headers=headers)).status_code == 204
    assert (await kb_client.get(f"{base}/{tool['id']}", headers=headers)).status_code == 404
    async with session_factory() as s:
        rows = (await s.execute(select(AuditLog).order_by(AuditLog.created_at))).scalars().all()
    tool_rows = [r for r in rows if r.action.startswith("business_tool.")]
    assert [r.action for r in tool_rows] == [
        "business_tool.created", "business_tool.updated", "business_tool.updated", "business_tool.deleted",
    ]
    dump = repr([r.__dict__ for r in tool_rows])
    assert "super-secret" not in dump and "rotated" not in dump and "9876" not in dump and "0001" not in dump
    assert "'auth_header': 'changed'" in dump


async def test_patch_to_a_new_host_shows_no_auth_header(kb_client):
    headers, org_id = await _setup(kb_client)
    base = f"/orgs/{org_id}/tools"
    tool = (await kb_client.post(base, json=BODY, headers=headers)).json()
    r = await kb_client.patch(
        f"{base}/{tool['id']}", json={"url_template": "https://other.example.net/o/{order_id}"}, headers=headers
    )
    assert r.status_code == 200, r.text
    assert (r.json()["auth_header_name"], r.json()["auth_header_last4"]) == (None, None)


async def test_tool_routes_require_bot_configure(kb_client, session_factory):
    headers, org_id = await _setup(kb_client)
    base = f"/orgs/{org_id}/tools"
    tool = (await kb_client.post(base, json=BODY, headers=headers)).json()
    agent = await join_org(kb_client, session_factory, UUID(org_id), "agent@x.com", role_name="Agent")
    viewer = await join_org(kb_client, session_factory, UUID(org_id), "viewer@x.com", role_name="Viewer")
    for who in (agent, viewer):
        assert (await kb_client.get(base, headers=who)).status_code == 403
        assert (await kb_client.post(base, json=BODY, headers=who)).status_code == 403
        assert (await kb_client.get(f"{base}/{tool['id']}", headers=who)).status_code == 403
        assert (await kb_client.patch(f"{base}/{tool['id']}", json={"enabled": False}, headers=who)).status_code == 403
        assert (await kb_client.delete(f"{base}/{tool['id']}", headers=who)).status_code == 403
        r = await kb_client.post(f"{base}/{tool['id']}/test", json={"arguments": {"order_id": "1"}}, headers=who)
        assert r.status_code == 403
    assert (await kb_client.get(base)).status_code == 401
    role = await kb_client.post(
        f"/orgs/{org_id}/roles", json={"name": "Botter", "permissions": ["bot.configure"]}, headers=headers
    )
    member = await join_org(kb_client, session_factory, UUID(org_id), "bot@x.com", role_id=UUID(role.json()["id"]))
    assert (await kb_client.get(base, headers=member)).status_code == 200


async def test_cross_tenant_tool_404(kb_client):
    headers, org_id = await _setup(kb_client)
    tool = (await kb_client.post(f"/orgs/{org_id}/tools", json=BODY, headers=headers)).json()
    other = await signup(kb_client, "other@x.com")
    other_org = await create_org(kb_client, other, "Other")
    base = f"/orgs/{other_org}/tools/{tool['id']}"
    assert (await kb_client.get(base, headers=other)).status_code == 404
    assert (await kb_client.patch(base, json={"enabled": False}, headers=other)).status_code == 404
    assert (await kb_client.delete(base, headers=other)).status_code == 404
    assert (await kb_client.post(f"{base}/test", json={"arguments": {"order_id": "1"}}, headers=other)).status_code == 404
    assert (await kb_client.get(f"/orgs/{other_org}/tools", headers=other)).json() == []
    # Someone else's org id in the path is not enough.
    assert (await kb_client.get(f"/orgs/{org_id}/tools", headers=other)).status_code in (403, 404)


async def test_error_codes(kb_client):
    headers, org_id = await _setup(kb_client)
    base = f"/orgs/{org_id}/tools"

    async def code(body, expected_status):
        r = await kb_client.post(base, json=body, headers=headers)
        assert r.status_code == expected_status, r.text
        return r.json()["code"]

    assert await code({**BODY, "input_schema": {"type": "array"}}, 422) == "invalid_tool_schema"
    assert await code({**BODY, "url_template": "https://x.com/{nope}"}, 422) == "invalid_tool_schema"
    assert await code({**BODY, "method": "PUT"}, 422) == "invalid_tool"
    assert await code({**BODY, "name": "Bad Name"}, 422) == "invalid_tool"
    assert (await kb_client.post(base, json=BODY, headers=headers)).status_code == 201
    assert await code(BODY, 409) == "tool_name_taken"


async def test_test_endpoint_calls_the_tool(kb_client, kb_app):
    headers, org_id = await _setup(kb_client)
    tool = (await kb_client.post(f"/orgs/{org_id}/tools", json=BODY, headers=headers)).json()
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, text="shipped") if request.url.path != "/orders/404" else httpx.Response(404)

    container = kb_app.state.container
    container.adapters.tool_executor = HttpToolExecutor(container.settings, transport=httpx.MockTransport(handler))
    url = f"/orgs/{org_id}/tools/{tool['id']}/test"
    r = await kb_client.post(url, json={"arguments": {"order_id": "A1"}}, headers=headers)
    assert r.status_code == 200, r.text
    result = r.json()
    assert (result["ok"], result["status"], result["text"], result["error"]) == (True, 200, "shipped", None)
    assert seen[0].headers["X-Api-Key"] == "super-secret-9876"
    assert "super-secret" not in r.text
    r = await kb_client.post(url, json={"arguments": {"order_id": "404"}}, headers=headers)
    assert r.status_code == 200 and (r.json()["ok"], r.json()["error"]) == (False, "http_404")
    r = await kb_client.post(url, json={"arguments": {}}, headers=headers)
    assert (r.status_code, r.json()["code"]) == (422, "invalid_tool_arguments")
    r = await kb_client.post(f"/orgs/{org_id}/tools/{uuid4()}/test", json={"arguments": {}}, headers=headers)
    assert r.status_code == 404
