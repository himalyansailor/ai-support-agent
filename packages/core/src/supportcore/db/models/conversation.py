"""Customers (people chatting through a widget), their conversations, messages and the bot's turn records."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    literal_column,
)
from sqlalchemy import text as sql_text  # `text` is also a Message column name
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from supportcore.db.base import Base, TenantMixin, TimestampMixin, uuid_pk

CONVERSATION_STATUSES = ("bot", "open", "pending", "resolved", "closed")
PRIORITIES = ("low", "normal", "high", "urgent")
SLA_BREACHES = ("first_response", "resolution")
CONVERSATION_CHANNELS = ("chat", "email", "voice")
MESSAGE_ROLES = ("customer", "bot", "agent", "system")
CALLBACK_STATUSES = ("requested", "done")
TURN_OUTCOMES = ("answered", "offered_human", "escalated", "error")


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _now() -> datetime:
    return datetime.now(UTC)


class Customer(TenantMixin, TimestampMixin, Base):
    """An end user. Anonymous customers are known by `visitor_id` (random, kept in the browser);
    identified ones by the business's own user id (`external_id`, from a signed identity token)."""

    __tablename__ = "customers"
    __table_args__ = (
        Index(
            "uq_customers_organisation_id_external_id",
            "organisation_id",
            "external_id",
            unique=True,
            postgresql_where=sql_text("external_id IS NOT NULL"),
        ),
        Index(
            "uq_customers_organisation_id_visitor_id",
            "organisation_id",
            "visitor_id",
            unique=True,
            postgresql_where=sql_text("visitor_id IS NOT NULL"),
        ),
        # Inbox search: fuzzy match on name / email (pg_trgm, created by migration 0011 and infra/postgres/init.sql).
        Index(
            "ix_customers_name_trgm",
            literal_column("lower(name)"),
            postgresql_using="gin",
            postgresql_ops={"lower(name)": "gin_trgm_ops"},
        ),
        Index(
            "ix_customers_email_trgm",
            literal_column("lower(email)"),
            postgresql_using="gin",
            postgresql_ops={"lower(email)": "gin_trgm_ops"},
        ),
        # Email conversations find the sender's customer by lower(email).
        Index("ix_customers_organisation_id_lower_email", "organisation_id", literal_column("lower(email)")),
        # Voice conversations find the caller's customer by phone number.
        Index("ix_customers_organisation_id_phone", "organisation_id", "phone"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    visitor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # E.164 number of a voice caller; how a repeat caller is found again.
    phone: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Conversation(TenantMixin, TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint(_one_of("status", CONVERSATION_STATUSES), name="ck_conversations_status"),
        CheckConstraint(_one_of("channel", CONVERSATION_CHANNELS), name="ck_conversations_channel"),
        CheckConstraint("csat_score IS NULL OR csat_score BETWEEN 1 AND 5", name="ck_conversations_csat_score"),
        Index("ix_conversations_organisation_id_status_last_message_at", "organisation_id", "status", "last_message_at"),
        CheckConstraint(_one_of("priority", PRIORITIES), name="ck_conversations_priority"),
        Index(
            "ix_conversations_org_status_assignee_last_message",
            "organisation_id",
            "status",
            "assignee_membership_id",
            "last_message_at",
        ),
        Index("ix_conversations_assignee_membership_id", "assignee_membership_id"),
        Index("ix_conversations_customer_id", "customer_id"),
        Index("ix_conversations_widget_id", "widget_id"),
        CheckConstraint(
            "callback_status IS NULL OR " + _one_of("callback_status", CALLBACK_STATUSES),
            name="ck_conversations_callback_status",
        ),
        # The callbacks still to make.
        Index(
            "ix_conversations_organisation_id_callback_status",
            "organisation_id",
            "callback_status",
            postgresql_where=sql_text("callback_status = 'requested'"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    widget_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("widgets.id", ondelete="SET NULL"), nullable=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(16), default="chat", server_default="chat", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="bot", server_default="bot", nullable=False)
    # The subject of an email conversation (the first mail's), for the inbox list and reply subjects.
    subject: Mapped[str | None] = mapped_column(String(998), nullable=True)
    language: Mapped[str] = mapped_column(String(16), default="en", server_default="en", nullable=False)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sql_text("false"), nullable=False)
    # {action, rule_key, rule_name, reason, source} of the risk decision that sent it to a person.
    escalation: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    # Voice: NULL, `requested` (a person should call back) or `done`; the number to call is callback_phone.
    callback_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    callback_phone: Mapped[str | None] = mapped_column(String(16), nullable=True)
    csat_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    csat_comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # The team member handling it (set when it is claimed or assigned); cleared if the member is removed.
    assignee_membership_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memberships.id", ondelete="SET NULL"), nullable=True
    )
    priority: Mapped[str] = mapped_column(String(8), default="normal", server_default="normal", nullable=False)
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the conversation last entered the team (bot -> open, or reopened): the start of the SLA clock, which a
    # priority change recomputes the due dates from. None while with the bot.
    team_entered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_response_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # A subset of SLA_BREACHES: the targets that were missed.
    sla_breached: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb"), nullable=False
    )
    # While in the future, a bot turn is running for this conversation (one at a time).
    turn_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now(), nullable=False
    )
    # Bumped by every UPDATE (ORM flush or Core `update(Conversation)`); inbox clients drop upserts older than
    # the version they hold. A counter, not a timestamp: concurrent transactions cannot make it go backwards.
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default=sql_text("1"), onupdate=sql_text("conversations.version + 1"), nullable=False
    )


