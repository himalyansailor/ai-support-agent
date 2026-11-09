"""GET /admin/overview and /admin/orgs (Phase 5, Task 9): platform-admin gate, rollup-backed numbers, the
Prometheus-unavailable flag, org links."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from kb_helpers import create_org, signup
from pydantic import SecretStr
from sqlalchemy import update

from supportcore.analytics.events import record
from supportcore.analytics.rollup import compute_hour, store_hour
from supportcore.billing.fake_billing import FakeBilling
from supportcore.db.models import BillingAccount, Conversation, Customer, Plan, PlanProviderPrice, User
from supportcore.metricsquery import system
from supportcore.metricsquery.fake_query import FakeMetricsQuery
from supportcore.metricsquery.ports import Sample


def _hours_ago(n: int) -> datetime:
    return datetime.now(UTC).replace(minute=5, second=0, microsecond=0) - timedelta(hours=n)


async def _admin(kb_client, session_factory) -> dict[str, str]:
    headers = await signup(kb_client, "root@x.com")
    async with session_factory() as s:
        await s.execute(update(User).where(User.email == "root@x.com").values(is_platform_admin=True))
        await s.commit()
    return headers


async def _conversation(session_factory, org_id) -> UUID:
    async with session_factory() as s:
        customer = Customer(organisation_id=UUID(org_id), visitor_id=uuid4().hex)
        s.add(customer)
        await s.flush()
        conv = Conversation(organisation_id=UUID(org_id), customer_id=customer.id, status="bot", is_test=False, channel="chat")
        s.add(conv)
        await s.commit()
        return conv.id


async def _activity(session_factory, org_id, when, *, rolled: bool, cost: int | None, tokens=(100, 50)):
    conv = await _conversation(session_factory, org_id)
    data = {"outcome": "answered", "no_answer": False, "rule_key": None, "rule_name": None, "input_tokens": tokens[0],
            "output_tokens": tokens[1], "provider": "openai", "model": "gpt-x", "cost_micros": cost, "kb_ids": []}
    async with session_factory() as s:
        record(s, UUID(org_id), conv, "conversation_created", channel="chat", membership_id=None, data={}, occurred_at=when)
        record(s, UUID(org_id), conv, "bot_turn", channel="chat", membership_id=None, data=data, occurred_at=when)
        await s.commit()
    if rolled:
        async with session_factory() as s:
            await store_hour(s, UUID(org_id), when, await compute_hour(s, UUID(org_id), when))
            await s.commit()


@pytest.fixture(autouse=True)
def _fake_metrics(kb_app):
    """Never reach a real Prometheus: swap in the fake (the same one SUPPORT_METRICS_QUERY=fake builds in env=test)."""
    kb_app.state.container.adapters.metrics_query = FakeMetricsQuery()


def _metrics(kb_app):
    return kb_app.state.container.adapters.metrics_query


async def test_admin_routes_require_platform_admin(kb_client, session_factory):
    owner = await signup(kb_client, "owner@x.com")
    await create_org(kb_client, owner)
    for path in ("/admin/overview", "/admin/orgs", "/admin/links"):
        assert (await kb_client.get(path)).status_code == 401
        r = await kb_client.get(path, headers=owner)
        assert r.status_code == 403 and r.json() == {"detail": "Platform admin only"}


async def test_overview_from_rollups(kb_client, kb_app, session_factory):
    admin = await _admin(kb_client, session_factory)
    owner = await signup(kb_client, "owner@x.com")
    org_a = await create_org(kb_client, owner)
    await _activity(session_factory, org_a, _hours_ago(26), rolled=True, cost=1000)        # closed hour: rollup
    await _activity(session_factory, org_a, _hours_ago(24 * 20), rolled=True, cost=None)   # 20 days ago, unpriced
    # the current hour is not rolled: it must come from the live computation
    await _activity(session_factory, org_a, datetime.now(UTC).replace(minute=0, second=1, microsecond=0), rolled=False, cost=500)
    _metrics(kb_app).results = {system.Q_ERROR_RATE_5M: [Sample({}, 0.01)], system.Q_QUEUE_DEPTH: [Sample({}, 3.0)],
                                system.Q_PROVIDER_FAILURE_RATE_1H: [Sample({"provider": "openai"}, 0.5)]}

    body = (await kb_client.get("/admin/overview", headers=admin)).json()
    assert body["orgs"]["total"] >= 1 and body["orgs"]["active_7d"] == 1 and body["orgs"]["active_30d"] == 1
    assert body["conversations"] == {"7d": 2, "30d": 3}
    assert body["llm"] == {"tokens_30d": 450, "cost_micros_30d": 1500, "unpriced_turns_30d": 1}
    assert body["system"]["error_rate_5m"] == 0.01 and body["system"]["queue_depth"] == 3.0
    assert body["system"]["provider_failure_rate_1h"] == {"openai": 0.5}
    assert body["system"]["metrics_unavailable"] is False
    assert set(body["links"]) == {"grafana", "jaeger", "langfuse"}


async def test_overview_cost_null_when_nothing_priced(kb_client, session_factory):
    admin = await _admin(kb_client, session_factory)
    owner = await signup(kb_client, "owner@x.com")
    org = await create_org(kb_client, owner)
    await _activity(session_factory, org, _hours_ago(30), rolled=True, cost=None)
    body = (await kb_client.get("/admin/overview", headers=admin)).json()
    assert body["llm"]["cost_micros_30d"] is None and body["llm"]["unpriced_turns_30d"] == 1


async def test_prometheus_unavailable_flag(kb_client, kb_app, session_factory):
    admin = await _admin(kb_client, session_factory)
    fake = _metrics(kb_app)
    fake.unavailable = {system.Q_P95_MS}
    fake.results = {system.Q_P50_MS: [Sample({}, 120.0)]}
    r = await kb_client.get("/admin/overview", headers=admin)
    assert r.status_code == 200  # the page still loads
    s = r.json()["system"]
    assert s["metrics_unavailable"] is True and s["p95_ms"] is None and s["p50_ms"] == 120.0
    assert r.json()["orgs"] == {"total": 0, "active_7d": 0, "active_30d": 0}  # database numbers unaffected


def _langfuse_on(monkeypatch, settings, host="http://lf:3030", project="proj 1"):
    for name, value in (("llm_tracer", "langfuse"), ("langfuse_host", host), ("langfuse_project_id", project),
                        ("langfuse_public_key", "pk-lf-test"), ("langfuse_secret_key", SecretStr("sk-lf-test"))):
        monkeypatch.setattr(settings, name, value)


async def test_org_list_stats_and_links_encoded(kb_client, kb_app, session_factory, monkeypatch):
    settings = kb_app.state.container.settings
    _langfuse_on(monkeypatch, settings)
    admin = await _admin(kb_client, session_factory)
    owner = await signup(kb_client, "owner@x.com")
    org = await create_org(kb_client, owner)
    idle = await create_org(kb_client, owner, "Idle")
    await _activity(session_factory, org, _hours_ago(30), rolled=True, cost=700, tokens=(10, 5))
    rows = (await kb_client.get("/admin/orgs", headers=admin)).json()
    row = next(r for r in rows if r["id"] == org)
    assert row["member_count"] == 1 and row["conversations_30d"] == 1 and row["tokens_30d"] == 15
    assert row["cost_micros_30d"] == 700 and row["last_activity_at"] is not None
    assert row["links"]["grafana"].endswith(f"/d/support-platform?var-org={org}")
    assert row["links"]["langfuse"] == (
        f"http://lf:3030/project/proj%201/traces?filter=tags%3BarrayOptions%3B%3Bany+of%3Borganisation%3A{org}"
    )
    other = next(r for r in rows if r["id"] == idle)
    assert (other["conversations_30d"], other["tokens_30d"], other["cost_micros_30d"], other["last_activity_at"]) == (0, 0, None, None)


async def test_langfuse_links_only_while_tracing_to_langfuse(kb_client, kb_app, session_factory, monkeypatch):
    """Host and project id set but SUPPORT_LLM_TRACER=none (the .env.example default): no Langfuse links."""
    settings = kb_app.state.container.settings
    monkeypatch.setattr(settings, "llm_tracer", "none")
    monkeypatch.setattr(settings, "langfuse_host", "http://lf:3030")
    monkeypatch.setattr(settings, "langfuse_project_id", "proj")
    admin = await _admin(kb_client, session_factory)
    owner = await signup(kb_client, "owner@x.com")
    org = await create_org(kb_client, owner)

    assert (await kb_client.get("/admin/overview", headers=admin)).json()["links"]["langfuse"] is None
    assert (await kb_client.get("/admin/links", headers=admin)).json()["langfuse"] is None
    row = next(r for r in (await kb_client.get("/admin/orgs", headers=admin)).json() if r["id"] == org)
    assert row["links"]["langfuse"] is None

    _langfuse_on(monkeypatch, settings, project="proj")
    assert (await kb_client.get("/admin/links", headers=admin)).json()["langfuse"] == "http://lf:3030/project/proj/traces"


async def test_links_endpoint_reads_no_metrics(kb_client, kb_app, session_factory):
    """The trace links' source: settings only, never the Prometheus queries of the overview."""
    admin = await _admin(kb_client, session_factory)
    fake = _metrics(kb_app)
    calls = []
    original = fake.query

    async def query(*args, **kwargs):
        calls.append(args)
        return await original(*args, **kwargs)

    fake.query = query
    r = await kb_client.get("/admin/links", headers=admin)
    assert r.status_code == 200 and calls == []
    assert set(r.json()) == {"grafana", "jaeger", "langfuse"}
    assert r.json()["jaeger"].endswith("/search?service=" + kb_app.state.container.settings.service_name)


