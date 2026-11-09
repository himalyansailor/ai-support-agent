from dataclasses import asdict
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from supportapi.container import Container
from supportapi.deps import OrgContext, SessionDep, get_container, get_org_context, require_any_permission
from supportapi.schemas.team import MemberOut, UpdateMemberRequest
from supportcore.db.models import Membership
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import NotFoundError
from supportcore.rbac.checker import has_permission
from supportcore.rbac.permissions import Permission
from supportcore.team.service import UNSET, MemberService, MemberView

router = APIRouter(tags=["team"])

# Agents need the member list to reassign conversations.
_list_members = require_any_permission(Permission.TEAM_MANAGE, Permission.QUERIES_ANSWER)

_SELF_EDITABLE = {"phone_number", "call_preference"}


def _out(view: MemberView) -> MemberOut:
    return MemberOut(**asdict(view))


@router.get("/orgs/{org_id}/members", response_model=list[MemberOut])
async def list_members(
    session: SessionDep,
    ctx: OrgContext = Depends(_list_members),
    container: Container = Depends(get_container),
) -> list[MemberOut]:
    views = await MemberService(session, ctx.organisation.id, container.events).list()
    return [_out(v) for v in views]


@router.patch("/orgs/{org_id}/members/{membership_id}", response_model=MemberOut)
async def update_member(
    session: SessionDep,
    membership_id: UUID,
    body: UpdateMemberRequest,
    ctx: OrgContext = Depends(get_org_context),
    container: Container = Depends(get_container),
) -> MemberOut:
    fields = body.model_fields_set
    # Existence in this org first, so a foreign id is always 404, never 403.
    if await TenantScopedRepository(session, Membership, ctx.organisation.id).get(membership_id) is None:
        raise NotFoundError("Member not found")
    if not has_permission(ctx.permissions, Permission.TEAM_MANAGE):
        # Without team.manage a member may only edit their own phone number and call preference.
        if ctx.membership is None or membership_id != ctx.membership.id or not fields <= _SELF_EDITABLE:
            raise HTTPException(status_code=403, detail=f"Missing permission: {Permission.TEAM_MANAGE.value}")
    view = await MemberService(session, ctx.organisation.id, container.events).update(
        membership_id,
        role_id=body.role_id,
        status=body.status,
        phone_number=body.phone_number if "phone_number" in fields else UNSET,
        call_preference=body.call_preference if body.call_preference is not None else UNSET,
        actor=ctx.user,
    )
    return _out(view)
