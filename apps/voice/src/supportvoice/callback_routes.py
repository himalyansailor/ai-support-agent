"""The callback webhooks: an agent calling a customer back from the platform.

`start_browser_callback` is the agent branch of `/twilio/voice/browser` (the agent's softphone placed the call with a
`callback_ticket`). `callback-bridge` is the TwiML of the agent's phone once it answers, and `callback-done` is the
`<Dial action>` that marks an answered callback done. Each checks Twilio's signature first (`webhooks.verified_account`),
and the call is found by its id within the verified account's organisation. Nothing here logs a phone number, token or
spoken text."""

from uuid import UUID

import structlog
from fastapi import APIRouter, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from supportcore.billing.errors import PlanLimitError
from supportcore.db.models import (
    Call,
    Conversation,
    Membership,
    Organisation,
    TelephonyAccount,
)
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import NotFoundError
from supportcore.voice.callbacks import CallbackPlan, CallbackService
from supportcore.voice.errors import (
    InvalidVoiceTokenError,
    NoCallbackNumberError,
    VoiceNotAvailableError,
)
from supportcore.voice.recordings import recording_status_url
from supportcore.voice.softphone import agent_identity
from supportcore.voice.strings import strings_for
from supportcore.voice.tokens import verify_callback_ticket

from supportvoice import twiml
from supportvoice.container import VoiceContainer
from supportvoice.webhooks import (
    ContainerDep,
    SessionDep,
    forbidden,
    read_form,
    verified_account,
    xml,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/twilio", include_in_schema=False)


def _service(session: AsyncSession, container: VoiceContainer, organisation_id: UUID) -> CallbackService:
    return CallbackService(
        session, organisation_id, container.events, container.inbox_publisher, container.adapters, container.settings
    )


def _dial(container: VoiceContainer, plan: CallbackPlan, call_id: UUID, *, say: str | None = None, language: str = "en") -> str:
    base = container.settings.voice_public_url
    return twiml.dial_customer(
        plan.customer_e164,
        plan.from_e164,
        f"{base}/twilio/voice/callback-done?call_id={call_id}",
        recording_status_url=recording_status_url(base, call_id) if plan.record else None,
        say=say,
        language=language,
    )


async def start_browser_callback(
    session: AsyncSession,
    container: VoiceContainer,
    account: TelephonyAccount,
    params: dict[str, str],
) -> Response:
    """`/voice/browser` with a `callback_ticket`: the ticket is the agent's, for one conversation and one call (its
    `From` must be that agent's softphone identity); the customer is dialled from the organisation's number."""
    organisation_id = account.organisation_id
    try:
        ticket = verify_callback_ticket(container.settings, params.get("callback_ticket", ""))
    except InvalidVoiceTokenError:
        return xml(twiml.hangup())
    if ticket.organisation_id != organisation_id or params.get("From") != f"client:{agent_identity(ticket.membership_id)}":
        return xml(twiml.hangup())
    membership = await TenantScopedRepository(session, Membership, organisation_id).get(ticket.membership_id)
    if membership is None or membership.status != "active":
        return xml(twiml.hangup())
    service = _service(session, container, organisation_id)
    try:
        plan = await service.prepare(ticket.conversation_id, membership)
        call = await service.start_outbound(
            ticket.conversation_id, membership, call_sid=params.get("CallSid", ""), ticket_jti=ticket.jti
        )
    except (InvalidVoiceTokenError, NotFoundError, NoCallbackNumberError, VoiceNotAvailableError, PlanLimitError) as exc:
        logger.info("callback_rejected", organisation_id=str(organisation_id), reason=type(exc).__name__)
        return xml(twiml.hangup())
    logger.info("callback_dialling", call_id=str(call.id), conversation_id=str(call.conversation_id))
    return xml(_dial(container, plan, call.id))


def _call_id(request: Request) -> UUID | None:
    try:
        return UUID(request.query_params.get("call_id", ""))
    except ValueError:
        return None


@router.post("/voice/callback-bridge")
async def callback_bridge(request: Request, session: SessionDep, container: ContainerDep) -> Response:
    """The agent's phone answered a callback: tell them, then dial the customer."""
    params = await read_form(request)
    account = await verified_account(session, container, request, params)
    if account is None:
        return forbidden(request)
    organisation_id = account.organisation_id
    call_id = _call_id(request)
    repository = TenantScopedRepository(session, Call, organisation_id)
    call = await repository.get(call_id) if call_id is not None else None
    if call is None or call.source != "callback" or call.finalised or call.agent_membership_id is None:
        return xml(twiml.hangup())
    membership = await TenantScopedRepository(session, Membership, organisation_id).get(call.agent_membership_id)
    conversation = await TenantScopedRepository(session, Conversation, organisation_id).get(call.conversation_id)
    if membership is None or conversation is None:
        return xml(twiml.hangup())
    try:
        plan = await _service(session, container, organisation_id).prepare(conversation.id, membership)
    except (NotFoundError, NoCallbackNumberError, VoiceNotAvailableError):
        return xml(twiml.hangup())
    # The agent hears this line: the organisation's language (like the transfer whisper), not the customer's.
    organisation = (await session.execute(select(Organisation).where(Organisation.id == organisation_id))).scalar_one()
    language = organisation.language
    logger.info("callback_bridging", call_id=str(call.id))
    return xml(_dial(container, plan, call.id, say=strings_for(language)["voice_callback_connecting"], language=language))


@router.post("/voice/callback-done")
async def callback_done(request: Request, session: SessionDep, container: ContainerDep) -> Response:
    """The callback's `<Dial action>`: an answered call (`DialCallStatus=completed`) marks the callback done."""
    params = await read_form(request)
    account = await verified_account(session, container, request, params)
    if account is None:
        return forbidden(request)
    call_id = _call_id(request)
    if call_id is not None:
        try:
            await _service(session, container, account.organisation_id).dial_result(
                call_id, params.get("DialCallStatus", "")
            )
        except NotFoundError:
            pass
    return xml(twiml.hangup())
