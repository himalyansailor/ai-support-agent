"""Public widget API (/widget/v1). No dashboard auth: the widget's public key selects the tenant for
config and sessions, a widget session token identifies the customer for everything else. Every route
is rate limited through the RateLimiter port (429 rate_limited with Retry-After)."""

import hmac
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from supportapi.client_ip import client_ip
from supportapi.container import Container
from supportapi.deps import (
    SessionDep,
    build_conversation_service,
    build_widget_session_service,
    get_container,
    get_widget_claims,
)
from supportapi.schemas.widget_public import (
    ContactRequest,
    AuthorOut,
    CallTokenOut,
    ConversationOut,
    CsatRequest,
    FramePolicyOut,
    MessageOut,
    PublicConfigOut,
    SessionOut,
    StartSessionRequest,
)
from supportcore.conversations.service import ConversationService
from supportcore.db.models import Organisation
from supportcore.inbox.events import message_payload
from supportcore.ratelimit.ports import enforce
from supportcore.voice.browser_calls import issue_browser_call
from supportcore.voice.errors import VoiceNotAvailableError
from supportcore.widget.limits import (
    CALL_TOKEN_PER_SESSION,
    CONFIG_PER_IP,
    CONVERSATION_CREATE_PER_CUSTOMER,
    SESSION_PER_IP,
)
from supportcore.widget.tokens import WidgetClaims

router = APIRouter(prefix="/widget/v1", tags=["widget-public"])


async def _limit_ip(request: Request, container: Container, route: str, limit_window: tuple[int, int]) -> None:
    ip = client_ip(request, container.settings.trusted_proxy_hops)
    await enforce(container.adapters.rate_limiter, f"ip:{route}:{ip}", limit_window)


def _trusted_forwarded_ip(request: Request, container: Container) -> str | None:
    """X-Support-Client-IP, honoured only with a valid X-Support-Internal-Token."""
    expected = container.settings.internal_api_token
    token = request.headers.get("x-support-internal-token")
    forwarded = request.headers.get("x-support-client-ip", "").strip()[:64]
    if expected is None or not token or not forwarded:
        return None
    if not hmac.compare_digest(token.encode(), expected.get_secret_value().encode()):
        return None
    return forwarded


@router.get("/config", response_model=PublicConfigOut)
async def get_config(
    request: Request,
    session: SessionDep,
    key: str = Query(max_length=100),
    preview: bool = Query(False),  # the dashboard preview: no Call button (`/calls/token` refuses preview sessions)
    container: Container = Depends(get_container),
) -> PublicConfigOut:
    await _limit_ip(request, container, "config", CONFIG_PER_IP)
    config = await build_widget_session_service(session, container).public_config(key, preview=preview)
    return PublicConfigOut(**vars(config))


@router.get("/frame-policy", response_model=FramePolicyOut)
async def get_frame_policy(
    request: Request,
    session: SessionDep,
    key: str = Query(max_length=100),
    container: Container = Depends(get_container),
) -> FramePolicyOut:
    """The CSP frame-ancestors sources for the iframe page; an unknown or disabled key gets 'none'."""
    forwarded = _trusted_forwarded_ip(request, container)
    if forwarded is not None:  # the web server's proxy, vouching for the visitor's IP
        await enforce(container.adapters.rate_limiter, f"ip:frame-policy:{forwarded}", CONFIG_PER_IP)
    else:
        await _limit_ip(request, container, "frame-policy", CONFIG_PER_IP)
    return FramePolicyOut(frame_ancestors=await build_widget_session_service(session, container).frame_policy(key))


async def _conversation_out(service: ConversationService, conversation: Any) -> ConversationOut:
    name = await service.assignee_name(conversation)
    return ConversationOut.model_validate(conversation).model_copy(
        update={"agent": AuthorOut(name=name) if name is not None else None}
    )


@router.post("/sessions", response_model=SessionOut)
async def start_session(
    request: Request,
    session: SessionDep,
    body: StartSessionRequest,
    container: Container = Depends(get_container),
) -> SessionOut:
    await _limit_ip(request, container, "sessions", SESSION_PER_IP)
    started = await build_widget_session_service(session, container).start(
        body.key,
        parent_origin=body.parent_origin,
        visitor_id=body.visitor_id,
        customer_token=body.customer_token,
        is_preview=body.preview,
        preview_ticket=body.preview_ticket,
    )
    return SessionOut(
        token=started.token,
        customer_id=started.customer_id,
        visitor_id=started.visitor_id,
        expires_in=container.settings.widget_session_ttl_seconds,
        conversation=await _conversation_out(
            build_conversation_service(session, started.conversation.organisation_id, container), started.conversation
        )
        if started.conversation
        else None,
    )


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(
    request: Request,
    session: SessionDep,
    claims: WidgetClaims = Depends(get_widget_claims),
    container: Container = Depends(get_container),
) -> list[ConversationOut]:
    await _limit_ip(request, container, "conversations", CONFIG_PER_IP)
    service = build_conversation_service(session, claims.organisation_id, container)
    return [await _conversation_out(service, c) for c in await service.list_for_customer(claims.customer_id)]


