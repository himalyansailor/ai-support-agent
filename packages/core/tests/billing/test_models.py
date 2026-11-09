import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from supportcore.db.models import BillingAccount, Call, Conversation, Customer, Plan, UsageEvent


async def test_usage_event_source_unique(session, org_a):
    source = uuid.uuid4()
    session.add(UsageEvent(organisation_id=org_a.id, meter="messages", quantity=1, source_id=source))
    await session.flush()
    session.add(UsageEvent(organisation_id=org_a.id, meter="messages", quantity=1, source_id=source))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_usage_event_same_source_other_meter_allowed(session, org_a):
    source = uuid.uuid4()
    session.add(UsageEvent(organisation_id=org_a.id, meter="messages", quantity=1, source_id=source))
    session.add(UsageEvent(organisation_id=org_a.id, meter="voice_minutes", quantity=2, source_id=source))
    await session.flush()


async def test_billing_account_one_per_org(session, org_a):
    session.add(BillingAccount(organisation_id=org_a.id))
    await session.flush()
    session.add(BillingAccount(organisation_id=org_a.id))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_billing_account_defaults(session, org_a):
    account = BillingAccount(organisation_id=org_a.id)
    session.add(account)
    await session.flush()
    await session.refresh(account)
    assert account.status == "no_plan"
    assert account.limit_behaviour == "hard_stop"
    assert account.subscription_price_ids == []
    assert account.cancel_at_period_end is False


async def test_plan_limits_nullable_mean_unlimited(session):
    plan = Plan(key="unlimited", name="Unlimited", monthly_price_minor=0)
    session.add(plan)
    await session.flush()
    await session.refresh(plan)
    for field in ("seats", "messages", "voice_minutes", "storage_mb", "knowledge_bases", "phone_lines"):
        assert getattr(plan, field) is None
    assert plan.messages_overage_micros is None
    assert plan.sync_status == "pending"
    assert plan.active is True


async def test_plan_key_unique(session):
    session.add(Plan(key="dup", name="A", monthly_price_minor=0))
    await session.flush()
    session.add(Plan(key="dup", name="B", monthly_price_minor=0))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_call_end_reason_plan_limit_allowed(session, org_a):
    customer = Customer(organisation_id=org_a.id, phone="+15551230000")
    session.add(customer)
    await session.flush()
    conv = Conversation(organisation_id=org_a.id, customer_id=customer.id, channel="voice")
    session.add(conv)
    await session.flush()
    session.add(
        Call(
            organisation_id=org_a.id,
            conversation_id=conv.id,
            source="phone",
            direction="inbound",
            provider_call_sid="CA" + "1" * 32,
            end_reason="plan_limit",
        )
    )
    await session.flush()
