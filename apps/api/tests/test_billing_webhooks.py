"""The public billing webhook route (Phase 8, Task 7). Real commits."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from kb_helpers import create_org, signup
from sqlalchemy import select
from supportcore.config import Settings
from supportcore.db.models import (
    BillingAccount,
    BillingWebhookEvent,
    Plan,
    PlanProviderPrice,
    UsageAlert,
)


@pytest.fixture
def test_settings(test_settings):
    return Settings(**{**test_settings.model_dump(), "billing": "fake"})


@pytest.fixture(autouse=True)
def _truncate(session_factory):
    """Real-commit tests: session_factory truncates every table at teardown."""


@pytest.fixture
async def org_id(raw_client, session_factory):
    headers = await signup(raw_client, "owner@x.com")
    org = await create_org(raw_client, headers)
    async with session_factory() as s:
        plan = Plan(key="pro", name="Pro", monthly_price_minor=4900, sync_status="synced", provider="fake")
        s.add(plan)
        await s.flush()
        s.add(PlanProviderPrice(plan_id=plan.id, provider="fake", price_id="p_pro_base", kind="base", amount_minor=4900))
        await s.commit()
    return org


def _event(event_id, type_="customer.subscription.updated", *, org_id=None, status="active", customer="cus_1", **extra):
    now = datetime.now(UTC)
    body = {"event_id": event_id, "type": type_, "created_at": now.isoformat(), **extra}
    if type_.startswith("customer.subscription"):
        body["subscription"] = {
            "subscription_id": "sub_1",
            "customer_id": customer,
            "organisation_id": org_id,
            "status": status,
            "price_ids": ["p_pro_base"],
            "current_period_start": (now - timedelta(days=1)).isoformat(),
            "current_period_end": (now + timedelta(days=29)).isoformat(),
            "event_at": now.isoformat(),
        }
    return json.dumps(body).encode()


async def _post(client, body, signature="ok", provider="fake"):
    return await client.post(
        f"/billing/webhooks/{provider}",
        content=body,
        headers={"x-fake-signature": signature, "content-type": "application/json"},
    )


async def test_webhook_applies_subscription_and_duplicate_is_ignored(raw_client, org_id, session_factory):
    r = await _post(raw_client, _event("evt_1", org_id=org_id))
    assert r.status_code == 200 and r.json() == {"received": True}
    async with session_factory() as s:
        account = (await s.execute(select(BillingAccount))).scalar_one()
        assert account.status == "active" and account.provider_subscription_id == "sub_1"
        account.status = "canceled"  # a redelivery must not re-apply
        await s.commit()
    r = await _post(raw_client, _event("evt_1", org_id=org_id))
    assert r.status_code == 200 and r.json() == {"received": True}
    async with session_factory() as s:
        assert (await s.execute(select(BillingAccount.status))).scalar_one() == "canceled"
        assert len((await s.execute(select(BillingWebhookEvent))).scalars().all()) == 1


async def test_webhook_bad_signature_400(raw_client, org_id, session_factory):
    r = await _post(raw_client, _event("evt_1", org_id=org_id), signature="forged")
    assert r.status_code == 400 and r.json()["code"] == "invalid_webhook"
    r = await _post(raw_client, b"not json")
    assert r.status_code == 400 and r.json()["code"] == "invalid_webhook"
    async with session_factory() as s:
        assert (await s.execute(select(BillingWebhookEvent))).scalars().all() == []


async def test_webhook_unknown_provider_404(raw_client, org_id):
    assert (await _post(raw_client, _event("evt_1", org_id=org_id), provider="stripe")).status_code == 404


async def test_webhook_payment_failed_records_alert(raw_client, org_id, session_factory):
    await _post(raw_client, _event("evt_1", org_id=org_id, customer="cus_9"))
    r = await _post(raw_client, _event("evt_2", "invoice.payment_failed", customer_id="cus_9"))
    assert r.status_code == 200
    async with session_factory() as s:
        alerts = (await s.execute(select(UsageAlert))).scalars().all()
    assert [(str(a.organisation_id), a.kind) for a in alerts] == [(org_id, "payment_failed")]


async def test_webhook_retrieval_failure_is_not_recorded_so_the_provider_retries(app, raw_client, org_id, session_factory):
    """I1: the subscription's current state is fetched before anything is recorded; a failed fetch is a 502."""
    from supportcore.billing.errors import BillingProviderError

    fake = app.state.container.adapters.billing
    fake.fail_next = BillingProviderError
    r = await _post(raw_client, _event("evt_1", org_id=org_id))
    assert r.status_code == 502 and r.json()["code"] == "billing_provider_error"
    async with session_factory() as s:
        assert (await s.execute(select(BillingWebhookEvent))).scalars().all() == []
    r = await _post(raw_client, _event("evt_1", org_id=org_id))
    assert r.status_code == 200
    async with session_factory() as s:
        assert (await s.execute(select(BillingAccount.status))).scalar_one() == "active"
