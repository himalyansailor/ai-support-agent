"""Analytics: the conversation event log the dashboards are computed from, and its hourly rollups."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from supportcore.db.base import Base, TenantMixin, uuid_pk

__all__ = [
    "AgentHourlyStats",
    "ConversationEvent",
    "IntegrationEvent",
    "ModelPrice",
    "OrgHourlyStats",
    "ReasonHourlyStats",
    "RollupDirtyHour",
    "RollupWatermark",
]


class ConversationEvent(TenantMixin, Base):
    """One thing that happened to a conversation (see supportcore.analytics.events for the kinds). Append-only.
    `data` holds ids, numbers and rule keys, never message text, emails or names."""

    __tablename__ = "conversation_events"
    __table_args__ = (
        Index("ix_conversation_events_organisation_id_occurred_at", "organisation_id", "occurred_at"),
        # The rollup's per-conversation lookups (the latest entered_team before an event, ...); also serves the
        # conversation foreign key.
        Index("ix_conversation_events_conversation_id_kind_occurred_at", "conversation_id", "kind", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # The database clock, so the order matches the commit order across replicas.
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    membership_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=sql_text("'{}'::jsonb"), nullable=False
    )


class ModelPrice(Base):
    """What a chat model costs, entered by the platform admin (nothing is seeded). Not a tenant table:
    one price per (provider, model) for every organisation. Integer micro-units of `currency` per million
    tokens."""

    __tablename__ = "model_prices"
    __table_args__ = (UniqueConstraint("provider", "model", name="uq_model_prices_provider_model"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    input_per_million_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    output_per_million_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", server_default="USD", nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class IntegrationEvent(TenantMixin, Base):
    """How often an integration failed in one UTC hour: one row per (organisation, kind, provider, error_type,
    hour), with a count (see supportcore.analytics.integration_errors). Holds a short error code, never message
    text, urls or secrets."""

    __tablename__ = "integration_events"
    __table_args__ = (
        UniqueConstraint(
            "organisation_id", "kind", "provider", "error_type", "hour", name="uq_integration_events_bucket"
        ),
        Index("ix_integration_events_organisation_id_last_seen_at", "organisation_id", "last_seen_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(200), nullable=False)  # a provider id, or a tool name
    error_type: Mapped[str] = mapped_column(String(64), nullable=False)
    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # UTC hour start
    count: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _count() -> Mapped[int]:
    return mapped_column(Integer, default=0, nullable=False)


def _total() -> Mapped[int]:
    return mapped_column(BigInteger, default=0, nullable=False)


class OrgHourlyStats(TenantMixin, Base):
    """One organisation's numbers for one UTC hour and channel (supportcore.analytics.rollup; the metric
    definitions are in supportcore.analytics.definitions). Rewritten whole by every rollup of the hour."""

    __tablename__ = "org_hourly_stats"
    __table_args__ = (UniqueConstraint("organisation_id", "hour", "channel", name="uq_org_hourly_stats_bucket"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # UTC hour start
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    conversations_started: Mapped[int] = _count()
    entered_team: Mapped[int] = _count()
    resolved: Mapped[int] = _count()
    ai_resolved: Mapped[int] = _count()
    first_response_count: Mapped[int] = _count()
    first_response_seconds_sum: Mapped[int] = _total()
    resolution_count: Mapped[int] = _count()
    resolution_seconds_sum: Mapped[int] = _total()
    csat_count: Mapped[int] = _count()
    csat_sum: Mapped[int] = _count()
    csat_positive: Mapped[int] = _count()
    sla_breaches: Mapped[int] = _count()
    messages_customer: Mapped[int] = _count()
    messages_bot: Mapped[int] = _count()
    messages_agent: Mapped[int] = _count()
    bot_turns: Mapped[int] = _count()
    knowledge_gaps: Mapped[int] = _count()
    input_tokens: Mapped[int] = _total()
    output_tokens: Mapped[int] = _total()
    cost_micros: Mapped[int] = _total()  # priced turns only
    cost_priced_turns: Mapped[int] = _count()  # turns with a model and a cost; cost_micros is unknown at 0
    cost_unpriced_turns: Mapped[int] = _count()


class AgentHourlyStats(TenantMixin, Base):
    """One team member's numbers for one UTC hour. `membership_id` has no foreign key: a removed member's
    history stays."""

    __tablename__ = "agent_hourly_stats"
    __table_args__ = (
        UniqueConstraint("organisation_id", "hour", "membership_id", name="uq_agent_hourly_stats_bucket"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    membership_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    assigned: Mapped[int] = _count()
    handled: Mapped[int] = _count()
    handle_seconds_sum: Mapped[int] = _total()
    first_responses: Mapped[int] = _count()
    first_response_seconds_sum: Mapped[int] = _total()
    csat_count: Mapped[int] = _count()
    csat_sum: Mapped[int] = _count()


class ReasonHourlyStats(TenantMixin, Base):
    """Counts per reason for one UTC hour: `kind` is `escalation` (key: the rule key or the source),
    `topic` (key: `topic:<id>`) or `knowledge_gap_kb` (key: the knowledge base id). `label` is a rule or
    knowledge base name, never customer text."""

    __tablename__ = "reason_hourly_stats"
    __table_args__ = (
        UniqueConstraint("organisation_id", "hour", "kind", "key", name="uq_reason_hourly_stats_bucket"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    count: Mapped[int] = _count()


class RollupDirtyHour(TenantMixin, Base):
    """An hour to roll up again: an event was recorded with an `occurred_at` older than the job's window.
    Not unique: events.record only adds rows (it runs inside the caller's transaction, without a query), and
    the job reads the distinct hours and deletes the rows it read."""

    __tablename__ = "rollup_dirty_hours"
    __table_args__ = (Index("ix_rollup_dirty_hours_organisation_id_hour", "organisation_id", "hour"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RollupWatermark(TenantMixin, Base):
    """Every hour of the organisation before `rolled_through` has been rolled up, so a job that was down for
    longer than its window catches up from here."""

    __tablename__ = "rollup_watermarks"
    __table_args__ = (UniqueConstraint("organisation_id", name="uq_rollup_watermarks_organisation_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    rolled_through: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
