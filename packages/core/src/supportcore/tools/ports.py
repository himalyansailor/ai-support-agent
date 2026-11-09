"""Business tool ports: what the bot gets to call, and the executor that calls it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True)
class ToolSnapshot:
    """A plain copy of a BusinessTool, usable after the database session closed. `auth_value` is the
    decrypted header value: hold it for one turn only and never log it."""

    id: UUID
    name: str
    description: str
    method: str
    url_template: str
    input_schema: dict[str, Any]
    read_only: bool
    auth_header_name: str | None = None
    auth_value: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    status: int | None
    text: str  # what the model sees; already cut to MAX_TEXT_CHARS
    latency_ms: int
    error: str | None = None  # timeout | blocked_address | http_<status> | response_too_large | network_error | invalid_arguments


class ToolExecutor(Protocol):
    async def call(self, tool: ToolSnapshot, args: dict[str, Any], auth_value: str | None) -> ToolResult:
        """Calls the tool's endpoint. Never raises: failures come back as ok=False with `error` set."""
        ...
