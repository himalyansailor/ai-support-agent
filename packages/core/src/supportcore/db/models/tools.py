"""Business tools: HTTP endpoints of the organisation's own API that the bot may call."""

import uuid
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, String, UniqueConstraint
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from supportcore.db.base import Base, TenantMixin, TimestampMixin, uuid_pk

TOOL_METHODS = ("GET", "POST")


class BusinessTool(TenantMixin, TimestampMixin, Base):
    __tablename__ = "business_tools"
    __table_args__ = (
        UniqueConstraint("organisation_id", "name", name="uq_business_tools_organisation_id_name"),
        CheckConstraint("method IN ('GET', 'POST')", name="ck_business_tools_method"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    # The function name the model sees.
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    url_template: Mapped[str] = mapped_column(String(2000), nullable=False)
    auth_header_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Encrypted at rest (SecretCipher); only last4 is ever shown.
    encrypted_auth_header_value: Mapped[str | None] = mapped_column(String, nullable=True)
    auth_header_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    input_schema: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=sql_text("'{}'::jsonb"), nullable=False
    )
    read_only: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sql_text("true"), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sql_text("true"), nullable=False)
