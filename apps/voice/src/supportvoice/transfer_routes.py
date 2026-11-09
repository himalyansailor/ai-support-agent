"""The live-transfer webhooks: the caller's hold music, and the agent legs' answer, accept and status callbacks.

Each checks Twilio's signature first (`webhooks.verified_account`); the transfer is found by its id within the
verified account's organisation (another organisation's transfer is not found). The request's unit of work commits
before the response; the Twilio work it returns runs after that commit (`container.run_actions`). Nothing here logs a
phone number, token or spoken text."""

from uuid import UUID

import structlog
from fastapi import APIRouter, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from supportcore.db.models import CallTransfer, Organisation, TelephonyAccount
from supportcore.db.session import after_commit
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import NotFoundError
from supportcore.voice.strings import strings_for
from supportcore.voice.transfer_actions import agent_leg_url
from supportcore.voice.transfers import (
    ACCEPT_GATHER_SECONDS,
    TransferAction,
    TransferService,
)

from supportvoice import twiml
from supportvoice.container import VoiceContainer
from supportvoice.webhooks import (
    ContainerDep,
    SessionDep,
    account_ref,
    forbidden,
    read_form,
    verified_account,
    xml,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/twilio", include_in_schema=False)

_PARTICIPANT_JOIN = "participant-join"
# An agent leg's terminal CallStatus values (its status callback asks for `completed` events only).
_LEG_OVER = frozenset({"completed", "busy", "failed", "no-answer", "canceled"})


def _transfer_id(request: Request) -> UUID | None:
    try:
        return UUID(request.query_params.get("transfer_id", ""))
    except ValueError:
        return None


def _leg(request: Request) -> tuple[UUID, int] | None:
    """(transfer id, attempt) from the agent leg's URL, or None when malformed."""
    transfer_id = _transfer_id(request)
    attempt = request.query_params.get("attempt", "")
    if transfer_id is None or not (attempt.isascii() and attempt.isdecimal() and len(attempt) <= 3):
        return None
    return transfer_id, int(attempt)


def _service(session: AsyncSession, container: VoiceContainer, organisation_id: UUID) -> TransferService:
    return TransferService(
        session,
        organisation_id,
        container.events,
        container.inbox_publisher,
        container.adapters,
        container.settings,
        session_factory=container.session_factory,
    )


def _run_after_commit(
    session: AsyncSession, container: VoiceContainer, account: TelephonyAccount, actions: list[TransferAction]
) -> None:
    ref = account_ref(container, account)
    if actions and ref is not None:
        organisation_id = account.organisation_id
        after_commit(session, lambda: container.run_actions(organisation_id, ref, actions))


async def _organisation(session: AsyncSession, organisation_id: UUID) -> Organisation:
    return (await session.execute(select(Organisation).where(Organisation.id == organisation_id))).scalar_one()


async def _taken(session: AsyncSession, organisation_id: UUID) -> Response:
    language = (await _organisation(session, organisation_id)).language
    return xml(twiml.say_and_hangup(strings_for(language)["voice_agent_taken"], language))


@router.post("/voice/hold")
async def hold(request: Request, session: SessionDep, container: ContainerDep) -> Response:
    """The conference's `waitUrl`: the hold track while the transfer exists, else a short pause (a waitUrl cannot
    hang up)."""
    params = await read_form(request)
    account = await verified_account(session, container, request, params)
    if account is None:
        return forbidden(request)
    transfer_id = _transfer_id(request)
    transfer = (
        await TenantScopedRepository(session, CallTransfer, account.organisation_id).get(transfer_id)
        if transfer_id is not None
        else None
    )
    return xml(twiml.play_hold() if transfer is not None else twiml.hold_pause())


@router.post("/voice/agent-answer")
async def agent_answer(request: Request, session: SessionDep, container: ContainerDep) -> Response:
    """The agent leg picked up: a browser joins the room at once; a phone is asked to press 1 (voicemail must not
    accept); a leg whose transfer has moved on hears that the call was taken."""
    params = await read_form(request)
    account = await verified_account(session, container, request, params)
    if account is None:
        return forbidden(request)
    leg = _leg(request)
    if leg is None:
        return xml(twiml.hangup())
    transfer_id, attempt = leg
    try:
        outcome = await _service(session, container, account.organisation_id).agent_answered(transfer_id, attempt)
    except NotFoundError:
        return xml(twiml.hangup())
    logger.info("transfer_agent_answered", transfer_id=str(transfer_id), attempt=attempt, outcome=outcome)
    if outcome == "join":
        return xml(twiml.agent_conference(transfer_id))
    if outcome == "gather":
        organisation = await _organisation(session, account.organisation_id)
        language = organisation.language
        whisper = strings_for(language)["voice_agent_whisper"].format(organisation=organisation.name)
        action = agent_leg_url(container.settings, "agent-accept", transfer_id, attempt)
        return xml(twiml.gather_accept(whisper, language, action, ACCEPT_GATHER_SECONDS))
    return await _taken(session, account.organisation_id)


@router.post("/voice/agent-accept")
async def agent_accept(request: Request, session: SessionDep, container: ContainerDep) -> Response:
    """The phone leg's `<Gather>` result: "1" joins when this agent is still the one being rung, else the call was
    taken; any other key hangs up (the leg's status callback then rings the next agent)."""
    params = await read_form(request)
    account = await verified_account(session, container, request, params)
    if account is None:
        return forbidden(request)
    leg = _leg(request)
    if leg is None or params.get("Digits") != "1":
        return xml(twiml.hangup())
    transfer_id, attempt = leg
    try:
        accepted, actions = await _service(session, container, account.organisation_id).accept(transfer_id, attempt)
    except NotFoundError:
        return xml(twiml.hangup())
    _run_after_commit(session, container, account, actions)
    if accepted:
        return xml(twiml.agent_conference(transfer_id))
    return await _taken(session, account.organisation_id)


@router.post("/voice/agent-status")
async def agent_status(request: Request, session: SessionDep, container: ContainerDep) -> Response:
    """The agent leg ended: when it was the one being rung (no answer, busy, declined, voicemail without "1",
    failed), the next agent rings or the transfer gives up."""
    params = await read_form(request)
    account = await verified_account(session, container, request, params)
    if account is None:
        return forbidden(request)
    leg = _leg(request)
    call_status = params.get("CallStatus", "")
    if leg is None or call_status not in _LEG_OVER:
        return Response(status_code=204)
    transfer_id, attempt = leg
    try:
        actions = await _service(session, container, account.organisation_id).leg_ended(
            transfer_id, attempt, call_status, refused=call_status == "failed"
        )
    except NotFoundError:
        return Response(status_code=204)
    _run_after_commit(session, container, account, actions)
    return Response(status_code=204)


@router.post("/voice/conference-status")
async def conference_status(request: Request, session: SessionDep, container: ContainerDep) -> Response:
    """The room's status callback (set by the caller's `<Conference>`): a participant joined. When it is the
    connected agent's leg, the agent is in the room (an agent leg that ends before this gives the transfer up)."""
    params = await read_form(request)
    account = await verified_account(session, container, request, params)
    if account is None:
        return forbidden(request)
    transfer_id = _transfer_id(request)
    if transfer_id is None or params.get("StatusCallbackEvent") != _PARTICIPANT_JOIN:
        return Response(status_code=204)
    try:
        await _service(session, container, account.organisation_id).agent_joined(
            transfer_id, params.get("CallSid", "")
        )
    except NotFoundError:
        pass
    return Response(status_code=204)
