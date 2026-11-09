"""`InboxSummary`: one inbox row, also the `conversation` of a `conversation.upsert` event.

Loaded for many conversations at once (a handful of queries, never one per row). Customer names, emails and
message text appear in the summary itself (the preview) but are never logged.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import exists, func
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.db.models import (
    Call,
    CallRecording,
    CallTransfer,
    Conversation,
    ConversationTag,
    Customer,
    Membership,
    Message,
    Tag,
    User,
)
from supportcore.db.tenancy import TenantScopedRepository

__all__ = ["InboxSummary", "PREVIEW_LENGTH", "inbox_summary", "load_call_views", "load_summaries"]

PREVIEW_LENGTH = 120


@dataclass(frozen=True)
class InboxSummary:
    id: UUID
    status: str
    channel: str
    priority: str
    assignee: dict[str, Any] | None  # {membership_id, name}
    customer: dict[str, Any]  # {id, name, email, phone, identified}
    last_message_preview: str | None
    last_message_role: str | None
    last_message_at: datetime
    escalation: dict[str, Any] | None
    tags: list[dict[str, Any]]  # [{id, name}]
    sla: dict[str, Any]  # {first_response_due_at, resolution_due_at, first_response_at, breached: [..]}
    # The conversation has an assignee and the newest non-system message is the customer's, so it is newer than
    # the assignee's last reply (or there is none).
    unread_for_assignee: bool
    is_test: bool
    # The conversation's change counter (`conversations.version`): clients drop an upsert older than what they hold.
    version: int
    # Email conversations: the first mail's subject; the customer's contact email (also set on chat follow-ups).
    subject: str | None = None
    contact_email: str | None = None
    # Voice: `requested` | `done` | None, the number to call back, and the call's meta (None for other channels):
    # {id, source, mode, status, end_reason, started_at, duration_seconds, has_recording}. Inbox viewers only.
    callback_status: str | None = None
    callback_phone: str | None = None
    call: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        """The JSON-ready form carried by `conversation.upsert`."""

        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value is not None else None

        return {
            "id": str(self.id),
            "status": self.status,
            "channel": self.channel,
            "priority": self.priority,
            "assignee": {**self.assignee, "membership_id": str(self.assignee["membership_id"])}
            if self.assignee
            else None,
            "customer": {**self.customer, "id": str(self.customer["id"])},
            "last_message_preview": self.last_message_preview,
            "last_message_role": self.last_message_role,
            "last_message_at": iso(self.last_message_at),
            "escalation": self.escalation,
            "tags": [{"id": str(t["id"]), "name": t["name"]} for t in self.tags],
            "sla": {
                "first_response_due_at": iso(self.sla["first_response_due_at"]),
                "resolution_due_at": iso(self.sla["resolution_due_at"]),
                "first_response_at": iso(self.sla["first_response_at"]),
                "breached": list(self.sla["breached"]),
            },
            "unread_for_assignee": self.unread_for_assignee,
            "is_test": self.is_test,
            "version": self.version,
            "subject": self.subject,
            "contact_email": self.contact_email,
            "callback_status": self.callback_status,
            "callback_phone": self.callback_phone,
            "call": {**self.call, "id": str(self.call["id"]), "started_at": iso(self.call["started_at"])}
            if self.call
            else None,
        }


async def load_summaries(session: AsyncSession, organisation_id: UUID, conversation_ids: Sequence[UUID]) -> list[InboxSummary]:
    """Summaries of the organisation's conversations among `conversation_ids`, in that order (missing or
    other-organisation ids are skipped)."""
    if not conversation_ids:
        return []
    ids = list(conversation_ids)
    rows = (
        await session.execute(
            TenantScopedRepository(session, Conversation, organisation_id)
            .select(Conversation, Customer, User.name)
            .join(
                Customer,
                (Customer.id == Conversation.customer_id) & (Customer.organisation_id == Conversation.organisation_id),
            )
            .outerjoin(
                Membership,
                (Membership.id == Conversation.assignee_membership_id)
                & (Membership.organisation_id == Conversation.organisation_id),
            )
            .outerjoin(User, User.id == Membership.user_id)
            .where(Conversation.id.in_(ids))
        )
    ).all()
    found = {c.id: (c, customer, assignee_name) for c, customer, assignee_name in rows}

    last_stmt = (
        TenantScopedRepository(session, Message, organisation_id)
        .select(
            Message.conversation_id,
            func.left(Message.text, PREVIEW_LENGTH),
            Message.role,
            Message.created_at,
        )
        .where(Message.conversation_id.in_(ids), Message.role != "system")
        .distinct(Message.conversation_id)
        .order_by(Message.conversation_id, Message.created_at.desc(), Message.id.desc())
    )
    last = {cid: (preview, role) for cid, preview, role, _ in (await session.execute(last_stmt)).all()}

    tag_stmt = (
        TenantScopedRepository(session, ConversationTag, organisation_id)
        .select(ConversationTag.conversation_id, Tag.id, Tag.name)
        .join(Tag, (Tag.id == ConversationTag.tag_id) & (Tag.organisation_id == ConversationTag.organisation_id))
        .where(ConversationTag.conversation_id.in_(ids))
        .order_by(func.lower(Tag.name), Tag.id)
    )
    tags: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
    for cid, tag_id, name in (await session.execute(tag_stmt)).all():
        tags[cid].append({"id": tag_id, "name": name})

    call_stmt = (
        TenantScopedRepository(session, Call, organisation_id)
        .select(
            Call,
            exists()
            .where(CallRecording.call_id == Call.id, CallRecording.organisation_id == Call.organisation_id)
            .label("has_recording"),
        )
        .where(Call.conversation_id.in_(ids), Call.direction == "inbound")
    )
    calls = {
        call.conversation_id: {
            "id": call.id,
            "source": call.source,
            "mode": call.mode,
            "status": call.status,
            "end_reason": call.end_reason,
            "started_at": call.started_at,
            "duration_seconds": call.duration_seconds,
            "has_recording": has_recording,
        }
        for call, has_recording in (await session.execute(call_stmt)).all()
    }

    summaries = []
    for cid in ids:
        if cid not in found:
            continue
        c, customer, assignee_name = found[cid]
        preview, role = last.get(cid, (None, None))
        summaries.append(
            InboxSummary(
                id=c.id,
                status=c.status,
                channel=c.channel,
                priority=c.priority,
                assignee={"membership_id": c.assignee_membership_id, "name": assignee_name}
                if c.assignee_membership_id is not None
                else None,
                customer={
                    "id": customer.id,
                    "name": customer.name,
                    "email": customer.email,
                    "phone": customer.phone,
                    "identified": customer.external_id is not None,
                },
                last_message_preview=preview,
                last_message_role=role,
                last_message_at=c.last_message_at,
                escalation=c.escalation,
                tags=tags.get(cid, []),
                sla={
                    "first_response_due_at": c.first_response_due_at,
                    "resolution_due_at": c.resolution_due_at,
                    "first_response_at": c.first_response_at,
                    "breached": list(c.sla_breached or []),
                },
                unread_for_assignee=c.assignee_membership_id is not None and role == "customer",
                is_test=c.is_test,
                version=c.version,
                subject=c.subject,
                # An email conversation's contact is its sender (older rows: the customer's email). A chat's is only
                # the address the customer left in it: the composer offers the email follow-up on that alone.
                contact_email=c.contact_email or (customer.email if c.channel == "email" else None),
                callback_status=c.callback_status,
                callback_phone=c.callback_phone,
                call=calls.get(cid),
            )
        )
    return summaries


async def inbox_summary(session: AsyncSession, organisation_id: UUID, conversation_id: UUID) -> dict[str, Any] | None:
    """The `conversation` of a `conversation.upsert` (the InboxPublisher's default summary loader), or None when
    the conversation no longer exists."""
    found = await load_summaries(session, organisation_id, [conversation_id])
    return found[0].to_payload() if found else None


async def load_call_views(session: AsyncSession, organisation_id: UUID, conversation_id: UUID) -> list[dict[str, Any]]:
    """Every call of the conversation, oldest first, with the transfer's meta and the agents' display names (never
    emails or phone numbers). Three queries however many calls there are."""
    rows = (
        await session.execute(
            TenantScopedRepository(session, Call, organisation_id)
            .select(
                Call,
                exists()
                .where(CallRecording.call_id == Call.id, CallRecording.organisation_id == Call.organisation_id)
                .label("has_recording"),
                CallTransfer,
            )
            .outerjoin(
                CallTransfer,
                (CallTransfer.call_id == Call.id) & (CallTransfer.organisation_id == Call.organisation_id),
            )
            .where(Call.conversation_id == conversation_id)
            .order_by(Call.started_at, Call.id)
        )
    ).all()
    membership_ids = {call.agent_membership_id for call, _, _ in rows if call.agent_membership_id} | {
        t.connected_membership_id for _, _, t in rows if t is not None and t.connected_membership_id
    }
    names: dict[UUID, str | None] = {}
    if membership_ids:
        names = dict(
            (
                await session.execute(
                    TenantScopedRepository(session, Membership, organisation_id)
                    .select(Membership.id, User.name)
                    .join(User, User.id == Membership.user_id)
                    .where(Membership.id.in_(membership_ids))
                )
            ).all()
        )
    return [
        {
            "id": call.id,
            "direction": call.direction,
            "source": call.source,
            "mode": call.mode,
            "status": call.status,
            "end_reason": call.end_reason,
            "started_at": call.started_at,
            "duration_seconds": call.duration_seconds,
            "has_recording": has_recording,
            "agent_name": names.get(call.agent_membership_id),
            "transfer": {
                "status": transfer.status,
                "attempts": transfer.attempt,
                "connected_agent_name": names.get(transfer.connected_membership_id),
            }
            if transfer is not None
            else None,
        }
        for call, has_recording, transfer in rows
    ]
