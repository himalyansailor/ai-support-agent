from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class CustomerOut(BaseModel):
    id: UUID
    name: str | None
    email: str | None
    identified: bool


class ConversationSummaryOut(BaseModel):
    id: UUID
    status: str
    channel: str
    customer: CustomerOut
    last_message_preview: str | None
    escalation: dict[str, Any] | None
    csat_score: int | None
    last_message_at: datetime
    is_test: bool


class ConversationPage(BaseModel):
    items: list[ConversationSummaryOut]
    next_cursor: str | None


class EmailAttachmentOut(BaseModel):
    id: UUID
    filename: str
    content_type: str
    size_bytes: int


class EmailMetaOut(BaseModel):
    direction: str
    subject: str | None
    from_address: str
    from_name: str | None
    to: list[str]
    cc: list[str]
    has_html: bool
    has_remote_images: bool
    attachments: list[EmailAttachmentOut]
    skipped_attachments: list[dict[str, Any]]
    delivery_status: str
    error_type: str | None
    reply_to: str | None = None  # inbound: the Reply-To address, where a reply to this email goes


class MessageOut(BaseModel):
    id: UUID
    role: str
    text: str
    citations: list[dict[str, Any]]
    author_membership_id: UUID | None = None  # the team member who wrote an `agent` message
    created_at: datetime
    email: EmailMetaOut | None = None  # inbox responses only: the email side of an email message


class TurnChunkOut(BaseModel):
    chunk_id: UUID
    document_name: str | None  # null once the chunk (or its document) was deleted
    ordinal: int | None
    score: float | None
    confidence: float | None
    rerank_score: float | None


class TurnOut(BaseModel):
    id: UUID
    message_id: UUID | None
    reply_message_id: UUID | None
    outcome: str
    no_answer: bool
    risk: dict[str, Any]
    chunks: list[TurnChunkOut]
    warnings: list[Any]
    timings_ms: dict[str, Any]
    tool_calls: list[dict[str, Any]]
    model: dict[str, Any]
    latency_ms: int | None
    trace_id: str | None
    created_at: datetime


class ConversationDetailOut(ConversationSummaryOut):
    language: str
    contact_email: str | None
    csat_comment: str | None
    created_at: datetime
    messages: list[MessageOut]
    turns: list[TurnOut]
