"""The team inbox: reads (queries.answer, analytics.view or bot.configure); claim, (re)assign and the agent actions
(reply, notes, status, hand back, priority, tags) need queries.answer. Reply and note text is never logged."""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from supportapi.container import Container
from supportapi.deps import (
    OrgContext,
    SessionDep,
    build_assignment_service,
    build_conversation_service,
    build_inbox_service,
    build_note_service,
    build_outbound_email_service,
    build_tag_service,
    get_container,
    require_any_permission,
    require_permission,
)
from supportapi.pagination import decode_cursor, encode_cursor
from supportapi.routers.conversations import turns_out
from supportapi.schemas.conversations import EmailMetaOut, MessageOut
from supportapi.schemas.inbox import (
    AssigneeOut,
    CallDetailOut,
    EmailHtmlOut,
    InboxCountsOut,
    InboxDetailOut,
    InboxPage,
    InboxSummaryOut,
    MentionOut,
    MessagesPage,
    NoteOut,
    ReplyIn,
)
from supportapi.schemas.tags import ConversationTagsIn, TagOut
from supportcore.db.models import Conversation
from supportcore.db.models.team import Membership
from supportcore.inbox.notes import NoteView
from supportcore.inbox.summary import InboxSummary
from supportcore.mail.views import EmailMeta, attachment_file, email_meta_for, sanitized_html
from supportcore.rbac.permissions import Permission
from supportcore.voice.recordings import recording_of_conversation

router = APIRouter(tags=["inbox"])

AnswerQueries = Depends(require_permission(Permission.QUERIES_ANSWER))
ViewInbox = Depends(require_any_permission(Permission.QUERIES_ANSWER, Permission.ANALYTICS_VIEW, Permission.BOT_CONFIGURE))


class AssigneeRequest(BaseModel):
    membership_id: UUID | None  # required; null unassigns


class TextIn(BaseModel):
    text: str = Field(max_length=20000)  # the service trims and checks the limit (reply 10000, note 5000)


class StatusIn(BaseModel):
    status: Literal["resolved", "closed", "open"]  # open = reopen (from resolved); hand back has its own route


class PriorityIn(BaseModel):
    priority: str = Field(max_length=16)  # an unknown one is 422 invalid_priority


class CallbackOut(BaseModel):
    id: UUID
    callback_status: str | None


class ConversationAssignmentOut(BaseModel):
    id: UUID
    status: str
    priority: str
    assignee_membership_id: UUID | None
    first_response_due_at: datetime | None
    resolution_due_at: datetime | None


def _out(conversation: Conversation) -> ConversationAssignmentOut:
    return ConversationAssignmentOut(
        id=conversation.id,
        status=conversation.status,
        priority=conversation.priority,
        assignee_membership_id=conversation.assignee_membership_id,
        first_response_due_at=conversation.first_response_due_at,
        resolution_due_at=conversation.resolution_due_at,
    )


def _member(ctx: OrgContext) -> Membership:
    if ctx.membership is None:  # a platform admin who is not a member cannot take conversations
        raise HTTPException(status_code=403, detail="Not a member of this organisation")
    return ctx.membership


@router.post("/orgs/{org_id}/inbox/conversations/{conversation_id}/claim", response_model=ConversationAssignmentOut)
async def claim_conversation(
    conversation_id: UUID,
    session: SessionDep,
    ctx: OrgContext = AnswerQueries,
    container: Container = Depends(get_container),
) -> ConversationAssignmentOut:
    service = build_assignment_service(session, ctx.organisation.id, container)
    return _out(await service.claim(conversation_id, _member(ctx)))


@router.put("/orgs/{org_id}/inbox/conversations/{conversation_id}/assignee", response_model=ConversationAssignmentOut)
async def set_assignee(
    conversation_id: UUID,
    body: AssigneeRequest,
    session: SessionDep,
    ctx: OrgContext = AnswerQueries,
    container: Container = Depends(get_container),
) -> ConversationAssignmentOut:
    service = build_assignment_service(session, ctx.organisation.id, container)
    return _out(await service.assign(conversation_id, body.membership_id, _member(ctx)))


def _summary_out(summary: InboxSummary) -> InboxSummaryOut:
    return InboxSummaryOut.model_validate(summary, from_attributes=True)


def _email_out(meta: EmailMeta | None) -> EmailMetaOut | None:
    return EmailMetaOut.model_validate(meta, from_attributes=True) if meta is not None else None


