"""report_usage: ledger rows become usage reports, then meter events (Phase 8, Task 8).

Real commits (the job opens its own units of work), so these tests use `session_factory`."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from supportcore.billing.fake_billing import FakeBilling
from supportcore.billing.memory_counter import MemoryUsageCounter
from supportcore.billing.reporting import report_usage
from supportcore.db.models import (
    BillingAccount,
    Organisation,
    Plan,
    PlanProviderPrice,
    UsageEvent,
    UsageReport,
)

NOW = datetime.now(UTC)


class Crash(BaseException):
    """A process dying mid-job: not an Exception, so nothing catches it."""


@pytest.fixture
def fake():
    return FakeBilling()


@pytest.fixture
def rt(session_factory, fake):
    return SimpleNamespace(
        session_factory=session_factory, adapters=SimpleNamespace(billing=fake, usage_counter=MemoryUsageCounter())
    )


@pytest.fixture
async def org(session_factory):
    """An organisation subscribed (active, period started a day ago) to a plan with a messages overage price."""
    async with session_factory() as s:
        org = Organisation(name="Acme", slug=f"acme-{uuid.uuid4().hex[:6]}")
        plan = Plan(key="pro", name="Pro", monthly_price_minor=4900, messages=100, messages_overage_micros=5000)
        s.add_all([org, plan])
        await s.flush()
        s.add_all(
            [
                PlanProviderPrice(plan_id=plan.id, provider="fake", price_id="p_base", kind="base", amount_minor=4900),
                PlanProviderPrice(
                    plan_id=plan.id, provider="fake", price_id="p_msg", kind="messages",
                    included_units=100, overage_micros=5000,
                ),
            ]
        )
        s.add(
            BillingAccount(
                organisation_id=org.id,
                plan_id=plan.id,
                status="active",
                limit_behaviour="overage",
                provider="fake",
                provider_customer_id="cus_1",
                subscription_price_ids=["p_base", "p_msg"],
                current_period_start=NOW - timedelta(days=1),
                current_period_end=NOW + timedelta(days=29),
            )
        )
        await s.commit()
        return org


async def _add_rows(session_factory, org_id, *quantities, meter="messages", at=None):
    async with session_factory() as s:
        for i, quantity in enumerate(quantities):
            s.add(
                UsageEvent(
                    organisation_id=org_id,
                    meter=meter,
                    quantity=quantity,
                    source_id=uuid.uuid4(),
                    occurred_at=(at or NOW - timedelta(minutes=30)) + timedelta(seconds=i),
                )
            )
        await s.commit()


async def _set_account(session_factory, org_id, **values):
    async with session_factory() as s:
        account = (await s.execute(select(BillingAccount).where(BillingAccount.organisation_id == org_id))).scalar_one()
        for name, value in values.items():
            setattr(account, name, value)
        await s.commit()


async def _rows(session_factory, org_id):
    async with session_factory() as s:
        return (
            (await s.execute(select(UsageEvent).where(UsageEvent.organisation_id == org_id).order_by(UsageEvent.occurred_at)))
            .scalars()
            .all()
        )


async def _reports(session_factory):
    async with session_factory() as s:
        return (await s.execute(select(UsageReport).order_by(UsageReport.created_at))).scalars().all()


async def test_report_usage_sends_once_per_row(rt, fake, org, session_factory):
    await _add_rows(session_factory, org.id, 1, 2, 3)
    assert await report_usage(rt, NOW) == 1
    [report] = await _reports(session_factory)
    assert report.status == "sent" and report.attempts == 1 and report.sent_at is not None
    assert report.quantity == 6
    rows = await _rows(session_factory, org.id)
    assert {r.report_id for r in rows} == {report.id}
    assert report.timestamp == rows[-1].occurred_at  # the latest occurred_at
    assert fake.meter_events == [
        {
            "customer_id": "cus_1",
            "meter": "messages",
            "quantity": 6,
            "identifier": str(report.id),
            "timestamp": report.timestamp,
        }
    ]

    assert await report_usage(rt, NOW) == 0  # nothing new: nothing sent
    assert len(fake.meter_events) == 1

    await _add_rows(session_factory, org.id, 4)
    assert await report_usage(rt, NOW) == 1
    assert [e["quantity"] for e in fake.meter_events] == [6, 4]
    assert len(await _reports(session_factory)) == 2


async def test_report_retry_reuses_identifier(rt, fake, org, session_factory, monkeypatch):
    identifiers: list[str] = []
    original = fake.report_usage

    async def recording(customer_id, meter, quantity, identifier, timestamp):
        identifiers.append(identifier)
        await original(customer_id, meter, quantity, identifier, timestamp)

    monkeypatch.setattr(fake, "report_usage", recording)
    await _add_rows(session_factory, org.id, 2, 3)
    fake.fail_next = TimeoutError
    assert await report_usage(rt, NOW) == 0
    [report] = await _reports(session_factory)
    assert report.status == "failed" and report.attempts == 1 and report.sent_at is None

    assert await report_usage(rt, NOW) == 1
    [report] = await _reports(session_factory)
    assert report.status == "sent" and report.attempts == 2
    assert identifiers == [str(report.id), str(report.id)]
    assert [e["quantity"] for e in fake.meter_events] == [5]
    assert {r.report_id for r in await _rows(session_factory, org.id)} == {report.id}


async def test_report_gives_up_after_ten_attempts(rt, fake, org, session_factory):
    await _add_rows(session_factory, org.id, 1)
    for _ in range(10):
        fake.fail_next = ConnectionError
        assert await report_usage(rt, NOW) == 0
    [report] = await _reports(session_factory)
    assert report.status == "failed" and report.attempts == 10
    assert await report_usage(rt, NOW) == 0  # no 11th attempt
    assert fake.meter_events == []
    [report] = await _reports(session_factory)
    assert report.attempts == 10


async def test_crash_after_report_created_resends_same_report(rt, fake, org, session_factory, monkeypatch):
    await _add_rows(session_factory, org.id, 1, 1)

    async def crash(*args, **kwargs):
        raise Crash

    monkeypatch.setattr(fake, "report_usage", crash)
    with pytest.raises(Crash):
        await report_usage(rt, NOW)
    [report] = await _reports(session_factory)
    assert report.status == "pending" and report.attempts == 0  # created and committed, never marked
    monkeypatch.undo()

    assert await report_usage(rt, NOW) == 1
    [again] = await _reports(session_factory)
    assert again.id == report.id and again.status == "sent"
    assert [(e["identifier"], e["quantity"]) for e in fake.meter_events] == [(str(report.id), 2)]
    assert {r.report_id for r in await _rows(session_factory, org.id)} == {report.id}


async def test_hard_stop_usage_never_sent_and_skipped_after_period(rt, fake, org, session_factory):
    await _set_account(session_factory, org.id, limit_behaviour="hard_stop")
    await _add_rows(session_factory, org.id, 5, at=NOW - timedelta(days=20))  # a closed period
    await _add_rows(session_factory, org.id, 7)  # this period
    assert await report_usage(rt, NOW) == 0
    assert fake.meter_events == [] and await _reports(session_factory) == []
    old, current = await _rows(session_factory, org.id)
    assert old.skipped is True and old.report_id is None
    assert current.skipped is False and current.report_id is None  # the period is still open


async def test_switch_to_overage_sends_earlier_period_usage(rt, fake, org, session_factory):
    await _set_account(session_factory, org.id, limit_behaviour="hard_stop")
    await _add_rows(session_factory, org.id, 5, at=NOW - timedelta(days=20))  # a closed hard-stop period
    await _add_rows(session_factory, org.id, 3, 4)  # earlier in this period, under hard stop
    assert await report_usage(rt, NOW) == 0

    await _set_account(session_factory, org.id, limit_behaviour="overage")
    await _add_rows(session_factory, org.id, 1, at=NOW - timedelta(minutes=1))
    assert await report_usage(rt, NOW) == 1
    assert [e["quantity"] for e in fake.meter_events] == [8]  # this period's usage, not the closed one's
    old, *current = await _rows(session_factory, org.id)
    assert old.skipped is True and old.report_id is None
    assert all(r.report_id is not None for r in current)


async def test_meter_without_overage_price_not_reported(rt, fake, org, session_factory):
    await _add_rows(session_factory, org.id, 3, meter="voice_minutes")  # the plan prices no voice overage
    await _add_rows(session_factory, org.id, 2, meter="voice_minutes", at=NOW - timedelta(days=20))
    assert await report_usage(rt, NOW) == 0
    assert fake.meter_events == []
    old, current = await _rows(session_factory, org.id)
    assert old.skipped is True and current.skipped is False


async def test_no_customer_id_nothing_reported(rt, fake, org, session_factory):
    await _set_account(session_factory, org.id, provider_customer_id=None)
    await _add_rows(session_factory, org.id, 3)
    assert await report_usage(rt, NOW) == 0
    assert fake.meter_events == []
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(UsageReport)) == 0


async def test_rows_straddling_period_boundary_become_two_reports(rt, fake, org, session_factory):
    start = NOW - timedelta(days=1)  # the current period's start (fixture)
    await _add_rows(session_factory, org.id, 2, 3, at=start - timedelta(minutes=8))  # left over from the last period
    await _add_rows(session_factory, org.id, 4, at=start + timedelta(minutes=5))
    assert await report_usage(rt, NOW) == 2
    earlier, current = sorted(await _reports(session_factory), key=lambda r: r.timestamp)
    assert (earlier.quantity, current.quantity) == (5, 4)
    assert earlier.timestamp == start - timedelta(minutes=8) + timedelta(seconds=1) < start  # its own side
    assert current.timestamp == start + timedelta(minutes=5) >= start
    assert earlier.status == current.status == "sent"
    assert sorted((e["quantity"], e["identifier"]) for e in fake.meter_events) == sorted(
        [(5, str(earlier.id)), (4, str(current.id))]
    )
    rows = await _rows(session_factory, org.id)
    assert [r.report_id for r in rows] == [earlier.id, earlier.id, current.id]
    assert await report_usage(rt, NOW) == 0  # at most once


async def test_row_older_than_35_days_skipped_never_sent(rt, fake, org, session_factory):
    await _add_rows(session_factory, org.id, 9, at=NOW - timedelta(days=36))
    await _add_rows(session_factory, org.id, 1)
    assert await report_usage(rt, NOW) == 1
    assert [e["quantity"] for e in fake.meter_events] == [1]
    old, current = await _rows(session_factory, org.id)
    assert old.skipped is True and old.report_id is None
    assert current.report_id is not None
    assert await report_usage(rt, NOW) == 0
    assert len(fake.meter_events) == 1


async def test_abandoned_report_logged_once(rt, fake, org, session_factory):
    """T8: the tenth failure abandons the report: logged once, at error level, with what was lost."""
    from structlog.testing import capture_logs

    await _add_rows(session_factory, org.id, 4)
    with capture_logs() as logs:
        for _ in range(10):
            fake.fail_next = ConnectionError
            await report_usage(rt, NOW)
        await report_usage(rt, NOW)
    abandoned = [e for e in logs if e["event"] == "usage_report_abandoned"]
    [report] = await _reports(session_factory)
    assert len(abandoned) == 1 and abandoned[0]["log_level"] == "error"
    assert abandoned[0]["organisation_id"] == str(org.id) and abandoned[0]["meter"] == "messages"
    assert abandoned[0]["quantity"] == 4 and abandoned[0]["usage_report_id"] == str(report.id)
