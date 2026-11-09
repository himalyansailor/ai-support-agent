"""Agent replies by email: the outbound row with its threading headers, and the `send_email` job.

`OutboundEmailService` runs in the caller's transaction (the reply route): `ensure_can_email` decides whether a
reply is emailed, `queue_reply` writes the outbound `EmailMessage` (`queued`) and enqueues `send_email` after the
commit. The Message-ID is generated once, when the row is created, so a retry sends the very same message.

One `send_email` run:

1. Claim: one atomic UPDATE moves the row from `queued` (or `retry` once its `next_attempt_at` is due) to
   `sending`, counts the attempt and sets `sending_until` from the database clock (now() + CLAIM_SECONDS); then
   commit. A row whose mailbox was disconnected (mailbox_id NULL) is sent through the organisation's current
   mailbox (from its address), so a retry after a reconnect works. A row already `sending` belongs to another run, unless its claim expired (database clock): that run died
   mid-send, so the row becomes `failed`/`interrupted` and is never sent again (it may have gone out).
2. Send with no session open (the adapter's own deadline is below the claim).
3. Record the outcome, only while the claim is still this run's: `sent`; a transient failure before the
   MAX_ATTEMPTS-th attempt -> `retry` and the job again after RETRY_DELAYS[attempts - 1] seconds; everything else
   -> `failed` with its error type (`ambiguous` included: never resent). The mailbox's SMTP health follows: an auth
   failure sets `smtp_status` `auth_failed`, a connection-level failure (SMTP_HEALTH_ERRORS) `error`, a send clears
   it. Only the `smtp_*` columns are written: the IMAP `status` (and so the sync back-off) is never touched by a
   send. Every failure is an `email`/`smtp` integration error; every outcome publishes `email.status`
   after the commit. A run whose claim was lost meanwhile writes, records and counts nothing.

`sweep_outbound` runs with every mailbox fan-out tick (MAIL_SYNC_INTERVAL_SECONDS): rows left `sending` past their
claim (a worker died, a redelivered job landed inside the claim, the outcome write failed) become
`failed`/`interrupted`, and `queued`/`retry` rows overdue by more than SWEEP_GRACE (their job was lost) are
enqueued again. Duplicate jobs are harmless: the claim lets one run send.

Logs, spans, metrics and integration-error rows carry ids, counts and short codes only: never an address,
subject, name, body or password.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.analytics.integration_errors import record_integration_error
from supportcore.db.models import Conversation, Customer, EmailMailbox, EmailMessage, Message
from supportcore.db.session import after_commit
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.db.uow import UnitOfWork
from supportcore.errors import DomainError, NotFoundError
from supportcore.events import EventBus
from supportcore.inbox.publisher import InboxPublisher
from supportcore.mail.compose import build_mime, make_message_id, reply_subject, trim_references
from supportcore.mail.limits import MAX_SUBJECT
from supportcore.mail.metrics import record_outbound
from supportcore.mail.ports import MailSendError, MailServer, OutboundEmail
from supportcore.mail.strings import strings_for
from supportcore.observability.logging import get_logger
from supportcore.secrets.ports import SecretDecryptError

if TYPE_CHECKING:
    from supportcore.jobs.ports import JobRuntime

__all__ = [
    "CLAIM_SECONDS",
    "EmailNotAvailableError",
    "EmailNotRetryableError",
    "MAX_ATTEMPTS",
    "OutboundEmailService",
    "RETRY_DELAYS",
    "SEND_EMAIL_JOB",
    "send_email",
    "sweep_outbound",
]

logger = get_logger(__name__)
tracer = trace.get_tracer("supportcore.mail")

SEND_EMAIL_JOB = "send_email"
CLAIM_SECONDS = 120
MAX_ATTEMPTS = 5
RETRY_DELAYS = (30, 120, 600, 1800, 7200)  # seconds before attempt n + 1, by attempts made (n)
# A retry job may start this much before `next_attempt_at` (queue countdowns and worker clocks are not exact).
RETRY_EARLY_SLACK = timedelta(seconds=5)
SWEEP_GRACE = timedelta(minutes=5)  # a queued/retry row this overdue lost its job: enqueue it again
SWEEP_BATCH = 500
# SMTP failures that say something about the mailbox's SMTP settings or server (not about one message):
# `smtp_status` becomes `error` with this type. Auth failures make it `auth_failed`.
SMTP_HEALTH_ERRORS = frozenset({"connection_failed", "timeout", "tls_error", "address_not_allowed"})


class EmailNotAvailableError(DomainError):
    def __init__(self, message: str = "This conversation cannot be answered by email") -> None:
        super().__init__(message)


class EmailNotRetryableError(DomainError):
    def __init__(self, message: str = "Only a failed email can be sent again") -> None:
        super().__init__(message)


def _bracket(message_id: str) -> str:
    return f"<{message_id}>"


class OutboundEmailService:
    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        events: EventBus,
        adapters: Any,
        publisher: InboxPublisher,
    ) -> None:
        self._session = session
        self._org_id = organisation_id
        self._events = events
        self._adapters = adapters
        self._publisher = publisher
        self._emails = TenantScopedRepository(session, EmailMessage, organisation_id)
        self._mailboxes = TenantScopedRepository(session, EmailMailbox, organisation_id)

    async def _mailbox(self) -> EmailMailbox | None:
        return (await self._session.execute(self._mailboxes.select())).scalar_one_or_none()

    async def ensure_can_email(self, conversation: Conversation, send_email: bool) -> bool:
        """True when a reply in this conversation is emailed: always on the email channel, on chat only when asked
        (`send_email`) and the customer left a contact email. EmailNotAvailableError when it should be emailed but
        cannot be (no mailbox, no contact email, another channel)."""
        if conversation.channel == "email":
            if await self._mailbox() is None:
                raise EmailNotAvailableError("No mailbox is connected")
            return True
        if not send_email:
            return False
        if conversation.channel != "chat":
            raise EmailNotAvailableError()
        if not conversation.contact_email:
            raise EmailNotAvailableError("The customer left no email address")
        if await self._mailbox() is None:
            raise EmailNotAvailableError("No mailbox is connected")
        return True

    async def queue_reply(self, conversation: Conversation, message: Message) -> EmailMessage:
        """The outbound row of `message` (`queued`), a reply to the conversation's newest email (inbound or
        outbound). `send_email` is enqueued after the commit."""
        mailbox = await self._mailbox()
        if mailbox is None:
            raise EmailNotAvailableError("No mailbox is connected")
        parent = (
            await self._session.execute(
                self._emails.select()
                .where(EmailMessage.conversation_id == conversation.id)
                .order_by(EmailMessage.created_at.desc(), EmailMessage.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        recipient = await self._recipient(conversation, parent)
        if not recipient:
            raise EmailNotAvailableError("The customer has no email address")
        followup = strings_for(conversation.language)["email_followup_subject"]
        if conversation.channel == "email":
            subject = reply_subject(conversation.subject, followup)
        else:  # a chat follow-up: the first email has the follow-up subject, later ones answer the newest email
            subject = reply_subject(parent.subject if parent is not None else None, followup)
        references = trim_references([*parent.references, parent.rfc_message_id]) if parent is not None else []
        email = EmailMessage(
            message_id=message.id,
            conversation_id=conversation.id,
            mailbox_id=mailbox.id,
            direction="outbound",
            rfc_message_id=make_message_id(mailbox.address).strip("<>"),  # stored like inbound ids: no <>
            in_reply_to=parent.rfc_message_id if parent is not None else None,
            references=references,
            subject=subject[:MAX_SUBJECT],
            from_address=mailbox.address,
            from_name=mailbox.display_name or None,
            to_addresses=[recipient],
            cc_addresses=[],
            delivery_status="queued",
            attempts=0,
        )
        await self._emails.add(email)
        await self._session.flush()
        self._enqueue(email.id)
        logger.info(
            "email_reply_queued",
            organisation_id=str(self._org_id),
            conversation_id=str(conversation.id),
            message_id=str(message.id),
            email_message_id=str(email.id),
        )
        return email

    async def retry(self, conversation_id: UUID, message_id: UUID) -> EmailMessage:
        """Sends a `failed` outbound email again: `queued`, attempts 0, no error. The Message-ID is unchanged, so
        the customer's mail program threads it like the first try. NotFoundError for an unknown (or another
        organisation's) message, EmailNotRetryableError unless it failed."""
        email = (
            await self._session.execute(
                self._emails.select(for_update=True).where(
                    EmailMessage.conversation_id == conversation_id,
                    EmailMessage.message_id == message_id,
                    EmailMessage.direction == "outbound",
                )
            )
        ).scalar_one_or_none()
        if email is None:
            raise NotFoundError("Email not found")
        if email.delivery_status != "failed":
            raise EmailNotRetryableError()
        if await self._mailbox() is None:
            raise EmailNotAvailableError("No mailbox is connected")
        email.delivery_status = "queued"
        email.attempts = 0
        email.error_type = None
        email.next_attempt_at = datetime.now(UTC)  # due now: the sweep counts its grace from here
        email.sending_until = None
        await self._session.flush()
        self._enqueue(email.id)
        self._publisher.email_status(self._session, self._org_id, email.conversation_id, email.message_id, "queued")
        logger.info(
            "email_retry_requested",
            organisation_id=str(self._org_id),
            conversation_id=str(email.conversation_id),
            message_id=str(email.message_id),
            email_message_id=str(email.id),
        )
        return email

    # --- internals -----------------------------------------------------------------------------------

    def _enqueue(self, email_message_id: UUID) -> None:
        queue = self._adapters.job_queue
        if queue is None:
            raise RuntimeError("No job queue is configured")
        payload = {"organisation_id": str(self._org_id), "email_message_id": str(email_message_id)}
        after_commit(self._session, lambda: queue.enqueue(SEND_EMAIL_JOB, payload))

    async def _recipient(self, conversation: Conversation, parent: EmailMessage | None) -> str | None:
        if conversation.channel != "email":
            return conversation.contact_email
        if parent is not None and parent.direction == "inbound":
            return parent.reply_to or parent.from_address
        if parent is not None and parent.to_addresses:
            return parent.to_addresses[0]
        customer = await TenantScopedRepository(self._session, Customer, self._org_id).get(conversation.customer_id)
        return customer.email if customer is not None else None