def _messages_out(messages: list[Any], emails: dict[UUID, EmailMeta]) -> list[MessageOut]:
    return [
        MessageOut.model_validate(m, from_attributes=True).model_copy(update={"email": _email_out(emails.get(m.id))})
        for m in messages
    ]


def _viewer_id(ctx: OrgContext) -> UUID | None:
    return ctx.membership.id if ctx.membership is not None else None


@router.get("/orgs/{org_id}/inbox/conversations", response_model=InboxPage)
async def list_inbox(
    session: SessionDep,
    view: Annotated[str, Query(max_length=16)] = "all",
    status: Annotated[str | None, Query(max_length=32)] = None,
    channel: Annotated[str | None, Query(max_length=16)] = None,
    assignee: Annotated[str | None, Query(max_length=40)] = None,
    priority: Annotated[str | None, Query(max_length=16)] = None,
    tag_id: UUID | None = None,
    kb_id: UUID | None = None,
    include_test: bool = False,
    q: Annotated[str | None, Query(max_length=1000)] = None,
    callback: Annotated[str | None, Query(max_length=16)] = None,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    ctx: OrgContext = ViewInbox,
    container: Container = Depends(get_container),
) -> InboxPage:
    service = build_inbox_service(session, ctx.organisation.id, container)
    page = await service.list(
        view,
        status=status,
        channel=channel,
        assignee=assignee,
        priority=priority,
        tag_id=tag_id,
        kb_id=kb_id,
        include_test=include_test,
        q=q,
        callback=callback,
        cursor=decode_cursor(cursor) if cursor is not None else None,
        limit=limit,
        viewer_membership_id=_viewer_id(ctx),
    )
    next_cursor = encode_cursor(*page.next_cursor) if page.next_cursor is not None else None
    return InboxPage(items=[_summary_out(s) for s in page.items], next_cursor=next_cursor)


@router.get("/orgs/{org_id}/inbox/counts", response_model=InboxCountsOut)
async def inbox_counts(
    session: SessionDep, ctx: OrgContext = ViewInbox, container: Container = Depends(get_container)
) -> InboxCountsOut:
    service = build_inbox_service(session, ctx.organisation.id, container)
    return InboxCountsOut(**await service.counts(_viewer_id(ctx)))


@router.get("/orgs/{org_id}/inbox/conversations/{conversation_id}", response_model=InboxDetailOut)
async def get_inbox_conversation(
    conversation_id: UUID,
    session: SessionDep,
    ctx: OrgContext = ViewInbox,
    container: Container = Depends(get_container),
) -> InboxDetailOut:
    detail = await build_inbox_service(session, ctx.organisation.id, container).detail(conversation_id)
    return InboxDetailOut(
        summary=_summary_out(detail.summary),
        messages=_messages_out(detail.messages, detail.emails),
        has_more=detail.has_more,
        notes=[_note_out(n) for n in detail.notes],
        turns=turns_out(detail.turns, detail.chunks),
        history=[_summary_out(h) for h in detail.history],
        calls=[CallDetailOut.model_validate(c) for c in detail.calls],
    )


@router.get("/orgs/{org_id}/inbox/conversations/{conversation_id}/messages", response_model=MessagesPage)
async def older_messages(
    conversation_id: UUID,
    before: datetime,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    ctx: OrgContext = ViewInbox,
    container: Container = Depends(get_container),
) -> MessagesPage:
    if before.tzinfo is None:
        before = before.replace(tzinfo=UTC)
    window = await build_inbox_service(session, ctx.organisation.id, container).messages_before(
        conversation_id, before, limit
    )
    return MessagesPage(items=_messages_out(window.items, window.emails), has_more=window.has_more)


# --- agent actions -----------------------------------------------------------------------------------------


def _note_out(note: NoteView) -> NoteOut:
    author = (
        AssigneeOut(membership_id=note.author_membership_id, name=note.author_name)
        if note.author_membership_id is not None
        else None
    )
    mentions = [MentionOut(membership_id=m.membership_id, name=m.name) for m in note.mentions]
    return NoteOut(id=note.id, author=author, text=note.text, mentions=mentions, created_at=note.created_at)