async def test_rollup_lag_reported(kb_client, session_factory):
    """No watermark for an organisation older than the live window: the rollup is behind and the admin pages say so."""
    from sqlalchemy import update as sql_update

    from supportcore.db.models import Organisation, RollupWatermark

    admin = await _admin(kb_client, session_factory)
    owner = await signup(kb_client, "owner@x.com")
    stale = await create_org(kb_client, owner)
    fresh = await create_org(kb_client, owner, "Fresh")
    async with session_factory() as s:
        await s.execute(sql_update(Organisation).where(Organisation.id == UUID(stale)).values(created_at=_hours_ago(24 * 10)))
        s.add(RollupWatermark(organisation_id=UUID(fresh), rolled_through=_hours_ago(0).replace(minute=0)))
        await s.commit()

    rollup = (await kb_client.get("/admin/overview", headers=admin)).json()["rollup"]
    assert rollup["lagging_orgs"] >= 1 and rollup["oldest_live_from"] is not None
    rows = {r["id"]: r for r in (await kb_client.get("/admin/orgs", headers=admin)).json()}
    assert rows[stale]["rollup_lagging"] is True and rows[stale]["rolled_through"] is None
    assert rows[fresh]["rollup_lagging"] is False and rows[fresh]["rolled_through"] is not None


