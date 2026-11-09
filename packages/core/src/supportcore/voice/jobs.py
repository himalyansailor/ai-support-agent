"""The stale-call sweep (celery beat, every STALE_CALL_SWEEP_INTERVAL_SECONDS).

A call that connected a media stream is finalised by the later of Twilio's status callback and the end of its
stream session. When one side never comes, the sweep finalises it:
  - Twilio's terminal status is stored but the stream end was never recorded (the voice process crashed or failed
    to write it): finalised with the stored status and duration STREAM_END_GRACE after the status arrived;
  - no status callback at all and the call is still `ringing` / `in_progress` well past its organisation's call
    limit (`max_call_minutes` + NO_STATUS_GRACE): finalised as `failed`. A callback call (an agent and the customer,
    no bot) gets MAX_TRANSFERRED_CALL_HOURS instead.

Live transfers (the voice process's watchdog normally handles them; the sweep is the backstop when it is gone):
  - a transfer still `ringing` TRANSFER_SWEEP_GRACE past its deadline is given up (`deadline_passed`): the ringing
    leg is ended and the caller hears the failure line, unless the caller's leg is already over;
  - a call with a `connected` transfer (an agent is talking to the caller; the bot's call limit no longer applies) is
    left alone until MAX_TRANSFERRED_CALL_HOURS after `connected_at`.

The Twilio work runs after each unit of work has committed, never inside one. Ids and counts only in the logs.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from sqlalchemy import DateTime, case, exists, func, select

from supportcore.db.models import Call, CallTransfer, VoiceSettings
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.db.uow import UnitOfWork
from supportcore.inbox.publisher import InboxPublisher
from supportcore.observability.context import job_context
from supportcore.voice.calls import CallService
from supportcore.voice.metrics import VOICE_CALLS, mode_label
from supportcore.voice.session import TURN_FINISH_TIMEOUT_SECONDS
from supportcore.voice.transfer_actions import load_account_ref, run_transfer_actions
from supportcore.voice.transfers import (
    MAX_TRANSFERRED_CALL_HOURS,
    RedirectCaller,
    TransferAction,
    TransferService,
)

if TYPE_CHECKING:
    from supportcore.jobs.ports import JobRuntime

__all__ = [
    "NO_STATUS_GRACE",
    "STALE_CALL_SWEEP_INTERVAL_SECONDS",
    "STREAM_END_GRACE",
    "TRANSFER_SWEEP_GRACE",
    "finalise_stale_calls",
]

logger = structlog.get_logger()

STALE_CALL_SWEEP_INTERVAL_SECONDS = 300
# Longer than a hung-up call's bounded wait for its last turn (session.TURN_FINISH_TIMEOUT_SECONDS), so the sweep
# never finalises a call whose session is still storing its reply and usage.
STREAM_END_GRACE = timedelta(seconds=TURN_FINISH_TIMEOUT_SECONDS + 120)
NO_STATUS_GRACE = timedelta(minutes=10)
TRANSFER_SWEEP_GRACE = timedelta(seconds=60)  # past a ringing transfer's deadline: its watchdog is gone
TRANSFERRED_CALL_LIMIT = timedelta(hours=MAX_TRANSFERRED_CALL_HOURS)
DEFAULT_MAX_CALL_MINUTES = 30  # VoiceSettings.max_call_minutes' default, for an organisation without the row
_BATCH = 100


async def finalise_stale_calls(runtime: JobRuntime) -> int:
    """Finalises the calls neither the stream nor Twilio's status callback finished. Returns how many."""
    async with runtime.session_factory() as s:
        # The database clock, like the call timestamps' server-side comparisons.
        now = (await s.execute(select(func.clock_timestamp(type_=DateTime(timezone=True))))).scalar_one()
        # Deliberately across organisations (like the other beat sweeps): only the ids of organisations with an
        # unfinalised call; each organisation's calls are then read and written tenant-scoped.
        organisation_ids = list(
            (await s.execute(select(Call.organisation_id).where(Call.finalised.is_(False)).distinct())).scalars()
        )
        # Likewise across organisations: only the ids of organisations with a transfer stuck ringing.
        stuck_organisation_ids = list(
            (
                await s.execute(
                    select(CallTransfer.organisation_id)
                    .where(CallTransfer.status == "ringing", CallTransfer.deadline_at < now - TRANSFER_SWEEP_GRACE)
                    .distinct()
                )
            ).scalars()
        )
    for org in stuck_organisation_ids:
        with job_context(org, "finalise_stale_calls"):
            await _give_up_stuck_transfers(runtime, org, now)
    finalised = 0
    for org in organisation_ids:
        with job_context(org, "finalise_stale_calls"):
            finalised += await _finalise_of(runtime, org, now)
    if finalised:
        logger.info("stale_calls_finalised", calls=finalised)
    return finalised


