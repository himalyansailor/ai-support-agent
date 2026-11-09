from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from supportapi.schemas.conversations import CustomerOut, MessageOut, TurnOut
from supportapi.schemas.tags import TagOut


class AssigneeOut(BaseModel):
    membership_id: UUID
    name: str | None


class SlaOut(BaseModel):
    first_response_due_at: datetime | None
    resolution_due_at: datetime | None
    first_response_at: datetime | None  # set once the team replied: the first-response target is then met
    breached: list[str]  # the targets missed (the only source of "breached"; a met target never appears)


class InboxCustomerOut(CustomerOut):
    phone: str | None = None


class CallOut(BaseModel):
    id: UUID
    source: str
    mode: str | None = None
    status: str
    end_reason: str | None
    started_at: datetime
    duration_seconds: int | None
    has_recording: bool


class CallTransferOut(BaseModel):
    status: str
    attempts: int
    connected_agent_name: str | None


class CallDetailOut(BaseModel):
    id: UUID
    direction: str
    source: str
    mode: str | None
    status: str
    end_reason: str | None
    started_at: datetime
    duration_seconds: int | None
    has_recording: bool
    agent_name: str | None  # the agent who placed a callback call
    transfer: CallTransferOut | None


class InboxSummaryOut(BaseModel):
    id: UUID
    status: str
    channel: str
    priority: str
    assignee: AssigneeOut | None
    customer: InboxCustomerOut
    last_message_preview: str | None
    last_message_role: str | None
    last_message_at: datetime
    escalation: dict[str, Any] | None
    tags: list[TagOut]
    sla: SlaOut
    unread_for_assignee: bool
    is_test: bool
    version: int
    subject: str | None = None
    contact_email: str | None = None
    callback_status: str | None = None
    callback_phone: str | None = None  # the number to call back; inbox viewers only (this schema is the inbox's)
    call: CallOut | None = None


class InboxPage(BaseModel):
    items: list[InboxSummaryOut]
    next_cursor: str | None


class InboxCountsOut(BaseModel):
    mine: int
    unassigned: int
    bot: int
    callbacks: int


class MessagesPage(BaseModel):
    items: list[MessageOut]
    has_more: bool


class MentionOut(BaseModel):
    membership_id: UUID
    name: str | None  # the member's display name from the database (never the name typed in the note)


class NoteOut(BaseModel):
    id: UUID
    author: AssigneeOut | None  # None once the author was removed
    text: str
    mentions: list[MentionOut]  # the mentioned members, in order of appearance
    created_at: datetime


class InboxDetailOut(BaseModel):
    summary: InboxSummaryOut
    messages: list[MessageOut]
    has_more: bool
    notes: list[NoteOut]
    turns: list[TurnOut]
    history: list[InboxSummaryOut]
    calls: list[CallDetailOut] = []  # every call of a voice conversation, oldest first


class ReplyIn(BaseModel):
    text: str = Field(max_length=20000)  # the service trims and checks the limit (10000)
    # Chat conversations only: also email the reply to the customer's contact email. Replies in an email
    # conversation are always emailed.
    send_email: bool = False


class EmailHtmlOut(BaseModel):
    html: str  # sanitised (nh3) at ingest; the client still renders it in a sandboxed iframe
