"""The organisation's conversation tags: names 1-40 characters, unique per organisation case-insensitively,
at most MAX_TAGS_PER_ORG of them, and at most MAX_TAGS_PER_CONVERSATION on one conversation."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.db.models import Conversation, ConversationTag, Tag
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import DomainError, NotFoundError
from supportcore.observability.logging import get_logger

if TYPE_CHECKING:
    from supportcore.inbox.publisher import InboxPublisher

__all__ = [
    "InvalidTagError",
    "MAX_TAG_NAME_LENGTH",
    "MAX_TAGS_PER_CONVERSATION",
    "MAX_TAGS_PER_ORG",
    "TagNameTakenError",
    "TagService",
    "TooManyTagsError",
]

logger = get_logger(__name__)

MAX_TAG_NAME_LENGTH = 40
MAX_TAGS_PER_ORG = 200
MAX_TAGS_PER_CONVERSATION = 10


class InvalidTagError(DomainError):
    def __init__(self, message: str = f"A tag name must be 1-{MAX_TAG_NAME_LENGTH} characters") -> None:
        super().__init__(message)


class TooManyTagsError(DomainError):
    def __init__(self, message: str = f"A conversation can have at most {MAX_TAGS_PER_CONVERSATION} tags") -> None:
        super().__init__(message)


class TagNameTakenError(DomainError):
    def __init__(self) -> None:
        super().__init__("A tag with that name already exists")


def _clean_name(name: str) -> str:
    name = (name or "").strip()
    if not 1 <= len(name) <= MAX_TAG_NAME_LENGTH:
        raise InvalidTagError()
    return name


class TagService:
    def __init__(self, session: AsyncSession, organisation_id: UUID, *, publisher: InboxPublisher) -> None:
        self._session = session
        self._org_id = organisation_id
        self._publisher = publisher
        self._tags = TenantScopedRepository(session, Tag, organisation_id)
        self._conversation_tags = TenantScopedRepository(session, ConversationTag, organisation_id)

    async def list(self) -> list[Tag]:
        """Every tag of the organisation, by name (case-insensitively)."""
        return await self._tags.list(order_by=(func.lower(Tag.name), Tag.id), limit=MAX_TAGS_PER_ORG)

    async def create(self, name: str) -> Tag:
        name = _clean_name(name)
        count = (await self._session.execute(self._tags.select(func.count()))).scalar_one()
        if count >= MAX_TAGS_PER_ORG:
            raise TooManyTagsError(f"An organisation can have at most {MAX_TAGS_PER_ORG} tags")
        tag = Tag()
        await self._save(tag, name)
        logger.info("tag_created", tag_id=str(tag.id))
        return tag

    async def rename(self, tag_id: UUID, name: str) -> Tag:
        name = _clean_name(name)
        tag = await self._get(tag_id)
        if tag.name != name:
            await self._save(tag, name)
            logger.info("tag_renamed", tag_id=str(tag.id))
        return tag

    async def delete(self, tag_id: UUID) -> None:
        """Deletes the tag; it comes off every conversation (the foreign key cascades)."""
        await self._tags.delete(await self._get(tag_id))
        logger.info("tag_deleted", tag_id=str(tag_id))

    async def set_tags(self, conversation_id: UUID, tag_ids: Sequence[UUID]) -> list[Tag]:
        """Replaces the conversation's tags (duplicates collapse). Every id must be one of this organisation's
        tags (InvalidTagError), at most MAX_TAGS_PER_CONVERSATION (TooManyTagsError). Returns the tags by name."""
        wanted = set(tag_ids)
        if len(wanted) > MAX_TAGS_PER_CONVERSATION:
            raise TooManyTagsError()
        conversation = await TenantScopedRepository(self._session, Conversation, self._org_id).get(
            conversation_id, for_update=True  # concurrent replacements of the same conversation's tags wait
        )
        if conversation is None:
            raise NotFoundError("Conversation not found")
        tags = await self._tags.list(Tag.id.in_(wanted), limit=MAX_TAGS_PER_CONVERSATION) if wanted else []
        if len(tags) != len(wanted):
            raise InvalidTagError("Unknown tag")
        current = await self._conversation_tags.list(
            ConversationTag.conversation_id == conversation_id, limit=MAX_TAGS_PER_ORG
        )
        have = {row.tag_id for row in current}
        for row in current:
            if row.tag_id not in wanted:
                await self._session.delete(row)
        for tag_id in wanted - have:
            await self._conversation_tags.add(ConversationTag(conversation_id=conversation_id, tag_id=tag_id))
        if have != wanted:
            # Tags live in their own table: touch the row so `version` moves on, or clients holding the
            # current version would drop this upsert as stale.
            conversation.version = Conversation.version + 1
        await self._session.flush()
        if have != wanted:
            self._publisher.conversation_changed(self._session, self._org_id, conversation_id)
            logger.info("conversation_tags_set", conversation_id=str(conversation_id), tags=len(wanted))
        return sorted(tags, key=lambda t: (t.name.lower(), t.id))

    async def _get(self, tag_id: UUID) -> Tag:
        tag = await self._tags.get(tag_id)
        if tag is None:
            raise NotFoundError("Tag not found")
        return tag

    async def _save(self, tag: Tag, name: str) -> None:
        """Names (and adds) `tag` in a savepoint; a case-insensitive duplicate name is TagNameTakenError (the
        unique index decides, so concurrent creates cannot both win)."""
        try:
            async with self._session.begin_nested():
                tag.name = name
                await self._tags.add(tag)
                await self._session.flush()
        except IntegrityError:
            raise TagNameTakenError() from None
