"""The Twilio webhooks. Each one checks Twilio's signature first (against `voice_public_url + path + query`, with the
token of the account the call belongs to); a bad signature is a 403 with an empty body and a log line with the path
only. Nothing here logs a phone number, token or recording URL."""

import re
from collections.abc import Awaitable, Callable
from uuid import UUID

import structlog
from fastapi import APIRouter, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from supportcore.billing.entitlements import EntitlementService
from supportcore.db.models import (
    Call,
    CallTransfer,
    Conversation,
    PhoneNumber,
    TelephonyAccount,
)
from supportcore.db.session import after_commit
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.db.uow import UnitOfWork
from supportcore.observability.context import bind_organisation
from supportcore.voice.calls import CallService
from supportcore.voice.errors import InvalidVoiceTokenError
from supportcore.voice.metrics import VOICE_CALLS, mode_label
from supportcore.voice.settings_service import VoiceSettingsService, load_voice_setup
from supportcore.voice.strings import strings_for, unavailable_key
from supportcore.voice.tokens import CallTicket, issue_stream_token, verify_call_ticket
from supportcore.voice.transfer_actions import redirect_twiml
from supportcore.voice.transfers import RedirectCaller, TransferService

from supportvoice import twiml
from supportvoice.callback_routes import start_browser_callback
from supportvoice.container import VoiceContainer
from supportvoice.webhooks import (
    ContainerDep,
    SessionDep,
    account_ref,
    auth_token,
    flatten_form,  # noqa: F401  (re-exported: the tests import it from here)
    forbidden,
    read_form,
    signature_ok,
    verified_account,
    xml,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/twilio", include_in_schema=False)

RECORDING_JOB = "fetch_call_recording"
# Call.end_reason -> what after-stream says before hanging up (any other reason: just hang up).
_AFTER_STREAM_MESSAGES = {
    "error": "voice_trouble",
    "voice_unavailable": "voice_unavailable",
    "capacity": "voice_unavailable",
    "plan_limit": "voice_unavailable",
    # The transfer gave up at once and the first after-stream response (which said so) was lost: say it again.
    "transfer_no_answer": "voice_transfer_failed",
}
# Twilio's terminal CallStatus -> Call.status; anything else (ringing, in-progress, ...) is ignored.
_TERMINAL_STATUSES = {
    "completed": "completed",
    "busy": "busy",
    "failed": "failed",
    "no-answer": "no_answer",
    "canceled": "canceled",
}


# E.164 only; Twilio's withheld-caller-ID sentinels also look like numbers.
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
_WITHHELD = {"+266696687", "+7378742833", "+2562533", "+8656696"}


def caller_number(raw: str | None) -> str | None:
    """`From` as a caller's number, or None for anonymous / restricted / sip: / client: callers."""
    if raw and _E164.match(raw) and raw not in _WITHHELD:
        return raw
    return None


def _stream_url(container: VoiceContainer) -> str:
    """The Media Streams WebSocket: the public https:// origin with the wss:// scheme (prod refuses http://)."""
    base = container.settings.voice_public_url or ""
    scheme, _, rest = base.partition("://")
    return f"{'wss' if scheme == 'https' else 'ws'}://{rest}/twilio/media"


def _service(session: AsyncSession, container: VoiceContainer, organisation_id: UUID) -> CallService:
    return CallService(
        session,
        organisation_id,
        container.events,
        container.inbox_publisher,
        adapters=container.adapters,
        settings=container.settings,
    )


async def _answer(
    session: AsyncSession,
    container: VoiceContainer,
    organisation_id: UUID,
    source: str,
    start: Callable[[CallService], Awaitable[Call]],
) -> Response:
    """The call exists from here: connect the media stream when voice is ready for this `source` and the process
    has room, else open a callback ticket and have Twilio say sorry."""
    settings = container.settings
    voice = VoiceSettingsService(
        session,
        organisation_id,
        container.events,
        include_fake=settings.env == "test",
        voice_public_url=settings.voice_public_url,
    )
    readiness = await voice.readiness()
    language = await voice.language()
    reason: str | None = None
    if not (readiness.phone if source == "phone" else readiness.browser):
        reason = "voice_unavailable"
    elif container.active_calls.full:
        reason = "capacity"
    service = _service(session, container, organisation_id)
    call = await start(service)
    if call.status != "ringing" or call.stream_connected_at is not None:
        # A retried (or replayed: signatures carry no timestamp) webhook for a call that has moved on: no new token.
        logger.info("call_webhook_repeated", call_id=str(call.id), source=source)
        return xml(twiml.hangup())
    if (
        reason is None
        and await load_voice_setup(
            session, organisation_id, container.adapters, settings, container.events, readiness=readiness
        )
        is None
    ):
        reason = "voice_unavailable"
    if reason is not None:
        await service.request_callback(call.id, reason)
        logger.info("call_not_connected", call_id=str(call.id), source=source, reason=reason)
        key = unavailable_key("voice_unavailable", call.source, call.caller_e164)
        return xml(twiml.say_and_hangup(strings_for(language)[key], language))
    token = issue_stream_token(settings, call.id, organisation_id)
    action = f"{settings.voice_public_url}/twilio/voice/after-stream?call_id={call.id}"
    logger.info("call_connecting", call_id=str(call.id), source=source)
    return xml(twiml.connect_stream(_stream_url(container), token, action))


@router.post("/voice/inbound")
async def inbound(request: Request, session: SessionDep, container: ContainerDep) -> Response:
    params = await read_form(request)
    # Deliberately not tenant-scoped: the called number's e164 is the public identifier that tells us the
    # organisation (unique across organisations), exactly like the widget's public_key. An unknown or disabled
    # number stores nothing.
    number = (
        await session.execute(select(PhoneNumber).where(PhoneNumber.e164 == params.get("To", "")))
    ).scalar_one_or_none()
    if number is None or not number.enabled:
        # (A known number with a bad signature gets a 403 and an unknown one this 200: the difference is accepted,
        # the numbers are publicly dialable and an unknown number has no token to check against.)
        return xml(twiml.hangup())
    account = await TenantScopedRepository(session, TelephonyAccount, number.organisation_id).get(
        number.telephony_account_id
    )
    token = auth_token(container, account) if account is not None else None
    if token is None or not signature_ok(container, request, token, params):
        return forbidden(request)
    bind_organisation(number.organisation_id)
    call_sid, caller = params.get("CallSid", ""), caller_number(params.get("From"))
    return await _answer(
        session,
        container,
        number.organisation_id,
        "phone",
        lambda service: service.start_phone_call(number, call_sid, caller),
    )


@router.post("/voice/browser")
async def browser(request: Request, session: SessionDep, container: ContainerDep) -> Response:
    params = await read_form(request)
    account = await verified_account(session, container, request, params)
    if account is None:
        return forbidden(request)
    if "callback_ticket" in params:  # an agent's softphone calling a customer back, not a customer's browser
        return await start_browser_callback(session, container, account, params)
    try:
        ticket: CallTicket = verify_call_ticket(container.settings, params.get("ticket", ""))
    except InvalidVoiceTokenError:
        return xml(twiml.hangup())
    if ticket.organisation_id != account.organisation_id:
        return xml(twiml.hangup())
    voice_row = await VoiceSettingsService(
        session,
        account.organisation_id,
        container.events,
        include_fake=container.settings.env == "test",
        voice_public_url=container.settings.voice_public_url,
    ).get()
    if not voice_row.browser_calls_enabled or voice_row.browser_widget_id not in (None, ticket.widget_id):
        return xml(twiml.hangup())
    call_sid = params.get("CallSid", "")
    try:
        return await _answer(
            session,
            container,
            account.organisation_id,
            "browser",
            lambda service: service.start_browser_call(ticket, call_sid),
        )
    except InvalidVoiceTokenError:  # a ticket starts one call only
        logger.info("call_ticket_rejected", organisation_id=str(account.organisation_id))
        return xml(twiml.hangup())


async def _call_from_query(session: AsyncSession, account: TelephonyAccount, request: Request) -> Call | None:
    try:
        call_id = UUID(request.query_params.get("call_id", ""))
    except ValueError:
        return None
    return await TenantScopedRepository(session, Call, account.organisation_id).get(call_id)


@router.post("/voice/after-stream")
async def after_stream(request: Request, session: SessionDep, container: ContainerDep) -> Response:
    """`<Connect action>`: what Twilio plays once the media stream has closed. A call handed to a live transfer
    joins the transfer's conference and the first agent is rung; after an error, the trouble message; when the
    stream was refused (voice no longer ready, or this process full), the unavailable message."""
    params = await read_form(request)
    account = await verified_account(session, container, request, params)
    if account is None:
        return forbidden(request)
    call = await _call_from_query(session, account, request)
    if call is not None:
        transfer = (
            await session.execute(
                TenantScopedRepository(session, CallTransfer, account.organisation_id)
                .select()
                .where(CallTransfer.call_id == call.id)
            )
        ).scalar_one_or_none()
        if transfer is not None and transfer.status == "ringing":
            return await _join_transfer(session, container, account, call, transfer)
    key = _AFTER_STREAM_MESSAGES.get(call.end_reason or "") if call is not None else None
    if call is None or key is None:
        return xml(twiml.hangup())
    conversation = await TenantScopedRepository(session, Conversation, account.organisation_id).get(
        call.conversation_id
    )
    language = conversation.language if conversation is not None else "en"
    caller = call.caller_e164
    if call.end_reason == "plan_limit" and (
        await EntitlementService(session, account.organisation_id, container.adapters).state()
    ).locked:
        caller = None  # a locked account cannot call back: promise only a follow-up
    key = unavailable_key(key, call.source, caller)
    return xml(twiml.say_and_hangup(strings_for(language)[key], language))


async def _join_transfer(
    session: AsyncSession, container: VoiceContainer, account: TelephonyAccount, call: Call, transfer: CallTransfer
) -> Response:
    """The caller waits on hold in the conference while agents are rung (from after the commit), and a watchdog
    gives up at the deadline. Should the transfer give up at once (nobody left to ring), the caller hears the
    failure line here instead of being redirected."""
    organisation_id = account.organisation_id
    ref = account_ref(container, account)
    actions = await _transfer_service(session, container, organisation_id).start(transfer.id)
    giving_up = next(
        (a for a in actions if isinstance(a, RedirectCaller) and a.call_sid == call.provider_call_sid), None
    )
    actions = [a for a in actions if a is not giving_up]
    if ref is not None:
        after_commit(session, lambda: container.run_actions(organisation_id, ref, actions))
    if giving_up is not None:
        return xml(redirect_twiml(giving_up))
    transfer_id, deadline = transfer.id, transfer.deadline_at
    after_commit(session, lambda: container.watch_transfer(transfer_id, organisation_id, deadline))
    base = container.settings.voice_public_url
    hold_url = f"{base}/twilio/voice/hold?transfer_id={transfer_id}"
    status_url = f"{base}/twilio/voice/conference-status?transfer_id={transfer_id}"
    logger.info("call_joining_transfer", call_id=str(call.id), transfer_id=str(transfer_id))
    return xml(twiml.join_conference(transfer_id, hold_url, status_url))


def _transfer_service(session: AsyncSession, container: VoiceContainer, organisation_id: UUID) -> TransferService:
    return TransferService(
        session,
        organisation_id,
        container.events,
        container.inbox_publisher,
        container.adapters,
        container.settings,
        session_factory=container.session_factory,
    )


@router.post("/voice/status")
async def status(request: Request, session: SessionDep, container: ContainerDep) -> Response:
    params = await read_form(request)
    account = await verified_account(session, container, request, params)
    if account is None:
        return forbidden(request)
    final = _TERMINAL_STATUSES.get(params.get("CallStatus", ""))
    if final is None:
        return Response(status_code=204)
    call = (
        await session.execute(
            TenantScopedRepository(session, Call, account.organisation_id)
            .select()
            .where(Call.provider_call_sid == params.get("CallSid", ""))
        )
    ).scalar_one_or_none()
    if call is None:
        return Response(status_code=204)
    try:
        await _caller_left(container, account, call.id)
    except Exception as exc:  # noqa: BLE001 - the call must still be finalised (which also ends its transfer)
        logger.warning("transfer_caller_left_failed", call_id=str(call.id), error_type=type(exc).__name__)
    duration = params.get("CallDuration", "")
    # Stored now; finalised now unless the call's stream session is still running (it then finalises).
    finalised = await _service(session, container, account.organisation_id).provider_ended(
        call.id, final, int(duration) if duration.isdigit() else None
    )
    if finalised:
        VOICE_CALLS.labels(
            source=call.source, mode=mode_label(call.mode), end_reason=call.end_reason or "caller_hangup"
        ).inc()
    return Response(status_code=204)


async def _caller_left(container: VoiceContainer, account: TelephonyAccount, call_id: UUID) -> None:
    """The caller's leg is over: its live transfer (if any) learns it in its own unit of work, committed before the
    call is finalised (lock order transfer -> call everywhere, so the request's session holds no call lock yet).
    A ringing transfer is abandoned and its agent leg ended; a connected one gets its end time."""
    organisation_id = account.organisation_id
    async with UnitOfWork(container.session_factory) as uow:
        actions = await _transfer_service(uow.session, container, organisation_id).caller_left(call_id)
        await uow.commit()
    if actions and (ref := account_ref(container, account)) is not None:
        container.run_actions(organisation_id, ref, actions)


@router.post("/recording/status")
async def recording_status(request: Request, session: SessionDep, container: ContainerDep) -> Response:
    params = await read_form(request)
    account = await verified_account(session, container, request, params)
    if account is None:
        return forbidden(request)
    recording_sid = params.get("RecordingSid", "")
    if params.get("RecordingStatus") == "completed" and recording_sid:
        call = await _call_from_query(session, account, request)
        if call is not None:
            payload = {
                "organisation_id": str(account.organisation_id),
                "call_id": str(call.id),
                "recording_sid": recording_sid,
            }
            duration = params.get("RecordingDuration", "")
            if duration.isascii() and duration.isdecimal() and len(duration) <= 9:
                payload["recording_duration"] = int(duration)
            # Enqueue only once the request's transaction has committed.
            after_commit(session, lambda: container.adapters.job_queue.enqueue(RECORDING_JOB, payload))
    return Response(status_code=204)
