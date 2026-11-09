"""Billing: plans (platform), each organisation's billing account, the usage ledger and the provider's webhook log."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from supportcore.db.base import Base, TenantMixin, TimestampMixin, uuid_pk

METERS = ("messages", "voice_minutes")
CAPACITY_RESOURCES = ("seats", "knowledge_bases", "storage_mb", "phone_lines")
ACCOUNT_STATUSES = ("no_plan", "trialing", "active", "past_due", "canceled", "incomplete")
LIMIT_BEHAVIOURS = ("hard_stop", "overage")
PLAN_SYNC_STATUSES = ("pending", "synced", "failed")
PRICE_KINDS = ("base", "messages", "voice_minutes")
USAGE_REPORT_STATUSES = ("pending", "sent", "failed")

__all__ = [
    "ACCOUNT_STATUSES",
    "CAPACITY_RESOURCES",
    "LIMIT_BEHAVIOURS",
    "METERS",
    "PLAN_SYNC_STATUSES",
    "PRICE_KINDS",
    "USAGE_REPORT_STATUSES",
    "BillingAccount",
    "BillingWebhookEvent",
    "Plan",
    "PlanProviderPrice",
    "UsageAlert",
    "UsageEvent",
    "UsageReport",
]


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _nullable_int() -> Mapped[int | None]:
    return mapped_column(Integer, nullable=True)


class Plan(TimestampMixin, Base):
    """A plan the platform admin offers. Platform table (no organisation). A limit of None means unlimited; an
    overage price of None means that meter has no overage."""

    __tablename__ = "plans"
    __table_args__ = (
        CheckConstraint("monthly_price_minor >= 0", name="ck_plans_monthly_price_minor"),
        CheckConstraint("trial_days BETWEEN 0 AND 90", name="ck_plans_trial_days"),
        CheckConstraint(_one_of("sync_status", PLAN_SYNC_STATUSES), name="ck_plans_sync_status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    key: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    # Offered to new organisations; existing subscribers keep an inactive plan.
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sql_text("true"), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default=sql_text("0"), nullable=False)
    monthly_price_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    trial_days: Mapped[int] = mapped_column(Integer, default=0, server_default=sql_text("0"), nullable=False)
    seats: Mapped[int | None] = _nullable_int()
    messages: Mapped[int | None] = _nullable_int()
    voice_minutes: Mapped[int | None] = _nullable_int()
    storage_mb: Mapped[int | None] = _nullable_int()
    knowledge_bases: Mapped[int | None] = _nullable_int()
    phone_lines: Mapped[int | None] = _nullable_int()
    # Micros (1e-6 of the currency unit) per message / per voice minute beyond the limit.
    messages_overage_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    voice_minutes_overage_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_product_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sync_status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending", nullable=False
    )
    # A short code, never the provider's message.
    sync_error: Mapped[str | None] = mapped_column(String(64), nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlanProviderPrice(Base):
    """A price object at the billing provider. Edited plans get new prices; old ones stay (inactive) so webhooks
    for earlier subscribers still resolve to the plan. Platform table."""

    __tablename__ = "plan_provider_prices"
    __table_args__ = (
        UniqueConstraint("provider", "price_id", name="uq_plan_provider_prices_provider_price_id"),
        CheckConstraint(_one_of("kind", PRICE_KINDS), name="ck_plan_provider_prices_kind"),
        Index("ix_plan_provider_prices_plan_id", "plan_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    price_id: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)  # base only
    # Metered only: what the price was created with (the plan's limit for its meter, which the provider bills as free
    # units, and the overage price). A subscriber's limit and overage come from these, not the plan's current row.
    included_units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overage_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sql_text("true"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.timezone("utc", func.now()), nullable=False
    )


class BillingAccount(TenantMixin, TimestampMixin, Base):
    """One per organisation: its plan, subscription state and the provider's ids."""

    __tablename__ = "billing_accounts"
    __table_args__ = (
        UniqueConstraint("organisation_id", name="uq_billing_accounts_organisation_id"),
        CheckConstraint(_one_of("status", ACCOUNT_STATUSES), name="ck_billing_accounts_status"),
        CheckConstraint(_one_of("limit_behaviour", LIMIT_BEHAVIOURS), name="ck_billing_accounts_limit_behaviour"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="no_plan", server_default="no_plan", nullable=False)
    limit_behaviour: Mapped[str] = mapped_column(
        String(16), default="hard_stop", server_default="hard_stop", nullable=False
    )
    trial_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    provider_subscription_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    subscription_price_ids: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb"), nullable=False
    )
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sql_text("false"), nullable=False
    )
    # The provider timestamp of the newest event applied: an older snapshot never overwrites a newer one.
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UsageReport(TenantMixin, Base):
    """One batch of usage sent to the billing provider. Its id is the provider's idempotency identifier."""

    __tablename__ = "usage_reports"
    __table_args__ = (
        CheckConstraint(_one_of("meter", METERS), name="ck_usage_reports_meter"),
        CheckConstraint(_one_of("status", USAGE_REPORT_STATUSES), name="ck_usage_reports_status"),
        CheckConstraint("quantity > 0", name="ck_usage_reports_quantity"),
        Index("ix_usage_reports_organisation_id_status", "organisation_id", "status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    meter: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider_customer_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # The latest occurred_at among the events included.
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=sql_text("0"), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.timezone("utc", func.now()), nullable=False
    )


class UsageEvent(TenantMixin, Base):
    """The usage ledger: one row per billable thing (a message, a call's minutes). Append-only, and the source
    the Redis counters are rebuilt from. `source_id` makes recording idempotent."""

    __tablename__ = "usage_events"
    __table_args__ = (
        UniqueConstraint("organisation_id", "meter", "source_id", name="uq_usage_events_organisation_id_meter_source_id"),
        CheckConstraint(_one_of("meter", METERS), name="ck_usage_events_meter"),
        CheckConstraint("quantity > 0", name="ck_usage_events_quantity"),
        Index("ix_usage_events_organisation_id_meter_occurred_at", "organisation_id", "meter", "occurred_at"),
        Index("ix_usage_events_report_id", "report_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    meter: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    # The database clock, so the order matches the commit order across replicas.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    report_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usage_reports.id", ondelete="SET NULL"), nullable=True
    )
    # Counted for limits but never reported to the provider (no overage to bill).
    skipped: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sql_text("false"), nullable=False)


class UsageAlert(TenantMixin, Base):
    """A billing warning for a billing period, recorded once (the unique key) and emailed by the alerts job.
    `sent_at` stays null until the email went out, so a failed send is retried on the next run."""

    __tablename__ = "usage_alerts"
    __table_args__ = (
        UniqueConstraint("organisation_id", "period_start", "kind", name="uq_usage_alerts_organisation_id_period_start_kind"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BillingWebhookEvent(Base):
    """A provider webhook already received, so a redelivery does nothing. Platform table."""

    __tablename__ = "billing_webhook_events"
    __table_args__ = (UniqueConstraint("provider", "event_id", name="uq_billing_webhook_events_provider_event_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(100), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.timezone("utc", func.now()), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
