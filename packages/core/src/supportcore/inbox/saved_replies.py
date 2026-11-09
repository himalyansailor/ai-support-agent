"""The organisation's saved replies: title 1-100 and body 1-5000 characters, an optional shortcut
(`^[a-z0-9_-]{1,30}$`, unique per organisation), at most MAX_SAVED_REPLIES_PER_ORG of them. `render_saved_reply`
fills the two placeholders; the dashboard does the same client-side."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.db.models import SavedReply
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import DomainError, NotFoundError
from supportcore.observability.logging import get_logger

__all__ = [
    "InvalidSavedReplyError",
    "MAX_SAVED_REPLIES_PER_ORG",
    "SavedReplyService",
    "SavedReplyShortcutTakenError",
    "TooManySavedRepliesError",
    "render_saved_reply",
]

logger = get_logger(__name__)

MAX_SAVED_REPLIES_PER_ORG = 500
MAX_TITLE_LENGTH = 100
MAX_BODY_LENGTH = 5000
_SHORTCUT = re.compile(r"[a-z0-9_-]{1,30}")
_PLACEHOLDER = re.compile(r"\{(customer_name|agent_name)\}")
_UNSET: Any = object()


class InvalidSavedReplyError(DomainError):
    def __init__(self, message: str = "Invalid saved reply") -> None:
        super().__init__(message)


class TooManySavedRepliesError(DomainError):
    def __init__(self) -> None:
        super().__init__(f"An organisation can have at most {MAX_SAVED_REPLIES_PER_ORG} saved replies")


class SavedReplyShortcutTakenError(DomainError):
    def __init__(self) -> None:
        super().__init__("A saved reply with that shortcut already exists")


def render_saved_reply(body: str, *, customer_name: str | None, agent_name: str | None) -> str:
    """Replaces `{customer_name}` and `{agent_name}` (a missing value becomes ""). Any other `{...}` stays as is.
    One pass, no str.format: the inserted values are never expanded again."""
    values = {"customer_name": customer_name or "", "agent_name": agent_name or ""}
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], body)


def _clean_title(title: str) -> str:
    title = (title or "").strip()
    if not 1 <= len(title) <= MAX_TITLE_LENGTH:
        raise InvalidSavedReplyError(f"A title must be 1-{MAX_TITLE_LENGTH} characters")
    return title


def _clean_body(body: str) -> str:
    body = (body or "").strip()
    if not 1 <= len(body) <= MAX_BODY_LENGTH:
        raise InvalidSavedReplyError(f"A body must be 1-{MAX_BODY_LENGTH} characters")
    return body


def _clean_shortcut(shortcut: str | None) -> str | None:
    shortcut = (shortcut or "").strip()
    if not shortcut:
        return None
    if not _SHORTCUT.fullmatch(shortcut):
        raise InvalidSavedReplyError("A shortcut is 1-30 characters of a-z, 0-9, _ and -")
    return shortcut


class SavedReplyService:
    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None:
        self._session = session
        self._replies = TenantScopedRepository(session, SavedReply, organisation_id)

    async def list(self, query: str | None = None) -> list[SavedReply]:
        """By title (case-insensitively). `query` matches title or shortcut as a case-insensitive substring,
        with its LIKE wildcards escaped."""
        where = []
        if query:
            escaped = query.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            where.append(
                or_(
                    func.lower(SavedReply.title).like(pattern, escape="\\"),
                    func.lower(SavedReply.shortcut).like(pattern, escape="\\"),
                )
            )
        return await self._replies.list(
            *where, order_by=(func.lower(SavedReply.title), SavedReply.id), limit=MAX_SAVED_REPLIES_PER_ORG
        )

    async def get(self, reply_id: UUID) -> SavedReply:
        reply = await self._replies.get(reply_id)
        if reply is None:
            raise NotFoundError("Saved reply not found")
        return reply

    async def create(
        self, title: str, body: str, shortcut: str | None = None, *, created_by_membership_id: UUID | None = None
    ) -> SavedReply:
        reply = SavedReply(
            title=_clean_title(title),
            body=_clean_body(body),
            shortcut=_clean_shortcut(shortcut),
            created_by_membership_id=created_by_membership_id,
        )
        count = (await self._session.execute(self._replies.select(func.count()))).scalar_one()
        if count >= MAX_SAVED_REPLIES_PER_ORG:
            raise TooManySavedRepliesError()
        await self._save(reply, None)
        logger.info("saved_reply_created", saved_reply_id=str(reply.id))
        return reply

    async def update(
        self, reply_id: UUID, *, title: str = _UNSET, body: str = _UNSET, shortcut: str | None = _UNSET
    ) -> SavedReply:
        """Changes only the fields given (shortcut=None removes the shortcut)."""
        reply = await self.get(reply_id)
        new_title = _clean_title(title) if title is not _UNSET else reply.title
        new_body = _clean_body(body) if body is not _UNSET else reply.body
        new_shortcut = _clean_shortcut(shortcut) if shortcut is not _UNSET else reply.shortcut

        def apply() -> None:
            reply.title, reply.body, reply.shortcut = new_title, new_body, new_shortcut

        await self._save(reply, apply)
        await self._session.refresh(reply)  # a rolled-back earlier attempt may have expired it
        logger.info("saved_reply_updated", saved_reply_id=str(reply.id))
        return reply

    async def delete(self, reply_id: UUID) -> None:
        await self._replies.delete(await self.get(reply_id))
        logger.info("saved_reply_deleted", saved_reply_id=str(reply_id))

    async def _save(self, reply: SavedReply, apply: Callable[[], None] | None) -> None:
        """Adds (apply is None) or changes `reply` inside a savepoint and flushes; a taken shortcut is
        SavedReplyShortcutTakenError (the unique index decides, so concurrent writes cannot both win). The change
        is made after the savepoint opens, so the rolled-back change leaves the object as it was."""
        try:
            async with self._session.begin_nested():
                if apply is None:
                    await self._replies.add(reply)
                else:
                    apply()
                await self._session.flush()
        except IntegrityError:
            raise SavedReplyShortcutTakenError() from None