@router.post(
    "/orgs/{org_id}/inbox/conversations/{conversation_id}/messages", response_model=MessageOut, status_code=201
)
async def reply(
    conversation_id: UUID,
    body: ReplyIn,
    session: SessionDep,
    ctx: OrgContext = AnswerQueries,
    container: Container = Depends(get_container),
) -> MessageOut:
    """The reply goes to the widget and, on an email conversation (or a chat one with `send_email`), out by email
    once the transaction commits. 409 email_not_available when it should be emailed but cannot be."""
    member = _member(ctx)
    service = build_conversation_service(session, ctx.organisation.id, container)
    conversation = await service.locked(conversation_id)
    outbound = build_outbound_email_service(session, ctx.organisation.id, container)
    emailed = await outbound.ensure_can_email(conversation, body.send_email)
    message = await service.add_agent_message(conversation, member, body.text)
    out = MessageOut.model_validate(message, from_attributes=True)
    if emailed:
        await outbound.queue_reply(conversation, message)
        meta = await email_meta_for(session, ctx.organisation.id, [message.id])
        out = out.model_copy(update={"email": _email_out(meta.get(message.id))})
    return out


@router.post("/orgs/{org_id}/inbox/conversations/{conversation_id}/callback/done", response_model=CallbackOut)
async def callback_done(
    conversation_id: UUID,
    session: SessionDep,
    ctx: OrgContext = AnswerQueries,
    container: Container = Depends(get_container),
) -> CallbackOut:
    """A person called the customer back: `requested` -> `done`. 409 invalid_conversation_state otherwise."""
    service = build_conversation_service(session, ctx.organisation.id, container)
    conversation = await service.mark_callback_done(await service.locked(conversation_id))
    return CallbackOut(id=conversation.id, callback_status=conversation.callback_status)


@router.post(
    "/orgs/{org_id}/inbox/conversations/{conversation_id}/messages/{message_id}/email/retry",
    response_model=EmailMetaOut,
)
async def retry_email(
    conversation_id: UUID,
    message_id: UUID,
    session: SessionDep,
    ctx: OrgContext = AnswerQueries,
    container: Container = Depends(get_container),
) -> EmailMetaOut:
    """Sends a failed email reply again (same Message-ID). 409 email_not_retryable unless it failed."""
    email = await build_outbound_email_service(session, ctx.organisation.id, container).retry(
        conversation_id, message_id
    )
    meta = (await email_meta_for(session, ctx.organisation.id, [email.message_id]))[email.message_id]
    return EmailMetaOut.model_validate(meta, from_attributes=True)


@router.get(
    "/orgs/{org_id}/inbox/conversations/{conversation_id}/messages/{message_id}/email-html",
    response_model=EmailHtmlOut,
)
async def email_html(
    conversation_id: UUID, message_id: UUID, session: SessionDep, ctx: OrgContext = ViewInbox
) -> EmailHtmlOut:
    """The message's sanitised HTML. 404 when it has none."""
    return EmailHtmlOut(html=await sanitized_html(session, ctx.organisation.id, conversation_id, message_id))


@router.get("/orgs/{org_id}/inbox/conversations/{conversation_id}/attachments/{attachment_id}")
async def download_attachment(
    conversation_id: UUID,
    attachment_id: UUID,
    session: SessionDep,
    ctx: OrgContext = ViewInbox,
    container: Container = Depends(get_container),
) -> Response:
    """The attachment's bytes, always as a download (never rendered inline)."""
    found = await attachment_file(session, ctx.organisation.id, conversation_id, attachment_id)
    await session.commit()  # release the connection before the storage read
    data = await container.adapters.storage.get(found.storage_key)
    return Response(
        content=data,
        media_type=found.content_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(found.filename or 'attachment', safe='')}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


_MAX_RANGE_DIGITS = 15
RECORDING_CHUNK_BYTES = 1024 * 1024


def _byte_range(header: str | None, size: int) -> tuple[int, int] | None | Literal["unsatisfiable"]:
    """The inclusive (start, end) a single `Range: bytes=a-b` / `a-` / `-n` asks for. None when the header is absent,
    malformed or asks for several ranges (the whole object is served); "unsatisfiable" when it starts past the end."""
    if not header or not header.startswith("bytes=") or "," in header:
        return None
    first, _, last = header[6:].strip().partition("-")

    def number(text: str) -> bool:
        return text.isascii() and text.isdecimal() and len(text) <= _MAX_RANGE_DIGITS

    if not ((number(first) and (last == "" or number(last))) or (first == "" and number(last))):
        return None
    if first == "":  # the last n bytes
        n = int(last)
        return (max(size - n, 0), size - 1) if n and size else "unsatisfiable"
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size:
        return "unsatisfiable"
    return None if end < start else (start, end)


