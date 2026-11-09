"""The team's own records on conversations: tags, internal notes and the mentions in them, saved replies, and
the notifications sent to team members."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from supportcore.db.base import Base, TenantMixin, TimestampMixin, uuid_pk

__all__ = ["ConversationTag", "Mention", "Note", "Notification", "SavedReply", "Tag"]


def _now() -> datetime:
    return datetime.now(UTC)


class Tag(TenantMixin, TimestampMixin, Base):
    """A label agents put on conversations. Names are unique per organisation, case-insensitively."""

    __tablename__ = "tags"
    __table_args__ = (
        Index("uq_tags_organisation_id_lower_name", "organisation_id", func.lower(sql_text("name")), unique=True),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(40), nullable=False)


class ConversationTag(TenantMixin, Base):
    __tablename__ = "conversation_tags"
    __table_args__ = (Index("ix_conversation_tags_tag_id", "tag_id"),)

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )


class Note(TenantMixin, Base):
    """An internal note on a conversation: seen by the team only, never sent to the widget."""

    __tablename__ = "notes"
    __table_args__ = (
        Index("ix_notes_conversation_id_created_at", "conversation_id", "created_at"),
        Index("ix_notes_author_membership_id", "author_membership_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    # Kept (author unknown) when the member is removed.
    author_membership_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memberships.id", ondelete="SET NULL"), nullable=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now(), nullable=False
    )


class Mention(TenantMixin, Base):
    """A team member @mentioned in a note (one row per member per note)."""

    __tablename__ = "mentions"
    __table_args__ = (
        UniqueConstraint("note_id", "membership_id", name="uq_mentions_note_id_membership_id"),
        # The mentions view: the conversations a member was mentioned in.
        Index("ix_mentions_membership_id_conversation_id", "membership_id", "conversation_id"),
        Index("ix_mentions_conversation_id", "conversation_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    note_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("notes.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    membership_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memberships.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now(), nullable=False
    )


class SavedReply(TenantMixin, TimestampMixin, Base):
    """A canned reply with {customer_name}/{agent_name} placeholders. The optional shortcut (a-z, 0-9, _ and -,
    at most 30) is unique per organisation."""

    __tablename__ = "saved_replies"
    __table_args__ = (
        Index(
            "uq_saved_replies_organisation_id_shortcut",
            "organisation_id",
            "shortcut",
            unique=True,
            postgresql_where=sql_text("shortcut IS NOT NULL"),
        ),
        Index("ix_saved_replies_created_by_membership_id", "created_by_membership_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    title: Mapped[str] = mapped_column(String(100), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    shortcut: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # Kept (author unknown) when the member is removed.
    created_by_membership_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memberships.id", ondelete="SET NULL"), nullable=True
    )


class Notification(TenantMixin, Base):
    """Something a team member should know: a conversation assigned to them, a mention, an SLA breach. Ids only:
    never note or message text. Names shown in the UI are looked up at read time."""

    __tablename__ = "notifications"
    __table_args__ = (
        # The list (newest first, all or unread only) and the unread count both start from the member.
        Index("ix_notifications_membership_id_created_at", "membership_id", sql_text("created_at DESC")),
        Index(
            "ix_notifications_membership_id_unread",
            "membership_id",
            postgresql_where=sql_text("read_at IS NULL"),
        ),
        Index("ix_notifications_conversation_id", "conversation_id"),
        Index("ix_notifications_actor_membership_id", "actor_membership_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    membership_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memberships.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # assigned | mentioned | sla_breached
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    # None for the system (a bot escalation routed to them, an SLA breach); kept (unknown) when the actor is removed.
    actor_membership_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memberships.id", ondelete="SET NULL"), nullable=True
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now(), nullable=False
    )
