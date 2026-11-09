"""Organisation billing routes and the test-only subscription route (Phase 8, Task 7). Real commits: the billing
service opens its own units of work."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from kb_helpers import create_org, join_org, signup
from sqlalchemy import select
from supportcore.config import Settings
from supportcore.db.models import (
    AuditLog,
    BillingAccount,
    Plan,
    PlanProviderPrice,
    UsageEvent,
)


@pytest.fixture
def test_settings(test_settings):
    return Settings(**{**test_settings.model_dump(), "billing": "fake", "web_origin": "https://app.example.com"})


@pytest.fixture(autouse=True)
def _truncate(session_factory):
    """Real-commit tests: session_factory truncates every table at teardown."""


async def _plans(session_factory) -> dict[str, str]:
    async with session_factory() as s:
        pro = Plan(
            key="pro", name="Pro", monthly_price_minor=4900, trial_days=14, messages=100,
            messages_overage_micros=5000, sync_status="synced", provider="fake",
        )
        team = Plan(key="team", name="Team", monthly_price_minor=9900, trial_days=0, sync_status="synced", provider="fake")
        s.add_all([pro, team])
        await s.flush()
        s.add_all(
            [
                PlanProviderPrice(plan_id=pro.id, provider="fake", price_id="p_pro_base", kind="base", amount_minor=4900),
                PlanProviderPrice(plan_id=pro.id, provider="fake", price_id="p_pro_msg", kind="messages"),
                PlanProviderPrice(plan_id=team.id, provider="fake", price_id="p_team_base", kind="base", amount_minor=9900),
            ]
        )
        await s.commit()
        return {"pro": str(pro.id), "team": str(team.id)}


@pytest.fixture
async def setup(raw_client, session_factory):
    headers = await signup(raw_client, "owner@x.com")
    org_id = await create_org(raw_client, headers)
    return raw_client, headers, org_id, await _plans(session_factory)


async def _activate(client, org_id, plan_id, *, status="active", event_at=None):
    now = datetime.now(UTC)
    body = {
        "organisation_id": org_id,
        "plan_id": plan_id,
        "status": status,
        "period_start": (now - timedelta(days=1)).isoformat(),
        "period_end": (now + timedelta(days=29)).isoformat(),
        "event_at": (event_at or now).isoformat(),
    }
    r = await client.post("/testing/billing/subscription", json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def test_summary_trial_and_audit(setup, session_factory):
    client, headers, org_id, plans = setup
    r = await client.get(f"/orgs/{org_id}/billing", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "no_plan" and body["plan"] is None and body["locked"] and not body["subscribed"]
    assert body["supports_metered"] and not body["grants_everything"] and body["currency"] == "USD"
    assert [m["meter"] for m in body["meters"]] == ["messages", "voice_minutes"]
    assert body["capacity"]["seats"] == {"used": 1, "limit": 1}  # locked: held at the current count

    r = await client.post(f"/orgs/{org_id}/billing/trial", json={"plan_id": plans["pro"], "limit_behaviour": "hard_stop"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "trialing" and body["plan"]["key"] == "pro" and not body["locked"]
    assert body["meters"][0] == {"meter": "messages", "used": 0, "limit": 100, "overage": False, "reached": False, "percent": 0}

    again = await client.post(f"/orgs/{org_id}/billing/trial", json={"plan_id": plans["pro"]}, headers=headers)
    assert again.status_code == 409 and again.json()["code"] == "trial_not_available"

    async with session_factory() as s:
        actions = (await s.execute(select(AuditLog.action, AuditLog.metadata_).where(AuditLog.organisation_id == UUID(org_id)))).all()
    assert ("billing_account.changed", "trial_started") in [(a, m.get("change")) for a, m in actions]


async def test_checkout_returns_url_then_refused_when_subscribed_409(setup, app):
    client, headers, org_id, plans = setup
    r = await client.post(f"/orgs/{org_id}/billing/checkout", json={"plan_id": plans["pro"]}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["url"].startswith(f"https://app.example.com/orgs/{org_id}/settings/billing?fake_checkout=")
    fake = app.state.container.adapters.billing
    assert fake.checkouts[-1]["success_url"].endswith(f"/orgs/{org_id}/settings/billing?checkout=success")

    assert (await _activate(client, org_id, plans["pro"])) == {"applied": True}
    r = await client.post(f"/orgs/{org_id}/billing/checkout", json={"plan_id": plans["pro"]}, headers=headers)
    assert r.status_code == 409 and r.json()["code"] == "already_subscribed"

    summary = (await client.get(f"/orgs/{org_id}/billing", headers=headers)).json()
    assert summary["status"] == "active" and summary["subscribed"] and summary["plan"]["key"] == "pro"
    portal = await client.post(f"/orgs/{org_id}/billing/portal", headers=headers)
    assert portal.status_code == 200 and "fake_portal=1" in portal.json()["url"]


async def test_change_plan_and_limit_behaviour_routes(setup):
    client, headers, org_id, plans = setup
    r = await client.post(f"/orgs/{org_id}/billing/change-plan", json={"plan_id": plans["team"]}, headers=headers)
    assert r.status_code == 409 and r.json()["code"] == "not_subscribed"
    assert (await client.post(f"/orgs/{org_id}/billing/portal", headers=headers)).json()["code"] == "not_subscribed"

    await _activate(client, org_id, plans["pro"])
    r = await client.patch(f"/orgs/{org_id}/billing", json={"limit_behaviour": "overage"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["limit_behaviour"] == "overage" and r.json()["meters"][0]["overage"] is True

    r = await client.post(f"/orgs/{org_id}/billing/change-plan", json={"plan_id": plans["team"]}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["plan"]["key"] == "team"
    assert (await client.patch(f"/orgs/{org_id}/billing", json={"limit_behaviour": "nope"}, headers=headers)).status_code == 422


async def test_overage_refused_when_provider_not_metered_409(setup, app):
    client, headers, org_id, plans = setup
    app.state.container.adapters.billing.supports_metered = False
    r = await client.patch(f"/orgs/{org_id}/billing", json={"limit_behaviour": "overage"}, headers=headers)
    assert r.status_code == 409 and r.json()["code"] == "overage_unsupported"
    r = await client.post(f"/orgs/{org_id}/billing/trial", json={"plan_id": plans["pro"], "limit_behaviour": "overage"}, headers=headers)
    assert r.status_code == 409 and r.json()["code"] == "overage_unsupported"


async def test_billing_routes_need_billing_manage_403(setup, session_factory):
    client, _, org_id, plans = setup
    agent = await join_org(client, session_factory, UUID(org_id), "agent@x.com", role_name="Agent")
    base = f"/orgs/{org_id}/billing"
    for call in (
        client.get(base, headers=agent),
        client.patch(base, json={"limit_behaviour": "hard_stop"}, headers=agent),
        client.post(f"{base}/trial", json={"plan_id": plans["pro"]}, headers=agent),
        client.post(f"{base}/checkout", json={"plan_id": plans["pro"]}, headers=agent),
        client.post(f"{base}/change-plan", json={"plan_id": plans["pro"]}, headers=agent),
        client.post(f"{base}/portal", headers=agent),
    ):
        assert (await call).status_code == 403


async def test_banner_readable_by_agent(setup, session_factory):
    client, headers, org_id, plans = setup
    agent = await join_org(client, session_factory, UUID(org_id), "agent@x.com", role_name="Agent")
    r = await client.get(f"/orgs/{org_id}/billing/banner", headers=agent)
    assert r.status_code == 200, r.text
    assert r.json() == {"effective_status": "no_plan", "locked": True, "trial_ends_at": None, "reached": [], "past_due": False}

    await client.post(f"/orgs/{org_id}/billing/trial", json={"plan_id": plans["pro"]}, headers=headers)
    body = (await client.get(f"/orgs/{org_id}/billing/banner", headers=agent)).json()
    assert body["effective_status"] == "trialing" and not body["locked"] and body["trial_ends_at"] is not None

    await _activate(client, org_id, plans["pro"], status="past_due")
    body = (await client.get(f"/orgs/{org_id}/billing/banner", headers=agent)).json()
    assert body["past_due"] and body["trial_ends_at"] is None


async def test_cross_tenant_billing_404(setup):
    client, _, org_id, plans = setup
    other = await signup(client, "other@x.com")
    await create_org(client, other, name="Other")
    base = f"/orgs/{org_id}/billing"
    for call in (
        client.get(base, headers=other),
        client.get(f"{base}/banner", headers=other),
        client.patch(base, json={"limit_behaviour": "hard_stop"}, headers=other),
        client.post(f"{base}/trial", json={"plan_id": plans["pro"]}, headers=other),
        client.post(f"{base}/checkout", json={"plan_id": plans["pro"]}, headers=other),
        client.post(f"{base}/change-plan", json={"plan_id": plans["pro"]}, headers=other),
        client.post(f"{base}/portal", headers=other),
    ):
        assert (await call).status_code == 404


async def test_testing_subscription_route_ignores_older_snapshot(setup, session_factory):
    client, _, org_id, plans = setup
    now = datetime.now(UTC)
    assert (await _activate(client, org_id, plans["pro"], status="past_due", event_at=now)) == {"applied": True}
    assert (await _activate(client, org_id, plans["pro"], event_at=now - timedelta(minutes=1))) == {"applied": False}
    async with session_factory() as s:
        account = (await s.execute(select(BillingAccount))).scalar_one()
    assert account.status == "past_due" and account.subscription_price_ids == ["p_pro_base"]


async def test_testing_subscription_route_absent_without_fake_billing(test_settings, db_engine):
    import httpx
    from httpx import ASGITransport
    from supportapi.main import create_app

    app = create_app(Settings(**{**test_settings.model_dump(), "billing": "dev"}))
    try:
        async with httpx.AsyncClient(transport=ASGITransport(app), base_url="https://test") as c:
            assert (await c.post("/testing/billing/subscription", json={})).status_code == 404
            assert (await c.get("/testing/billing/meter-events")).status_code == 404
            assert (await c.post("/testing/billing/report-usage")).status_code == 404
    finally:
        await app.state.engine.dispose()


async def test_testing_report_usage_and_meter_events_read_back(setup, app, session_factory):
    """The e2e suite runs report_usage in the API process (whose fake recorded the meter events) and reads them back."""
    client, headers, org_id, plans = setup
    r = await client.post(f"/orgs/{org_id}/billing/checkout", json={"plan_id": plans["pro"]}, headers=headers)
    assert r.status_code == 200, r.text
    r = await client.patch(f"/orgs/{org_id}/billing", json={"limit_behaviour": "overage"}, headers=headers)
    assert r.status_code == 200, r.text
    assert (await _activate(client, org_id, plans["pro"])) == {"applied": True}
    assert (await client.get("/testing/billing/meter-events")).json() == {"meter_events": []}

    now = datetime.now(UTC)
    async with session_factory() as s:
        s.add(UsageEvent(organisation_id=UUID(org_id), meter="messages", quantity=1, source_id=uuid4(), occurred_at=now - timedelta(minutes=1)))
        await s.commit()
    r = await client.post("/testing/billing/report-usage")
    assert r.status_code == 200 and r.json() == {"sent": 1}, r.text

    events = (await client.get("/testing/billing/meter-events")).json()["meter_events"]
    assert len(events) == 1
    event = events[0]
    assert event["organisation_id"] == org_id and event["meter"] == "messages" and event["quantity"] == 1
    assert event["customer_id"] == app.state.container.adapters.billing.customers[-1]["id"]
    datetime.fromisoformat(event["timestamp"])
