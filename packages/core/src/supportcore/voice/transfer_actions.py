"""Runs a live transfer's Twilio work (the `TransferAction`s `TransferService` returns) after the unit of work that
produced it has committed. Shared by the voice process (webhooks, the watchdog) and the stale-call sweep.

No DB session is open while Twilio is called: each follow-up state change (`set_leg_sid`, `ring_failed`) is its own
short unit of work, and the actions it returns run in turn. Failures are logged by exception class name and never
raised. Never log a phone number, token or TwiML here (`Ring.to` / `Ring.from_` stay out of every log line).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from supportcore.config import Settings
from supportcore.db.models import TelephonyAccount
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.db.uow import UnitOfWork
from supportcore.events import EventBus
from supportcore.inbox.publisher import InboxPublisher
from supportcore.observability.logging import get_logger
from supportcore.secrets.ports import SecretDecryptError
from supportcore.voice.strings import strings_for
from supportcore.voice.telephony.ports import CallWebhooks, TelephonyAccountRef
from supportcore.voice.transfers import (
    AGENT_RING_SECONDS,
    EndLeg,
    RedirectCaller,
    Ring,
    TransferAction,
    TransferService,
)
from supportcore.voice.twiml import say_and_hangup

if TYPE_CHECKING:
    from supportcore.wiring import Adapters

__all__ = ["agent_leg_url", "load_account_ref", "redirect_twiml", "run_transfer_actions"]

logger = get_logger(__name__)


def agent_leg_url(settings: Settings, endpoint: str, transfer_id: UUID, attempt: int) -> str:
    """`/twilio/voice/{endpoint}?transfer_id=&attempt=` on the voice process's public origin."""
    return f"{settings.voice_public_url}/twilio/voice/{endpoint}?transfer_id={transfer_id}&attempt={attempt}"


def redirect_twiml(action: RedirectCaller) -> str:
    """What the caller hears when the transfer gives up: the failure line, then the hang-up."""
    return say_and_hangup(strings_for(action.language)[action.key], action.language)


async def load_account_ref(session: AsyncSession, adapters: Adapters, organisation_id: UUID) -> TelephonyAccountRef | None:
    """The organisation's Twilio account (one per organisation), or None when it has none or its token is unreadable."""
    account = (
        await session.execute(TenantScopedRepository(session, TelephonyAccount, organisation_id).select())
    ).scalar_one_or_none()
    if account is None:
        return None
    try:
        return TelephonyAccountRef(account.account_sid, adapters.cipher.decrypt(account.auth_token_encrypted))
    except SecretDecryptError:
        logger.warning("telephony_token_unreadable", account_id=str(account.id))
        return None


async def run_transfer_actions(
    *,
    adapters: Adapters,
    session_factory: async_sessionmaker[AsyncSession],
    events: EventBus,
    publisher: InboxPublisher,
    settings: Settings,
    organisation_id: UUID,
    account: TelephonyAccountRef,
    actions: Iterable[TransferAction],
) -> None:
    """Runs `actions` in order, and the actions that running them produces (a refused leg rings the next agent; a
    leg placed after its transfer moved on is ended)."""
    pending: deque[TransferAction] = deque(actions)

    async def in_unit_of_work(work: Callable[[TransferService], Awaitable[list[TransferAction]]]) -> None:
        async with UnitOfWork(session_factory) as uow:
            service = TransferService(
                uow.session, organisation_id, events, publisher, adapters, settings, session_factory=session_factory
            )
            more = await work(service)
            await uow.commit()
        pending.extend(more)

    while pending:
        action = pending.popleft()
        try:
            if isinstance(action, Ring):
                await _ring(adapters, settings, account, action, in_unit_of_work)
            elif isinstance(action, EndLeg):
                await adapters.telephony.update_call(account, action.call_sid, end=True)
                logger.info("transfer_leg_end_requested", call_sid=action.call_sid)
            elif isinstance(action, RedirectCaller):
                await adapters.telephony.update_call(account, action.call_sid, twiml=redirect_twiml(action))
                logger.info("transfer_caller_redirected", call_sid=action.call_sid, key=action.key)
        except Exception as exc:  # noqa: BLE001  (logged, never raised: the next action still runs)
            logger.warning(
                "transfer_action_failed",
                organisation_id=str(organisation_id),
                action=type(action).__name__,
                error_type=type(exc).__name__,
            )


async def _ring(
    adapters: Adapters,
    settings: Settings,
    account: TelephonyAccountRef,
    ring: Ring,
    in_unit_of_work: Callable[[Callable[[TransferService], Awaitable[list[TransferAction]]]], Awaitable[None]],
) -> None:
    webhooks = CallWebhooks(
        answer_url=agent_leg_url(settings, "agent-answer", ring.transfer_id, ring.attempt),
        status_callback=agent_leg_url(settings, "agent-status", ring.transfer_id, ring.attempt),
    )
    try:
        sid = await adapters.telephony.create_call(account, ring.to, ring.from_, webhooks, AGENT_RING_SECONDS)
    except Exception as exc:  # noqa: BLE001  (any failure to place the leg: the next agent rings)
        logger.warning(
            "transfer_leg_refused",
            transfer_id=str(ring.transfer_id),
            attempt=ring.attempt,
            target=ring.target,
            error_type=type(exc).__name__,
        )
        await in_unit_of_work(lambda service: service.ring_failed(ring.transfer_id, ring.attempt))
        return
    logger.info("transfer_leg_placed", transfer_id=str(ring.transfer_id), attempt=ring.attempt, call_sid=sid)
    try:
        await in_unit_of_work(lambda service: service.set_leg_sid(ring.transfer_id, ring.attempt, sid))
    except Exception as exc:  # noqa: BLE001
        # The sid is not stored, so nothing else could stop this leg: end it rather than let it ring orphaned
        # (the watchdog gives the transfer up at its deadline).
        logger.warning(
            "transfer_leg_sid_not_stored",
            transfer_id=str(ring.transfer_id),
            attempt=ring.attempt,
            call_sid=sid,
            error_type=type(exc).__name__,
        )
        await adapters.telephony.update_call(account, sid, end=True)