async def _plan(session_factory, key: str, name: str, prices: dict[str, int]) -> Plan:
    """A plan with one base price per (price_id -> amount_minor), all on the fake provider."""
    async with session_factory() as s:
        plan = Plan(key=key, name=name, monthly_price_minor=max(prices.values()))
        s.add(plan)
        await s.flush()
        for price_id, amount in prices.items():
            s.add(PlanProviderPrice(plan_id=plan.id, provider="fake", price_id=price_id, kind="base", amount_minor=amount))
        await s.commit()
        return plan


async def _account(session_factory, org_id, status: str, *, plan: Plan | None = None, price_ids=(), **fields) -> None:
    """Sets the organisation's billing account (creating the organisation made one) to the given state."""
    async with session_factory() as s:
        values = {"status": status, "plan_id": plan.id if plan else None, "provider": "fake",
                  "subscription_price_ids": list(price_ids), **fields}
        result = await s.execute(update(BillingAccount).where(BillingAccount.organisation_id == UUID(org_id)).values(**values))
        if result.rowcount == 0:
            s.add(BillingAccount(organisation_id=UUID(org_id), **values))
        await s.commit()


async def test_overview_mrr_counts_paying_at_their_price(kb_client, session_factory):
    admin = await _admin(kb_client, session_factory)
    pro = await _plan(session_factory, "pro", "Pro", {"price_pro_new": 6900, "price_pro_old": 4900})
    owners = [await signup(kb_client, f"o{i}@x.com") for i in range(6)]
    orgs = [await create_org(kb_client, h) for h in owners]
    await _account(session_factory, orgs[0], "active", plan=pro, price_ids=["price_pro_new", "price_msgs_metered"])
    await _account(session_factory, orgs[1], "active", plan=pro, price_ids=["price_pro_old"])  # grandfathered
    await _account(session_factory, orgs[2], "past_due", plan=pro, price_ids=["price_pro_new"])
    await _account(session_factory, orgs[3], "canceled", plan=pro, price_ids=["price_pro_new"])  # not counted
    await _account(session_factory, orgs[4], "trialing", plan=pro, price_ids=["price_pro_new"])  # not counted
    await _account(session_factory, orgs[5], "active", plan=pro, price_ids=["price_unknown"])  # matches nothing: 0

    revenue = (await kb_client.get("/admin/overview", headers=admin)).json()["revenue"]
    assert revenue == {"mrr_minor": 6900 + 4900 + 6900, "currency": "usd", "paying_orgs": 4, "trialing_orgs": 1}


async def test_overview_revenue_zero_without_accounts(kb_client, session_factory):
    admin = await _admin(kb_client, session_factory)
    revenue = (await kb_client.get("/admin/overview", headers=admin)).json()["revenue"]
    assert revenue == {"mrr_minor": 0, "currency": "usd", "paying_orgs": 0, "trialing_orgs": 0}


async def test_orgs_list_shows_plan_and_status(kb_client, kb_app, session_factory):
    kb_app.state.container.adapters.billing = FakeBilling()  # the dev provider would show "dev" for everyone
    admin = await _admin(kb_client, session_factory)
    pro = await _plan(session_factory, "pro", "Pro", {"price_pro": 4900})
    names = ("active", "expired", "none")
    orgs = {}
    for name in names:
        orgs[name] = await create_org(kb_client, await signup(kb_client, f"{name}@x.com"), name=name)
    await _account(session_factory, orgs["active"], "active", plan=pro, price_ids=["price_pro"])
    await _account(session_factory, orgs["expired"], "trialing", plan=pro,
                   trial_ends_at=datetime.now(UTC) - timedelta(days=1))

    rows = {r["name"]: r for r in (await kb_client.get("/admin/orgs", headers=admin)).json()}
    assert (rows["active"]["plan_name"], rows["active"]["billing_status"]) == ("Pro", "active")
    assert (rows["expired"]["plan_name"], rows["expired"]["billing_status"]) == ("Pro", "trial_expired")
    assert (rows["none"]["plan_name"], rows["none"]["billing_status"]) == (None, "no_plan")