@router.post("/conversations", response_model=ConversationOut, status_code=201)
async def create_conversation(
    session: SessionDep,
    claims: WidgetClaims = Depends(get_widget_claims),
    container: Container = Depends(get_container),
) -> ConversationOut:
    await enforce(
        container.adapters.rate_limiter, f"cust:{claims.customer_id}:conv", CONVERSATION_CREATE_PER_CUSTOMER
    )
    organisation = await session.get(Organisation, claims.organisation_id)
    language = organisation.language if organisation is not None else "en"
    service = build_conversation_service(session, claims.organisation_id, container)
    conversation = await service.create(claims.customer_id, claims.widget_id, language, claims.preview)
    await session.refresh(conversation)  # server-generated timestamps
    return await _conversation_out(service, conversation)


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(
    request: Request,
    session: SessionDep,
    conversation_id: UUID,
    after: datetime | None = None,
    claims: WidgetClaims = Depends(get_widget_claims),
    container: Container = Depends(get_container),
) -> list[MessageOut]:
    await _limit_ip(request, container, "messages", CONFIG_PER_IP)
    service = build_conversation_service(session, claims.organisation_id, container)
    conversation = await service.get_for_customer(conversation_id, claims.customer_id)
    return [
        MessageOut.model_validate(message_payload(m))
        for m in await service.messages(conversation.id, after=after)
    ]


@router.post("/conversations/{conversation_id}/end", response_model=ConversationOut)
async def end_conversation(
    request: Request,
    session: SessionDep,
    conversation_id: UUID,
    claims: WidgetClaims = Depends(get_widget_claims),
    container: Container = Depends(get_container),
) -> ConversationOut:
    await _limit_ip(request, container, "end", CONFIG_PER_IP)
    service = build_conversation_service(session, claims.organisation_id, container)
    await service.get_for_customer(conversation_id, claims.customer_id)  # 404 unless it is this customer's
    conversation = await service.locked(conversation_id)  # a concurrent reply, claim or hand back waits
    return await _conversation_out(service, await service.end(conversation))


@router.post("/conversations/{conversation_id}/csat", response_model=ConversationOut)
async def submit_csat(
    request: Request,
    session: SessionDep,
    conversation_id: UUID,
    body: CsatRequest,
    claims: WidgetClaims = Depends(get_widget_claims),
    container: Container = Depends(get_container),
) -> ConversationOut:
    await _limit_ip(request, container, "csat", CONFIG_PER_IP)
    service = build_conversation_service(session, claims.organisation_id, container)
    conversation = await service.get_for_customer(conversation_id, claims.customer_id)
    return await _conversation_out(service, await service.rate(conversation, body.score, body.comment))


@router.post("/conversations/{conversation_id}/contact", response_model=ConversationOut)
async def set_contact_email(
    request: Request,
    session: SessionDep,
    conversation_id: UUID,
    body: ContactRequest,
    claims: WidgetClaims = Depends(get_widget_claims),
    container: Container = Depends(get_container),
) -> ConversationOut:
    await _limit_ip(request, container, "contact", CONFIG_PER_IP)
    service = build_conversation_service(session, claims.organisation_id, container)
    await service.get_for_customer(conversation_id, claims.customer_id)  # 404 unless it is this customer's
    conversation = await service.locked(conversation_id)
    return await _conversation_out(service, await service.set_contact_email(conversation, body.email))


@router.post("/calls/token", response_model=CallTokenOut)
async def create_call_token(
    session: SessionDep,
    claims: WidgetClaims = Depends(get_widget_claims),
    container: Container = Depends(get_container),
) -> CallTokenOut:
    """A Twilio access token (identity `customer-{customer_id}`) and the single-use call ticket the browser sends as
    the call's `ticket` parameter. 409 voice_not_available when browser calls are not ready for this widget."""
    if claims.preview:  # a ticket carries no preview flag: a preview call would open a real (non-test) conversation
        raise VoiceNotAvailableError("Voice calls are not available in the preview")
    # Per session: the widget session token is bound to one customer.
    await enforce(container.adapters.rate_limiter, f"cust:{claims.customer_id}:call-token", CALL_TOKEN_PER_SESSION)
    issued = await issue_browser_call(
        session,
        claims.organisation_id,
        claims.widget_id,
        claims.customer_id,
        container.adapters,
        container.settings,
        container.events,
    )
    return CallTokenOut(token=issued.token, ticket=issued.ticket, expires_in=issued.expires_in)
