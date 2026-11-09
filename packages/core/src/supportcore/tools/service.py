"""Business tools: CRUD with validation, encrypted auth header, plain snapshots for the bot, and a test call."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.ai.base_url import ensure_public_base_url
from supportcore.ai.errors import CredentialInvalidError
from supportcore.config import Settings
from supportcore.db.models import BusinessTool, User
from supportcore.db.models.tools import TOOL_METHODS
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import DomainError, NotFoundError
from supportcore.events import EventBus
from supportcore.secrets.ports import SecretCipher, SecretDecryptError
from supportcore.tools.events import BusinessToolCreated, BusinessToolDeleted, BusinessToolUpdated
from supportcore.tools.ports import ToolExecutor, ToolResult, ToolSnapshot
from supportcore.tools.schema import placeholders, validate_arguments, validate_input_schema, validate_placeholders

__all__ = ["BusinessToolService", "InvalidToolError", "ToolNameTakenError", "snapshot"]

logger = structlog.get_logger()

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
HEADER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,100}$")
FORBIDDEN_HEADERS = frozenset(
    {"host", "content-length", "content-type", "transfer-encoding", "connection", "upgrade", "accept-encoding"}
)
MAX_DESCRIPTION_LENGTH = 1000
MAX_URL_LENGTH = 2000
MAX_AUTH_VALUE_LENGTH = 2000
CHANGE_FIELDS = ("name", "description", "method", "url_template", "input_schema", "read_only", "enabled")


class InvalidToolError(DomainError):
    pass


class ToolNameTakenError(DomainError):
    def __init__(self) -> None:
        super().__init__("A tool with this name already exists")


def snapshot(tool: BusinessTool, auth_value: str | None = None) -> ToolSnapshot:
    return ToolSnapshot(
        id=tool.id,
        name=tool.name,
        description=tool.description,
        method=tool.method,
        url_template=tool.url_template,
        input_schema=dict(tool.input_schema),
        read_only=tool.read_only,
        auth_header_name=tool.auth_header_name,
        auth_value=auth_value,
    )


def _clean_name(name: Any) -> str:
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise InvalidToolError("Name must be 2-64 characters: lowercase letters, digits and underscores, starting with a letter")
    return name


def _clean_description(description: Any) -> str:
    description = description.strip() if isinstance(description, str) else ""
    if not 1 <= len(description) <= MAX_DESCRIPTION_LENGTH:
        raise InvalidToolError(f"Description must be 1-{MAX_DESCRIPTION_LENGTH} characters")
    return description


def _clean_method(method: Any) -> str:
    if method not in TOOL_METHODS:
        raise InvalidToolError(f"Method must be one of {', '.join(TOOL_METHODS)}")
    return method


def _clean_header_name(name: Any) -> str:
    if not isinstance(name, str) or not HEADER_NAME_PATTERN.match(name) or name.lower() in FORBIDDEN_HEADERS:
        raise InvalidToolError("Auth header name is not a usable header name")
    return name


def _clean_header_value(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_AUTH_VALUE_LENGTH
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise InvalidToolError(f"Auth header value must be 1-{MAX_AUTH_VALUE_LENGTH} characters without control characters")
    return value


def _origin(url_template: str) -> tuple[str, str, int | None]:
    """(scheme, host, port) of a saved URL template; placeholders are never in the host."""
    parts = urlsplit(url_template)
    port = parts.port or {"http": 80, "https": 443}.get(parts.scheme)
    return parts.scheme, (parts.hostname or "").lower(), port


class BusinessToolService:
    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        events: EventBus,
        cipher: SecretCipher,
        settings: Settings,
        executor: ToolExecutor | None = None,
    ) -> None:
        self._session = session
        self._org_id = organisation_id
        self._events = events
        self._cipher = cipher
        self._settings = settings
        self._executor = executor
        self._tools = TenantScopedRepository(session, BusinessTool, organisation_id)

    async def list(self) -> list[BusinessTool]:
        return await self._tools.list(order_by=(BusinessTool.name, BusinessTool.id), limit=1000)

    async def get(self, tool_id: UUID, *, for_update: bool = False) -> BusinessTool:
        stmt = self._tools.select(for_update=for_update).where(BusinessTool.id == tool_id)
        if for_update:
            stmt = stmt.execution_options(populate_existing=True)
        tool = (await self._session.execute(stmt)).scalar_one_or_none()
        if tool is None:
            raise NotFoundError("Tool not found")
        return tool

    async def create(
        self,
        *,
        name: str,
        description: str,
        method: str,
        url_template: str,
        input_schema: dict[str, Any],
        actor: User,
        auth_header_name: str | None = None,
        auth_header_value: str | None = None,
        read_only: bool = True,
        enabled: bool = True,
    ) -> BusinessTool:
        schema = validate_input_schema(input_schema)
        url_template = await self._clean_url(url_template, schema)
        name = _clean_name(name)
        await self._ensure_name_free(name)
        tool = BusinessTool(
            name=name,
            description=_clean_description(description),
            method=_clean_method(method),
            url_template=url_template,
            input_schema=schema,
            read_only=bool(read_only),
            enabled=bool(enabled),
        )
        if auth_header_name is not None or auth_header_value is not None:
            if auth_header_name is None or auth_header_value is None:
                raise InvalidToolError("Auth header name and value are set together")
            self._set_auth(tool, _clean_header_name(auth_header_name), _clean_header_value(auth_header_value))
        await self._tools.add(tool)
        await self._flush_unique()
        await self._events.publish(
            BusinessToolCreated(
                organisation_id=self._org_id,
                actor_user_id=actor.id,
                tool_id=tool.id,
                name=tool.name,
                method=tool.method,
            )
        )
        return tool

    async def update(self, tool_id: UUID, *, actor: User, **fields: Any) -> BusinessTool:
        """Only the fields passed change. `auth_header_name=None` removes the header; `auth_header_value`
        replaces the secret (the header name must exist)."""
        allowed = {*CHANGE_FIELDS, "auth_header_name", "auth_header_value"}
        unknown = set(fields) - allowed
        if unknown:
            raise InvalidToolError(f"Unknown field: {sorted(unknown)[0][:50]}")
        tool = await self.get(tool_id, for_update=True)
        new: dict[str, Any] = {}
        if "name" in fields:
            new["name"] = _clean_name(fields["name"])
            if new["name"] != tool.name:
                await self._ensure_name_free(new["name"])
        if "description" in fields:
            new["description"] = _clean_description(fields["description"])
        if "method" in fields:
            new["method"] = _clean_method(fields["method"])
        if "read_only" in fields:
            new["read_only"] = self._clean_bool(fields["read_only"], "read_only")
        if "enabled" in fields:
            new["enabled"] = self._clean_bool(fields["enabled"], "enabled")
        schema = tool.input_schema
        if "input_schema" in fields:
            schema = new["input_schema"] = validate_input_schema(fields["input_schema"])
        if "url_template" in fields or "input_schema" in fields:
            new["url_template"] = await self._clean_url(fields.get("url_template", tool.url_template), schema)
        changes: dict[str, Any] = {}
        for field_name, value in new.items():
            old = getattr(tool, field_name)
            if value != old:
                changes[field_name] = {"from": old, "to": value}
        auth_changed = False
        if "url_template" in changes and _origin(changes["url_template"]["from"]) != _origin(
            changes["url_template"]["to"]
        ):
            auth_changed = self._drop_auth_for_new_origin(tool, fields)
        auth_changed = self._apply_auth(tool, fields) or auth_changed
        for field_name, diff in changes.items():
            setattr(tool, field_name, diff["to"])
        if auth_changed:
            changes["auth_header"] = "changed"
        if changes:
            await self._flush_unique()
            await self._session.refresh(tool)  # updated_at is set by the database
            await self._events.publish(
                BusinessToolUpdated(
                    organisation_id=self._org_id,
                    actor_user_id=actor.id,
                    tool_id=tool.id,
                    name=tool.name,
                    changes=changes,
                )
            )
        return tool

    async def delete(self, tool_id: UUID, actor: User) -> None:
        tool = await self.get(tool_id, for_update=True)
        event = BusinessToolDeleted(
            organisation_id=self._org_id, actor_user_id=actor.id, tool_id=tool.id, name=tool.name
        )
        await self._tools.delete(tool)
        await self._events.publish(event)

    async def enabled_snapshots(self) -> list[ToolSnapshot]:
        """Plain copies of the enabled tools with their decrypted auth values. Hold them for one turn only."""
        snapshots = []
        for tool in await self._tools.list(BusinessTool.enabled.is_(True), order_by=(BusinessTool.name,), limit=1000):
            try:
                snapshots.append(snapshot(tool, self._decrypt(tool)))
            except SecretDecryptError:
                logger.warning("business_tool_auth_undecryptable", tool_id=str(tool.id))
        return snapshots

    async def test(self, tool_id: UUID, args: dict[str, Any]) -> ToolResult:
        """Calls the tool once with `args`. The transaction is committed first: no session is held open
        across the HTTP call."""
        if self._executor is None:
            raise RuntimeError("BusinessToolService needs an executor to test a tool")
        tool = await self.get(tool_id)
        args = validate_arguments(tool.input_schema, args, tool.url_template)
        try:
            plain = snapshot(tool, self._decrypt(tool))
        except SecretDecryptError:
            raise InvalidToolError("The stored auth header value could not be read; set it again") from None
        await self._session.commit()
        return await self._executor.call(plain, args, plain.auth_value)

    # --- internals ---------------------------------------------------------------------------------

    @staticmethod
    def _clean_bool(value: Any, name: str) -> bool:
        if not isinstance(value, bool):
            raise InvalidToolError(f"{name} must be true or false")
        return value

    def _decrypt(self, tool: BusinessTool) -> str | None:
        if tool.encrypted_auth_header_value is None:
            return None
        return self._cipher.decrypt(tool.encrypted_auth_header_value)

    def _set_auth(self, tool: BusinessTool, name: str, value: str) -> None:
        tool.auth_header_name = name
        tool.encrypted_auth_header_value = self._cipher.encrypt(value)
        tool.auth_header_last4 = value[-4:]

    @staticmethod
    def _drop_auth_for_new_origin(tool: BusinessTool, fields: dict[str, Any]) -> bool:
        """A stored auth value never follows the URL to a new scheme, host or port: it is cleared unless the
        same update supplies a new value. Otherwise anyone who can edit the tool could repoint it and read
        the secret off the wire."""
        if tool.encrypted_auth_header_value is None or fields.get("auth_header_value") is not None:
            return False
        tool.auth_header_name = tool.encrypted_auth_header_value = tool.auth_header_last4 = None
        return True

    def _apply_auth(self, tool: BusinessTool, fields: dict[str, Any]) -> bool:
        """Applies auth_header_name / auth_header_value; True when anything about the header changed."""
        if "auth_header_name" not in fields and "auth_header_value" not in fields:
            return False
        if "auth_header_name" in fields and fields["auth_header_name"] is None:
            if fields.get("auth_header_value") is not None:
                raise InvalidToolError("Auth header value needs an auth header name")
            changed = tool.auth_header_name is not None
            tool.auth_header_name = tool.encrypted_auth_header_value = tool.auth_header_last4 = None
            return changed
        name = _clean_header_name(fields["auth_header_name"]) if "auth_header_name" in fields else tool.auth_header_name
        if name is None:
            raise InvalidToolError("Auth header value needs an auth header name")
        changed = name != tool.auth_header_name
        tool.auth_header_name = name
        if fields.get("auth_header_value") is not None:
            self._set_auth(tool, name, _clean_header_value(fields["auth_header_value"]))
            return True
        if tool.encrypted_auth_header_value is None:
            raise InvalidToolError("Auth header name and value are set together")
        return changed

    async def _clean_url(self, url_template: Any, schema: dict[str, Any]) -> str:
        if not isinstance(url_template, str) or not 1 <= len(url_template.strip()) <= MAX_URL_LENGTH:
            raise InvalidToolError(f"URL must be 1-{MAX_URL_LENGTH} characters")
        url_template = url_template.strip()
        validate_placeholders(url_template, schema)
        dummy = url_template
        for name in placeholders(url_template):
            dummy = dummy.replace("{" + name + "}", "x")
        parts = urlsplit(dummy)
        try:
            parts.port  # noqa: B018 - raises ValueError for a bad port
        except ValueError:
            raise InvalidToolError("URL has an invalid port") from None
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise InvalidToolError("URL must be an http(s) URL with a host")
        if "@" in parts.netloc:
            raise InvalidToolError("URL must not contain credentials; use the auth header")
        if "{" in url_template.split("//", 1)[-1].split("/", 1)[0]:
            raise InvalidToolError("URL placeholders are not allowed in the host")
        allow_private = self._settings.effective_allow_private_base_urls()
        if parts.scheme == "http" and (self._settings.env == "prod" or not allow_private):
            raise InvalidToolError("URL must be https")
        try:
            await ensure_public_base_url(dummy, allow_private=allow_private)
        except CredentialInvalidError as exc:
            raise InvalidToolError(f"URL host is not allowed: {exc}") from None
        return url_template

    async def _ensure_name_free(self, name: str) -> None:
        if (await self._session.execute(self._tools.select(BusinessTool.id).where(BusinessTool.name == name))).first():
            raise ToolNameTakenError()

    async def _flush_unique(self) -> None:
        try:
            async with self._session.begin_nested():
                await self._session.flush()
        except IntegrityError as exc:
            if "uq_business_tools_organisation_id_name" in str(exc.orig):
                raise ToolNameTakenError() from None
            raise
