import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from supportcore.billing.accounts import load_account
from supportcore.billing.dev_billing import DevBilling
from supportcore.billing.entitlements import EntitlementService, MeterUsage
from supportcore.billing.errors import PlanLimitError
from supportcore.billing.fake_billing import FakeBilling
from supportcore.billing.memory_counter import MemoryUsageCounter
from supportcore.billing.usage import record_usage
from supportcore.db.models import (
    Document,
    KnowledgeBase,
    Membership,
    PlanProviderPrice,
    Role,
    User,
)
from supportcore.testing import start_trial_plan


def _adapters(provider=None):
    return SimpleNamespace(billing=provider or FakeBilling(), usage_counter=MemoryUsageCounter())


async def _use(session, org_id, meter, quantity, adapters):
    await record_usage(session, org_id, meter, quantity, uuid.uuid4(), counter=adapters.usage_counter)
    await session.flush()


async def _subscribe(session, org_id, *, behaviour, **limits):
    """The account on an active subscription of a new plan (the trial fields are what start_trial_plan set). With
    overage, the subscription has the plan's metered prices (created with the plan's limits and overage prices)."""
    plan = await start_trial_plan(session, org_id, **limits)
    account = await load_account(session, org_id)
    now = datetime.now(UTC)
    account.status, account.limit_behaviour = "active", behaviour
    price_ids = [f"base_{plan.id.hex}"]
    session.add(PlanProviderPrice(plan_id=plan.id, provider="fake", price_id=price_ids[0], kind="base"))
    for meter in ("messages", "voice_minutes"):
        micros = limits.get(f"{meter}_overage_micros")
        if behaviour == "overage" and micros is not None:
            price_ids.append(f"{meter}_{plan.id.hex}")
            session.add(
                PlanProviderPrice(
                    plan_id=plan.id, provider="fake", price_id=price_ids[-1], kind=meter,
                    included_units=limits.get(meter), overage_micros=micros,
                )
            )
    account.subscription_price_ids = price_ids
    account.current_period_start, account.current_period_end = now - timedelta(days=1), now + timedelta(days=29)
    await session.flush()
    return plan


def test_meter_usage_properties():
    assert MeterUsage("messages", 2, 2, False).reached and not MeterUsage("messages", 2, 2, False).exceeded
    assert MeterUsage("messages", 3, 2, False).exceeded
    assert not MeterUsage("messages", 3, 2, True).reached  # overage: never reached
    assert MeterUsage("messages", 3, None, False).percent is None
    assert MeterUsage("messages", 1, 4, False).percent == 25
    assert MeterUsage("messages", 0, 0, False).percent == 100


async def test_dev_provider_grants_everything(session, org_a):
    adapters = _adapters(DevBilling())
    await start_trial_plan(session, org_a.id, messages=0, seats=0, knowledge_bases=0)
    service = EntitlementService(session, org_a.id, adapters)
    state = await service.state()
    assert state.effective_status == "dev" and not state.locked
    usage = await service.meter("messages")
    assert usage.limit is None and not usage.reached
    assert await service.capacity("seats") == (0, None)
    await service.check_capacity("knowledge_bases", adding=100)


async def test_trial_uses_plan_limits(session, org_a):
    adapters = _adapters()
    await start_trial_plan(session, org_a.id, messages=5, seats=3, storage_mb=None)
    await _use(session, org_a.id, "messages", 3, adapters)
    service = EntitlementService(session, org_a.id, adapters)
    state = await service.state()
    assert (state.account_status, state.effective_status, state.locked, state.overage_active) == (
        "trialing", "trialing", False, False,
    )
    usage = await service.meter("messages")
    assert (usage.used, usage.limit, usage.reached, usage.percent) == (3, 5, False, 60)
    assert await service.capacity("seats") == (0, 3)
    assert await service.capacity("storage_mb") == (0, None)  # None: unlimited


async def test_trial_expired_locks_to_zero(session, org_a):
    adapters = _adapters()
    await start_trial_plan(
        session, org_a.id, trial_days=14, started_at=datetime.now(UTC) - timedelta(days=15), messages=100,
        knowledge_bases=10,
    )
    service = EntitlementService(session, org_a.id, adapters)
    state = await service.state()
    assert state.effective_status == "trial_expired" and state.locked
    usage = await service.meter("messages")
    assert usage.limit == 0 and usage.reached
    assert await service.capacity("knowledge_bases") == (0, 0)
    with pytest.raises(PlanLimitError) as raised:
        await service.check_capacity("knowledge_bases")
    assert (raised.value.resource, raised.value.limit) == ("knowledge_bases", 0)


async def test_no_plan_is_locked(session, org_a):
    service = EntitlementService(session, org_a.id, _adapters())
    state = await service.state()
    assert state.effective_status == "no_plan" and state.locked
    assert (await service.meter("voice_minutes")).reached


async def test_overage_only_for_paid_metered(session, org_a, org_b):
    adapters = _adapters()
    await _subscribe(session, org_a.id, behaviour="overage", messages=2, messages_overage_micros=1000, voice_minutes=5)
    await _use(session, org_a.id, "messages", 5, adapters)
    service = EntitlementService(session, org_a.id, adapters)
    assert (await service.state()).overage_active
    messages = await service.meter("messages")
    assert messages.overage and not messages.reached and not messages.exceeded
    voice = await service.meter("voice_minutes")  # no overage price for this meter: a hard stop
    assert not voice.overage

    # A trial is always a hard stop, whatever the behaviour.
    await start_trial_plan(session, org_b.id, messages=2, messages_overage_micros=1000)
    account = await load_account(session, org_b.id)
    account.limit_behaviour = "overage"
    await session.flush()
    await _use(session, org_b.id, "messages", 3, adapters)
    trial = EntitlementService(session, org_b.id, adapters)
    assert not (await trial.state()).overage_active
    assert (await trial.meter("messages")).exceeded


