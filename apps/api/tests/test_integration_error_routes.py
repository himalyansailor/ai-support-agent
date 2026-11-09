"""GET /orgs/{org}/integration-errors (Phase 5, Task 3): analytics.view, tenant isolation, kind filter,
org-timezone date range, cursor paging newest last_seen_at first."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from kb_helpers import create_org, join_org, signup
from supportcore.analytics.integration_errors import record_integration_error


async def _setup(kb_client):
    headers = await signup(kb_client, "owner@x.com")
    return headers, await create_org(kb_client, headers)


async def test_route_permissions_and_cross_tenant_404(kb_client, session_factory):
    headers, org_id = await _setup(kb_client)
    await record_integration_error(session_factory, UUID(org_id), "llm", "anthropic", "timeout")
    url = f"/orgs/{org_id}/integration-errors"

    assert (await kb_client.get(url)).status_code == 401
    agent = await join_org(kb_client, session_factory, UUID(org_id), "agent@x.com", role_name="Agent")
    assert (await kb_client.get(url, headers=agent)).status_code == 403  # no analytics.view
    viewer = await join_org(kb_client, session_factory, UUID(org_id), "viewer@x.com", role_name="Viewer")
    r = await kb_client.get(url, headers=viewer)
    assert r.status_code == 200 and [i["kind"] for i in r.json()["items"]] == ["llm"]
    assert set(r.json()["items"][0]) == {
        "id", "kind", "provider", "error_type", "hour", "count", "first_seen_at", "last_seen_at"
    }

    other = await signup(kb_client, "other@x.com")  # not a member of the organisation
    assert (await kb_client.get(url, headers=other)).status_code == 404
    other_org = await create_org(kb_client, other, "Other")
    assert (await kb_client.get(f"/orgs/{other_org}/integration-errors", headers=other)).json()["items"] == []


async def test_filters_and_paging(kb_client, session_factory):
    headers, org_id = await _setup(kb_client)
    org = UUID(org_id)
    now = datetime.now(UTC)
    for i in range(5):
        await record_integration_error(session_factory, org, "tool", f"tool_{i}", "http_500", now=now - timedelta(minutes=i))
    await record_integration_error(session_factory, org, "llm", "openai", "timeout", now=now - timedelta(days=30))
    url = f"/orgs/{org_id}/integration-errors"

    first = (await kb_client.get(url, params={"limit": 2}, headers=headers)).json()
    assert [i["provider"] for i in first["items"]] == ["tool_0", "tool_1"] and first["next_before"]
    rest = (await kb_client.get(url, params={"limit": 10, "before": first["next_before"]}, headers=headers)).json()
    assert [i["provider"] for i in rest["items"]] == ["tool_2", "tool_3", "tool_4"] and rest["next_before"] is None

    # default range: the last 7 days, so the 30-day-old llm row is out
    assert len((await kb_client.get(url, headers=headers)).json()["items"]) == 5
    old = (now - timedelta(days=30)).date().isoformat()
    r = await kb_client.get(url, params={"from": old, "to": old, "kind": "llm"}, headers=headers)
    assert [i["provider"] for i in r.json()["items"]] == ["openai"]
    assert (await kb_client.get(url, params={"kind": "llm"}, headers=headers)).json()["items"] == []


async def test_validation(kb_client):
    headers, org_id = await _setup(kb_client)
    url = f"/orgs/{org_id}/integration-errors"
    assert (await kb_client.get(url, params={"kind": "telephony"}, headers=headers)).status_code == 422
    assert (await kb_client.get(url, params={"before": "garbage"}, headers=headers)).status_code == 422
    assert (await kb_client.get(url, params={"from": "2026-01-02", "to": "2026-01-01"}, headers=headers)).status_code == 422
    r = await kb_client.get(url, params={"from": "2026-01-01", "to": "2026-06-01"}, headers=headers)
    assert r.status_code == 422 and r.json()["code"] == "invalid_date_range"
