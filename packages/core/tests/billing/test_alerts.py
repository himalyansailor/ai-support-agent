"""run_alerts: usage, trial and payment warnings emailed to billing managers once (Phase 8, Task 8).

Real commits (the job opens its own units of work), so these tests use `session_factory`."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from supportcore.billing.alerts import run_alerts
from supportcore.billing.dev_billing import DevBilling
from supportcore.billing.fake_billing import FakeBilling
from supportcore.config import Settings
from supportcore.db.models import (
    BillingAccount,
    Membership,
    Organisation,
    Plan,
    Role,
    UsageAlert,
    UsageEvent,
    User,
)
from supportcore.notifications.ports import EmailDeliveryError, EmailMessage
from supportcore.rbac.permissions import Permission

WEB = "https://app.example.com"
NOW = datetime.now(UTC)


class LedgerCounter:
    """A usage counter that never has a key, so every read is the ledger's sum."""

    async def get(self, key):
        return None

    async def incr_if_present(self, key, amount):
        return None

    async def set_if_absent(self, key, value, ttl_seconds):
        return None

    async def set(self, key, value, ttl_seconds):
        return None


class Outbox:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []
        self.fail_next = False

    async def send(self, message: EmailMessage) -> None:
        if self.fail_next:
            self.fail_next = False
            raise EmailDeliveryError()
        self.sent.append(message)


@pytest.fixture
def outbox():
    return Outbox()


@pytest.fixture
def rt(session_factory, outbox):
    settings = Settings(_env_file=None, env="test", billing="fake", web_origin=WEB, jwt_secret="t" * 40)
    return SimpleNamespace(
        settings=settings,
        session_factory=session_factory,
        adapters=SimpleNamespace(billing=FakeBilling(web_origin=WEB), usage_counter=LedgerCounter(), email_sender=outbox),
    )


@pytest.fixture
async def org(session_factory):
    """Acme, trialing a plan of 10 messages for 14 days (started an hour ago), with a billing manager, an agent
    and a suspended billing manager."""
    async with session_factory() as s:
        org = Organisation(name="Acme Ltd", slug=f"acme-{uuid.uuid4().hex[:6]}")
        plan = Plan(key="pro", name="Pro", monthly_price_minor=4900, trial_days=14, messages=10)
        users = [
            User(email="boss@acme.test", password_hash="x", name="Boss"),
            User(email="agent@acme.test", password_hash="x", name="Agent"),
            User(email="gone@acme.test", password_hash="x", name="Gone"),
        ]
        s.add_all([org, plan, *users])
        await s.flush()
        owner = Role(organisation_id=org.id, name="Owner", permissions=[p.value for p in Permission])
        agent = Role(organisation_id=org.id, name="Agent", permissions=[Permission.QUERIES_ANSWER.value])
        s.add_all([owner, agent])
        await s.flush()
        s.add_all(
            [
                Membership(organisation_id=org.id, user_id=users[0].id, role_id=owner.id),
                Membership(organisation_id=org.id, user_id=users[1].id, role_id=agent.id),
                Membership(organisation_id=org.id, user_id=users[2].id, role_id=owner.id, status="suspended"),
                BillingAccount(
                    organisation_id=org.id,
                    plan_id=plan.id,
                    status="trialing",
                    trial_started_at=NOW - timedelta(hours=1),
                    trial_ends_at=NOW - timedelta(hours=1) + timedelta(days=14),
                ),
            ]
        )
        await s.commit()
        return org


async def _use(session_factory, org_id, quantity, meter="messages"):
    async with session_factory() as s:
        s.add(
            UsageEvent(
                organisation_id=org_id,
                meter=meter,
                quantity=quantity,
                source_id=uuid.uuid4(),
                occurred_at=NOW - timedelta(minutes=5),
            )
        )
        await s.commit()


async def _alerts(session_factory):
    async with session_factory() as s:
        return (await s.execute(select(UsageAlert).order_by(UsageAlert.kind))).scalars().all()


async def test_alert_80_and_100_sent_once_per_period(rt, outbox, org, session_factory):
    assert await run_alerts(rt, NOW) == 0  # no usage yet
    await _use(session_factory, org.id, 8)
    assert await run_alerts(rt, NOW) == 1
    [email] = outbox.sent
    assert email.to == "boss@acme.test"  # the agent and the suspended manager get nothing
    assert "Acme Ltd" in email.subject + email.text
    assert "80%" in email.subject
    assert "8 of 10 messages" in email.text
    assert f"{WEB}/orgs/{org.id}/settings/billing" in email.text

    assert await run_alerts(rt, NOW) == 0  # once per period
    assert len(outbox.sent) == 1

    await _use(session_factory, org.id, 3)
    assert await run_alerts(rt, NOW) == 1
    assert len(outbox.sent) == 2
    assert "11 of 10 messages" in outbox.sent[1].text
    assert await run_alerts(rt, NOW) == 0
    alerts = await _alerts(session_factory)
    assert [a.kind for a in alerts] == ["messages_100", "messages_80"]
    assert all(a.sent_at is not None for a in alerts)