async def _stream_range(storage: Any, key: str, start: int, end: int) -> Any:
    """The object's bytes start..end (inclusive) in bounded chunks, so a long recording never sits in memory."""
    position = start
    while position <= end:
        chunk_end = min(position + RECORDING_CHUNK_BYTES - 1, end)
        yield await storage.get_range(key, position, chunk_end)
        position = chunk_end + 1


@router.api_route("/orgs/{org_id}/inbox/conversations/{conversation_id}/recording", methods=["GET", "HEAD"])
async def call_recording(
    request: Request,
    conversation_id: UUID,
    session: SessionDep,
    ctx: OrgContext = ViewInbox,
    container: Container = Depends(get_container),
    range_header: Annotated[str | None, Header(alias="Range")] = None,
    call_id: UUID | None = None,
) -> Response:
    """The recording of `call_id` (a call of this conversation; default: the inbound call), streamed inline in chunks. A single `Range: bytes=a-b` is answered with 206 so the player
    can seek; HEAD answers with the headers only."""
    recording = await recording_of_conversation(session, ctx.organisation.id, conversation_id, call_id)
    storage_key, content_type = recording.storage_key, recording.content_type
    await session.commit()  # release the connection before the storage reads
    storage = container.adapters.storage
    size = await storage.stat_size(storage_key)
    headers = {
        "Content-Disposition": "inline",
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Accept-Ranges": "bytes",
    }
    wanted = _byte_range(range_header, size)
    if wanted == "unsatisfiable":
        return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
    status = 200
    start, end = 0, size - 1
    if wanted is not None:
        status, (start, end) = 206, wanted
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(max(end - start + 1, 0))
    if request.method == "HEAD":
        return Response(status_code=status, media_type=content_type, headers=headers)
    return StreamingResponse(
        _stream_range(storage, storage_key, start, end), status_code=status, media_type=content_type, headers=headers
    )


@router.post("/orgs/{org_id}/inbox/conversations/{conversation_id}/notes", response_model=NoteOut, status_code=201)
async def add_note(
    conversation_id: UUID,
    body: TextIn,
    session: SessionDep,
    ctx: OrgContext = AnswerQueries,
    container: Container = Depends(get_container),
) -> NoteOut:
    member = _member(ctx)
    note = await build_note_service(session, ctx.organisation.id, container).add(conversation_id, member, body.text)
    return _note_out(note)


@router.post(
    "/orgs/{org_id}/inbox/conversations/{conversation_id}/status", response_model=ConversationAssignmentOut
)
async def set_status(
    conversation_id: UUID,
    body: StatusIn,
    session: SessionDep,
    ctx: OrgContext = AnswerQueries,
    container: Container = Depends(get_container),
) -> ConversationAssignmentOut:
    member = _member(ctx)
    service = build_conversation_service(session, ctx.organisation.id, container)
    return _out(await service.transition(await service.locked(conversation_id), body.status, member))


@router.post(
    "/orgs/{org_id}/inbox/conversations/{conversation_id}/hand-back", response_model=ConversationAssignmentOut
)
async def hand_back(
    conversation_id: UUID,
    session: SessionDep,
    ctx: OrgContext = AnswerQueries,
    container: Container = Depends(get_container),
) -> ConversationAssignmentOut:
    member = _member(ctx)
    service = build_conversation_service(session, ctx.organisation.id, container)
    return _out(await service.transition(await service.locked(conversation_id), "bot", member))


@router.put(
    "/orgs/{org_id}/inbox/conversations/{conversation_id}/priority", response_model=ConversationAssignmentOut
)
async def set_priority(
    conversation_id: UUID,
    body: PriorityIn,
    session: SessionDep,
    ctx: OrgContext = AnswerQueries,
    container: Container = Depends(get_container),
) -> ConversationAssignmentOut:
    _member(ctx)
    service = build_conversation_service(session, ctx.organisation.id, container)
    return _out(await service.set_priority(await service.locked(conversation_id), body.priority))


@router.put("/orgs/{org_id}/inbox/conversations/{conversation_id}/tags", response_model=list[TagOut])
async def set_conversation_tags(
    conversation_id: UUID,
    body: ConversationTagsIn,
    session: SessionDep,
    ctx: OrgContext = AnswerQueries,
    container: Container = Depends(get_container),
) -> list[TagOut]:
    _member(ctx)
    tags = await build_tag_service(session, ctx.organisation.id, container).set_tags(conversation_id, body.tag_ids)
    return [TagOut(id=t.id, name=t.name) for t in tags]
