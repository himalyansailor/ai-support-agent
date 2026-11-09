from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from supportapi.container import Container
from supportapi.deps import OrgContext, SessionDep, get_container, get_current_user, require_permission
from supportapi.schemas.roles import CreateRoleRequest, PermissionOut, RoleOut, UpdateRoleRequest
from supportcore.db.models import Role, User
from supportcore.rbac.permissions import PERMISSION_LABELS, Permission
from supportcore.roles.service import RoleService

router = APIRouter(tags=["roles"])

_manage_team = require_permission(Permission.TEAM_MANAGE)


def _service(ctx: OrgContext, session: AsyncSession, container: Container) -> RoleService:
    return RoleService(session, ctx.organisation.id, container.events)


def _out(role: Role, member_count: int) -> RoleOut:
    return RoleOut(
        id=role.id,
        name=role.name,
        permissions=list(role.permissions),
        is_system=role.is_system,
        member_count=member_count,
    )


@router.get("/permissions", response_model=list[PermissionOut])
async def list_permissions(_: User = Depends(get_current_user)) -> list[PermissionOut]:
    return [PermissionOut(key=p.value, label=PERMISSION_LABELS[p]) for p in Permission]


@router.get("/orgs/{org_id}/roles", response_model=list[RoleOut])
async def list_roles(
    session: SessionDep,
    ctx: OrgContext = Depends(_manage_team),
    container: Container = Depends(get_container),
) -> list[RoleOut]:
    svc = _service(ctx, session, container)
    counts = await svc.member_counts()
    return [_out(r, counts.get(r.id, 0)) for r in await svc.list()]


@router.post("/orgs/{org_id}/roles", response_model=RoleOut, status_code=201)
async def create_role(
    session: SessionDep,
    body: CreateRoleRequest,
    ctx: OrgContext = Depends(_manage_team),
    container: Container = Depends(get_container),
) -> RoleOut:
    role = await _service(ctx, session, container).create(
        body.name, body.permissions, ctx.user, actor_permissions=ctx.permissions
    )
    return _out(role, 0)


@router.patch("/orgs/{org_id}/roles/{role_id}", response_model=RoleOut)
async def update_role(
    session: SessionDep,
    role_id: UUID,
    body: UpdateRoleRequest,
    ctx: OrgContext = Depends(_manage_team),
    container: Container = Depends(get_container),
) -> RoleOut:
    svc = _service(ctx, session, container)
    role = await svc.update(role_id, body.name, body.permissions, ctx.user, actor_permissions=ctx.permissions)
    return _out(role, (await svc.member_counts()).get(role.id, 0))


@router.delete("/orgs/{org_id}/roles/{role_id}", status_code=204)
async def delete_role(
    session: SessionDep,
    role_id: UUID,
    ctx: OrgContext = Depends(_manage_team),
    container: Container = Depends(get_container),
) -> Response:
    await _service(ctx, session, container).delete(role_id, ctx.user, actor_permissions=ctx.permissions)
    return Response(status_code=204)