async def test_jump_past_100_sends_both_once(rt, outbox, org, session_factory):
    await _use(session_factory, org.id, 4, meter="messages")
    await _use(session_factory, org.id, 12, meter="messages")
    assert await run_alerts(rt, NOW) == 2
    assert await run_alerts(rt, NOW) == 0
    assert len(outbox.sent) == 2


async def test_trial_ending_alert_three_days_before(rt, outbox, org, session_factory):
    ends = NOW - timedelta(hours=1) + timedelta(days=14)
    assert await run_alerts(rt, ends - timedelta(days=3, minutes=1)) == 0
    assert await run_alerts(rt, ends - timedelta(days=3) + timedelta(minutes=1)) == 1
    [email] = outbox.sent
    assert "trial" in email.subject.lower() and "Acme Ltd" in email.text
    assert f"{WEB}/orgs/{org.id}/settings/billing" in email.text
    assert await run_alerts(rt, ends - timedelta(days=1)) == 0  # once

    assert await run_alerts(rt, ends + timedelta(minutes=1)) == 1  # trial_ended
    assert await run_alerts(rt, ends + timedelta(days=40)) == 0  # once per trial, not once a month
    assert [a.kind for a in await _alerts(session_factory)] == ["trial_ended", "trial_ending"]
    assert "ended" in outbox.sent[1].subject.lower()


async def test_email_failure_retries_next_run(rt, outbox, org, session_factory):
    await _use(session_factory, org.id, 9)
    outbox.fail_next = True
    assert await run_alerts(rt, NOW) == 0
    [alert] = await _alerts(session_factory)
    assert alert.kind == "messages_80" and alert.sent_at is None

    assert await run_alerts(rt, NOW) == 1
    [alert] = await _alerts(session_factory)
    assert alert.sent_at is not None
    assert len(outbox.sent) == 1
    assert await run_alerts(rt, NOW) == 0


async def test_payment_failed_alert_sent_once(rt, outbox, org, session_factory):
    async with session_factory() as s:  # as the webhook records it: sent_at unset
        s.add(UsageAlert(organisation_id=org.id, period_start=NOW - timedelta(hours=1), kind="payment_failed"))
        await s.commit()
    assert await run_alerts(rt, NOW) == 1
    [email] = outbox.sent
    assert "payment" in email.subject.lower() and "Acme Ltd" in email.text
    assert f"{WEB}/orgs/{org.id}/settings/billing" in email.text
    assert await run_alerts(rt, NOW) == 0
    [alert] = await _alerts(session_factory)
    assert alert.sent_at is not None


async def test_dev_accounts_get_no_alerts(rt, outbox, org, session_factory):
    rt.adapters.billing = DevBilling()
    await _use(session_factory, org.id, 50)
    assert await run_alerts(rt, NOW + timedelta(days=20)) == 0  # past the limit and the trial's end
    assert outbox.sent == []
    assert await _alerts(session_factory) == []


def test_usage_emails_for_overage_accounts_say_usage_continues():
    """M4: an account on overage is never told its usage is blocked."""
    from supportcore.billing.strings import AlertContent, alert_email

    org_id = uuid.uuid4()
    for kind in ("messages_80", "messages_100"):
        _subject, text = alert_email(
            kind, "Acme", org_id, WEB, AlertContent(meter="messages", used=10, limit=10, overage=True)
        )
        assert "blocked" not in text and "billed as overage" in text
        _subject, text = alert_email(kind, "Acme", org_id, WEB, AlertContent(meter="messages", used=10, limit=10))
        assert "blocked" in text


async def test_overage_subscriber_100_alert_says_billed_as_overage(rt, outbox, org, session_factory):
    from supportcore.db.models import PlanProviderPrice

    async with session_factory() as s:
        account = (await s.execute(select(BillingAccount).where(BillingAccount.organisation_id == org.id))).scalar_one()
        s.add(PlanProviderPrice(
            plan_id=account.plan_id, provider="fake", price_id="p_msg", kind="messages", included_units=10,
            overage_micros=5000,
        ))
        account.status, account.limit_behaviour, account.subscription_price_ids = "active", "overage", ["p_msg"]
        account.current_period_start, account.current_period_end = NOW - timedelta(days=1), NOW + timedelta(days=29)
        await s.commit()
    await _use(session_factory, org.id, 12)
    assert await run_alerts(rt, NOW) == 2
    assert all("blocked" not in e.text and "billed as overage" in e.text for e in outbox.sent)