async def test_overage_hidden_when_provider_not_metered(session, org_a):
    adapters = _adapters(FakeBilling(supports_metered=False))
    await _subscribe(session, org_a.id, behaviour="overage", messages=2, messages_overage_micros=1000)
    await _use(session, org_a.id, "messages", 2, adapters)
    service = EntitlementService(session, org_a.id, adapters)
    assert not (await service.state()).overage_active
    usage = await service.meter("messages")
    assert not usage.overage and usage.reached


async def test_locked_seats_capped_at_current(session, org_a):
    role = Role(organisation_id=org_a.id, name="Agent", permissions=[])
    session.add(role)
    await session.flush()
    for i in range(2):
        user = User(email=f"m{i}@x.com", password_hash="x", name=f"M{i}")
        session.add(user)
        await session.flush()
        session.add(Membership(organisation_id=org_a.id, user_id=user.id, role_id=role.id, status="active"))
    await session.flush()
    service = EntitlementService(session, org_a.id, _adapters())  # no_plan: locked
    assert await service.capacity("seats") == (2, 2)  # nobody is removed
    with pytest.raises(PlanLimitError):
        await service.check_capacity("seats")


async def test_capacity_storage_counts_new_upload(session, org_a):
    await start_trial_plan(session, org_a.id, storage_mb=2)
    kb = KnowledgeBase(
        organisation_id=org_a.id, name="KB", embedding_provider="fake", embedding_model="m", embedding_dimensions=8,
        chunking={},
    )
    session.add(kb)
    await session.flush()
    session.add(
        Document(
            organisation_id=org_a.id, knowledge_base_id=kb.id, filename="a.txt", file_type="text",
            size_bytes=1_500_000, sha256="a" * 64, storage_key="k/a",
        )
    )
    await session.flush()
    service = EntitlementService(session, org_a.id, _adapters())
    assert await service.capacity("storage_mb") == (2, 2)  # 1.5 MB, rounded up
    with pytest.raises(PlanLimitError) as raised:
        await service.check_capacity("storage_mb", adding=1)
    assert raised.value.resource == "storage_mb"
    await service.check_capacity("storage_mb", adding=0)


async def test_check_storage_compares_raw_bytes(session, org_a):
    """M7: 1.5 MB stored on a 2 MB plan leaves exactly 500,000 bytes (rounding each side up would refuse any upload)."""
    await start_trial_plan(session, org_a.id, storage_mb=2)
    kb = KnowledgeBase(
        organisation_id=org_a.id, name="KB", embedding_provider="fake", embedding_model="m", embedding_dimensions=8,
        chunking={},
    )
    session.add(kb)
    await session.flush()
    session.add(
        Document(
            organisation_id=org_a.id, knowledge_base_id=kb.id, filename="a.txt", file_type="text",
            size_bytes=1_500_000, sha256="a" * 64, storage_key="k/a",
        )
    )
    await session.flush()
    service = EntitlementService(session, org_a.id, _adapters())
    await service.check_storage(500_000)
    with pytest.raises(PlanLimitError) as raised:
        await service.check_storage(500_001)
    assert (raised.value.resource, raised.value.limit) == ("storage_mb", 2)


async def test_subscriber_limits_come_from_subscribed_metered_prices(session, org_a):
    """I2: the plan was edited after the organisation subscribed. Its messages limit and overage come from the metered
    price it is billed with (1000 free units), not the plan's new row (2000)."""
    adapters = _adapters()
    plan = await _subscribe(
        session, org_a.id, behaviour="overage", messages=1000, messages_overage_micros=5000, voice_minutes=50
    )
    plan.messages, plan.voice_minutes_overage_micros = 2000, 9000  # edited later: new prices for new subscribers
    await session.flush()
    await _use(session, org_a.id, "messages", 1500, adapters)
    service = EntitlementService(session, org_a.id, adapters)
    messages = await service.meter("messages")
    assert (messages.limit, messages.overage, messages.used) == (1000, True, 1500)
    voice = await service.meter("voice_minutes")  # no subscribed voice price: the plan row's limit, no overage
    assert (voice.limit, voice.overage) == (50, False)
    assert await service.overage_meters() == ("messages",)


async def test_overage_behaviour_without_metered_item_is_a_hard_stop(session, org_a):
    """I2: overage chosen, but the subscription has no metered item (the plan gained an overage price later): no
    unbilled, unlimited usage."""
    adapters = _adapters()
    plan = await _subscribe(session, org_a.id, behaviour="hard_stop", messages=2)
    plan.messages_overage_micros = 1000
    account = await load_account(session, org_a.id)
    account.limit_behaviour = "overage"
    await session.flush()
    await _use(session, org_a.id, "messages", 2, adapters)
    service = EntitlementService(session, org_a.id, adapters)
    usage = await service.meter("messages")
    assert (usage.limit, usage.overage, usage.reached) == (2, False, True)
    assert await service.overage_meters() == ()


async def test_trial_uses_plan_row_limits(session, org_a):
    await start_trial_plan(session, org_a.id, messages=7, messages_overage_micros=1000)
    usage = await EntitlementService(session, org_a.id, _adapters()).meter("messages")
    assert (usage.limit, usage.overage) == (7, False)
