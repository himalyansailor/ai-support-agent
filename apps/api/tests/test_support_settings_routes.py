from uuid import UUID

from sqlalchemy import select

from kb_helpers import create_org, join_org, signup
from supportcore.db.models import AuditLog

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
VIEW = {"timezone", "weekly_hours", "holidays", "sla_targets"}


def _body(**overrides):
    return {
        "timezone": "Europe/London",
        "weekly_hours": {d: ([["08:00", "16:00"]] if d in WEEKDAYS[:5] else []) for d in WEEKDAYS},
        "holidays": ["2027-12-25"],
        "sla_targets": {
            "urgent": {"first_response": 10, "resolution": 120},
            "high": {"first_response": 60, "resolution": 480},
            "normal": {"first_response": 240, "resolution": 1440},
            "low": {"first_response": 480, "resolution": 2880},
        },
    } | overrides


async def test_get_returns_defaults(kb_client):
    headers = await signup(kb_client, "owner@x.com")
    org_id = await create_org(kb_client, headers)
    r = await kb_client.get(f"/orgs/{org_id}/support-settings", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == VIEW
    assert body["timezone"] == "UTC" and body["holidays"] == []
    assert body["weekly_hours"]["mon"] == [["09:00", "17:00"]] and body["weekly_hours"]["sat"] == []
    assert body["sla_targets"]["normal"] == {"first_response": 240, "resolution": 1440}


async def test_support_settings_requires_org_manage_to_write(kb_client, session_factory):
    headers = await signup(kb_client, "owner@x.com")
    org_id = await create_org(kb_client, headers)
    url = f"/orgs/{org_id}/support-settings"
    agent = await join_org(kb_client, session_factory, UUID(org_id), "agent@x.com", role_name="Agent")
    viewer = await join_org(kb_client, session_factory, UUID(org_id), "viewer@x.com", role_name="Viewer")
    assert (await kb_client.put(url, json=_body(), headers=agent)).status_code == 403
    assert (await kb_client.put(url, json=_body(), headers=viewer)).status_code == 403
    r = await kb_client.put(url, json=_body(), headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == _body()
    assert (await kb_client.get(url, headers=headers)).json() == _body()
    async with session_factory() as s:
        actions = (await s.execute(select(AuditLog.action))).scalars().all()
    assert "support_settings.updated" in actions


async def test_any_member_can_read(kb_client, session_factory):
    headers = await signup(kb_client, "owner@x.com")
    org_id = await create_org(kb_client, headers)
    url = f"/orgs/{org_id}/support-settings"
    await kb_client.put(url, json=_body(), headers=headers)
    for email, role in (("agent@x.com", "Agent"), ("viewer@x.com", "Viewer")):
        member = await join_org(kb_client, session_factory, UUID(org_id), email, role_name=role)
        r = await kb_client.get(url, headers=member)
        assert r.status_code == 200 and r.json()["timezone"] == "Europe/London"


async def test_cross_tenant_404(kb_client):
    owner = await signup(kb_client, "owner@x.com")
    org_id = await create_org(kb_client, owner)
    outsider = await signup(kb_client, "outsider@x.com")
    url = f"/orgs/{org_id}/support-settings"
    assert (await kb_client.get(url, headers=outsider)).status_code == 404
    assert (await kb_client.put(url, json=_body(), headers=outsider)).status_code == 404


async def test_invalid_payloads_return_stable_codes(kb_client):
    headers = await signup(kb_client, "owner@x.com")
    org_id = await create_org(kb_client, headers)
    url = f"/orgs/{org_id}/support-settings"
    r = await kb_client.put(url, json=_body(timezone="Mars/Olympus"), headers=headers)
    assert r.status_code == 422 and r.json()["code"] == "invalid_business_hours"
    overlap = {d: [["09:00", "12:00"], ["11:00", "17:00"]] for d in WEEKDAYS}
    r = await kb_client.put(url, json=_body(weekly_hours=overlap), headers=headers)
    assert r.status_code == 422 and r.json()["code"] == "invalid_business_hours"
    r = await kb_client.put(url, json=_body(weekly_hours={d: [] for d in WEEKDAYS}), headers=headers)
    assert r.status_code == 422 and r.json()["code"] == "invalid_business_hours"
    bad = _body()["sla_targets"] | {"urgent": {"first_response": 0, "resolution": 5}}
    r = await kb_client.put(url, json=_body(sla_targets=bad), headers=headers)
    assert r.status_code == 422 and r.json()["code"] == "invalid_sla_targets"


async def test_non_string_timezone_is_422_not_500(kb_client):
    headers = await signup(kb_client, "owner@x.com")
    org_id = await create_org(kb_client, headers)
    url = f"/orgs/{org_id}/support-settings"
    for tz in (5, None, True, ["UTC"]):
        r = await kb_client.put(url, json=_body(timezone=tz), headers=headers)
        assert r.status_code == 422, (tz, r.status_code, r.text)
    r = await kb_client.put(url, json=_body(timezone="europe/london"), headers=headers)
    assert r.status_code == 422 and r.json()["code"] == "invalid_business_hours"
