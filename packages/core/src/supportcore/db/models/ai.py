import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from supportcore.db.base import Base, TenantMixin, TimestampMixin, uuid_pk


class ProviderCredential(TenantMixin, TimestampMixin, Base):
    """An org-provided LLM/embedding API key. Only the ciphertext and the last 4 characters are stored."""

    __tablename__ = "provider_credentials"
    # Labels are unique per organisation, case-insensitively (matches CredentialService's check).
    __table_args__ = (
        Index(
            "uq_provider_credentials_organisation_id_lower_label",
            "organisation_id",
            func.lower(text("label")),
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    last4: Mapped[str] = mapped_column(String(4), nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # never include the ciphertext
        return f"ProviderCredential(id={self.id}, provider={self.provider!r}, label={self.label!r}, last4=***)"


class OrganisationAISettings(TenantMixin, TimestampMixin, Base):
    """Per-organisation chat model selection; one row per organisation, created lazily."""

    __tablename__ = "organisation_ai_settings"

    # Overrides TenantMixin's column: the organisation id is the primary key here.
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organisations.id", ondelete="CASCADE"), primary_key=True
    )
    chat_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    chat_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    chat_credential_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provider_credentials.id", ondelete="SET NULL"), nullable=True, index=True
    )
    temperature: Mapped[float] = mapped_column(Float, default=0.2, server_default=text("0.2"), nullable=False)
