"""Request and response bodies of the public widget API."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PublicConfigOut(BaseModel):
    appearance: dict[str, Any]
    strings: dict[str, str]
    language: str
    csat_enabled: bool
    organisation_name: str
    voice_call_enabled: bool


class CallTokenOut(BaseModel):
    token: str
    ticket: str
    expires_in: int


class FramePolicyOut(BaseModel):
    frame_ancestors: str


class StartSessionRequest(BaseModel):
    # Bounds only limit request size; the service validates.
    key: str = Field(max_length=100)
    parent_origin: str = Field(max_length=300)
    visitor_id: str | None = Field(default=None, max_length=100)
    customer_token: str | None = Field(default=None, max_length=8192)
    preview: bool = False
    preview_ticket: str | None = Field(default=None, max_length=2048)


class AuthorOut(BaseModel):
    name: str


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: str
    language: str
    is_test: bool
    csat_score: int | None
    created_at: datetime
    last_message_at: datetime
    agent: AuthorOut | None = None  # the current assignee, by display name only


class SessionOut(BaseModel):
    token: str
    customer_id: UUID
    visitor_id: str
    expires_in: int
    conversation: ConversationOut | None


class CitationOut(BaseModel):
    chunk_id: str | None = None
    document_id: str | None = None
    document_name: str | None = None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    role: str
    text: str
    citations: list[CitationOut]
    client_id: str | None
    author: AuthorOut | None = None  # an agent message's author, by display name only
    created_at: datetime


class CsatRequest(BaseModel):
    score: int
    comment: str | None = Field(default=None, max_length=5000)


class ContactRequest(BaseModel):
    email: str = Field(max_length=1000)
