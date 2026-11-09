"""Business hours, timezone, holidays and SLA targets (read: any member, write: org.manage)."""

from fastapi import APIRouter, Depends

from supportapi.container import Container
from supportapi.deps import (
    OrgContext,
    SessionDep,
    build_support_settings_service,
    get_container,
    get_org_context,
    require_permission,
)
from supportapi.schemas.support_settings import SupportSettingsOut, SupportSettingsRequest
from supportcore.db.models import SupportSettings
from supportcore.rbac.permissions import Permission

router = APIRouter(tags=["support-settings"])


def settings_out(row: SupportSettings) -> SupportSettingsOut:
    return SupportSettingsOut.model_validate(row, from_attributes=True)


@router.get("/orgs/{org_id}/support-settings", response_model=SupportSettingsOut)
async def get_support_settings(
    session: SessionDep,
    ctx: OrgContext = Depends(get_org_context),
    container: Container = Depends(get_container),
) -> SupportSettingsOut:
    return settings_out(await build_support_settings_service(session, ctx.organisation.id, container).get())


@router.put("/orgs/{org_id}/support-settings", response_model=SupportSettingsOut)
async def put_support_settings(
    session: SessionDep,
    body: SupportSettingsRequest,
    ctx: OrgContext = Depends(require_permission(Permission.ORG_MANAGE)),
    container: Container = Depends(get_container),
) -> SupportSettingsOut:
    service = build_support_settings_service(session, ctx.organisation.id, container)
    row = await service.update(body.timezone, body.weekly_hours, body.holidays, body.sla_targets, ctx.user)
    return settings_out(row)
