"""A team member's own notifications. Any member of the org may use theirs; a platform admin who is not a member
has none (403)."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from supportapi.deps import OrgContext, SessionDep, get_org_context
from supportapi.schemas.notifications import MarkReadIn, MarkReadOut, NotificationOut, UnreadCountOut
from supportcore.inbox.notifications import NotificationService

router = APIRouter(tags=["notifications"])


def _service(session: SessionDep, ctx: OrgContext) -> tuple[NotificationService, UUID]:
    if ctx.membership is None:
        raise HTTPException(status_code=403, detail="Not a member of this organisation")
    return NotificationService(session, ctx.organisation.id), ctx.membership.id


@router.get("/orgs/{org_id}/notifications", response_model=list[NotificationOut])
async def list_notifications(
    session: SessionDep, unread_only: bool = Query(default=False), ctx: OrgContext = Depends(get_org_context)
) -> list[NotificationOut]:
    service, membership_id = _service(session, ctx)
    return [NotificationOut(**n.__dict__) for n in await service.list(membership_id, unread_only=unread_only)]


@router.get("/orgs/{org_id}/notifications/unread-count", response_model=UnreadCountOut)
async def unread_count(session: SessionDep, ctx: OrgContext = Depends(get_org_context)) -> UnreadCountOut:
    service, membership_id = _service(session, ctx)
    return UnreadCountOut(unread=await service.unread_count(membership_id))


@router.post("/orgs/{org_id}/notifications/read", response_model=MarkReadOut)
async def mark_read(body: MarkReadIn, session: SessionDep, ctx: OrgContext = Depends(get_org_context)) -> MarkReadOut:
    service, membership_id = _service(session, ctx)
    return MarkReadOut(updated=await service.mark_read(membership_id, None if body.ids == "all" else body.ids))
