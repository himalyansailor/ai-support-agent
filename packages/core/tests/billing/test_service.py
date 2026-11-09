"""BillingService: trial, checkout, plan change, limit behaviour, portal and webhooks (Phase 8, Task 7).

Real commits: the service opens its own units of work, so these tests use `session_factory` (every table is
truncated at teardown)."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from supportcore.audit.subscribers import register_audit_subscribers
from supportcore.billing.errors import (
    AlreadySubscribedError,
    BillingProviderError,
    NotSubscribedError,
    OverageUnsupportedError,
    PlanNotAvailableError,
    TrialNotAvailableError,
)
from supportcore.billing.fake_billing import FakeBilling
from supportcore.billing.memory_counter import MemoryUsageCounter
from supportcore.billing.ports import SubscriptionSnapshot
from supportcore.billing.service import BillingService
from supportcore.config import Settings
from supportcore.db.models import (
    AuditLog,
    BillingAccount,
    BillingWebhookEvent,
    Organisation,
    Plan,
    PlanProviderPrice,
    UsageAlert,
    User,
)
from supportcore.db.session import get_current_session
from supportcore.events import EventBus

WEB = "https://app.example.com"


@pytest.fixture
def fake():
    return FakeBilling(web_origin=WEB)


@pytest.fixture
def service(session_factory, fake):
    settings = Settings(_env_file=None, env="test", billing="fake", web_origin=WEB, jwt_secret="t" * 40)
    bus = EventBus()
    register_audit_subscribers(bus, get_current_session)
    adapters = SimpleNamespace(billing=fake, usage_counter=MemoryUsageCounter())
    return BillingService(session_factory, adapters, settings, bus)


@pytest.fixture
async def world(session_factory):
    """An organisation, its owner and two synced plans (`pro` with metered prices, `team` without overage)."""
    async with session_factory() as s:
        org = Organisation(name="Acme", slug=f"acme-{uuid.uuid4().hex[:6]}")
        user = User(email="owner@x.com", password_hash="x", name="Owner")
        pro = Plan(
            key="pro", name="Pro", monthly_price_minor=4900, trial_days=14, messages=100,
            messages_overage_micros=5000, sync_status="synced", provider="fake",
        )
        team = Plan(key="team", name="Team", monthly_price_minor=9900, trial_days=0, sync_status="synced", provider="fake")
        s.add_all([org, user, pro, team])
        await s.flush()
        s.add_all(
            [
                PlanProviderPrice(plan_id=pro.id, provider="fake", price_id="p_pro_base", kind="base", amount_minor=4900),
                PlanProviderPrice(
                    plan_id=pro.id, provider="fake", price_id="p_pro_msg", kind="messages",
                    included_units=100, overage_micros=5000,
                ),
                PlanProviderPrice(plan_id=team.id, provider="fake", price_id="p_team_base", kind="base", amount_minor=9900),
            ]
        )
        await s.commit()
        return SimpleNamespace(org=org, user=user, pro=pro, team=team)


async def _account(session_factory, org_id) -> BillingAccount:
    async with session_factory() as s:
        return (await s.execute(select(BillingAccount).where(BillingAccount.organisation_id == org_id))).scalar_one()


async def _set(session_factory, org_id, **values) -> None:
    async with session_factory() as s:
        account = (await s.execute(select(BillingAccount).where(BillingAccount.organisation_id == org_id))).scalar_one()
        for name, value in values.items():
            setattr(account, name, value)
        await s.commit()


def _snapshot(org_id, *, status="active", price_ids=("p_pro_base",), event_at=None, sub="sub_1", customer="cus_1"):
    now = datetime.now(UTC)
    return SubscriptionSnapshot(
        subscription_id=sub,
        customer_id=customer,
        organisation_id=org_id,
        status=status,
        price_ids=list(price_ids),
        current_period_start=now - timedelta(days=1),
        current_period_end=now + timedelta(days=29),
        cancel_at_period_end=False,
        event_at=event_at or now,
    )


async def _subscribe(service, world, **kwargs):
    await service.summary(world.org.id)  # creates the account
    assert await service.apply_subscription(_snapshot(world.org.id, **kwargs))


def _webhook(event_id, type_, *, subscription=None, customer_id=None):
    body = {"event_id": event_id, "type": type_, "created_at": datetime.now(UTC).isoformat(), "customer_id": customer_id}
    if subscription is not None:
        snap = subscription
        body["subscription"] = {
            "subscription_id": snap.subscription_id,
            "customer_id": snap.customer_id,
            "organisation_id": str(snap.organisation_id) if snap.organisation_id else None,
            "status": snap.status,
            "price_ids": snap.price_ids,
            "current_period_start": snap.current_period_start.isoformat(),
            "current_period_end": snap.current_period_end.isoformat(),
            "cancel_at_period_end": snap.cancel_at_period_end,
            "event_at": snap.event_at.isoformat(),
        }
    return json.dumps(body).encode(), {"x-fake-signature": "ok"}


async def test_start_trial_sets_window_and_once_only(service, world, session_factory):
    await service.start_trial(world.org.id, world.pro.id, "hard_stop", world.user)
    account = await _account(session_factory, world.org.id)
    assert account.status == "trialing" and account.plan_id == world.pro.id
    assert account.trial_ends_at - account.trial_started_at == timedelta(days=14)
    assert abs((account.trial_started_at - datetime.now(UTC)).total_seconds()) < 60

    with pytest.raises(TrialNotAvailableError):  # once only
        await service.start_trial(world.org.id, world.pro.id, "hard_stop", world.user)
    await _set(session_factory, world.org.id, status="no_plan")
    with pytest.raises(TrialNotAvailableError):  # trial_started_at is set: still once only
        await service.start_trial(world.org.id, world.pro.id, "hard_stop", world.user)

    async with session_factory() as s:
        org2 = Organisation(name="Two", slug="two")
        s.add(org2)
        await s.commit()
    with pytest.raises(TrialNotAvailableError):  # no trial on this plan
        await service.start_trial(org2.id, world.team.id, "hard_stop", world.user)

    async with session_factory() as s:
        audit = (await s.execute(select(AuditLog).where(AuditLog.organisation_id == world.org.id))).scalars().all()
    assert [(a.action, a.metadata_["change"]) for a in audit] == [("billing_account.changed", "trial_started")]
    assert audit[0].actor_user_id == world.user.id


async def test_start_trial_overage_needs_metered_provider(service, world, fake):
    fake.supports_metered = False
    with pytest.raises(OverageUnsupportedError):
        await service.start_trial(world.org.id, world.pro.id, "overage", world.user)


async def test_checkout_creates_customer_once_and_uses_overage_lines(service, world, fake, session_factory):
    await service.set_limit_behaviour(world.org.id, "overage", world.user)
    url = await service.checkout(world.org.id, world.pro.id, world.user)
    assert url.startswith(f"{WEB}/orgs/{world.org.id}/settings/billing")
    assert len(fake.customers) == 1 and fake.customers[0]["email"] == "owner@x.com"
    checkout = fake.checkouts[0]
    assert checkout["line_prices"] == ["p_pro_base", "p_pro_msg"]
    assert checkout["success_url"] == f"{WEB}/orgs/{world.org.id}/settings/billing?checkout=success"
    assert checkout["cancel_url"] == f"{WEB}/orgs/{world.org.id}/settings/billing?checkout=cancelled"
    account = await _account(session_factory, world.org.id)
    assert account.provider_customer_id == "fake_cus_1" and account.provider == "fake"

    await service.set_limit_behaviour(world.org.id, "hard_stop", world.user)
    await service.checkout(world.org.id, world.pro.id, world.user)
    assert len(fake.customers) == 1  # reused
    assert fake.checkouts[1]["customer_id"] == "fake_cus_1" and fake.checkouts[1]["line_prices"] == ["p_pro_base"]


async def test_checkout_keeps_customer_stored_meanwhile(service, world, fake, session_factory):
    """Another request stored a customer while ours was being created at the provider: the stored one wins."""
    original = fake.create_customer

    async def racing(organisation_id, name, email):
        customer_id = await original(organisation_id, name, email)
        await _set(session_factory, world.org.id, provider="fake", provider_customer_id="cus_other")
        return customer_id

    fake.create_customer = racing
    await service.checkout(world.org.id, world.pro.id, world.user)
    assert fake.checkouts[0]["customer_id"] == "cus_other"
    assert (await _account(session_factory, world.org.id)).provider_customer_id == "cus_other"


async def test_checkout_refused_when_subscribed(service, world):
    await _subscribe(service, world)
    with pytest.raises(AlreadySubscribedError):
        await service.checkout(world.org.id, world.pro.id, world.user)


async def test_change_plan_applies_snapshot(service, world, session_factory):
    with pytest.raises(NotSubscribedError):
        await service.change_plan(world.org.id, world.team.id, world.user)
    await _subscribe(service, world)
    await service.change_plan(world.org.id, world.team.id, world.user)
    account = await _account(session_factory, world.org.id)
    assert account.plan_id == world.team.id and account.subscription_price_ids == ["p_team_base"]
    assert account.status == "active" and account.provider_subscription_id == "sub_1"
    async with session_factory() as s:
        changes = [
            a.metadata_ for a in (await s.execute(select(AuditLog).order_by(AuditLog.created_at))).scalars()
        ]
    assert changes[-1]["change"] == "plan_changed"
    assert changes[-1]["changes"]["plan_id"] == {"from": str(world.pro.id), "to": str(world.team.id)}


async def test_overage_switch_replaces_items(service, world, fake, session_factory):
    await _subscribe(service, world)
    calls = []
    original = fake.replace_items

    async def recording(subscription_id, line_prices):
        calls.append((subscription_id, list(line_prices)))
        return await original(subscription_id, line_prices)

    fake.replace_items = recording
    await service.set_limit_behaviour(world.org.id, "overage", world.user)
    assert calls == [("sub_1", ["p_pro_base", "p_pro_msg"])]
    account = await _account(session_factory, world.org.id)
    assert account.limit_behaviour == "overage" and account.subscription_price_ids == ["p_pro_base", "p_pro_msg"]

    await service.set_limit_behaviour(world.org.id, "hard_stop", world.user)
    assert calls[-1] == ("sub_1", ["p_pro_base"])
    assert (await _account(session_factory, world.org.id)).limit_behaviour == "hard_stop"


async def test_overage_refused_when_provider_not_metered(service, world, fake, session_factory):
    fake.supports_metered = False
    with pytest.raises(OverageUnsupportedError):
        await service.set_limit_behaviour(world.org.id, "overage", world.user)
    await service.summary(world.org.id)
    assert (await _account(session_factory, world.org.id)).limit_behaviour == "hard_stop"


async def test_portal_needs_customer(service, world):
    with pytest.raises(NotSubscribedError):
        await service.portal(world.org.id, world.user)
    await service.checkout(world.org.id, world.pro.id, world.user)
    assert await service.portal(world.org.id, world.user) == f"{WEB}/orgs/{world.org.id}/settings/billing?fake_portal=1"


async def test_apply_active_clears_trial_and_resolves_plan(service, world, session_factory):
    await service.start_trial(world.org.id, world.pro.id, "hard_stop", world.user)
    assert await service.apply_subscription(_snapshot(world.org.id, price_ids=["p_team_base"]))
    account = await _account(session_factory, world.org.id)
    assert account.status == "active" and account.plan_id == world.team.id and account.trial_ends_at is None
    assert account.trial_started_at is not None  # the trial stays used


async def test_apply_unresolved_plan_keeps_current(service, world, session_factory):
    await _subscribe(service, world)
    assert await service.apply_subscription(_snapshot(world.org.id, price_ids=["p_unknown"], status="past_due"))
    account = await _account(session_factory, world.org.id)
    assert account.plan_id == world.pro.id and account.status == "past_due"


async def test_webhook_duplicate_event_ignored(service, world, session_factory):
    await service.summary(world.org.id)
    body, headers = _webhook("evt_1", "customer.subscription.updated", subscription=_snapshot(world.org.id))
    assert await service.handle_webhook("fake", body, headers) is True
    await _set(session_factory, world.org.id, status="canceled")  # a redelivery must not re-apply
    assert await service.handle_webhook("fake", body, headers) is False
    assert (await _account(session_factory, world.org.id)).status == "canceled"
    async with session_factory() as s:
        events = (await s.execute(select(BillingWebhookEvent))).scalars().all()
    assert len(events) == 1 and events[0].processed_at is not None


async def test_webhook_older_snapshot_ignored(service, world, session_factory):
    """Review Focus 1: T2 applied, then T1 < T2 arrives: the status stays at the T2 value."""
    await service.summary(world.org.id)
    t2 = datetime.now(UTC)
    t1 = t2 - timedelta(minutes=5)
    body, headers = _webhook("evt_2", "customer.subscription.updated", subscription=_snapshot(world.org.id, status="past_due", event_at=t2))
    await service.handle_webhook("fake", body, headers)
    body, headers = _webhook("evt_1", "customer.subscription.updated", subscription=_snapshot(world.org.id, status="active", event_at=t1))
    await service.handle_webhook("fake", body, headers)
    account = await _account(session_factory, world.org.id)
    assert account.status == "past_due" and account.last_event_at == t2
    assert await service.apply_subscription(_snapshot(world.org.id, status="active", event_at=t1)) is False


async def test_webhook_resolves_org_by_customer_when_metadata_missing(service, world, session_factory):
    await service.checkout(world.org.id, world.pro.id, world.user)  # stores fake_cus_1
    snap = _snapshot(None, sub="sub_9", customer="fake_cus_1")
    body, headers = _webhook("evt_3", "customer.subscription.created", subscription=snap)
    assert await service.handle_webhook("fake", body, headers) is True
    account = await _account(session_factory, world.org.id)
    assert account.status == "active" and account.provider_subscription_id == "sub_9"
    assert account.plan_id == world.pro.id

    # Unknown customer and no metadata: recorded and processed, nothing applied.
    body, headers = _webhook("evt_4", "customer.subscription.updated", subscription=_snapshot(None, sub="x", customer="nobody"))
    assert await service.handle_webhook("fake", body, headers) is True


async def test_replace_items_snapshot_found_by_subscription_id(service, world, session_factory):
    """FakeBilling.replace_items returns no customer and no metadata: the subscription id finds the account."""
    await _subscribe(service, world)
    snap = SubscriptionSnapshot("sub_1", "", None, "active", ["p_team_base"], datetime.now(UTC), datetime.now(UTC), False, datetime.now(UTC))
    assert await service.apply_subscription(snap)
    assert (await _account(session_factory, world.org.id)).plan_id == world.team.id


async def test_payment_failed_records_alert(service, world, session_factory):
    await service.checkout(world.org.id, world.pro.id, world.user)
    await _subscribe(service, world, customer="fake_cus_1")
    for event_id in ("evt_pf_1", "evt_pf_2"):  # one alert per period
        body, headers = _webhook(event_id, "invoice.payment_failed", customer_id="fake_cus_1")
        assert await service.handle_webhook("fake", body, headers) is True
    account = await _account(session_factory, world.org.id)
    async with session_factory() as s:
        alerts = (await s.execute(select(UsageAlert))).scalars().all()
    assert [(a.organisation_id, a.kind, a.period_start) for a in alerts] == [
        (world.org.id, "payment_failed", account.current_period_start)
    ]


async def test_summary_and_banner(service, world, session_factory):
    summary = await service.summary(world.org.id)
    assert summary.account.status == "no_plan" and summary.plan is None and summary.effective_status == "no_plan"
    assert summary.supports_metered and not summary.grants_everything and not summary.subscribed
    assert [m.meter for m in summary.meters] == ["messages", "voice_minutes"]
    assert summary.capacity["seats"] == (0, 0)

    await service.start_trial(world.org.id, world.pro.id, "hard_stop", world.user)
    summary = await service.summary(world.org.id)
    assert summary.plan.id == world.pro.id and summary.effective_status == "trialing"
    assert summary.meters[0].limit == 100 and summary.period.end == summary.account.trial_ends_at

    banner = await service.banner(world.org.id)
    assert banner.effective_status == "trialing" and not banner.locked and banner.reached == [] and not banner.past_due
    assert banner.trial_ends_at == summary.account.trial_ends_at

    await _set(session_factory, world.org.id, trial_ends_at=datetime.now(UTC) - timedelta(seconds=1))
    banner = await service.banner(world.org.id)
    assert banner.effective_status == "trial_expired" and banner.locked


async def test_local_snapshot_does_not_advance_ordering_guard(service, world, fake, session_factory):
    """A change_plan snapshot is stamped by our clock: it must not make a provider event from the same second (or
    from a provider clock behind ours) look stale."""
    t0 = datetime.now(UTC) - timedelta(minutes=5)
    await _subscribe(service, world, event_at=t0)
    local_now = datetime.now(UTC) + timedelta(seconds=30)  # our clock runs ahead of the provider's
    original = fake.replace_items

    async def stamped(subscription_id, line_prices):
        snap = await original(subscription_id, line_prices)
        return SubscriptionSnapshot(**{**vars(snap), "event_at": local_now})

    fake.replace_items = stamped
    await service.change_plan(world.org.id, world.team.id, world.user)
    account = await _account(session_factory, world.org.id)
    assert account.plan_id == world.team.id and account.last_event_at == t0  # not advanced by the local snapshot

    webhook_at = local_now - timedelta(seconds=1)  # earlier than our local "now", later than the last provider event
    snap = _snapshot(world.org.id, status="past_due", price_ids=["p_team_base"], event_at=webhook_at)
    body, headers = _webhook("evt_after_local", "customer.subscription.updated", subscription=snap)
    assert await service.handle_webhook("fake", body, headers) is True
    account = await _account(session_factory, world.org.id)
    assert account.status == "past_due" and account.last_event_at == webhook_at


# --- final fix wave ----------------------------------------------------------------------------------------------


def _record_replace_items(fake):
    calls = []
    original = fake.replace_items

    async def recording(subscription_id, line_prices):
        calls.append((subscription_id, list(line_prices)))
        return await original(subscription_id, line_prices)

    fake.replace_items = recording
    return calls


async def test_webhook_applies_the_providers_current_state_not_the_payload(service, world, fake, session_factory):
    """I1: checkout's `created` (incomplete) and `updated` (active) share a second and arrive reversed. Each event
    applies the subscription as the provider has it now, so the paid account ends active, not incomplete."""
    await service.summary(world.org.id)
    t = datetime.now(UTC).replace(microsecond=0)
    fake.subscriptions["sub_1"] = _snapshot(world.org.id, status="active", event_at=t)
    for event_id, type_, status in (
        ("evt_updated", "customer.subscription.updated", "active"),
        ("evt_created", "customer.subscription.created", "incomplete"),
    ):
        body, headers = _webhook(event_id, type_, subscription=_snapshot(world.org.id, status=status, event_at=t))
        assert await service.handle_webhook("fake", body, headers) is True
    account = await _account(session_factory, world.org.id)
    assert account.status == "active" and account.last_event_at == t


async def test_webhook_same_second_snapshot_is_applied(service, world, fake, session_factory):
    """I1: a fetched snapshot whose event time equals last_event_at is current truth and is applied (>=)."""
    await service.summary(world.org.id)
    t = datetime.now(UTC).replace(microsecond=0)
    body, headers = _webhook("evt_a", "customer.subscription.updated", subscription=_snapshot(world.org.id, event_at=t))
    await service.handle_webhook("fake", body, headers)
    fake.subscriptions["sub_1"] = _snapshot(world.org.id, status="past_due", event_at=t)
    body, headers = _webhook("evt_b", "customer.subscription.updated", subscription=_snapshot(world.org.id, event_at=t))
    assert await service.handle_webhook("fake", body, headers) is True
    assert (await _account(session_factory, world.org.id)).status == "past_due"


async def test_webhook_retrieval_failure_is_retried_by_the_provider(service, world, fake, session_factory):
    """I1: the fetch fails: the webhook raises (non-2xx) and its event id is not recorded, so the redelivery is
    processed."""
    await service.summary(world.org.id)
    body, headers = _webhook("evt_1", "customer.subscription.updated", subscription=_snapshot(world.org.id))
    fake.fail_next = BillingProviderError
    with pytest.raises(BillingProviderError):
        await service.handle_webhook("fake", body, headers)
    async with session_factory() as s:
        assert (await s.execute(select(BillingWebhookEvent))).scalars().all() == []
    assert (await _account(session_factory, world.org.id)).status == "no_plan"
    assert await service.handle_webhook("fake", body, headers) is True
    assert (await _account(session_factory, world.org.id)).status == "active"


async def test_overage_switch_keeps_grandfathered_base_price(service, world, fake, session_factory):
    """I3: the subscriber's old base price stays when overage is turned on or off, and the plan need not be synced
    (it is `pending` after an edit) while it has active metered prices."""
    async with session_factory() as s:
        s.add(
            PlanProviderPrice(
                plan_id=world.pro.id, provider="fake", price_id="p_pro_base_old", kind="base", amount_minor=3900,
                active=False,
            )
        )
        plan = await s.get(Plan, world.pro.id)
        plan.sync_status = "pending"
        await s.commit()
    await _subscribe(service, world, price_ids=("p_pro_base_old",))
    calls = _record_replace_items(fake)

    await service.set_limit_behaviour(world.org.id, "overage", world.user)
    assert calls == [("sub_1", ["p_pro_base_old", "p_pro_msg"])]
    await service.set_limit_behaviour(world.org.id, "hard_stop", world.user)
    assert calls[-1] == ("sub_1", ["p_pro_base_old"])
    account = await _account(session_factory, world.org.id)
    assert account.limit_behaviour == "hard_stop" and account.subscription_price_ids == ["p_pro_base_old"]


async def test_overage_on_refused_when_plan_has_no_metered_prices(service, world, fake, session_factory):
    await _subscribe(service, world, price_ids=("p_team_base",))
    calls = _record_replace_items(fake)
    with pytest.raises(PlanNotAvailableError):
        await service.set_limit_behaviour(world.org.id, "overage", world.user)
    assert calls == []
    assert (await _account(session_factory, world.org.id)).limit_behaviour == "hard_stop"


async def test_checkout_allowed_after_incomplete_subscription(service, world, fake, session_factory):
    """M9: a subscription that never completed is not live: the organisation can check out again."""
    await _subscribe(service, world, status="incomplete")
    assert not (await service.summary(world.org.id)).subscribed
    await service.checkout(world.org.id, world.pro.id, world.user)
    assert len(fake.checkouts) == 1


async def test_abandoned_subscription_expiring_does_not_cancel_the_new_one(service, world, session_factory):
    """M9 follow-on: after checking out again, the abandoned subscription's expiry must not overwrite the paid one."""
    t = datetime.now(UTC)
    await _subscribe(service, world, status="incomplete", sub="sub_old", event_at=t - timedelta(minutes=10))
    assert await service.apply_subscription(_snapshot(world.org.id, sub="sub_new", event_at=t - timedelta(minutes=5)))
    assert not await service.apply_subscription(_snapshot(world.org.id, status="canceled", sub="sub_old", event_at=t))
    account = await _account(session_factory, world.org.id)
    assert account.status == "active" and account.provider_subscription_id == "sub_new"
