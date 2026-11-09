"""Sends overage usage to the billing provider as meter events (the `report_usage` job, every 10 minutes).

1. Per organisation with unreported ledger rows, one unit of work. For each meter billed as overage, its unreported
   rows up to the period's end are locked (`FOR UPDATE SKIP LOCKED`) and split at the current period's start: the
   rows left over from before it (a period that rolled over between two runs, or while the worker was down) and the
   current period's rows each become one `pending` UsageReport, so a report never mixes two periods and its
   timestamp (the latest occurred_at of its own rows) lands in the right one. Rows older than MAX_REPORT_AGE (the
   provider refuses meter events older than 35 days) are marked `skipped` and logged (`usage_report_too_old`).
   Rows of meters that are not billed (hard stop, no overage price, no subscription) from a closed period are marked
   `skipped`: they will never be sent. Rows of the current period are left alone, so switching to overage mid-period
   still sends them (the provider sums per period).
2. Every `pending` or `failed` report with fewer than MAX_ATTEMPTS attempts is sent with no session open, its id as
   the provider's idempotency identifier: a retry, or a resend after a crash between steps 1 and 3, can never count
   the usage twice.
3. One unit of work per report records the outcome (`sent` or `failed`, attempts + 1). A report that fails its
   MAX_ATTEMPTS-th attempt is never sent again: that is logged once, at error level (`usage_report_abandoned`).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Row, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.billing.accounts import load_account
from supportcore.billing.entitlements import EntitlementService
from supportcore.db.models import UsageEvent, UsageReport
from supportcore.db.models.billing import METERS
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.db.uow import UnitOfWork
from supportcore.jobs.ports import JobRuntime
from supportcore.observability.logging import get_logger

__all__ = ["MAX_ATTEMPTS", "MAX_REPORT_AGE", "REPORT_USAGE_INTERVAL_SECONDS", "report_usage"]

logger = get_logger(__name__)

REPORT_USAGE_INTERVAL_SECONDS = 600
MAX_ATTEMPTS = 10
# Stripe refuses meter events whose timestamp is more than 35 days in the past.
MAX_REPORT_AGE = timedelta(days=35)


@dataclass(frozen=True)
class _Outgoing:
    id: UUID
    organisation_id: UUID
    customer_id: str
    meter: str
    quantity: int
    timestamp: datetime


async def _prepare(rt: JobRuntime, organisation_id: UUID, now: datetime) -> None:
    """Unit of work A for one organisation: skips its closed-period rows of unbilled meters and its too-old rows of
    billed meters, and turns the other unreported rows of billed meters into pending reports (one per side of the
    current period's start)."""
    billing = rt.adapters.billing
    async with UnitOfWork(rt.session_factory) as uow:
        session = uow.session
        entitlements = EntitlementService(session, organisation_id, rt.adapters, now=now)
        state = await entitlements.state()
        account = await load_account(session, organisation_id)
        customer_id = account.provider_customer_id if account.provider == billing.key else None
        billed = await entitlements.overage_meters() if customer_id else ()
        unbilled = [meter for meter in METERS if meter not in billed]
        unreported = (UsageEvent.report_id.is_(None), UsageEvent.skipped.is_(False))
        rows = TenantScopedRepository(session, UsageEvent, organisation_id)
        if unbilled:
            await session.execute(
                update(UsageEvent)
                .where(
                    UsageEvent.id.in_(
                        rows.select(UsageEvent.id).where(
                            UsageEvent.meter.in_(unbilled), UsageEvent.occurred_at < state.period.start, *unreported
                        )
                    )
                )
                .values(skipped=True)
            )
        for meter in billed:
            locked = (
                await session.execute(
                    rows.select(UsageEvent.id, UsageEvent.quantity, UsageEvent.occurred_at)
                    .where(UsageEvent.meter == meter, UsageEvent.occurred_at < state.period.end, *unreported)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            oldest = now - MAX_REPORT_AGE
            too_old = [row for row in locked if row.occurred_at < oldest]
            if too_old:
                await session.execute(
                    update(UsageEvent).where(UsageEvent.id.in_([row.id for row in too_old])).values(skipped=True)
                )
                logger.warning(
                    "usage_report_too_old",
                    organisation_id=str(organisation_id),
                    meter=meter,
                    quantity=sum(row.quantity for row in too_old),
                )
            sendable = [row for row in locked if row.occurred_at >= oldest]
            earlier = [row for row in sendable if row.occurred_at < state.period.start]
            current = [row for row in sendable if row.occurred_at >= state.period.start]
            for batch in (earlier, current):
                if batch:
                    await _create_report(session, organisation_id, meter, customer_id, batch)
        await uow.commit()


async def _create_report(
    session: AsyncSession, organisation_id: UUID, meter: str, customer_id: str, batch: list[Row]
) -> None:
    """One pending report of `batch` (rows of one meter, all on one side of a period boundary), linked to its rows."""
    report = UsageReport(
        organisation_id=organisation_id,
        meter=meter,
        quantity=sum(row.quantity for row in batch),
        provider_customer_id=customer_id,
        timestamp=max(row.occurred_at for row in batch),
        status="pending",
    )
    session.add(report)
    await session.flush()
    await session.execute(
        update(UsageEvent).where(UsageEvent.id.in_([row.id for row in batch])).values(report_id=report.id)
    )


async def _send(rt: JobRuntime, report: _Outgoing) -> bool:
    """Sends one report (no session open), then records the outcome in its own unit of work."""
    try:
        await rt.adapters.billing.report_usage(
            report.customer_id, report.meter, report.quantity, str(report.id), report.timestamp
        )
        sent = True
    except Exception as exc:  # the provider's message is never logged
        logger.warning(
            "usage_report_failed",
            organisation_id=str(report.organisation_id),
            usage_report_id=str(report.id),
            error_type=type(exc).__name__,
        )
        sent = False
    async with UnitOfWork(rt.session_factory) as uow:
        attempts = (
            await uow.session.execute(
                update(UsageReport)
                .where(UsageReport.id == report.id)
                .values(
                    status="sent" if sent else "failed",
                    attempts=UsageReport.attempts + 1,
                    sent_at=datetime.now(UTC) if sent else None,
                )
                .returning(UsageReport.attempts)
            )
        ).scalar_one()
        await uow.commit()
    if not sent and attempts >= MAX_ATTEMPTS:  # the last attempt: this usage will never be billed
        logger.error(
            "usage_report_abandoned",
            organisation_id=str(report.organisation_id),
            meter=report.meter,
            quantity=report.quantity,
            usage_report_id=str(report.id),
            attempts=attempts,
        )
    return sent


async def report_usage(rt: JobRuntime, now: datetime) -> int:
    """Turns unreported usage into reports and sends every report due. Returns how many reports were sent."""
    async with rt.session_factory() as session:
        organisation_ids = (
            (
                await session.execute(
                    select(UsageEvent.organisation_id)
                    .where(UsageEvent.report_id.is_(None), UsageEvent.skipped.is_(False))
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
    for organisation_id in organisation_ids:
        try:
            await _prepare(rt, organisation_id, now)
        except Exception as exc:  # one organisation never blocks the others
            logger.warning(
                "usage_report_prepare_failed", organisation_id=str(organisation_id), error_type=type(exc).__name__
            )

    async with rt.session_factory() as session:
        due = [
            _Outgoing(r.id, r.organisation_id, r.provider_customer_id, r.meter, r.quantity, r.timestamp)
            for r in (
                await session.execute(
                    select(UsageReport)
                    .where(UsageReport.status.in_(("pending", "failed")), UsageReport.attempts < MAX_ATTEMPTS)
                    .order_by(UsageReport.created_at)
                )
            )
            .scalars()
            .all()
        ]
    sent = 0
    for report in due:  # no session is open here: _send opens its own after the provider call
        sent += await _send(rt, report)
    if sent:
        logger.info("usage_reports_sent", reports=sent)
    return sent
