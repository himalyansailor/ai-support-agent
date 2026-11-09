"""Embeddable chat widgets and the knowledge bases each one answers from."""

import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from supportcore.db.base import Base, TenantMixin, TimestampMixin, uuid_pk


class Widget(TenantMixin, TimestampMixin, Base):
    __tablename__ = "widgets"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Identifies the widget (and so the organisation) in the public API. Not secret.
    public_key: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sql_text("true"), nullable=False)
    allowed_domains: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb"), nullable=False
    )
    appearance: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=sql_text("'{}'::jsonb"), nullable=False
    )
    csat_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sql_text("true"), nullable=False)
    # Signed-identity secret: encrypted at rest (SecretCipher); only last4 is ever shown.
    encrypted_identity_secret: Mapped[str | None] = mapped_column(String, nullable=True)
    identity_secret_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    identity_required: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sql_text("false"), nullable=False
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class WidgetKnowledgeBase(Base):
    """Which knowledge bases a widget's bot searches. Not a tenant table: both ends are tenant rows
    and the service verifies the KB belongs to the widget's organisation before linking."""

    __tablename__ = "widget_knowledge_bases"

    widget_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("widgets.id", ondelete="CASCADE"), primary_key=True
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), primary_key=True, index=True
    )
