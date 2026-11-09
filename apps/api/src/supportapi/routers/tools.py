"""Business tools: HTTP endpoints of the organisation's own API that the bot may call (bot.configure)."""

from dataclasses import asdict
from uuid import UUID

from fastapi import APIRouter, Depends, Response

from supportapi.container import Container
from supportapi.deps import OrgContext, SessionDep, build_tool_service, get_container, require_permission
from supportapi.schemas.tools import CreateToolRequest, ToolTestRequest, ToolOut, ToolResultOut, UpdateToolRequest
from supportcore.db.models import BusinessTool
from supportcore.rbac.permissions import Permission

router = APIRouter(tags=["tools"])


def tool_out(tool: BusinessTool) -> ToolOut:
    return ToolOut.model_validate(tool, from_attributes=True)


@router.get("/orgs/{org_id}/tools", response_model=list[ToolOut])
async def list_tools(
    session: SessionDep,
    ctx: OrgContext = Depends(require_permission(Permission.BOT_CONFIGURE)),
    container: Container = Depends(get_container),
) -> list[ToolOut]:
    return [tool_out(t) for t in await build_tool_service(session, ctx.organisation.id, container).list()]


@router.post("/orgs/{org_id}/tools", response_model=ToolOut, status_code=201)
async def create_tool(
    session: SessionDep,
    body: CreateToolRequest,
    ctx: OrgContext = Depends(require_permission(Permission.BOT_CONFIGURE)),
    container: Container = Depends(get_container),
) -> ToolOut:
    service = build_tool_service(session, ctx.organisation.id, container)
    return tool_out(await service.create(actor=ctx.user, **body.model_dump()))


@router.get("/orgs/{org_id}/tools/{tool_id}", response_model=ToolOut)
async def get_tool(
    session: SessionDep,
    tool_id: UUID,
    ctx: OrgContext = Depends(require_permission(Permission.BOT_CONFIGURE)),
    container: Container = Depends(get_container),
) -> ToolOut:
    return tool_out(await build_tool_service(session, ctx.organisation.id, container).get(tool_id))


@router.patch("/orgs/{org_id}/tools/{tool_id}", response_model=ToolOut)
async def update_tool(
    session: SessionDep,
    tool_id: UUID,
    body: UpdateToolRequest,
    ctx: OrgContext = Depends(require_permission(Permission.BOT_CONFIGURE)),
    container: Container = Depends(get_container),
) -> ToolOut:
    service = build_tool_service(session, ctx.organisation.id, container)
    return tool_out(await service.update(tool_id, actor=ctx.user, **body.model_dump(exclude_unset=True)))


@router.delete("/orgs/{org_id}/tools/{tool_id}", status_code=204)
async def delete_tool(
    session: SessionDep,
    tool_id: UUID,
    ctx: OrgContext = Depends(require_permission(Permission.BOT_CONFIGURE)),
    container: Container = Depends(get_container),
) -> Response:
    await build_tool_service(session, ctx.organisation.id, container).delete(tool_id, ctx.user)
    return Response(status_code=204)


@router.post("/orgs/{org_id}/tools/{tool_id}/test", response_model=ToolResultOut)
async def test_tool(
    session: SessionDep,
    tool_id: UUID,
    body: ToolTestRequest,
    ctx: OrgContext = Depends(require_permission(Permission.BOT_CONFIGURE)),
    container: Container = Depends(get_container),
) -> ToolResultOut:
    result = await build_tool_service(session, ctx.organisation.id, container).test(tool_id, body.arguments)
    return ToolResultOut(**asdict(result))
