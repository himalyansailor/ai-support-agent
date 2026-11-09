"""Which existing conversation an inbound email belongs to (Phase 6 threading order).

1. `In-Reply-To`; 2. `References`, newest first; 3. any match on an inbound or outbound `EmailMessage` of
the organisation (the first candidate id with a row wins); 4. the subject fallback. Every query is
organisation-scoped. Closed conversations are returned by the header chain; the caller decides what a late
reply to one means.
"""

import re
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.db.models import Conversation, EmailMessage
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.mail.parse import ParsedEmail

SUBJECT_FALLBACK_WINDOW = timedelta(days=30)
_SUBJECT_CANDIDATES = 50
_MAX_HEADER_IDS = 21  # In-Reply-To plus 20 References; a longer chain is hostile or broken
_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|sv|wg)\s*:|\[[^\]]*\])\s*", re.IGNORECASE)


def normalize_subject(subject: str | None) -> str:
    text = subject or ""
    while True:
        stripped = _PREFIX.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    return re.sub(r"\s+", " ", text).strip().casefold()


def candidate_ids(parsed: ParsedEmail) -> list[str]:
    ordered = [parsed.in_reply_to, *reversed(parsed.references)]
    seen: set[str] = set()
    out: list[str] = []
    for value in ordered:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


async def find_thread(
    session: AsyncSession, organisation_id: UUID, parsed: ParsedEmail, customer_id: UUID, now: datetime
) -> Conversation | None:
    emails = TenantScopedRepository(session, EmailMessage, organisation_id)
    conversations = TenantScopedRepository(session, Conversation, organisation_id)

    ids = candidate_ids(parsed)[:_MAX_HEADER_IDS]
    if ids:
        rows = (
            await session.execute(
                emails.select(EmailMessage.rfc_message_id, EmailMessage.conversation_id).where(
                    EmailMessage.rfc_message_id.in_(ids)
                )
            )
        ).all()
        by_id = {rfc: conversation_id for rfc, conversation_id in rows}
        for candidate in ids:
            if candidate in by_id:
                found = await conversations.get(by_id[candidate])
                if found is not None:
                    return found

    wanted = normalize_subject(parsed.subject)
    if not wanted:
        return None
    candidates = await conversations.list(
        Conversation.customer_id == customer_id,
        Conversation.status != "closed",
        Conversation.last_message_at >= now - SUBJECT_FALLBACK_WINDOW,
        order_by=[Conversation.last_message_at.desc(), Conversation.id],
        limit=_SUBJECT_CANDIDATES,
    )
    for conversation in candidates:
        if normalize_subject(conversation.subject) == wanted:
            return conversation
    return None
