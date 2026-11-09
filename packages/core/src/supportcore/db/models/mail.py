"""The email channel: a mailbox per organisation, the email details of a message, and its stored attachments."""

import uuid
from datetime import UTC, datetime
from typing import Any

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

MAILBOX_STATUSES = ("active", "auth_failed", "error")
MAIL_SECURITIES = ("ssl", "starttls", "none")
EMAIL_DIRECTIONS = ("inbound", "outbound")
EMAIL_DELIVERY_STATUSES = ("received", "queued", "sending", "sent", "retry", "failed")


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _now() -> datetime:
    return datetime.now(UTC)


class EmailMailbox(TenantMixin, TimestampMixin, Base):
    """The organisation's support mailbox: IMAP to read it, SMTP to answer from it. Passwords are Fernet ciphertext."""

    __tablename__ = "email_mailboxes"
    __table_args__ = (
        UniqueConstraint("organisation_id", name="uq_email_mailboxes_organisation_id"),
        CheckConstraint(_one_of("status", MAILBOX_STATUSES), name="ck_email_mailboxes_status"),
        CheckConstraint(_one_of("smtp_status", MAILBOX_STATUSES), name="ck_email_mailboxes_smtp_status"),
        CheckConstraint(_one_of("imap_security", MAIL_SECURITIES), name="ck_email_mailboxes_imap_security"),
        CheckConstraint(_one_of("smtp_security", MAIL_SECURITIES), name="ck_email_mailboxes_smtp_security"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    address: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), default="", server_default="", nullable=False)
    imap_host: Mapped[str] = mapped_column(String(253), nullable=False)
    imap_port: Mapped[int] = mapped_column(Integer, default=993, server_default=sql_text("993"), nullable=False)
    imap_security: Mapped[str] = mapped_column(String(16), default="ssl", server_default="ssl", nullable=False)
    imap_username: Mapped[str] = mapped_column(String(320), nullable=False)
    imap_password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    imap_folder: Mapped[str] = mapped_column(String(200), default="INBOX", server_default="INBOX", nullable=False)
    smtp_host: Mapped[str] = mapped_column(String(253), nullable=False)
    smtp_port: Mapped[int] = mapped_column(Integer, default=587, server_default=sql_text("587"), nullable=False)
    smtp_security: Mapped[str] = mapped_column(String(16), default="starttls", server_default="starttls", nullable=False)
    smtp_username: Mapped[str] = mapped_column(String(320), nullable=False)
    smtp_password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sql_text("true"), nullable=False)
    # The IMAP (receiving) side: written by sync runs only; `auth_failed` slows the sync fan-out down.
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active", nullable=False)
    last_error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The SMTP (sending) side: written by send runs only, so a failing SMTP login never delays inbound sync.
    smtp_status: Mapped[str] = mapped_column(String(16), default="active", server_default="active", nullable=False)
    smtp_error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    smtp_last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # IMAP sync cursor: UIDs are only comparable while UIDVALIDITY is unchanged.
    uidvalidity: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_uid: Mapped[int] = mapped_column(BigInteger, default=0, server_default=sql_text("0"), nullable=False)
    # While in the future, a sync run owns this mailbox.
    sync_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # never the ciphertext, address or hosts
        return f"EmailMailbox(id={self.id!s}, organisation_id={self.organisation_id!s}, status={self.status!r})"


class EmailMessage(TenantMixin, Base):
    """The email side of a conversation message (inbound or outbound): headers, sanitised HTML, delivery state."""

    __tablename__ = "email_messages"
    __table_args__ = (
        CheckConstraint(_one_of("direction", EMAIL_DIRECTIONS), name="ck_email_messages_direction"),
        CheckConstraint(_one_of("delivery_status", EMAIL_DELIVERY_STATUSES), name="ck_email_messages_delivery_status"),
        UniqueConstraint("message_id", name="uq_email_messages_message_id"),
        Index("uq_email_messages_organisation_id_rfc_message_id", "organisation_id", "rfc_message_id", unique=True),
        Index("ix_email_messages_conversation_id_created_at", "conversation_id", "created_at"),
        # Loop guard: how many mails this sender (any letter case) sent this mailbox recently.
        Index(
            "ix_email_messages_mailbox_id_lower_from_address_created_at",
            "mailbox_id",
            func.lower(sql_text("from_address")),
            "created_at",
        ),
        # The outbound sweep: rows still to send, or being sent.
        Index(
            "ix_email_messages_outbound_pending",
            "delivery_status",
            "sending_until",
            postgresql_where=sql_text(
                "direction = 'outbound' AND delivery_status IN ('queued', 'retry', 'sending')"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    mailbox_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_mailboxes.id", ondelete="SET NULL"), nullable=True
    )
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    rfc_message_id: Mapped[str] = mapped_column(String(998), nullable=False)
    in_reply_to: Mapped[str | None] = mapped_column(String(998), nullable=True)
    references: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb"), nullable=False
    )
    subject: Mapped[str | None] = mapped_column(String(998), nullable=True)
    from_address: Mapped[str] = mapped_column(String(320), nullable=False)
    from_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    to_addresses: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb"), nullable=False
    )
    cc_addresses: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb"), nullable=False
    )
    reply_to: Mapped[str | None] = mapped_column(String(320), nullable=True)
    # Sanitised (nh3) HTML body; None when the mail had only text.
    html: Mapped[str | None] = mapped_column(Text, nullable=True)
    has_remote_images: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sql_text("false"), nullable=False
    )
    # [{filename, size, reason}] of attachments that were not stored.
    skipped_attachments: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb"), nullable=False
    )
    imap_uid: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    delivery_status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=sql_text("0"), nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # While in the future, a sender owns this outbound row (the send claim).
    sending_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now(), nullable=False
    )


class EmailAttachment(TenantMixin, Base):
    __tablename__ = "email_attachments"
    __table_args__ = (Index("ix_email_attachments_email_message_id", "email_message_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    email_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("email_messages.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_id: Mapped[str | None] = mapped_column(String(998), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now(), nullable=False
    )
