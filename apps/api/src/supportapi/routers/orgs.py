from fastapi import APIRouter, Depends

from supportapi.container import Container
from supportapi.deps import (
    OrgContext,
    SessionDep,
    get_container,
    get_current_user,
    get_org_context,
    require_permission,
)
from supportapi.schemas.orgs import CreateOrgRequest, OrgOut, UpdateOrgRequest
from supportcore.db.models import User
from supportcore.orgs.service import OrganisationService
from supportcore.rbac.permissions import Permission

router = APIRouter(prefix="/orgs", tags=["orgs"])


@router.post("", response_model=OrgOut, status_code=201)
async def create_org(
    session: SessionDep,
    body: CreateOrgRequest,
    user: User = Depends(get_current_user),
    container: Container = Depends(get_container),
) -> OrgOut:
    org = await OrganisationService(session, container.events).create(user, body.name)
    return OrgOut.model_validate(org)


@router.get("/{org_id}", response_model=OrgOut)
async def get_org(ctx: OrgContext = Depends(get_org_context)) -> OrgOut:
    return OrgOut.model_validate(ctx.organisation)


@router.patch("/{org_id}", response_model=OrgOut)
async def update_org(
    session: SessionDep,
    body: UpdateOrgRequest,
    ctx: OrgContext = Depends(require_permission(Permission.ORG_MANAGE)),
    container: Container = Depends(get_container),
) -> OrgOut:
    org = await OrganisationService(session, container.events).set_language(ctx.organisation, body.language, ctx.user)
    return OrgOut.model_validate(org)
