import json
import uuid

import pytest
from supportcore.billing.errors import InvalidWebhookError
from supportcore.billing.fake_billing import FakeBilling


def test_parse_webhook_rejects_bad_signature():
    billing = FakeBilling()
    with pytest.raises(InvalidWebhookError):
        billing.parse_webhook(b"{}", {"x-fake-signature": "nope"})
    with pytest.raises(InvalidWebhookError):
        billing.parse_webhook(b"{}", {})


def test_parse_webhook_builds_event():
    org = uuid.uuid4()
    body = json.dumps(
        {
            "event_id": "evt_1",
            "type": "customer.subscription.updated",
            "created_at": "2026-10-01T00:00:00+00:00",
            "customer_id": "cus_1",
            "subscription": {
                "subscription_id": "sub_1",
                "customer_id": "cus_1",
                "organisation_id": str(org),
                "status": "active",
                "price_ids": ["p1"],
                "current_period_start": "2026-10-01T00:00:00+00:00",
                "current_period_end": "2026-11-01T00:00:00+00:00",
                "event_at": "2026-10-01T00:00:00+00:00",
            },
        }
    ).encode()
    event = FakeBilling().parse_webhook(body, {"X-Fake-Signature": "ok"})
    assert event.event_id == "evt_1"
    assert event.subscription is not None
    assert event.subscription.organisation_id == org
    assert event.subscription.cancel_at_period_end is False


async def test_checkout_url_points_back_to_billing_page():
    billing = FakeBilling(web_origin="https://app.test")
    org = uuid.uuid4()
    url = await billing.checkout_url("cus_1", org, ["p1"], "https://s", "https://c")
    assert url == f"https://app.test/orgs/{org}/settings/billing?fake_checkout=1"
    assert billing.checkouts[0]["line_prices"] == ["p1"]


async def test_fail_next_raises_once():
    billing = FakeBilling()
    billing.fail_next = RuntimeError
    with pytest.raises(RuntimeError):
        await billing.create_customer(uuid.uuid4(), "Acme", None)
    assert await billing.create_customer(uuid.uuid4(), "Acme", None) == "fake_cus_1"


async def test_dev_billing_price_ids_never_collide_across_instances():
    from supportcore.billing.dev_billing import DevBilling
    from supportcore.billing.ports import PlanSpec

    spec = PlanSpec("pro", "Pro", 1000, 0, 5, 7, "usd")
    first = await DevBilling().sync_plan(spec, None, None)
    second = await DevBilling().sync_plan(spec, None, None)
    ids = lambda r: {p.price_id for p in r.created}  # noqa: E731
    assert len(first.created) == 3 and not ids(first) & ids(second)
    assert first.product_id == second.product_id == "dev_prod_pro"


async def test_fake_billing_price_ids_never_collide_across_instances():
    """The e2e API restarts on a database that keeps its plans: a new process's prices must not reuse old ids."""
    from supportcore.billing.ports import PlanSpec

    spec = PlanSpec("pro", "Pro", 1000, 0, 5, 7, "usd")
    first = await FakeBilling().sync_plan(spec, None, None)
    second = await FakeBilling().sync_plan(spec, None, None)
    ids = lambda r: {p.price_id for p in r.created}  # noqa: E731
    assert len(first.created) == 3 and not ids(first) & ids(second)
