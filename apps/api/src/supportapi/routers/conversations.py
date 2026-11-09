"""Conversations, read-only, for the dashboard (queries.answer, analytics.view or bot.configure)."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from supportapi.container import Container
from supportapi.deps import OrgContext, SessionDep, build_conversation_service, get_container, require_any_permission
from supportapi.pagination import decode_cursor, encode_cursor
from supportapi.schemas.conversations import (
    ConversationDetailOut,
    ConversationPage,
    ConversationSummaryOut,
    CustomerOut,
    MessageOut,
    TurnChunkOut,
    TurnOut,
)
from supportcore.conversations.service import ConversationAdminRow, ConversationTrace
from supportcore.db.models import BotTurn
from supportcore.db.models.conversation import CONVERSATION_STATUSES
from supportcore.errors import NotFoundError
from supportcore.rbac.permissions import Permission

router = APIRouter(tags=["conversations"])

ReadConversations = Depends(
    require_any_permission(Permission.QUERIES_ANSWER, Permission.ANALYTICS_VIEW, Permission.BOT_CONFIGURE)
)


def _summary(row: ConversationAdminRow) -> dict[str, Any]:
    c, customer = row.conversation, row.customer
    return {
        "id": c.id,
        "status": c.status,
        "channel": c.channel,
        "customer": CustomerOut(
            id=customer.id, name=customer.name, email=customer.email, identified=customer.external_id is not None
        ),
        "last_message_preview": row.last_message_preview,
        "escalation": c.escalation,
        "csat_score": c.csat_score,
        "last_message_at": c.last_message_at,
        "is_test": c.is_test,
    }


def turns_out(turns: list[BotTurn], chunks: dict[UUID, list[dict[str, Any]]]) -> list[TurnOut]:
    return [
        TurnOut(
            id=t.id,
            message_id=t.message_id,
            reply_message_id=t.reply_message_id,
            outcome=t.outcome,
            no_answer=t.no_answer,
            risk=t.risk,
            chunks=[TurnChunkOut(**chunk) for chunk in chunks[t.id]],
            warnings=list((t.retrieval or {}).get("warnings") or []),
            timings_ms=dict((t.retrieval or {}).get("timings_ms") or {}),
            tool_calls=t.tool_calls,
            model=t.model,
            latency_ms=t.latency_ms,
            trace_id=t.trace_id,
            created_at=t.created_at,
        )
        for t in turns
    ]


@router.get("/orgs/{org_id}/conversations", response_model=ConversationPage)
async def list_conversations(
    session: SessionDep,
    status: Annotated[str | None, Query(max_length=32)] = None,
    widget_id: UUID | None = None,
    include_test: bool = False,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ctx: OrgContext = ReadConversations,
    container: Container = Depends(get_container),
) -> ConversationPage:
    if status is not None and status not in CONVERSATION_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {', '.join(CONVERSATION_STATUSES)}")
    after = decode_cursor(cursor) if cursor is not None else None
    service = build_conversation_service(session, ctx.organisation.id, container)
    rows = await service.admin_list(
        status=status, widget_id=widget_id, include_test=include_test, after=after, limit=limit + 1
    )
    page = rows[:limit]
    next_cursor = (
        encode_cursor(page[-1].conversation.last_message_at, page[-1].conversation.id) if len(rows) > limit else None
    )
    return ConversationPage(items=[ConversationSummaryOut(**_summary(r)) for r in page], next_cursor=next_cursor)


@router.get("/orgs/{org_id}/conversations/{conversation_id}", response_model=ConversationDetailOut)
async def get_conversation(
    session: SessionDep,
    conversation_id: UUID,
    ctx: OrgContext = ReadConversations,
    container: Container = Depends(get_container),
) -> ConversationDetailOut:
    service = build_conversation_service(session, ctx.organisation.id, container)
    trace: ConversationTrace | None = await service.admin_detail(conversation_id)
    if trace is None:
        raise NotFoundError("Conversation not found")
    c = trace.row.conversation
    return ConversationDetailOut(
        **_summary(trace.row),
        language=c.language,
        contact_email=c.contact_email,
        csat_comment=c.csat_comment,
        created_at=c.created_at,
        messages=[MessageOut.model_validate(m, from_attributes=True) for m in trace.messages],
        turns=turns_out(trace.turns, trace.chunks),
    )
