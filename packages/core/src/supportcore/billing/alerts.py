"""Billing warnings by email (the `billing_alerts` job, every 10 minutes).

Per organisation (none with the dev provider, which has no limits):
- `{meter}_80` and `{meter}_100` when a meter reaches 80% and 100% of its limit, once per billing period;
- `trial_ending` when the trial ends within TRIAL_ENDING_NOTICE, and `trial_ended` once it has ended, once per trial
  (keyed by the trial's start, so an expired trial is not warned about again every month);
- `payment_failed`, recorded by the webhook handling (BillingService), sent here.

Each alert is a UsageAlert row, inserted with ON CONFLICT DO NOTHING, so it exists once. Its email goes to the active
members whose role has `billing.manage`; `sent_at` is set only after every email went out, so a failed send is
retried on the next run. No session is open while emails are sent.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.billing.entitlements import EntitlementService
from supportcore.billing.service import PAYMENT_FAILED_ALERT
from supportcore.billing.strings import AlertContent, alert_email
from supportcore.db.models import (
    BillingAccount,
    Membership,
    Organisation,
    Role,
    UsageAlert,
    User,
)
from supportcore.db.models.billing import METERS
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.db.uow import UnitOfWork
from supportcore.jobs.ports import JobRuntime
from supportcore.notifications.ports import EmailMessage
from supportcore.observability.logging import get_logger
from supportcore.rbac.permissions import Permission

__all__ = ["BILLING_ALERTS_INTERVAL_SECONDS", "TRIAL_ENDING_NOTICE", "run_alerts"]

logger = get_logger(__name__)

BILLING_ALERTS_INTERVAL_SECONDS = 600
TRIAL_ENDING_NOTICE = timedelta(days=3)
_THRESHOLDS = (80, 100)
_TRIAL_KINDS = ("trial_ending", "trial_ended")


@dataclass(frozen=True)
class _Pending:
    alert_id: UUID
    subject: str
    text: str


async def _recipients(session: AsyncSession, organisation_id: UUID) -> list[str]:
    """Emails of the active members whose role has billing.manage."""
    stmt = (
        TenantScopedRepository(session, Membership, organisation_id)
        .select(User.email)
        .join(Role, Role.id == Membership.role_id)
        .join(User, User.id == Membership.user_id)
        .where(
            Membership.status == "active",
            User.is_active.is_(True),
            Role.permissions.contains([Permission.BILLING_MANAGE.value]),
        )
        .order_by(User.email)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _collect(rt: JobRuntime, organisation_id: UUID, now: datetime) -> tuple[list[_Pending], list[str]]:
    """One unit of work: records the alerts now due and returns every unsent one (with its email) and the
    recipients. Committed before any email is sent."""
    async with UnitOfWork(rt.session_factory) as uow:
        session = uow.session
        entitlements = EntitlementService(session, organisation_id, rt.adapters, now=now)
        state = await entitlements.state()
        account = (
            await session.execute(TenantScopedRepository(session, BillingAccount, organisation_id).select())
        ).scalar_one()
        contents: dict[tuple[datetime, str], AlertContent] = {}
        if not state.locked:
            for meter in METERS:
                usage = await entitlements.meter(meter)
                if usage.limit is None or usage.limit <= 0:
                    continue
                content = AlertContent(meter=meter, used=usage.used, limit=usage.limit, overage=usage.overage)
                for threshold in _THRESHOLDS:
                    if usage.used * 100 >= usage.limit * threshold:
                        contents[(state.period.start, f"{meter}_{threshold}")] = content
        trial = AlertContent(trial_ends_at=account.trial_ends_at)
        if account.trial_started_at is not None and account.trial_ends_at is not None:
            if state.effective_status == "trialing" and account.trial_ends_at - now <= TRIAL_ENDING_NOTICE:
                contents[(account.trial_started_at, "trial_ending")] = trial
            elif state.effective_status == "trial_expired":
                contents[(account.trial_started_at, "trial_ended")] = trial
        for period_start, kind in contents:
            await session.execute(
                pg_insert(UsageAlert)
                .values(organisation_id=organisation_id, period_start=period_start, kind=kind)
                .on_conflict_do_nothing(index_elements=["organisation_id", "period_start", "kind"])
            )
        # Unsent: just inserted, failed earlier, or recorded elsewhere (payment_failed). A usage alert of an earlier
        # period is not sent late: its numbers are gone.
        unsent = (
            (
                await session.execute(
                    TenantScopedRepository(session, UsageAlert, organisation_id)
                    .select()
                    .where(
                        UsageAlert.sent_at.is_(None),
                        or_(
                            UsageAlert.kind.in_((PAYMENT_FAILED_ALERT, *_TRIAL_KINDS)),
                            UsageAlert.period_start == state.period.start,
                        ),
                    )
                    .order_by(UsageAlert.kind)
                )
            )
            .scalars()
            .all()
        )
        pending: list[_Pending] = []
        if unsent:
            organisation = await session.get(Organisation, organisation_id)
            name = organisation.name if organisation is not None else "Your organisation"
            for alert in unsent:
                content = contents.get((alert.period_start, alert.kind))
                if content is None:
                    if alert.kind != PAYMENT_FAILED_ALERT:
                        continue  # no longer true (the plan changed, the trial converted): not sent late
                    content = AlertContent()
                subject, text = alert_email(alert.kind, name, organisation_id, rt.settings.web_origin, content)
                pending.append(_Pending(alert.id, subject, text))
        recipients = await _recipients(session, organisation_id) if pending else []
        await uow.commit()
    return pending, recipients


async def _deliver(rt: JobRuntime, organisation_id: UUID, alert: _Pending, recipients: list[str]) -> bool:
    for to in recipients:
        try:
            await rt.adapters.email_sender.send(EmailMessage(to=to, subject=alert.subject, text=alert.text))
        except Exception as exc:
            logger.warning(
                "billing_alert_send_failed",
                organisation_id=str(organisation_id),
                usage_alert_id=str(alert.alert_id),
                error_type=type(exc).__name__,
            )
            return False
    if not recipients:
        logger.warning("billing_alert_no_recipients", organisation_id=str(organisation_id))
    async with UnitOfWork(rt.session_factory) as uow:
        await uow.session.execute(
            update(UsageAlert).where(UsageAlert.id == alert.alert_id).values(sent_at=datetime.now(UTC))
        )
        await uow.commit()
    return True


async def run_alerts(rt: JobRuntime, now: datetime) -> int:
    """Records and emails the billing alerts now due. Returns how many alerts were sent."""
    if rt.adapters.billing.grants_everything:
        return 0
    async with rt.session_factory() as session:
        organisation_ids = (await session.execute(select(BillingAccount.organisation_id))).scalars().all()
    sent = 0
    for organisation_id in organisation_ids:
        try:
            pending, recipients = await _collect(rt, organisation_id, now)
        except Exception as exc:  # one organisation never blocks the others
            logger.warning("billing_alerts_failed", organisation_id=str(organisation_id), error_type=type(exc).__name__)
            continue
        for alert in pending:  # no session is open while emails are sent
            sent += await _deliver(rt, organisation_id, alert, recipients)
    if sent:
        logger.info("billing_alerts_sent", alerts=sent)
    return sent
