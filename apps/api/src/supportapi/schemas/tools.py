from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


# Bounds here only limit request size; BusinessToolService validates (with stable error codes).
class CreateToolRequest(BaseModel):
    name: str = Field(max_length=200)
    description: str = Field(max_length=5000)
    method: str = Field(max_length=20)
    url_template: str = Field(max_length=5000)
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    auth_header_name: str | None = Field(default=None, max_length=200)
    auth_header_value: str | None = Field(default=None, max_length=5000)
    read_only: bool = True
    enabled: bool = True


class UpdateToolRequest(BaseModel):
    """Partial: absent fields stay as they are. `auth_header_name: null` removes the auth header."""

    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    method: str | None = Field(default=None, max_length=20)
    url_template: str | None = Field(default=None, max_length=5000)
    input_schema: dict[str, Any] | None = None
    auth_header_name: str | None = Field(default=None, max_length=200)
    auth_header_value: str | None = Field(default=None, max_length=5000)
    read_only: bool | None = None
    enabled: bool | None = None


class ToolTestRequest(BaseModel):

    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolOut(BaseModel):
    """Never carries the auth header value; only its last four characters."""

    id: UUID
    name: str
    description: str
    method: str
    url_template: str
    auth_header_name: str | None
    auth_header_last4: str | None
    input_schema: dict[str, Any]
    read_only: bool
    enabled: bool
    created_at: datetime
    updated_at: datetime


class ToolResultOut(BaseModel):
    ok: bool
    status: int | None
    text: str
    latency_ms: int
    error: str | None
