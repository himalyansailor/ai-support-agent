"""Read models of the email side of messages for the inbox: per-message metadata, sanitised HTML, attachments.

Everything goes through TenantScopedRepository. Addresses, subjects and file names are returned to the agent but
never logged.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.db.models import EmailAttachment, EmailMessage
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import NotFoundError
from supportcore.mail.attachments import ALLOWED_TYPES

__all__ = ["AttachmentFile", "EmailAttachmentView", "EmailMeta", "attachment_file", "email_meta_for", "sanitized_html"]

DOWNLOAD_FALLBACK_TYPE = "application/octet-stream"


@dataclass(frozen=True)
class EmailAttachmentView:
    id: UUID
    filename: str
    content_type: str
    size_bytes: int


@dataclass(frozen=True)
class EmailMeta:
    direction: str
    subject: str | None
    from_address: str
    from_name: str | None
    to: list[str]
    cc: list[str]
    has_html: bool
    has_remote_images: bool
    attachments: list[EmailAttachmentView] = field(default_factory=list)
    skipped_attachments: list[dict[str, Any]] = field(default_factory=list)
    delivery_status: str = "received"
    error_type: str | None = None
    # Inbound only: where a reply goes when it differs from From (the composer shows the real recipient).
    reply_to: str | None = None


@dataclass(frozen=True)
class AttachmentFile:
    """What the download route needs once the session is released."""

    storage_key: str
    filename: str
    content_type: str  # the stored type if allowlisted, else application/octet-stream
    size_bytes: int


async def email_meta_for(session: AsyncSession, organisation_id: UUID, message_ids: list[UUID]) -> dict[UUID, EmailMeta]:
    """Email metadata keyed by conversation message id; messages without an email row are absent."""
    if not message_ids:
        return {}
    emails = (
        (
            await session.execute(
                TenantScopedRepository(session, EmailMessage, organisation_id)
                .select()
                .where(EmailMessage.message_id.in_(message_ids))
            )
        )
        .scalars()
        .all()
    )
    if not emails:
        return {}
    attachments: dict[UUID, list[EmailAttachmentView]] = defaultdict(list)
    rows = (
        await session.execute(
            TenantScopedRepository(session, EmailAttachment, organisation_id)
            .select()
            .where(EmailAttachment.email_message_id.in_([e.id for e in emails]))
            .order_by(EmailAttachment.created_at, EmailAttachment.id)
        )
    ).scalars()
    for a in rows:
        attachments[a.email_message_id].append(EmailAttachmentView(a.id, a.filename, a.content_type, a.size_bytes))
    return {
        e.message_id: EmailMeta(
            direction=e.direction,
            subject=e.subject,
            from_address=e.from_address,
            from_name=e.from_name,
            to=list(e.to_addresses or []),
            cc=list(e.cc_addresses or []),
            has_html=e.html is not None,
            has_remote_images=e.has_remote_images,
            attachments=attachments.get(e.id, []),
            skipped_attachments=list(e.skipped_attachments or []),
            delivery_status=e.delivery_status,
            error_type=e.error_type,
            reply_to=e.reply_to,
        )
        for e in emails
    }


async def sanitized_html(session: AsyncSession, organisation_id: UUID, conversation_id: UUID, message_id: UUID) -> str:
    """The sanitised HTML of a message of the conversation. NotFoundError when it has none (or is not found)."""
    html = (
        await session.execute(
            TenantScopedRepository(session, EmailMessage, organisation_id)
            .select(EmailMessage.html)
            .where(EmailMessage.conversation_id == conversation_id, EmailMessage.message_id == message_id)
        )
    ).scalar_one_or_none()
    if html is None:
        raise NotFoundError("Email HTML not found")
    return html


async def attachment_file(
    session: AsyncSession, organisation_id: UUID, conversation_id: UUID, attachment_id: UUID
) -> AttachmentFile:
    """The stored attachment of a message of the conversation. NotFoundError for anything else."""
    row = (
        await session.execute(
            TenantScopedRepository(session, EmailAttachment, organisation_id)
            .select(EmailAttachment)
            .join(
                EmailMessage,
                (EmailMessage.id == EmailAttachment.email_message_id)
                & (EmailMessage.organisation_id == EmailAttachment.organisation_id),
            )
            .where(EmailAttachment.id == attachment_id, EmailMessage.conversation_id == conversation_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("Attachment not found")
    content_type = row.content_type if row.content_type in ALLOWED_TYPES else DOWNLOAD_FALLBACK_TYPE
    return AttachmentFile(row.storage_key, row.filename or "attachment", content_type, row.size_bytes)
