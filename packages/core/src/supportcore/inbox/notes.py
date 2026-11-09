"""Internal notes on a conversation: for the team only, never sent to the widget, and with @mentions
(see inbox.mentions). Note text is never logged."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.db.models import Conversation, Membership, Mention, Note, Role, User
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import DomainError, NotFoundError
from supportcore.inbox.mentions import parse_mentions, rewrite_mentions, token_ids
from supportcore.inbox.notifications import NotificationService
from supportcore.inbox.publisher import InboxPublisher
from supportcore.observability.logging import get_logger
from supportcore.rbac.permissions import INBOX_VIEW_PERMISSIONS

__all__ = ["InvalidNoteError", "MAX_NOTE_LENGTH", "MentionView", "NoteService", "NoteView"]

logger = get_logger(__name__)

MAX_NOTE_LENGTH = 5000
MAX_LISTED_NOTES = 1000


class InvalidNoteError(DomainError):
    def __init__(self) -> None:
        super().__init__(f"A note must be 1-{MAX_NOTE_LENGTH} characters")


@dataclass(frozen=True)
class MentionView:
    membership_id: UUID
    name: str | None  # the member's current display name; None once they were removed


@dataclass(frozen=True)
class NoteView:
    id: UUID
    conversation_id: UUID
    author_membership_id: UUID | None
    author_name: str | None  # None once the author was removed
    text: str
    mentions: list[MentionView]  # the mentioned members, in order of appearance
    created_at: datetime


class NoteService:
    def __init__(self, session: AsyncSession, organisation_id: UUID, *, publisher: InboxPublisher | None = None) -> None:
        self._session = session
        self._org_id = organisation_id
        self._publisher = publisher
        self._notes = TenantScopedRepository(session, Note, organisation_id)
        self._mentions = TenantScopedRepository(session, Mention, organisation_id)
        self._memberships = TenantScopedRepository(session, Membership, organisation_id)
        self._notifications = NotificationService(session, organisation_id, publisher=publisher)

    async def add(self, conversation_id: UUID, author: Membership, text: str) -> NoteView:
        """Stores the note (any status of the conversation) and one Mention (and one `mentioned` notification, except
        for the author) per mentioned active member of this organisation whose role can view the inbox, at most
        MAX_MENTIONS. Every such member's token is stored with the member's own display name; tokens of unknown,
        inactive, inbox-less or other organisations' memberships are left as plain text."""
        text = (text or "").strip()
        if not 1 <= len(text) <= MAX_NOTE_LENGTH:
            raise InvalidNoteError()
        if await TenantScopedRepository(self._session, Conversation, self._org_id).get(conversation_id) is None:
            raise NotFoundError("Conversation not found")
        # The author is looked up with the mentioned ids (one query); only ids written in a token are affected.
        members = await self._active_members(token_ids(text) | {author.id})
        author_name = members[author.id][0] if author.id in members else None
        # Only members who can open the inbox can be mentioned: a notification names the customer.
        names = {mid: name for mid, (name, can_view) in members.items() if can_view}
        text = rewrite_mentions(text, names)
        mentioned = parse_mentions(text, names.keys())
        note = Note(conversation_id=conversation_id, author_membership_id=author.id, text=text)
        await self._notes.add(note)
        await self._session.flush()
        for membership_id in mentioned:
            await self._mentions.add(Mention(note_id=note.id, conversation_id=conversation_id, membership_id=membership_id))
        await self._session.flush()
        for membership_id in mentioned:  # the author is never told about their own mention
            await self._notifications.create(
                kind="mentioned",
                membership_id=membership_id,
                conversation_id=conversation_id,
                actor_membership_id=author.id,
            )
        if self._publisher is not None:  # after the commit: open dashboards refetch the notes
            self._publisher.note_added(self._session, self._org_id, conversation_id)
        logger.info(
            "note_added",
            conversation_id=str(conversation_id),
            note_id=str(note.id),
            membership_id=str(author.id),
            chars=len(text),
            mentions=len(mentioned),
        )
        return NoteView(
            id=note.id,
            conversation_id=conversation_id,
            author_membership_id=author.id,
            author_name=author_name,
            text=text,
            mentions=[MentionView(m, names[m]) for m in mentioned],
            created_at=note.created_at,
        )

    async def list(self, conversation_id: UUID, limit: int | None = None) -> list[NoteView]:
        """The conversation's newest `limit` (default MAX_LISTED_NOTES) notes, oldest first, with author and
        mentioned members' names."""
        stmt = (
            self._notes.select(Note, User.name)
            .outerjoin(
                Membership,
                (Membership.id == Note.author_membership_id) & (Membership.organisation_id == Note.organisation_id),
            )
            .outerjoin(User, User.id == Membership.user_id)
            .where(Note.conversation_id == conversation_id)
            .order_by(Note.created_at.desc(), Note.id.desc())
            .limit(MAX_LISTED_NOTES if limit is None else limit)
        )
        rows = list(reversed((await self._session.execute(stmt)).all()))
        stored: dict[UUID, dict[UUID, str | None]] = defaultdict(dict)
        if rows:
            mention_rows = await self._session.execute(
                self._mentions.select(Mention.note_id, Mention.membership_id, User.name)
                .outerjoin(
                    Membership,
                    (Membership.id == Mention.membership_id) & (Membership.organisation_id == Mention.organisation_id),
                )
                .outerjoin(User, User.id == Membership.user_id)
                .where(Mention.note_id.in_([note.id for note, _name in rows]))
            )
            for note_id, membership_id, name in mention_rows.all():
                stored[note_id][membership_id] = name
        views = []
        for note, name in rows:
            found = stored.get(note.id, {})
            order = parse_mentions(note.text, found.keys()) if found else []
            views.append(
                NoteView(
                    id=note.id,
                    conversation_id=note.conversation_id,
                    author_membership_id=note.author_membership_id,
                    author_name=name,
                    text=note.text,
                    mentions=[MentionView(m, found[m]) for m in order],
                    created_at=note.created_at,
                )
            )
        return views

    async def _active_members(self, membership_ids: set[UUID]) -> dict[UUID, tuple[str | None, bool]]:
        """{membership id: (display name, can view the inbox)} for those of `membership_ids` that are active
        members of this org."""
        if not membership_ids:
            return {}
        can_view = Role.permissions.overlap([p.value for p in INBOX_VIEW_PERMISSIONS])
        stmt = (
            self._memberships.select(Membership.id, User.name, can_view)
            .join(User, User.id == Membership.user_id)
            .join(Role, (Role.id == Membership.role_id) & (Role.organisation_id == Membership.organisation_id))
            .where(Membership.id.in_(membership_ids), Membership.status == "active")
        )
        return {mid: (name, bool(view)) for mid, name, view in (await self._session.execute(stmt)).all()}