async def _give_up_stuck_transfers(runtime: JobRuntime, org: UUID, now: datetime) -> None:
    """Each stuck transfer is given up in its own unit of work (under its row lock, like the watchdog), and its
    Twilio work runs after the commit."""
    async with runtime.session_factory() as s:
        stmt = (
            TenantScopedRepository(s, CallTransfer, org)
            .select(CallTransfer.id)
            .where(CallTransfer.status == "ringing", CallTransfer.deadline_at < now - TRANSFER_SWEEP_GRACE)
            .order_by(CallTransfer.deadline_at)
            .limit(_BATCH)
        )
        transfer_ids = list((await s.execute(stmt)).scalars())
    publisher = _publisher(runtime)
    for transfer_id in transfer_ids:
        try:
            async with UnitOfWork(runtime.session_factory) as uow:
                session = uow.session
                service = TransferService(
                    session,
                    org,
                    runtime.events,
                    publisher,
                    runtime.adapters,
                    runtime.settings,
                    session_factory=runtime.session_factory,
                )
                actions = await service.deadline_passed(transfer_id)
                transfer = await TenantScopedRepository(session, CallTransfer, org).get(transfer_id)
                status = transfer.status if transfer is not None else None
                call = await TenantScopedRepository(session, Call, org).get(transfer.call_id) if transfer else None
                caller_gone = call is not None and (call.finalised or call.provider_ended_at is not None)
                await uow.commit()
        except Exception as exc:  # noqa: BLE001 - one bad transfer must not stop the sweep
            logger.warning("stale_transfer_failed", transfer_id=str(transfer_id), error_type=type(exc).__name__)
            continue
        if status is None or status == "ringing":
            continue  # not past its deadline after all (or gone): left to its watchdog
        if caller_gone:
            # The caller's leg is already over: there is nobody to redirect to the failure line.
            actions = [action for action in actions if not isinstance(action, RedirectCaller)]
        logger.info("stale_transfer_given_up", transfer_id=str(transfer_id), status=status)
        await _run_actions(runtime, org, actions)


async def _run_actions(runtime: JobRuntime, org: UUID, actions: list[TransferAction]) -> None:
    """The transfer's Twilio work, through the shared runner, with no session open."""
    if not actions:
        return
    async with runtime.session_factory() as s:
        account = await load_account_ref(s, runtime.adapters, org)
    if account is None:
        logger.warning("stale_transfer_actions_skipped", organisation_id=str(org), actions=len(actions))
        return
    await run_transfer_actions(
        adapters=runtime.adapters,
        session_factory=runtime.session_factory,
        events=runtime.events,
        publisher=_publisher(runtime),
        settings=runtime.settings,
        organisation_id=org,
        account=account,
        actions=actions,
    )


async def _finalise_of(runtime: JobRuntime, org: UUID, now: datetime) -> int:
    count = 0
    while True:
        async with UnitOfWork(runtime.session_factory) as uow:
            session = uow.session
            settings = (
                await session.execute(TenantScopedRepository(session, VoiceSettings, org).select())
            ).scalar_one_or_none()
            max_minutes = settings.max_call_minutes if settings is not None else DEFAULT_MAX_CALL_MINUTES
            calls = TenantScopedRepository(session, Call, org)
            stream_never_ended = (Call.provider_ended_at.is_not(None)) & (
                Call.provider_ended_at < now - STREAM_END_GRACE
            )
            # A callback call is an agent talking to the customer: the bot's limit does not apply to it, the
            # transferred-call limit does.
            no_status_since = case(
                (Call.source == "callback", now - TRANSFERRED_CALL_LIMIT),
                else_=now - timedelta(minutes=max_minutes) - NO_STATUS_GRACE,
            )
            no_status = (
                Call.provider_ended_at.is_(None)
                & Call.status.in_(("ringing", "in_progress"))
                & (Call.started_at < no_status_since)
            )
            # An agent took the call: it may outlast the bot's limit, up to MAX_TRANSFERRED_CALL_HOURS.
            transferred_recently = exists().where(
                CallTransfer.organisation_id == Call.organisation_id,
                CallTransfer.call_id == Call.id,
                CallTransfer.status == "connected",
                CallTransfer.connected_at > now - TRANSFERRED_CALL_LIMIT,
            )
            stmt = (
                calls.select()
                .where(Call.finalised.is_(False), stream_never_ended | no_status, ~transferred_recently)
                .order_by(Call.started_at)
                .limit(_BATCH)
                .with_for_update(skip_locked=True)  # a call being finalised right now is left to its writer
            )
            batch = list((await session.execute(stmt)).scalars())
            service = CallService(session, org, runtime.events, _publisher(runtime), adapters=runtime.adapters)
            done: list[tuple[str, str, str]] = []
            for call in batch:
                if call.provider_ended_at is not None:
                    ok = await service.finalise(call.id, call.status, call.duration_seconds)
                else:
                    ok = await service.finalise(call.id, "failed", None)
                if ok:
                    done.append((call.source, mode_label(call.mode), call.end_reason or "caller_hangup"))
                    logger.info("stale_call_finalised", call_id=str(call.id), status=call.status)
            await uow.commit()
        for source, mode, end_reason in done:
            VOICE_CALLS.labels(source=source, mode=mode, end_reason=end_reason).inc()
        count += len(done)
        if len(batch) < _BATCH:
            return count


def _publisher(runtime: JobRuntime) -> InboxPublisher:
    """The runtime's shared publisher when it has one, else one for this run (like the idle-close job)."""
    shared = getattr(runtime, "publisher", None)
    if callable(shared):
        return shared()
    return InboxPublisher(runtime.adapters.pubsub, runtime.session_factory)
