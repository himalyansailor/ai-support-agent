import math
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy import update as sql_update
from supportcore.billing.accounts import load_account
from supportcore.billing.memory_counter import MemoryUsageCounter
from supportcore.billing.periods import current_period
from supportcore.billing.usage import counter_key, reconcile_counters, record_usage, used
from supportcore.db.models import BillingAccount, Customer, UsageEvent
from supportcore.inbox.publisher import InboxPublisher
from supportcore.realtime.spawn import drain_pending
from supportcore.voice.calls import CallService


class DownCounter:
    async def get(self, key):
        raise ConnectionError

    async def incr_if_present(self, key, amount):
        raise ConnectionError

    async def set_if_absent(self, key, value, ttl_seconds):
        raise ConnectionError

    async def set(self, key, value, ttl_seconds):
        raise ConnectionError


async def _period(session, org_id):
    return current_period(await load_account(session, org_id), datetime.now(UTC))


async def _rows(session, org_id, meter="messages") -> int:
    return (
        await session.execute(
            select(func.count()).select_from(UsageEvent).where(
                UsageEvent.organisation_id == org_id, UsageEvent.meter == meter
            )
        )
    ).scalar_one()


async def test_record_usage_idempotent_per_source(session, org_a):
    counter = MemoryUsageCounter()
    period = await _period(session, org_a.id)
    key = counter_key(org_a.id, "messages", period)
    await counter.set(key, 0, 600)
    source = uuid.uuid4()
    for _ in range(2):
        await record_usage(session, org_a.id, "messages", 1, source, counter=counter)
        await session.commit()
        await drain_pending()
    assert await _rows(session, org_a.id) == 1
    assert await counter.get(key) == 1


async def test_record_usage_ignores_non_positive_quantity(session, org_a):
    await record_usage(session, org_a.id, "messages", 0, uuid.uuid4(), counter=MemoryUsageCounter())
    assert await _rows(session, org_a.id) == 0


async def test_counter_not_created_by_incr(session, org_a):
    counter = MemoryUsageCounter()
    await record_usage(session, org_a.id, "messages", 1, uuid.uuid4(), counter=counter)
    await session.commit()
    await drain_pending()
    key = counter_key(org_a.id, "messages", await _period(session, org_a.id))
    assert await counter.get(key) is None


async def test_counter_not_bumped_on_rollback(session, org_a):
    counter = MemoryUsageCounter()
    key = counter_key(org_a.id, "messages", await _period(session, org_a.id))
    await counter.set(key, 5, 600)
    await record_usage(session, org_a.id, "messages", 1, uuid.uuid4(), counter=counter)
    await session.rollback()
    await drain_pending()
    assert await counter.get(key) == 5


async def test_used_rebuilds_from_ledger_when_counter_missing(session, org_a):
    counter = MemoryUsageCounter()
    for _ in range(3):
        await record_usage(session, org_a.id, "messages", 1, uuid.uuid4(), counter=counter)
    await record_usage(session, org_a.id, "voice_minutes", 4, uuid.uuid4(), counter=counter)
    await session.commit()
    await drain_pending()
    period = await _period(session, org_a.id)
    key = counter_key(org_a.id, "messages", period)
    assert await counter.get(key) is None  # flushed / never seeded
    assert await used(session, org_a.id, "messages", period, counter) == 3
    assert await counter.get(key) == 3  # seeded for the next check
    assert await used(session, org_a.id, "voice_minutes", period, counter) == 4


async def test_used_ignores_events_outside_the_period(session, org_a):
    counter = MemoryUsageCounter()
    period = await _period(session, org_a.id)
    session.add(
        UsageEvent(
            organisation_id=org_a.id, meter="messages", quantity=7, source_id=uuid.uuid4(),
            occurred_at=period.start - timedelta(seconds=1),
        )
    )
    await record_usage(session, org_a.id, "messages", 1, uuid.uuid4(), counter=counter)
    await session.flush()
    assert await used(session, org_a.id, "messages", period, counter) == 1


async def test_used_falls_back_to_ledger_when_redis_down(session, org_a):
    await record_usage(session, org_a.id, "messages", 2, uuid.uuid4(), counter=DownCounter())
    await session.commit()
    await drain_pending()  # the failed bump is logged, never raised
    assert await used(session, org_a.id, "messages", await _period(session, org_a.id), DownCounter()) == 2


