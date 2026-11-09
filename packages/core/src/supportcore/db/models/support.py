"""Per-organisation support settings: business hours, holidays, SLA targets and the round-robin pointer."""

import uuid
from typing import Any

from sqlalchemy import ForeignKey, String
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from supportcore.db.base import Base, TenantMixin, TimestampMixin

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_WEEKLY_HOURS: dict[str, list[list[str]]] = {
    day: ([["09:00", "17:00"]] if day in WEEKDAYS[:5] else []) for day in WEEKDAYS
}
# Business minutes.
DEFAULT_SLA_TARGETS: dict[str, dict[str, int]] = {
    "urgent": {"first_response": 15, "resolution": 240},
    "high": {"first_response": 60, "resolution": 480},
    "normal": {"first_response": 240, "resolution": 1440},
    "low": {"first_response": 480, "resolution": 2880},
}


class SupportSettings(TenantMixin, TimestampMixin, Base):
    """One row per organisation, created lazily."""

    __tablename__ = "support_settings"

    # Overrides TenantMixin's column: the organisation id is the primary key here.
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organisations.id", ondelete="CASCADE"), primary_key=True
    )
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", server_default="UTC", nullable=False)
    weekly_hours: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    holidays: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb"), nullable=False
    )
    sla_targets: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Round-robin pointer (updated under a row lock by assignment).
    last_assigned_membership_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memberships.id", ondelete="SET NULL"), nullable=True
    )
