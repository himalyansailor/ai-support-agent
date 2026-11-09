"""The organisation's saved replies. Using and managing them both need queries.answer."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from supportapi.deps import OrgContext, SessionDep, require_permission
from supportapi.schemas.saved_replies import SavedReplyIn, SavedReplyOut, SavedReplyPatch
from supportcore.db.models import SavedReply
from supportcore.inbox.saved_replies import SavedReplyService
from supportcore.rbac.permissions import Permission

router = APIRouter(tags=["saved-replies"])

AnswerQueries = Depends(require_permission(Permission.QUERIES_ANSWER))


def _out(r: SavedReply) -> SavedReplyOut:
    return SavedReplyOut(
        id=r.id, title=r.title, body=r.body, shortcut=r.shortcut, created_by_membership_id=r.created_by_membership_id
    )


@router.get("/orgs/{org_id}/saved-replies", response_model=list[SavedReplyOut])
async def list_saved_replies(
    session: SessionDep, q: str | None = Query(default=None, max_length=200), ctx: OrgContext = AnswerQueries
) -> list[SavedReplyOut]:
    return [_out(r) for r in await SavedReplyService(session, ctx.organisation.id).list(q)]


@router.post("/orgs/{org_id}/saved-replies", response_model=SavedReplyOut, status_code=201)
async def create_saved_reply(body: SavedReplyIn, session: SessionDep, ctx: OrgContext = AnswerQueries) -> SavedReplyOut:
    reply = await SavedReplyService(session, ctx.organisation.id).create(
        body.title,
        body.body,
        body.shortcut,
        created_by_membership_id=ctx.membership.id if ctx.membership else None,
    )
    return _out(reply)


@router.get("/orgs/{org_id}/saved-replies/{reply_id}", response_model=SavedReplyOut)
async def get_saved_reply(reply_id: UUID, session: SessionDep, ctx: OrgContext = AnswerQueries) -> SavedReplyOut:
    return _out(await SavedReplyService(session, ctx.organisation.id).get(reply_id))


@router.patch("/orgs/{org_id}/saved-replies/{reply_id}", response_model=SavedReplyOut)
async def update_saved_reply(
    reply_id: UUID, body: SavedReplyPatch, session: SessionDep, ctx: OrgContext = AnswerQueries
) -> SavedReplyOut:
    fields = {k: getattr(body, k) for k in body.model_fields_set}
    return _out(await SavedReplyService(session, ctx.organisation.id).update(reply_id, **fields))


@router.delete("/orgs/{org_id}/saved-replies/{reply_id}", status_code=204)
async def delete_saved_reply(reply_id: UUID, session: SessionDep, ctx: OrgContext = AnswerQueries) -> Response:
    await SavedReplyService(session, ctx.organisation.id).delete(reply_id)
    return Response(status_code=204)
