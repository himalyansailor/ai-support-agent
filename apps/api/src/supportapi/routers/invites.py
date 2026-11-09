from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from supportapi.container import Container
from supportapi.deps import (
    OrgContext,
    SessionDep,
    get_auth_service,
    get_container,
    get_optional_user,
    require_permission,
)
from supportapi.routers.auth import set_refresh_cookie
from supportapi.schemas.auth import UserOut
from supportapi.schemas.team import (
    AcceptInviteRequest,
    AcceptInviteResponse,
    CreateInviteRequest,
    InviteOut,
    InvitePreviewOut,
    InvitePreviewRequest,
)
from supportcore.db.models import Invite, Role, User
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.identity.service import AuthService
from supportcore.rbac.permissions import Permission
from supportcore.team.invites import InviteService, accept_invite, preview_invite

router = APIRouter(tags=["invites"])

_manage_team = require_permission(Permission.TEAM_MANAGE)


def _service(ctx: OrgContext, session: AsyncSession, container: Container) -> InviteService:
    return InviteService(
        session,
        ctx.organisation.id,
        container.events,
        container.email_sender,
        container.settings,
        adapters=container.adapters,
    )


async def _role_names(session: AsyncSession, org_id: UUID) -> dict[UUID, str]:
    roles = await TenantScopedRepository(session, Role, org_id).list(limit=1000)
    return {r.id: r.name for r in roles}


def _out(invite: Invite, role_names: dict[UUID, str]) -> InviteOut:
    return InviteOut(
        id=invite.id,
        email=invite.email,
        role_id=invite.role_id,
        role_name=role_names[invite.role_id],
        invited_by_user_id=invite.invited_by_user_id,
        expires_at=invite.expires_at,
        created_at=invite.created_at,
    )


@router.post("/orgs/{org_id}/invites", response_model=InviteOut, status_code=201)
async def create_invite(
    session: SessionDep,
    body: CreateInviteRequest,
    ctx: OrgContext = Depends(_manage_team),
    container: Container = Depends(get_container),
) -> InviteOut:
    invite = await _service(ctx, session, container).create(body.email, body.role_id, ctx.user)
    await session.refresh(invite, ["created_at"])
    return _out(invite, await _role_names(session, ctx.organisation.id))


@router.get("/orgs/{org_id}/invites", response_model=list[InviteOut])
async def list_invites(
    session: SessionDep,
    ctx: OrgContext = Depends(_manage_team),
    container: Container = Depends(get_container),
) -> list[InviteOut]:
    invites = await _service(ctx, session, container).list_pending()
    names = await _role_names(session, ctx.organisation.id)
    return [_out(i, names) for i in invites]


@router.delete("/orgs/{org_id}/invites/{invite_id}", status_code=204)
async def revoke_invite(
    session: SessionDep,
    invite_id: UUID,
    ctx: OrgContext = Depends(_manage_team),
    container: Container = Depends(get_container),
) -> Response:
    await _service(ctx, session, container).revoke(invite_id, ctx.user)
    return Response(status_code=204)


# POST with the token in the body, not GET /invites/{token}: request paths end up in spans and
# access logs, and the raw token is a bearer credential.
@router.post("/invites/preview", response_model=InvitePreviewOut)
async def preview(session: SessionDep, body: InvitePreviewRequest) -> InvitePreviewOut:
    preview = await preview_invite(session, body.token)
    return InvitePreviewOut(
        organisation_name=preview.organisation_name, email=preview.email, role_name=preview.role_name
    )


@router.post("/invites/accept", response_model=AcceptInviteResponse)
async def accept(
    session: SessionDep,
    body: AcceptInviteRequest,
    request: Request,
    response: Response,
    user: User | None = Depends(get_optional_user),
    container: Container = Depends(get_container),
    auth: AuthService = Depends(get_auth_service),
) -> AcceptInviteResponse:
    result = await accept_invite(
        session, container.events, auth, body.token, current_user=user, name=body.name, password=body.password
    )
    if result.auth is not None:
        set_refresh_cookie(response, request.app.state.settings, result.auth)
    return AcceptInviteResponse(
        organisation_id=result.membership.organisation_id,
        membership_id=result.membership.id,
        user=UserOut.model_validate(result.user),
        access_token=result.auth.access_token if result.auth is not None else None,
    )
