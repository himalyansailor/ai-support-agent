"""Puts a parsed inbound email into the human inbox: customer, conversation, message, attachment rows, routing
and the realtime publish. It runs in the caller's transaction (the caller commits; nothing is published before
that). Attachments are already in storage (`store_attachments`, called with no session open).

Nothing here logs or records an address, subject, name, file name or body: ids, counts and flags only.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.analytics.events import record
from supportcore.assignment.service import AssignmentService
from supportcore.billing.usage import record_usage
from supportcore.config import Settings
from supportcore.conversations.service import ConversationService, ensure_transition
from supportcore.db.models import Conversation, Customer, EmailAttachment, EmailMailbox, EmailMessage, Organisation
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.events import EventBus
from supportcore.inbox import sla
from supportcore.inbox.publisher import InboxPublisher
from supportcore.mail.attachments import StoredAttachment
from supportcore.mail.parse import ParsedEmail
from supportcore.mail.sanitize import SanitizedHtml
from supportcore.mail.thread_match import find_thread
from supportcore.observability.logging import get_logger

if TYPE_CHECKING:
    from supportcore.wiring import Adapters

__all__ = ["InboundEmailService", "IngestResult"]

logger = get_logger(__name__)

EMAIL_DECISION = {"action": "escalate", "rule_key": None, "rule_name": None, "reason": None, "source": "email"}


@dataclass(frozen=True)
class IngestResult:
    conversation_id: UUID | None
    message_id: UUID | None
    created: bool  # a new conversation was started
    duplicate: bool  # the Message-ID was already stored; nothing was written


class InboundEmailService:
    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        events: EventBus,
        adapters: "Adapters",
        settings: Settings,
        publisher: InboxPublisher,
    ) -> None:
        self._session = session
        self._org_id = organisation_id
        self._events = events
        self._adapters = adapters
        self._settings = settings
        self._publisher = publisher
        self._customers = TenantScopedRepository(session, Customer, organisation_id)
        self._emails = TenantScopedRepository(session, EmailMessage, organisation_id)
        # No publisher: the one `conversation_changed` per ingest comes from route_to_team or from ingest itself.
        self._conversations = ConversationService(session, organisation_id, events)

    async def ingest(
        self,
        mailbox: EmailMailbox,
        parsed: ParsedEmail,
        sanitized: SanitizedHtml | None,
        stored: list[StoredAttachment],
        skipped: list[dict],
        imap_uid: int | None,
    ) -> IngestResult:
        """A concurrent ingest of the same Message-ID loses at the unique index (IntegrityError): the caller
        rolls back and treats it as a duplicate."""
        existing = (
            await self._session.execute(
                self._emails.select(EmailMessage.message_id, EmailMessage.conversation_id).where(
                    EmailMessage.rfc_message_id == parsed.message_id
                )
            )
        ).first()
        if existing is not None:
            return IngestResult(existing.conversation_id, existing.message_id, created=False, duplicate=True)

        now = datetime.now(UTC)
        customer = await self._customer(parsed, now)
        conversation = await find_thread(self._session, self._org_id, parsed, customer.id, now)
        created = False
        routed = True  # route_to_team publishes the upsert
        if conversation is None or conversation.status == "closed":
            conversation = await self._start(customer, parsed)
            created = True
        else:
            routed = await self._continue(conversation, now)

        message = await self._conversations.add_message(conversation, "customer", parsed.text)
        # Email ingest stores its message directly (not through add_customer_message), so it is metered here.
        await record_usage(
            self._session, self._org_id, "messages", 1, message.id, counter=self._adapters.usage_counter
        )
        email = EmailMessage(
            message_id=message.id,
            conversation_id=conversation.id,
            mailbox_id=mailbox.id,
            direction="inbound",
            rfc_message_id=parsed.message_id,
            in_reply_to=parsed.in_reply_to,
            references=list(parsed.references),
            subject=parsed.subject,
            from_address=parsed.from_address,
            from_name=parsed.from_name or None,
            to_addresses=list(parsed.to),
            cc_addresses=list(parsed.cc),
            reply_to=parsed.reply_to,
            html=sanitized.html if sanitized is not None else None,
            has_remote_images=sanitized.has_remote_images if sanitized is not None else False,
            skipped_attachments=list(skipped),
            imap_uid=imap_uid,
            delivery_status="received",
        )
        await self._emails.add(email)
        await self._session.flush()
        attachments = TenantScopedRepository(self._session, EmailAttachment, self._org_id)
        for att in stored:
            await attachments.add(
                EmailAttachment(
                    email_message_id=email.id,
                    filename=att.filename,
                    content_type=att.content_type,
                    size_bytes=att.size_bytes,
                    sha256=att.sha256,
                    storage_key=att.storage_key,
                    content_id=att.content_id,
                )
            )
        await self._session.flush()
        if not routed:
            self._publisher.conversation_changed(self._session, self._org_id, conversation.id)
        logger.info(
            "email_ingested",
            organisation_id=str(self._org_id),
            conversation_id=str(conversation.id),
            message_id=str(message.id),
            created=created,
            attachments=len(stored),
        )
        return IngestResult(conversation.id, message.id, created=created, duplicate=False)

    async def handle_bounce(self, parsed: ParsedEmail) -> bool:
        """Marks the outbound row this bounce is about `failed`/`bounce`. False when no row of this
        organisation matches (or the bounce names no message)."""
        if not parsed.bounced_message_id:
            return False
        row = (
            await self._session.execute(
                self._emails.select().where(
                    EmailMessage.direction == "outbound", EmailMessage.rfc_message_id == parsed.bounced_message_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        if not (row.delivery_status == "failed" and row.error_type == "bounce"):
            row.delivery_status = "failed"
            row.error_type = "bounce"
            row.sending_until = None
            row.next_attempt_at = None
            await self._session.flush()
            self._publisher.email_status(
                self._session, self._org_id, row.conversation_id, row.message_id, "failed", error_type="bounce"
            )
            logger.info(
                "email_bounced",
                organisation_id=str(self._org_id),
                conversation_id=str(row.conversation_id),
                message_id=str(row.message_id),
            )
        return True

    # --- internals -----------------------------------------------------------------------------------

    async def _customer(self, parsed: ParsedEmail, now: datetime) -> Customer:
        address = parsed.from_address.lower()
        found = (
            await self._session.execute(
                self._customers.select()
                .where(func.lower(Customer.email) == address)
                .order_by(Customer.last_seen_at.desc().nulls_last(), Customer.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if found is None:
            found = Customer(email=parsed.from_address, name=parsed.from_name or None)
            await self._customers.add(found)
        elif not found.name and parsed.from_name:
            found.name = parsed.from_name
        found.last_seen_at = now
        await self._session.flush()
        return found

    async def _start(self, customer: Customer, parsed: ParsedEmail) -> Conversation:
        organisation = await self._session.get(Organisation, self._org_id)
        language = organisation.language if organisation is not None else "en"
        conversation = await self._conversations.create(
            customer.id, None, language, False, channel="email", subject=parsed.subject
        )
        conversation.contact_email = parsed.from_address
        await self._route(conversation)
        return conversation

    async def _route(self, conversation: Conversation) -> None:
        assignment = AssignmentService(
            self._session, self._org_id, self._events, self._adapters, self._settings, publisher=self._publisher
        )
        await assignment.route_to_team(conversation, EMAIL_DECISION, announce=False)

    async def _continue(self, conversation: Conversation, now: datetime) -> bool:
        """A customer message in an existing thread, by its status (`closed` never gets here). True when it
        was routed to the team (route_to_team already queued the inbox upsert)."""
        # The row as of the lock, so two ingests of one thread do not both transition it.
        await TenantScopedRepository(self._session, Conversation, self._org_id).get(conversation.id, for_update=True)
        await self._session.refresh(conversation)
        status = conversation.status
        if status == "pending":
            ensure_transition("pending", "open", "customer_message")
            conversation.status = "open"
        elif status == "resolved":
            ensure_transition("resolved", "open", "reopen")
            conversation.resolved_at = None
            await sla.on_reopen(self._session, self._org_id, self._events, conversation, now)
            conversation.status = "open"
            record(self._session, self._org_id, conversation.id, "reopened", channel=conversation.channel)
        elif status == "bot":
            await self._route(conversation)
            return True
        await self._session.flush()
        return False