async def test_reconcile_sets_exact_value(session_factory):
    from types import SimpleNamespace

    from supportcore.db.models import Organisation
    from supportcore.db.uow import UnitOfWork

    counter = MemoryUsageCounter()
    async with UnitOfWork(session_factory) as uow:
        org = Organisation(name="Org", slug="org-reconcile")
        uow.session.add(org)
        await uow.session.flush()
        for _ in range(3):
            await record_usage(uow.session, org.id, "messages", 1, uuid.uuid4(), counter=counter)
        period = await _period(uow.session, org.id)
        await uow.commit()
    key = counter_key(org.id, "messages", period)
    await counter.set(key, 99, 600)  # drifted
    rt = SimpleNamespace(session_factory=session_factory, adapters=SimpleNamespace(usage_counter=counter))
    assert await reconcile_counters(rt, datetime.now(UTC)) == 1
    assert await counter.get(key) == 3


@pytest.fixture
async def conversation_world(session, org_a):
    from supportcore.db.models import Widget

    widget = Widget(organisation_id=org_a.id, name="W", public_key="pk-" + uuid.uuid4().hex)
    session.add(widget)
    customer = Customer(organisation_id=org_a.id, visitor_id="v1")
    session.add(customer)
    await session.flush()
    return widget, customer


async def test_customer_chat_message_metered_test_conversation_not(session, org_a, conversation_world):
    from supportcore.conversations.service import ConversationService
    from supportcore.events import EventBus

    widget, customer = conversation_world
    service = ConversationService(session, org_a.id, EventBus(), usage_counter=MemoryUsageCounter())
    live = await service.create(customer.id, widget.id, "en", False)
    test = await service.create(customer.id, widget.id, "en", True)
    message, _ = await service.add_customer_message(live, "hello", "c-1")
    await service.add_customer_message(live, "hello again", "c-1")  # a resend: not metered twice
    await service.add_customer_message(test, "hello", None)
    await service.add_message(live, "bot", "hi")  # only customer messages count
    rows = (await session.execute(select(UsageEvent))).scalars().all()
    assert [(r.meter, r.quantity, r.source_id) for r in rows] == [("messages", 1, message.id)]


async def test_voice_conversation_message_not_metered(session, org_a, conversation_world):
    from supportcore.conversations.service import ConversationService
    from supportcore.events import EventBus

    widget, customer = conversation_world
    service = ConversationService(session, org_a.id, EventBus(), usage_counter=MemoryUsageCounter())
    conv = await service.create(customer.id, widget.id, "en", False)
    conv.channel = "voice"
    await service.add_customer_message(conv, "hello", None)
    assert await _rows(session, org_a.id) == 0


@pytest.mark.parametrize(("seconds", "minutes"), [(61, 2), (60, 1), (1, 1), (0, None)])
async def test_call_finalise_meters_rounded_minutes(session, org_a, conversation_world, seconds, minutes):
    from types import SimpleNamespace

    from supportcore.events import EventBus
    from supportcore.voice.tokens import CallTicket

    widget, customer = conversation_world
    adapters = SimpleNamespace(usage_counter=MemoryUsageCounter())
    service = CallService(
        session, org_a.id, EventBus(), InboxPublisher(SimpleNamespace(publish=None), None), adapters=adapters
    )
    call = await service.start_browser_call(CallTicket(org_a.id, widget.id, customer.id, "jti"), "CA" + "1" * 32)
    assert await service.finalise(call.id, "completed", seconds)
    assert await service.finalise(call.id, "completed", seconds + 600) is False
    rows = (await session.execute(select(UsageEvent))).scalars().all()
    if minutes is None:
        assert rows == []
    else:
        assert [(r.meter, r.quantity, r.source_id) for r in rows] == [("voice_minutes", minutes, call.id)]
    assert minutes is None or minutes == math.ceil(seconds / 60)


async def test_organisation_create_adds_billing_account(session):
    from supportcore.db.models import User
    from supportcore.events import EventBus
    from supportcore.orgs.service import OrganisationService

    owner = User(email="o@x.com", password_hash="x", name="O")
    session.add(owner)
    await session.flush()
    org = await OrganisationService(session, EventBus()).create(owner, "Acme")
    account = (await session.execute(select(BillingAccount).where(BillingAccount.organisation_id == org.id))).scalars().all()
    assert [a.status for a in account] == ["no_plan"]
