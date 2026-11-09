"""The organisation's conversation tags. Listing is part of viewing the inbox (queries.answer, analytics.view or
bot.configure: the inbox filters by tag); creating, renaming and deleting need queries.answer."""

from uuid import UUID

from fastapi import APIRouter, Depends, Response

from supportapi.container import Container
from supportapi.deps import OrgContext, SessionDep, build_tag_service, get_container, require_any_permission, require_permission
from supportapi.schemas.tags import TagIn, TagOut
from supportcore.rbac.permissions import Permission

router = APIRouter(tags=["tags"])

AnswerQueries = Depends(require_permission(Permission.QUERIES_ANSWER))
ViewInbox = Depends(require_any_permission(Permission.QUERIES_ANSWER, Permission.ANALYTICS_VIEW, Permission.BOT_CONFIGURE))


@router.get("/orgs/{org_id}/tags", response_model=list[TagOut])
async def list_tags(
    session: SessionDep, ctx: OrgContext = ViewInbox, container: Container = Depends(get_container)
) -> list[TagOut]:
    return [TagOut(id=t.id, name=t.name) for t in await build_tag_service(session, ctx.organisation.id, container).list()]


@router.post("/orgs/{org_id}/tags", response_model=TagOut, status_code=201)
async def create_tag(
    body: TagIn, session: SessionDep, ctx: OrgContext = AnswerQueries, container: Container = Depends(get_container)
) -> TagOut:
    tag = await build_tag_service(session, ctx.organisation.id, container).create(body.name)
    return TagOut(id=tag.id, name=tag.name)


@router.patch("/orgs/{org_id}/tags/{tag_id}", response_model=TagOut)
async def rename_tag(
    tag_id: UUID,
    body: TagIn,
    session: SessionDep,
    ctx: OrgContext = AnswerQueries,
    container: Container = Depends(get_container),
) -> TagOut:
    tag = await build_tag_service(session, ctx.organisation.id, container).rename(tag_id, body.name)
    return TagOut(id=tag.id, name=tag.name)


@router.delete("/orgs/{org_id}/tags/{tag_id}", status_code=204)
async def delete_tag(
    tag_id: UUID, session: SessionDep, ctx: OrgContext = AnswerQueries, container: Container = Depends(get_container)
) -> Response:
    await build_tag_service(session, ctx.organisation.id, container).delete(tag_id)
    return Response(status_code=204)
