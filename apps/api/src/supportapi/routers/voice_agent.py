"""An agent's browser softphone: the access token that receives transferred calls, and the presence heartbeat.
Also calling a customer back: a ticket for the agent's browser, or a call that rings the agent's phone first."""

from contextlib import suppress
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from supportcore.analytics.integration_errors import (
    error_type_of,
    record_integration_error,
)
from supportcore.db.uow import UnitOfWork
from supportcore.rbac.permissions import Permission
from supportcore.team.service import E164_PATTERN
from supportcore.voice.callbacks import (
    CALLBACK_RING_SECONDS,
    CallbackService,
    browser_calling_ready,
)
from supportcore.voice.errors import (
    AgentUnreachableError,
    TelephonyUnavailableError,
    VoiceNotAvailableError,
)
from supportcore.voice.softphone import (
    beat_softphone,
    clear_softphone,
    issue_agent_token,
)
from supportcore.voice.telephony.ports import CallWebhooks, TelephonyAuthError
from supportcore.voice.tokens import issue_callback_ticket

from supportapi.container import Container
from supportapi.deps import (
    OrgContext,
    SessionDep,
    SessionFactoryDep,
    _inbox_publisher,
    get_container,
    require_permission,
)
from supportapi.schemas.voice_agent import (
    AgentCallTokenOut,
    CallbackCallOut,
    CallbackTicketOut,
)

router = APIRouter(prefix="/orgs/{org_id}/voice/agent", tags=["voice-agent"])

Answer = Depends(require_permission(Permission.QUERIES_ANSWER))


def _membership_id(ctx: OrgContext):  # a platform admin who is not a member has no softphone
    if ctx.membership is None:
        raise HTTPException(status_code=403, detail="Not a member of this organisation")
    return ctx.membership.id


# The token and the callback ticket are credentials: never stored by a browser or proxy cache.
_NO_STORE = "no-store"


@router.post("/token", response_model=AgentCallTokenOut)
async def create_token(
    response: Response, session: SessionDep, ctx: OrgContext = Answer, container: Container = Depends(get_container)
) -> AgentCallTokenOut:
    _membership_id(ctx)
    response.headers["Cache-Control"] = _NO_STORE
    issued = await issue_agent_token(
        session, ctx.organisation.id, ctx.membership, container.adapters, container.settings
    )
    return AgentCallTokenOut(token=issued.token, identity=issued.identity, expires_in=issued.expires_in)


@router.put("/softphone", status_code=204)
async def heartbeat(ctx: OrgContext = Answer, container: Container = Depends(get_container)) -> Response:
    await beat_softphone(container.adapters, ctx.organisation.id, _membership_id(ctx))
    return Response(status_code=204)


@router.delete("/softphone", status_code=204)
async def clear(ctx: OrgContext = Answer, container: Container = Depends(get_container)) -> Response:
    await clear_softphone(container.adapters, ctx.organisation.id, _membership_id(ctx))
    return Response(status_code=204)


def _callbacks(session, organisation_id: UUID, container: Container) -> CallbackService:
    return CallbackService(
        session, organisation_id, container.events, _inbox_publisher(container), container.adapters, container.settings
    )


@router.post("/callbacks/{conversation_id}/ticket", response_model=CallbackTicketOut)
async def callback_ticket(
    conversation_id: UUID,
    response: Response,
    session: SessionDep,
    ctx: OrgContext = Answer,
    container: Container = Depends(get_container),
) -> CallbackTicketOut:
    """The agent's browser calls the customer back: this ticket rides along as the call's `callback_ticket`."""
    _membership_id(ctx)
    callbacks = _callbacks(session, ctx.organisation.id, container)
    await callbacks.prepare(conversation_id, ctx.membership)  # 404 / 409s now, not mid-call
    await callbacks.check_voice_minutes()  # 402 now too
    if not await browser_calling_ready(session, ctx.organisation.id):
        raise VoiceNotAvailableError("Browser calling is not set up")
    token, _jti = issue_callback_ticket(container.settings, ctx.organisation.id, ctx.membership.id, conversation_id)
    response.headers["Cache-Control"] = _NO_STORE
    return CallbackTicketOut(ticket=token)


@router.post("/callbacks/{conversation_id}/call", response_model=CallbackCallOut, status_code=202)
async def callback_by_phone(
    conversation_id: UUID,
    session: SessionDep,
    factory: SessionFactoryDep,
    ctx: OrgContext = Answer,
    container: Container = Depends(get_container),
) -> CallbackCallOut:
    """Rings the agent's phone from the organisation's number; once they answer, the customer is dialled and the two
    are bridged. The call row is committed first and no session is open while Twilio is called."""
    _membership_id(ctx)
    membership, organisation_id = ctx.membership, ctx.organisation.id
    service = _callbacks(session, organisation_id, container)
    plan = await service.prepare(conversation_id, membership)
    agent_phone = membership.phone_number
    if not agent_phone or not E164_PATTERN.fullmatch(agent_phone):
        raise AgentUnreachableError("Add your phone number to your profile to be called")
    call = await service.start_outbound(conversation_id, membership, call_sid=None, ticket_jti=None)
    call_id = call.id
    await session.commit()  # the call exists before Twilio rings; nothing is held while it is called
    base = container.settings.voice_public_url
    webhooks = CallWebhooks(
        answer_url=f"{base}/twilio/voice/callback-bridge?call_id={call_id}",
        status_callback=f"{base}/twilio/voice/status",
    )
    try:
        sid = await container.adapters.telephony.create_call(
            plan.account_ref, agent_phone, plan.from_e164, webhooks, CALLBACK_RING_SECONDS
        )
    except Exception as exc:  # noqa: BLE001  (any refusal: the row is closed and the agent told)
        error_type = "auth_failed" if isinstance(exc, TelephonyAuthError) else getattr(exc, "error_type", None)
        error_type = error_type or error_type_of(exc)
        await record_integration_error(factory, organisation_id, "voice", "twilio", error_type)
        async with UnitOfWork(factory) as uow:
            await _callbacks(uow.session, organisation_id, container).failed(call_id)
            await uow.commit()
        raise TelephonyUnavailableError("Twilio could not place the call; try again later") from None
    try:
        async with UnitOfWork(factory) as uow:
            await _callbacks(uow.session, organisation_id, container).set_sid(call_id, sid)
            await uow.commit()
    except Exception:
        # The sid is not stored, so nothing else could stop the agent's leg: end it rather than let it ring orphaned.
        with suppress(Exception):
            await container.adapters.telephony.update_call(plan.account_ref, sid, end=True)
        raise
    return CallbackCallOut(call_id=call_id)

