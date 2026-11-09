"""Which inbound mail is never answered: loops, bounces, auto-replies, floods.

`skip_reason` is pure (headers and addresses only); `db_skip_reason` needs the database. Neither logs
anything, and the reasons are short codes (never an address or a subject).
"""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.db.models import EmailMailbox, EmailMessage
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.mail.parse import ParsedEmail

BULK_PRECEDENCE = frozenset({"bulk", "junk", "list", "auto_reply"})
BOUNCE_LOCAL_PARTS = frozenset({"mailer-daemon", "postmaster"})
RATE_LIMIT_WINDOW = timedelta(hours=1)
RATE_LIMIT_MAX = 20  # this many inbound rows already stored in the window: the next (21st) message is a flood


def skip_reason(parsed: ParsedEmail, mailbox: EmailMailbox) -> str | None:
    sender = parsed.from_address.strip().casefold()
    if parsed.x_support_mailbox and parsed.x_support_mailbox.strip().casefold() == str(mailbox.id).casefold():
        return "own_message"
    if sender and sender == mailbox.address.strip().casefold():
        return "own_address"
    # A bounce is checked before the generic auto-reply headers: bounces carry Auto-Submitted too, and the
    # caller handles them specially (it marks our outbound row failed).
    local_part = sender.rpartition("@")[0]
    if parsed.is_delivery_report or parsed.return_path_empty or local_part in BOUNCE_LOCAL_PARTS:
        return "bounce"
    auto = (parsed.auto_submitted or "").strip().casefold()
    if auto and auto != "no":
        return "auto_submitted"
    if (parsed.precedence or "").strip().casefold() in BULK_PRECEDENCE:
        return "bulk"
    if parsed.has_autoreply_header:
        return "autoreply_header"
    return None


async def db_skip_reason(
    session: AsyncSession, organisation_id: UUID, mailbox_id: UUID, parsed: ParsedEmail, now: datetime
) -> str | None:
    rows = TenantScopedRepository(session, EmailMessage, organisation_id)
    ours = await session.scalar(
        rows.select(EmailMessage.id)
        .where(EmailMessage.direction == "outbound", EmailMessage.rfc_message_id == parsed.message_id)
        .limit(1)
    )
    if ours is not None:
        return "own_message"
    recent = await session.scalar(
        rows.select(func.count(EmailMessage.id)).where(
            EmailMessage.direction == "inbound",
            EmailMessage.mailbox_id == mailbox_id,
            # Case-insensitive, so a loop that changes the address case is still counted (matches the lower() index).
            func.lower(EmailMessage.from_address) == parsed.from_address.lower(),
            EmailMessage.created_at >= now - RATE_LIMIT_WINDOW,
        )
    )
    if (recent or 0) >= RATE_LIMIT_MAX:
        return "rate_limited"
    return None