# --- the send job ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Claimed:
    row_id: UUID
    conversation_id: UUID
    message_id: UUID
    mailbox_id: UUID | None
    attempts: int
    until: datetime
    email: OutboundEmail | None  # None: the message cannot be built (invalid_message)
    server: MailServer | None  # None: the mailbox is gone or its password unreadable


async def send_email(runtime: JobRuntime, organisation_id: UUID, email_message_id: UUID, now: datetime) -> str:
    """One send attempt of one outbound email (see the module docstring). Returns the outcome: `sent`, `retry`,
    `failed`, `ambiguous`, or `skipped` when there was nothing to send (already sent, or another run has it)."""
    org = str(organisation_id)
    with tracer.start_as_current_span("mail.send", record_exception=False, set_status_on_exception=False) as span:
        span.set_attribute("organisation_id", org)
        span.set_attribute("email_message_id", str(email_message_id))
        try:
            outcome = await _Send(runtime, organisation_id, email_message_id, now).execute(span)
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))  # the class only, never the text
            raise
        span.set_attribute("outcome", outcome)
    if outcome != "skipped":
        record_outbound(org, outcome)
    return outcome


class _Send:
    def __init__(self, runtime: JobRuntime, organisation_id: UUID, email_message_id: UUID, now: datetime) -> None:
        self.rt = runtime
        self.org_id = organisation_id
        self.org = str(organisation_id)
        self.row_id = email_message_id
        self.now = now
        self._started = time.monotonic()
        self.publisher = InboxPublisher(runtime.adapters.pubsub, runtime.session_factory)

    def _scoped(self, session: AsyncSession) -> Any:
        """`EmailMessage.id IN (this organisation's row)`: UPDATEs stay inside the tenant."""
        rows = TenantScopedRepository(session, EmailMessage, self.org_id)
        return EmailMessage.id.in_(rows.select(EmailMessage.id).where(EmailMessage.id == self.row_id))

    async def execute(self, span: trace.Span) -> str:
        claimed = await self._claim()
        if claimed is None:
            return await self._expired_claim()
        span.set_attribute("conversation_id", str(claimed.conversation_id))
        span.set_attribute("message_id", str(claimed.message_id))
        failure: MailSendError | None = None
        if claimed.server is None:
            failure = MailSendError("auth", "auth_failed") if claimed.mailbox_id else MailSendError(
                "permanent", "no_mailbox"
            )
        elif claimed.email is None:
            failure = MailSendError("permanent", "invalid_message")
        else:
            try:
                await self.rt.adapters.outbound_mail.send(claimed.server, claimed.email)
            except MailSendError as exc:
                failure = exc
            except Exception as exc:  # unknown: the message may have gone out, so it is never sent again
                logger.warning(
                    "mail_send_unexpected_error",
                    organisation_id=self.org,
                    email_message_id=str(self.row_id),
                    error_type=type(exc).__name__,
                )
                failure = MailSendError("ambiguous", "ambiguous")
        return await self._record(claimed, failure)

    async def _claim(self) -> _Claimed | None:
        async with UnitOfWork(self.rt.session_factory) as uow:
            s = uow.session
            row = (
                await s.execute(
                    update(EmailMessage)
                    .where(
                        self._scoped(s),
                        EmailMessage.direction == "outbound",
                        or_(
                            EmailMessage.delivery_status == "queued",
                            (EmailMessage.delivery_status == "retry")
                            & (
                                EmailMessage.next_attempt_at.is_(None)
                                | (EmailMessage.next_attempt_at <= self.now + RETRY_EARLY_SLACK)
                            ),
                        ),
                    )
                    .values(
                        delivery_status="sending",
                        attempts=EmailMessage.attempts + 1,
                        # The database clock: every worker judges the claim by the same clock.
                        sending_until=func.now() + timedelta(seconds=CLAIM_SECONDS),
                        next_attempt_at=None,
                    )
                    .returning(EmailMessage)
                    .execution_options(synchronize_session=False)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            text = await s.scalar(select(Message.text).where(Message.id == row.message_id))
            boxes = TenantScopedRepository(s, EmailMailbox, self.org_id)
            mailbox = None
            if row.mailbox_id is not None:
                mailbox = await boxes.get(row.mailbox_id)
            else:  # its mailbox was disconnected: send through the one connected now (one per organisation)
                mailbox = (await s.execute(boxes.select())).scalar_one_or_none()
                if mailbox is not None:
                    row.mailbox_id = mailbox.id
                    row.from_address = mailbox.address
                    row.from_name = mailbox.display_name or None
            server = self._smtp_server(mailbox) if mailbox is not None else None
            claimed = _Claimed(
                row_id=row.id,
                conversation_id=row.conversation_id,
                message_id=row.message_id,
                mailbox_id=mailbox.id if mailbox is not None else None,
                attempts=row.attempts,
                until=row.sending_until,
                email=_outbound(row, text or ""),
                server=server,
            )
            self.publisher.email_status(s, self.org_id, row.conversation_id, row.message_id, "sending")
            await uow.commit()
        return claimed

    def _smtp_server(self, mailbox: EmailMailbox) -> MailServer | None:
        """The SMTP side only (an unreadable IMAP password does not stop a send). None when the SMTP password
        cannot be decrypted (e.g. the secrets key changed: it must be entered again)."""
        try:
            password = self.rt.adapters.cipher.decrypt(mailbox.smtp_password_encrypted)
        except SecretDecryptError:
            return None
        return MailServer(
            mailbox.smtp_host, mailbox.smtp_port, mailbox.smtp_security, mailbox.smtp_username, password
        )

    async def _expired_claim(self) -> str:
        """No row to claim: a run that died mid-send left it `sending` past its claim -> `failed`/`interrupted`.
        It is never sent again (it may have gone out)."""
        async with UnitOfWork(self.rt.session_factory) as uow:
            s = uow.session
            row = (
                await s.execute(
                    update(EmailMessage)
                    .where(
                        self._scoped(s),
                        EmailMessage.direction == "outbound",
                        EmailMessage.delivery_status == "sending",
                        EmailMessage.sending_until < func.now(),
                    )
                    .values(delivery_status="failed", error_type="interrupted", sending_until=None)
                    .returning(EmailMessage.conversation_id, EmailMessage.message_id)
                    .execution_options(synchronize_session=False)
                )
            ).first()
            if row is None:
                return "skipped"  # sent, failed, or another run is sending it right now
            self.publisher.email_status(s, self.org_id, row.conversation_id, row.message_id, "failed", "interrupted")
            await uow.commit()
        await self._integration_error("interrupted")
        logger.warning(
            "mail_send_interrupted",
            organisation_id=self.org,
            conversation_id=str(row.conversation_id),
            message_id=str(row.message_id),
            email_message_id=str(self.row_id),
        )
        return "failed"

    async def _record(self, claimed: _Claimed, failure: MailSendError | None) -> str:
        at = self._clock()
        values: dict[str, Any] = {"sending_until": None}
        retry_in: int | None = None
        if failure is None:
            outcome, status, error_type = "sent", "sent", None
            values.update(sent_at=at, error_type=None, next_attempt_at=None)
        elif failure.kind == "transient" and claimed.attempts < MAX_ATTEMPTS:
            outcome, status, error_type = "retry", "retry", failure.error_type
            retry_in = RETRY_DELAYS[claimed.attempts - 1]
            values.update(error_type=error_type, next_attempt_at=at + timedelta(seconds=retry_in))
        else:
            error_type = "ambiguous" if failure.kind == "ambiguous" else failure.error_type
            outcome = "ambiguous" if failure.kind == "ambiguous" else "failed"
            status = "failed"
            values.update(error_type=error_type, next_attempt_at=None)
        values["delivery_status"] = status
        async with UnitOfWork(self.rt.session_factory) as uow:
            s = uow.session
            # Only while the claim is still this run's (the adapter's deadline is shorter than the claim).
            mine = await s.scalar(
                update(EmailMessage)
                .where(
                    self._scoped(s),
                    EmailMessage.delivery_status == "sending",
                    EmailMessage.sending_until == claimed.until,
                )
                .values(**values)
                .returning(EmailMessage.id)
                .execution_options(synchronize_session=False)
            )
            if mine is None:  # e.g. marked interrupted meanwhile: what happened is not ours to write or count
                logger.warning(
                    "mail_send_claim_lost", organisation_id=self.org, email_message_id=str(self.row_id), outcome=outcome
                )
                return "skipped"
            if claimed.mailbox_id is not None:
                await self._smtp_health(s, claimed.mailbox_id, failure, at)
            self.publisher.email_status(s, self.org_id, claimed.conversation_id, claimed.message_id, status, error_type)
            if retry_in is not None:
                self._enqueue_after_commit(s, retry_in)
            await uow.commit()
        if failure is not None:
            await self._integration_error(error_type or failure.error_type)
        logger.info(
            "mail_send_finished",
            organisation_id=self.org,
            conversation_id=str(claimed.conversation_id),
            message_id=str(claimed.message_id),
            email_message_id=str(self.row_id),
            outcome=outcome,
            error_type=error_type,
            attempts=claimed.attempts,
        )
        return outcome

    async def _smtp_health(
        self, session: AsyncSession, mailbox_id: UUID, failure: MailSendError | None, at: datetime
    ) -> None:
        """The mailbox's SMTP side after this send: cleared on success, `auth_failed` / `error` on a failure that
        concerns the server or its settings; a failure about this one message (4xx/5xx, invalid, ambiguous) leaves
        it. Never the IMAP `status`."""
        mailbox = EmailMailbox.id.in_(
            TenantScopedRepository(session, EmailMailbox, self.org_id)
            .select(EmailMailbox.id)
            .where(EmailMailbox.id == mailbox_id)
        )
        if failure is None:
            stmt = update(EmailMailbox).where(
                mailbox, or_(EmailMailbox.smtp_status != "active", EmailMailbox.smtp_error_type.is_not(None))
            )
            values: dict[str, Any] = {"smtp_status": "active", "smtp_error_type": None, "smtp_last_error_at": None}
        elif failure.kind == "auth":
            stmt = update(EmailMailbox).where(mailbox)
            values = {"smtp_status": "auth_failed", "smtp_error_type": "auth_failed", "smtp_last_error_at": at}
        elif failure.error_type in SMTP_HEALTH_ERRORS:
            stmt = update(EmailMailbox).where(mailbox)
            values = {"smtp_status": "error", "smtp_error_type": failure.error_type, "smtp_last_error_at": at}
        else:
            return
        await session.execute(stmt.values(**values).execution_options(synchronize_session=False))

    def _enqueue_after_commit(self, session: AsyncSession, countdown: int) -> None:
        queue = self.rt.adapters.job_queue
        if queue is None:
            raise RuntimeError("No job queue is configured")
        payload = {"organisation_id": self.org, "email_message_id": str(self.row_id)}
        after_commit(session, lambda: queue.enqueue(SEND_EMAIL_JOB, payload, countdown=countdown))

    async def _integration_error(self, error_type: str) -> None:
        await record_integration_error(self.rt.session_factory, self.org_id, "email", "smtp", error_type, now=self._clock())

    def _clock(self) -> datetime:
        """`now` as passed in, moved on by the time this run has taken."""
        return self.now + timedelta(seconds=time.monotonic() - self._started)


# --- the sweep ---------------------------------------------------------------------------------------------


async def sweep_outbound(runtime: JobRuntime, now: datetime) -> tuple[int, int]:
    """Recovers outbound rows no job will finish (see the module docstring). Returns (interrupted, re-enqueued).

    Finding the rows is a platform read of ids only (like the mailbox fan-out); each row is then handled inside its
    own organisation."""
    overdue = now - SWEEP_GRACE
    async with runtime.session_factory() as s:
        expired = (
            await s.execute(
                select(EmailMessage.id, EmailMessage.organisation_id)
                .where(
                    EmailMessage.direction == "outbound",
                    EmailMessage.delivery_status == "sending",
                    EmailMessage.sending_until < func.now(),
                )
                .order_by(EmailMessage.sending_until, EmailMessage.id)
                .limit(SWEEP_BATCH)
            )
        ).all()
        stale = (
            await s.execute(
                select(EmailMessage.id, EmailMessage.organisation_id)
                .where(
                    EmailMessage.direction == "outbound",
                    EmailMessage.delivery_status.in_(("queued", "retry")),
                    func.coalesce(EmailMessage.next_attempt_at, EmailMessage.created_at) < overdue,
                )
                .order_by(EmailMessage.created_at, EmailMessage.id)
                .limit(SWEEP_BATCH)
            )
        ).all()
    interrupted = 0
    for row_id, organisation_id in expired:
        try:
            if await _Send(runtime, organisation_id, row_id, now)._expired_claim() == "failed":
                record_outbound(str(organisation_id), "failed")
                interrupted += 1
        except Exception as exc:  # one row's trouble must not stop the others; the next tick tries again
            logger.warning(
                "mail_sweep_row_failed",
                organisation_id=str(organisation_id),
                email_message_id=str(row_id),
                error_type=type(exc).__name__,
            )
    queue = runtime.adapters.job_queue
    if stale and queue is None:
        raise RuntimeError("No job queue is configured")
    for row_id, organisation_id in stale:
        queue.enqueue(SEND_EMAIL_JOB, {"organisation_id": str(organisation_id), "email_message_id": str(row_id)})
    if interrupted or stale:
        logger.info("mail_outbound_swept", interrupted=interrupted, requeued=len(stale))
    return interrupted, len(stale)


def _outbound(row: EmailMessage, text: str) -> OutboundEmail | None:
    """The message as stored, or None when it cannot be sent as it is (not exactly one recipient, a From without
    `@`, CR/LF in a header)."""
    if len(row.to_addresses) != 1 or "@" not in (row.to_addresses[0] or "") or "@" not in row.from_address:
        return None
    if row.mailbox_id is None:
        return None
    email = OutboundEmail(
        from_address=row.from_address,
        from_name=row.from_name,
        to=row.to_addresses[0],
        subject=row.subject or "",
        text=text,
        message_id=_bracket(row.rfc_message_id),
        in_reply_to=_bracket(row.in_reply_to) if row.in_reply_to else None,
        references=tuple(_bracket(r) for r in row.references),
        mailbox_id=row.mailbox_id,
    )
    try:
        build_mime(email)
    except ValueError:
        return None
    return email