class Message(TenantMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(_one_of("role", MESSAGE_ROLES), name="ck_messages_role"),
        Index("ix_messages_conversation_id_created_at", "conversation_id", "created_at"),
        # A resent customer message (same client id) is not stored twice.
        Index(
            "uq_messages_conversation_id_client_id",
            "conversation_id",
            "client_id",
            unique=True,
            postgresql_where=sql_text("client_id IS NOT NULL"),
        ),
        Index("ix_messages_search_vector", "search_vector", postgresql_using="gin"),
        # The inbox's newest-message lookups (DISTINCT ON conversation_id ... created_at DESC, id DESC).
        Index(
            "ix_messages_conversation_id_created_at_id_desc",
            "conversation_id",
            sql_text("created_at DESC"),
            sql_text("id DESC"),
        ),
        Index(
            "ix_messages_author_membership_id",
            "author_membership_id",
            postgresql_where=sql_text("author_membership_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # [{chunk_id, document_id, document_name}]
    citations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb"), nullable=False
    )
    client_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The team member who wrote an `agent` message; cleared if the member is removed.
    author_membership_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memberships.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now(), nullable=False
    )
    # Full-text index of `text` for the inbox search; Postgres computes it, never loaded with the row.
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', text)", persisted=True), nullable=True, deferred=True
    )


class BotTurn(TenantMixin, Base):
    """What the bot did for one customer message: the risk decision, retrieval, tool calls and model usage."""

    __tablename__ = "bot_turns"
    __table_args__ = (
        CheckConstraint(_one_of("outcome", TURN_OUTCOMES), name="ck_bot_turns_outcome"),
        Index("ix_bot_turns_conversation_id_created_at", "conversation_id", "created_at"),
        # The knowledge gaps report (newest first, by range and cursor).
        Index("ix_bot_turns_organisation_id_created_at_id", "organisation_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    # The customer message that started the turn, and the reply it produced (if any).
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    reply_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    # The model replied [NO_ANSWER]; feeds the knowledge-gaps report.
    no_answer: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sql_text("false"), nullable=False)
    risk: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=sql_text("'{}'::jsonb"), nullable=False
    )
    # {chunks: [{chunk_id, kb_id, score, confidence, rerank_score}], warnings, timings_ms}
    retrieval: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=sql_text("'{}'::jsonb"), nullable=False
    )
    # [{tool_id, name, status, latency_ms}] (never arguments or responses)
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb"), nullable=False
    )
    # {provider, model, input_tokens, output_tokens}
    model: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=sql_text("'{}'::jsonb"), nullable=False
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now(), nullable=False
    )
