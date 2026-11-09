"""Outbound email replies (Phase 6, Task 7): the outbound row and its threading headers, the `send_email` job with
its claim, retries and backoff, and what it publishes and records. Real Postgres with committed data and the fake
outbound adapter (wrapped to see every attempt)."""

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import event, select, update

from supportcore.conversations.service import ConversationService
from supportcore.db.models import (
    Conversation,
    Customer,
    EmailMailbox,
    EmailMessage,
    IntegrationEvent,
    Membership,
    Message,
    User,
)
from supportcore.db.uow import UnitOfWork
from supportcore.events import EventBus
from supportcore.inbox.publisher import InboxPublisher
from supportcore.jobs.ports import JobRuntime
from supportcore.mail.compose import build_mime
from supportcore.mail.fake_outbound import FakeOutboundMailSender
from supportcore.mail.outbound import (
    SEND_EMAIL_JOB,
    EmailNotAvailableError,
    EmailNotRetryableError,
    OutboundEmailService,
    send_email,
    sweep_outbound,
)
from supportcore.mail.ports import MailSendError
from supportcore.presence.memory_presence import MemoryAgentPresence
from supportcore.realtime.spawn import drain_pending

from .test_inbound import RecordingPubSub
from .test_sync import error_types, make_org, settings  # noqa: F401  (the settings fixture)

pytestmark = pytest.mark.asyncio

SMTP_PASSWORD = "smtp-pass-SENTINEL-pw"


class CountdownQueue:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, dict, float | None]] = []

    def enqueue(self, name, payload, *, countdown=None) -> None:
        self.jobs.append((name, payload, countdown))

    def take(self) -> list[tuple[str, dict, float | None]]:
        jobs, self.jobs = self.jobs, []
        return jobs


class HookedOutbound(FakeOutboundMailSender):
    """The fake sender, also keeping every attempt (failed ones too) and running a hook before each."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = []
        self.servers = []
        self.before_send = None

    async def send(self, server, email):
        self.attempts.append(email)
        self.servers.append(server)
        if self.before_send:
            await self.before_send()
        await super().send(server, email)


@pytest.fixture
async def adapters(settings):  # noqa: F811
    from supportcore.wiring import build_adapters

    built = build_adapters(settings)
    built = replace(
        built, pubsub=RecordingPubSub(), presence=MemoryAgentPresence(), outbound_mail=HookedOutbound(),
        job_queue=CountdownQueue(),
    )
    yield built
    await drain_pending()
    await built.aclose()


@pytest.fixture
def runtime(settings, session_factory, adapters):  # noqa: F811
    return JobRuntime(settings=settings, session_factory=session_factory, adapters=adapters, events=EventBus())


@pytest.fixture
def fake(adapters) -> HookedOutbound:
    return adapters.outbound_mail


async def org_with_mailbox(session_factory, adapters, name="Acme", **box_fields):
    fields = {"smtp_password_encrypted": adapters.cipher.encrypt(SMTP_PASSWORD), **box_fields}
    return await make_org(session_factory, adapters, name, **fields)


async def email_conversation(session_factory, org_id, box_id, *, subject="Printer is broken", sender="cust@example.org"):
    """An open email conversation with one inbound mail (Message-ID `in1@example.org`, one older reference)."""
    async with UnitOfWork(session_factory) as uow:
        s = uow.session
        customer = Customer(organisation_id=org_id, email=sender)
        s.add(customer)
        await s.flush()
        conv = Conversation(
            organisation_id=org_id, customer_id=customer.id, status="open", channel="email", subject=subject
        )
        s.add(conv)
        await s.flush()
        msg = Message(organisation_id=org_id, conversation_id=conv.id, role="customer", text="It jams")
        s.add(msg)
        await s.flush()
        s.add(
            EmailMessage(
                organisation_id=org_id, message_id=msg.id, conversation_id=conv.id, mailbox_id=box_id,
                direction="inbound", rfc_message_id="in1@example.org", references=["r0@example.org"],
                subject=subject, from_address=sender, to_addresses=["support@acme.example"],
                delivery_status="received",
            )
        )
        await uow.commit()
        return conv.id


async def chat_conversation(session_factory, org_id, *, contact_email="chatter@example.org", channel="chat"):
    async with UnitOfWork(session_factory) as uow:
        s = uow.session
        customer = Customer(organisation_id=org_id, visitor_id=uuid.uuid4().hex)
        s.add(customer)
        await s.flush()
        conv = Conversation(
            organisation_id=org_id, customer_id=customer.id, status="open", channel=channel,
            contact_email=contact_email,
        )
        s.add(conv)
        await uow.commit()
        return conv.id


async def agent_of(session_factory, org_id) -> Membership:
    async with session_factory() as s:
        return (
            await s.execute(
                select(Membership).join(User, User.id == Membership.user_id).where(
                    Membership.organisation_id == org_id, User.name == "Ann"
                )
            )
        ).scalar_one()


async def reply(runtime, org_id, conv_id, text="We are on it", *, send=False, commit=True):
    """What the reply route does: ensure_can_email, add_agent_message, queue_reply, in one transaction.
    Returns (message id, outbound row id or None)."""
    member = await agent_of(runtime.session_factory, org_id)
    publisher = InboxPublisher(runtime.adapters.pubsub, runtime.session_factory)
    async with UnitOfWork(runtime.session_factory) as uow:
        s = uow.session
        conversations = ConversationService(s, org_id, runtime.events, publisher=publisher)
        outbound = OutboundEmailService(s, org_id, runtime.events, runtime.adapters, publisher)
        conversation = await conversations.locked(conv_id)
        emailed = await outbound.ensure_can_email(conversation, send)
        message = await conversations.add_agent_message(conversation, member, text)
        email_id = (await outbound.queue_reply(conversation, message)).id if emailed else None
        if commit:
            await uow.commit()
    await drain_pending()
    return message.id, email_id


async def send(runtime, org_id, email_id, now=None) -> str:
    outcome = await send_email(runtime, org_id, email_id, now or datetime.now(UTC))
    await drain_pending()
    return outcome


async def row(session_factory, email_id) -> EmailMessage:
    async with session_factory() as s:
        return await s.get(EmailMessage, email_id)


def statuses(adapters, org_id) -> list[tuple[str, str | None]]:
    return [
        (m["status"], m["error_type"])
        for m in adapters.pubsub.on(f"inbox:{org_id.hex}")
        if m["type"] == "email.status"
    ]


# --- queue and send ---------------------------------------------------------------------------------------


async def test_reply_on_email_conversation_queues_and_sends_once(runtime, session_factory, adapters, fake):
    org_id, box_id = await org_with_mailbox(session_factory, adapters, display_name="Acme Help")
    conv_id = await email_conversation(session_factory, org_id, box_id)

    message_id, email_id = await reply(runtime, org_id, conv_id, "We are on it")

    assert email_id is not None
    jobs = adapters.job_queue.take()
    assert jobs == [(SEND_EMAIL_JOB, {"organisation_id": str(org_id), "email_message_id": str(email_id)}, None)]
    queued = await row(session_factory, email_id)
    assert (queued.direction, queued.delivery_status, queued.attempts) == ("outbound", "queued", 0)
    assert queued.message_id == message_id and queued.in_reply_to == "in1@example.org"
    assert queued.references == ["r0@example.org", "in1@example.org"]
    assert not queued.rfc_message_id.startswith("<") and queued.rfc_message_id.endswith("@acme.example")

    assert await send(runtime, org_id, email_id) == "sent"

    assert len(fake.sent) == 1
    mime = fake.sent_mime()[0]
    assert mime["In-Reply-To"] == "<in1@example.org>"
    assert mime["References"].split() == ["<r0@example.org>", "<in1@example.org>"]
    assert mime["References"].split()[-1] == mime["In-Reply-To"]
    assert mime["Message-ID"] == f"<{queued.rfc_message_id}>"
    assert mime["Subject"] == "Re: Printer is broken" and mime["To"] == "cust@example.org"
    assert "support@acme.example" in mime["From"] and "Acme Help" in mime["From"]
    assert mime["X-Support-Mailbox"] == str(box_id) and mime["Auto-Submitted"] is None
    assert mime.get_content().strip() == "We are on it"
    assert fake.servers[0].password == SMTP_PASSWORD and fake.servers[0].host == "smtp.acme.example"
    sent = await row(session_factory, email_id)
    assert (sent.delivery_status, sent.attempts, sent.error_type, sent.sending_until) == ("sent", 1, None, None)
    assert sent.sent_at is not None

    # The job running again (a duplicate delivery) sends nothing.
    assert await send(runtime, org_id, email_id) == "skipped"
    assert len(fake.sent) == 1 and adapters.job_queue.take() == []


async def test_reply_subject_not_doubled_and_answers_newest_email(runtime, session_factory, adapters, fake):
    org_id, box_id = await org_with_mailbox(session_factory, adapters)
    conv_id = await email_conversation(session_factory, org_id, box_id, subject="RE: Printer")
    _, first = await reply(runtime, org_id, conv_id, "one")
    _, second = await reply(runtime, org_id, conv_id, "two")
    one, two = await row(session_factory, first), await row(session_factory, second)
    assert one.subject == two.subject == "RE: Printer"
    # The second reply answers the newest email of the conversation: our first reply.
    assert two.in_reply_to == one.rfc_message_id
    assert two.references == ["r0@example.org", "in1@example.org", one.rfc_message_id]
    assert two.to_addresses == ["cust@example.org"]


async def test_chat_followup_send_email_uses_contact_email_and_followup_subject(runtime, session_factory, adapters, fake):
    org_id, box_id = await org_with_mailbox(session_factory, adapters)
    conv_id = await chat_conversation(session_factory, org_id, contact_email="chatter@example.org")

    _, none = await reply(runtime, org_id, conv_id, "widget only")
    assert none is None and adapters.job_queue.take() == []

    _, email_id = await reply(runtime, org_id, conv_id, "Following up by email", send=True)
    assert [job[1]["email_message_id"] for job in adapters.job_queue.take()] == [str(email_id)]
    assert await send(runtime, org_id, email_id) == "sent"

    mime = fake.sent_mime()[0]
    assert mime["To"] == "chatter@example.org" and mime["Subject"] == "Your support request"
    assert mime["In-Reply-To"] is None and mime["References"] is None

    # A second follow-up answers the first one.
    _, again = await reply(runtime, org_id, conv_id, "And another thing", send=True)
    again_row = await row(session_factory, again)
    assert again_row.subject == "Re: Your support request" and again_row.to_addresses == ["chatter@example.org"]
    assert again_row.in_reply_to == (await row(session_factory, email_id)).rfc_message_id


async def test_send_email_without_contact_or_mailbox_409(runtime, session_factory, adapters):
    org_id, box_id = await org_with_mailbox(session_factory, adapters)
    no_contact = await chat_conversation(session_factory, org_id, contact_email=None)
    voice = await chat_conversation(session_factory, org_id, channel="voice")
    with pytest.raises(EmailNotAvailableError):
        await reply(runtime, org_id, no_contact, send=True)
    with pytest.raises(EmailNotAvailableError):
        await reply(runtime, org_id, voice, send=True)

    bare_org, bare_box = await org_with_mailbox(session_factory, adapters, "Bare")
    email_conv = await email_conversation(session_factory, bare_org, bare_box)
    chat = await chat_conversation(session_factory, bare_org)
    async with UnitOfWork(session_factory) as uow:
        await uow.session.execute(EmailMailbox.__table__.delete().where(EmailMailbox.id == bare_box))
        await uow.commit()
    with pytest.raises(EmailNotAvailableError):
        await reply(runtime, bare_org, chat, send=True)
    with pytest.raises(EmailNotAvailableError):
        await reply(runtime, bare_org, email_conv)

    # Nothing was written by the refused replies.
    async with session_factory() as s:
        assert (await s.execute(select(Message.id).where(Message.role == "agent"))).first() is None
        assert (await s.execute(select(EmailMessage.id).where(EmailMessage.direction == "outbound"))).first() is None
    assert adapters.job_queue.take() == []


# --- retries ------------------------------------------------------------------------------------------------


async def test_transient_failure_retries_with_same_message_id(runtime, session_factory, adapters, fake):
    org_id, box_id = await org_with_mailbox(session_factory, adapters)
    conv_id = await email_conversation(session_factory, org_id, box_id)
    _, email_id = await reply(runtime, org_id, conv_id)
    adapters.job_queue.take()
    fake.fail_next(MailSendError("transient", "smtp_4xx"))

    now = datetime.now(UTC)
    assert await send(runtime, org_id, email_id, now) == "retry"
    waiting = await row(session_factory, email_id)
    assert (waiting.delivery_status, waiting.attempts, waiting.error_type) == ("retry", 1, "smtp_4xx")
    assert waiting.next_attempt_at >= now + timedelta(seconds=30)
    assert adapters.job_queue.take() == [
        (SEND_EMAIL_JOB, {"organisation_id": str(org_id), "email_message_id": str(email_id)}, 30)
    ]

    # A duplicate job before the backoff is up claims nothing; the job due after it sends.
    assert await send(runtime, org_id, email_id, now + timedelta(seconds=10)) == "skipped"
    assert (await row(session_factory, email_id)).delivery_status == "retry"
    assert await send(runtime, org_id, email_id, now + timedelta(seconds=30)) == "sent"
    assert len(fake.sent) == 1 and len(fake.attempts) == 2
    ids = [build_mime(a)["Message-ID"] for a in fake.attempts]
    assert ids[0] == ids[1] == f"<{waiting.rfc_message_id}>"
    done = await row(session_factory, email_id)
    assert (done.delivery_status, done.attempts, done.error_type, done.next_attempt_at) == ("sent", 2, None, None)
    assert statuses(adapters, org_id)[-4:] == [("sending", None), ("retry", "smtp_4xx"), ("sending", None), ("sent", None)]


async def test_retry_gives_up_after_five_attempts(runtime, session_factory, adapters, fake):
    org_id, box_id = await org_with_mailbox(session_factory, adapters)
    conv_id = await email_conversation(session_factory, org_id, box_id)
    _, email_id = await reply(runtime, org_id, conv_id)
    adapters.job_queue.take()
    fake.fail_next(*[MailSendError("transient", "connection_failed")] * 6)

    outcomes, countdowns = [], []
    for n in range(5):  # each job runs once its backoff is up
        outcomes.append(await send(runtime, org_id, email_id, datetime.now(UTC) + timedelta(hours=3 * n)))
        countdowns += [job[2] for job in adapters.job_queue.take()]

    assert outcomes == ["retry", "retry", "retry", "retry", "failed"]
    assert countdowns == [30, 120, 600, 1800]
    failed = await row(session_factory, email_id)
    assert (failed.delivery_status, failed.attempts, failed.error_type) == ("failed", 5, "connection_failed")
    assert await send(runtime, org_id, email_id) == "skipped"  # a stray job: nothing more
    assert len(fake.attempts) == 5 and fake.sent == []
    async with session_factory() as s:  # one row per UTC hour: the attempts ran hours apart
        rows = (await s.execute(select(IntegrationEvent).where(IntegrationEvent.organisation_id == org_id))).scalars()
        assert sorted((r.kind, r.provider, r.error_type, r.count) for r in rows) == [
            ("email", "smtp", "connection_failed", 1)
        ] * 5


async def test_smtp_auth_failure_fails_without_retry(runtime, session_factory, adapters, fake):
    org_id, box_id = await org_with_mailbox(session_factory, adapters)
    conv_id = await email_conversation(session_factory, org_id, box_id)
    _, email_id = await reply(runtime, org_id, conv_id)
    adapters.job_queue.take()
    fake.fail_next(MailSendError("auth", "auth_failed"))

    assert await send(runtime, org_id, email_id) == "failed"

    failed = await row(session_factory, email_id)
    assert (failed.delivery_status, failed.attempts, failed.error_type) == ("failed", 1, "auth_failed")
    assert adapters.job_queue.take() == []
    async with session_factory() as s:
        box = await s.get(EmailMailbox, box_id)
    # The SMTP side only: the IMAP status (and the sync back-off built on it) is untouched.
    assert (box.smtp_status, box.smtp_error_type) == ("auth_failed", "auth_failed") and box.smtp_last_error_at
    assert (box.status, box.last_error_type, box.last_error_at) == ("active", None, None)
    assert (await error_types(session_factory, org_id)) == {("email", "smtp", "auth_failed"): 1}
    assert statuses(adapters, org_id)[-1] == ("failed", "auth_failed")


async def test_unreadable_imap_password_does_not_stop_a_send(runtime, session_factory, adapters, fake):
    org_id, box_id = await org_with_mailbox(session_factory, adapters, imap_password_encrypted="not-fernet")
    conv_id = await email_conversation(session_factory, org_id, box_id)
    _, email_id = await reply(runtime, org_id, conv_id)
    assert await send(runtime, org_id, email_id) == "sent"
    assert fake.servers[0].password == SMTP_PASSWORD
    async with session_factory() as s:
        assert (await s.get(EmailMailbox, box_id)).status == "active"


async def test_unreadable_smtp_password_is_an_auth_failure(runtime, session_factory, adapters, fake):
    org_id, box_id = await org_with_mailbox(session_factory, adapters, smtp_password_encrypted="not-fernet")
    conv_id = await email_conversation(session_factory, org_id, box_id)
    _, email_id = await reply(runtime, org_id, conv_id)
    assert await send(runtime, org_id, email_id) == "failed"
    assert fake.attempts == []
    assert (await row(session_factory, email_id)).error_type == "auth_failed"
    async with session_factory() as s:
        box = await s.get(EmailMailbox, box_id)
    assert (box.smtp_status, box.status) == ("auth_failed", "active")


async def test_smtp_auth_failure_does_not_delay_inbound_sync(runtime, session_factory, adapters, fake):
    """The SMTP password was rotated at the provider while IMAP still works: failed replies mark the SMTP side
    only, so the sync fan-out keeps enqueueing the mailbox every tick, and a successful sync keeps the SMTP error
    on show."""
    from supportcore.mail.sync import sync_mailbox, sync_mailboxes

    org_id, box_id = await org_with_mailbox(session_factory, adapters)
    conv_id = await email_conversation(session_factory, org_id, box_id)
    for _ in range(2):  # each failed reply moves smtp_last_error_at on: still no back-off
        _, email_id = await reply(runtime, org_id, conv_id)
        fake.fail_next(MailSendError("auth", "auth_failed"))
        assert await send(runtime, org_id, email_id) == "failed"
    adapters.job_queue.take()

    now = datetime.now(UTC)
    assert await sync_mailboxes(runtime, now) == 1
    assert [name for name, *_ in adapters.job_queue.take() if name == "sync_mailbox"] == ["sync_mailbox"]
    assert (await sync_mailbox(runtime, org_id, box_id, now)).outcome == "ok"
    await drain_pending()

    async with session_factory() as s:
        box = await s.get(EmailMailbox, box_id)
    assert (box.status, box.last_error_type) == ("active", None) and box.last_sync_at is not None
    assert (box.smtp_status, box.smtp_error_type) == ("auth_failed", "auth_failed")


async def test_smtp_health_follows_sends(runtime, session_factory, adapters, fake):
    """A connection-level failure marks the SMTP side `error`, a message-level one (5xx) leaves it, a send that
    goes through clears it."""
    org_id, box_id = await org_with_mailbox(session_factory, adapters)
    conv_id = await email_conversation(session_factory, org_id, box_id)

    async def smtp():
        async with session_factory() as s:
            box = await s.get(EmailMailbox, box_id)
        return box.smtp_status, box.smtp_error_type, box.status

    _, email_id = await reply(runtime, org_id, conv_id)
    fake.fail_next(MailSendError("transient", "connection_failed"))
    assert await send(runtime, org_id, email_id) == "retry"
    assert await smtp() == ("error", "connection_failed", "active")

    _, other_id = await reply(runtime, org_id, conv_id)
    fake.fail_next(MailSendError("permanent", "smtp_5xx"))
    assert await send(runtime, org_id, other_id) == "failed"
    assert await smtp() == ("error", "connection_failed", "active")  # about that message, not the server

    _, third_id = await reply(runtime, org_id, conv_id)
    assert await send(runtime, org_id, third_id) == "sent"
    async with session_factory() as s:
        box = await s.get(EmailMailbox, box_id)
    assert (box.smtp_status, box.smtp_error_type, box.smtp_last_error_at) == ("active", None, None)


async def test_ambiguous_failure_not_retried(runtime, session_factory, adapters, fake):
    org_id, box_id = await org_with_mailbox(session_factory, adapters)
    conv_id = await email_conversation(session_factory, org_id, box_id)
    _, email_id = await reply(runtime, org_id, conv_id)
    adapters.job_queue.take()
    fake.fail_next(MailSendError("ambiguous", "ambiguous"))

    assert await send(runtime, org_id, email_id) == "ambiguous"

    failed = await row(session_factory, email_id)
    assert (failed.delivery_status, failed.error_type) == ("failed", "ambiguous")
    assert adapters.job_queue.take() == []
    assert await send(runtime, org_id, email_id) == "skipped"
    assert len(fake.attempts) == 1
    assert (await error_types(session_factory, org_id)) == {("email", "smtp", "ambiguous"): 1}


async def test_permanent_failure_and_invalid_message_not_retried(runtime, session_factory, adapters, fake):
    org_id, box_id = await org_with_mailbox(session_factory, adapters)
    conv_id = await email_conversation(session_factory, org_id, box_id)
    _, rejected = await reply(runtime, org_id, conv_id, "one")
    _, broken = await reply(runtime, org_id, conv_id, "two")
    adapters.job_queue.take()
    fake.fail_next(MailSendError("permanent", "smtp_5xx"))
    assert await send(runtime, org_id, rejected) == "failed"
    assert (await row(session_factory, rejected)).error_type == "smtp_5xx"

    async with UnitOfWork(session_factory) as uow:  # CR/LF in a header: build_mime refuses it
        await uow.session.execute(
            update(EmailMessage).where(EmailMessage.id == broken).values(subject="Hi\r\nBcc: someone@evil.example")
        )
        await uow.commit()
    assert await send(runtime, org_id, broken) == "failed"
    failed = await row(session_factory, broken)
    assert (failed.delivery_status, failed.error_type) == ("failed", "invalid_message")
    assert len(fake.attempts) == 1 and adapters.job_queue.take() == []


async def test_expired_claim_marked_interrupted_not_resent(runtime, session_factory, adapters, fake):
    org_id, box_id = await org_with_mailbox(session_factory, adapters)
    conv_id = await email_conversation(session_factory, org_id, box_id)
    _, live = await reply(runtime, org_id, conv_id, "one")
    _, dead = await reply(runtime, org_id, conv_id, "two")
    now = datetime.now(UTC)
    async with UnitOfWork(session_factory) as uow:
        s = uow.session
        await s.execute(
            update(EmailMessage).where(EmailMessage.id == live)
            .values(delivery_status="sending", attempts=1, sending_until=now + timedelta(seconds=60))
        )
        await s.execute(
            update(EmailMessage).where(EmailMessage.id == dead)
            .values(delivery_status="sending", attempts=1, sending_until=now - timedelta(seconds=1))
        )
        await uow.commit()

    assert await send(runtime, org_id, live, now) == "skipped"  # another run is sending it right now
    assert (await row(session_factory, live)).delivery_status == "sending"
    assert await send(runtime, org_id, dead, now) == "failed"

    interrupted = await row(session_factory, dead)
    assert (interrupted.delivery_status, interrupted.error_type, interrupted.sending_until) == (
        "failed", "interrupted", None,
    )
    assert fake.attempts == []
    assert (await error_types(session_factory, org_id)) == {("email", "smtp", "interrupted"): 1}
    assert statuses(adapters, org_id)[-1] == ("failed", "interrupted")


async def test_concurrent_send_jobs_send_once(runtime, session_factory, adapters, fake):
    org_id, box_id = await org_with_mailbox(session_factory, adapters)
    conv_id = await email_conversation(session_factory, org_id, box_id)
    _, email_id = await reply(runtime, org_id, conv_id)

    async def slow():
        await asyncio.sleep(0.05)

    fake.before_send = slow
    outcomes = await asyncio.gather(*(send_email(runtime, org_id, email_id, datetime.now(UTC)) for _ in range(2)))
    await drain_pending()

    assert sorted(outcomes) == ["sent", "skipped"]
    assert len(fake.sent) == 1 and len(fake.attempts) == 1
    assert (await row(session_factory, email_id)).attempts == 1


# --- manual retry -------------------------------------------------------------------------------------------


async def manual_retry(runtime, org_id, conv_id, message_id, *, commit=True):
    publisher = InboxPublisher(runtime.adapters.pubsub, runtime.session_factory)
    async with UnitOfWork(runtime.session_factory) as uow:
        service = OutboundEmailService(uow.session, org_id, runtime.events, runtime.adapters, publisher)
        email = await service.retry(conv_id, message_id)
        if commit:
            await uow.commit()
    await drain_pending()
    return email


async def test_manual_retry_only_from_failed_409(runtime, session_factory, adapters, fake):
    from supportcore.errors import NotFoundError

    org_id, box_id = await org_with_mailbox(session_factory, adapters)
    conv_id = await email_conversation(session_factory, org_id, box_id)
    message_id, email_id = await reply(runtime, org_id, conv_id)
    adapters.job_queue.take()
    with pytest.raises(EmailNotRetryableError):  # queued
        await manual_retry(runtime, org_id, conv_id, message_id)

    fake.fail_next(MailSendError("permanent", "smtp_5xx"))
    assert await send(runtime, org_id, email_id) == "failed"
    failed = await row(session_factory, email_id)

    other_org, _ = await org_with_mailbox(session_factory, adapters, "Other")
    with pytest.raises(NotFoundError):  # another organisation's message
        await manual_retry(runtime, other_org, conv_id, message_id)
    with pytest.raises(NotFoundError):
        await manual_retry(runtime, org_id, conv_id, uuid.uuid4())

    # Not committed: nothing enqueued or published.
    await manual_retry(runtime, org_id, conv_id, message_id, commit=False)
    assert adapters.job_queue.take() == [] and statuses(adapters, org_id)[-1] == ("failed", "smtp_5xx")

    retried = await manual_retry(runtime, org_id, conv_id, message_id)
    assert (retried.delivery_status, retried.attempts, retried.error_type) == ("queued", 0, None)
    assert retried.rfc_message_id == failed.rfc_message_id
    assert adapters.job_queue.take() == [
        (SEND_EMAIL_JOB, {"organisation_id": str(org_id), "email_message_id": str(email_id)}, None)
    ]
    assert statuses(adapters, org_id)[-1] == ("queued", None)

    assert await send(runtime, org_id, email_id) == "sent"
    assert [build_mime(a)["Message-ID"] for a in fake.attempts] == [f"<{failed.rfc_message_id}>"] * 2
    with pytest.raises(EmailNotRetryableError):  # sent
        await manual_retry(runtime, org_id, conv_id, message_id)


async def test_retry_after_disconnect_and_reconnect_uses_the_current_mailbox(runtime, session_factory, adapters, fake):
    """Disconnecting sets the old rows' mailbox_id to NULL; after a reconnect, Retry sends through the new mailbox
    (its address and SMTP server) instead of failing `no_mailbox`."""
    org_id, box_id = await org_with_mailbox(session_factory, adapters)
    conv_id = await email_conversation(session_factory, org_id, box_id)
    message_id, email_id = await reply(runtime, org_id, conv_id)
    fake.fail_next(MailSendError("permanent", "smtp_5xx"))
    assert await send(runtime, org_id, email_id) == "failed"

    async with UnitOfWork(session_factory) as uow:  # disconnect, then connect a new mailbox
        await uow.session.delete(await uow.session.get(EmailMailbox, box_id))
        await uow.commit()
    assert (await row(session_factory, email_id)).mailbox_id is None
    async with UnitOfWork(session_factory) as uow:
        new_box = EmailMailbox(
            organisation_id=org_id, address="help@acme.example", display_name="Acme Help",
            imap_host="imap.acme.example", imap_username="u", imap_password_encrypted=adapters.cipher.encrypt("i"),
            smtp_host="smtp2.acme.example", smtp_username="u2",
            smtp_password_encrypted=adapters.cipher.encrypt(SMTP_PASSWORD),
        )
        uow.session.add(new_box)
        await uow.commit()
        new_box_id = new_box.id

    await manual_retry(runtime, org_id, conv_id, message_id)
    assert await send(runtime, org_id, email_id) == "sent"

    assert fake.servers[-1].host == "smtp2.acme.example"
    assert fake.attempts[-1].from_address == "help@acme.example" and fake.attempts[-1].mailbox_id == new_box_id
    sent = await row(session_factory, email_id)
    assert (sent.delivery_status, sent.mailbox_id, sent.from_address) == ("sent", new_box_id, "help@acme.example")


# --- no session across SMTP, no content in telemetry, publish after commit --------------------------------


async def test_no_session_open_during_smtp(runtime, session_factory, adapters, fake, db_engine):
    org_id, box_id = await org_with_mailbox(session_factory, adapters)
    conv_id = await email_conversation(session_factory, org_id, box_id)
    _, email_id = await reply(runtime, org_id, conv_id)
    open_connections = checkouts = 0

    def checkout(*args):
        nonlocal open_connections, checkouts
        open_connections += 1
        checkouts += 1

    def checkin(*args):
        nonlocal open_connections
        open_connections -= 1

    seen: list[int] = []

    async def no_connection():
        seen.append(open_connections)

    fake.before_send = no_connection
    event.listen(db_engine.sync_engine.pool, "checkout", checkout)
    event.listen(db_engine.sync_engine.pool, "checkin", checkin)
    try:
        assert await send(runtime, org_id, email_id) == "sent"
    finally:
        event.remove(db_engine.sync_engine.pool, "checkout", checkout)
        event.remove(db_engine.sync_engine.pool, "checkin", checkin)

    assert checkouts >= 2  # the claim and the outcome: the listener sees the job's own sessions
    assert seen == [0]


async def test_send_logs_spans_have_no_content(runtime, session_factory, adapters, fake, settings):  # noqa: F811
    from structlog.testing import capture_logs

    from supportcore.observability.tracing import configure_tracing, get_test_span_exporter

    configure_tracing(settings)
    exporter = get_test_span_exporter()
    exporter.clear()
    sentinels = ("SENTINEL-SUBJ-qx7", "sentinel-addr-qx7@example.org", "SENTINEL-BODY-qx7", "Sentinel Desk Qx")
    org_id, box_id = await org_with_mailbox(session_factory, adapters, display_name=sentinels[3])
    conv_id = await email_conversation(session_factory, org_id, box_id, subject=sentinels[0], sender=sentinels[1])

    with capture_logs() as logs:
        _, first = await reply(runtime, org_id, conv_id, sentinels[2])
        _, second = await reply(runtime, org_id, conv_id, sentinels[2])
        assert await send(runtime, org_id, first) == "sent"
        fake.fail_next(MailSendError("auth", "auth_failed"))
        assert await send(runtime, org_id, second) == "failed"

    assert logs
    spans = [span for span in exporter.get_finished_spans() if span.name == "mail.send"]
    assert len(spans) == 2
    attributes = [dict(span.attributes or {}) for span in spans]
    for attrs in attributes:
        assert set(attrs) <= {"organisation_id", "email_message_id", "message_id", "conversation_id", "outcome"}, attrs
    assert {a["outcome"] for a in attributes} == {"sent", "failed"}
    assert all(not span.events for span in spans)
    labels = [
        sample.labels
        for metric in REGISTRY.collect()
        if metric.name.startswith("support_email_outbound")
        for sample in metric.samples
        if sample.labels.get("organisation_id") == str(org_id)
    ]
    assert {lab["outcome"] for lab in labels} == {"sent", "failed"}
    blobs = [repr(logs), repr(attributes), repr(labels), repr(await error_types(session_factory, org_id))]
    for blob in blobs:
        for sentinel in (*sentinels, SMTP_PASSWORD):
            assert sentinel.lower() not in blob.lower(), sentinel


async def test_email_status_published_after_commit(runtime, session_factory, adapters, fake):
    org_id, box_id = await org_with_mailbox(session_factory, adapters)
    conv_id = await email_conversation(session_factory, org_id, box_id)

    # A reply whose transaction rolls back queues nothing.
    await reply(runtime, org_id, conv_id, "rolled back", commit=False)
    assert adapters.job_queue.take() == []
    async with session_factory() as s:
        assert (await s.execute(select(EmailMessage.id).where(EmailMessage.direction == "outbound"))).first() is None

    message_id, email_id = await reply(runtime, org_id, conv_id)
    during: list[list[tuple[str, str | None]]] = []

    async def during_send():  # the outcome is not committed yet, so it is not published either
        await asyncio.sleep(0.01)
        during.append(statuses(adapters, org_id))

    fake.before_send = during_send
    assert await send(runtime, org_id, email_id) == "sent"

    events = [m for m in adapters.pubsub.on(f"inbox:{org_id.hex}") if m["type"] == "email.status"]
    assert events == [
        {"type": "email.status", "conversation_id": str(conv_id), "message_id": str(message_id),
         "status": "sending", "error_type": None},
        {"type": "email.status", "conversation_id": str(conv_id), "message_id": str(message_id),
         "status": "sent", "error_type": None},
    ]
    assert during == [[("sending", None)]]


async def test_jobs_dispatch_send_email(runtime, session_factory, adapters, fake):
    from supportcore.knowledge.jobs import JOB_HANDLERS, dispatch

    assert "send_email" in JOB_HANDLERS
    org_id, box_id = await org_with_mailbox(session_factory, adapters)
    conv_id = await email_conversation(session_factory, org_id, box_id)
    await reply(runtime, org_id, conv_id)
    name, payload, _ = adapters.job_queue.take()[0]
    await dispatch(runtime, name, payload)
    await drain_pending()
    assert len(fake.sent) == 1


async def test_send_job_cannot_touch_another_organisations_row(runtime, session_factory, adapters, fake):
    org_id, box_id = await org_with_mailbox(session_factory, adapters)
    conv_id = await email_conversation(session_factory, org_id, box_id)
    _, email_id = await reply(runtime, org_id, conv_id)
    other_org, _ = await org_with_mailbox(session_factory, adapters, "Other")
    assert await send(runtime, other_org, email_id) == "skipped"
    assert fake.attempts == [] and (await row(session_factory, email_id)).delivery_status == "queued"


# --- fix round 1: claim clock, lost claim, trimming, the sweep ---------------------------------------------


def _outbound_metric(org_id, outcome) -> float:
    return REGISTRY.get_sample_value(
        "support_email_outbound_total", {"organisation_id": str(org_id), "outcome": outcome}
    ) or 0.0


async def test_claim_uses_database_clock(runtime, session_factory, adapters, fake):
    """`sending_until` comes from the database clock, not from the job's `now` (here an hour off)."""
    org_id, box_id = await org_with_mailbox(session_factory, adapters)
    conv_id = await email_conversation(session_factory, org_id, box_id)
    _, email_id = await reply(runtime, org_id, conv_id)
    claims = []

    async def look():
        claims.append((await row(session_factory, email_id)).sending_until)

    fake.before_send = look
    before = datetime.now(UTC)
    assert await send(runtime, org_id, email_id, before - timedelta(hours=1)) == "sent"
    assert len(claims) == 1
    assert before + timedelta(seconds=110) < claims[0] < datetime.now(UTC) + timedelta(seconds=130)


async def test_lost_claim_writes_records_and_counts_nothing(runtime, session_factory, adapters, fake):
    """While the send is in flight the row is taken from this run (here: marked interrupted, as the sweep does
    after an expired claim). The late outcome is not written, recorded as an integration error or counted."""
    org_id, box_id = await org_with_mailbox(session_factory, adapters)
    conv_id = await email_conversation(session_factory, org_id, box_id)
    _, email_id = await reply(runtime, org_id, conv_id)
    adapters.job_queue.take()

    async def taken():
        async with UnitOfWork(session_factory) as uow:
            await uow.session.execute(
                update(EmailMessage).where(EmailMessage.id == email_id)
                .values(delivery_status="failed", error_type="interrupted", sending_until=None)
            )
            await uow.commit()

    fake.before_send = taken
    fake.fail_next(MailSendError("transient", "smtp_4xx"))

    assert await send(runtime, org_id, email_id) == "skipped"

    lost = await row(session_factory, email_id)
    assert (lost.delivery_status, lost.error_type) == ("failed", "interrupted")
    assert await error_types(session_factory, org_id) == {}
    assert _outbound_metric(org_id, "retry") == 0.0
    assert adapters.job_queue.take() == []  # no retry job either
    assert statuses(adapters, org_id) == [("sending", None)]  # the claim only: nothing for the lost outcome


async def test_queue_reply_trims_references(runtime, session_factory, adapters):
    org_id, box_id = await org_with_mailbox(session_factory, adapters)
    conv_id = await email_conversation(session_factory, org_id, box_id)
    refs = [f"r{i}@example.org" for i in range(25)]
    async with UnitOfWork(session_factory) as uow:
        await uow.session.execute(
            update(EmailMessage).where(EmailMessage.rfc_message_id == "in1@example.org").values(references=refs)
        )
        await uow.commit()
    _, email_id = await reply(runtime, org_id, conv_id)
    got = (await row(session_factory, email_id)).references
    assert len(got) == 20
    assert got[0] == "r0@example.org" and got[-1] == "in1@example.org" and got[1] == "r7@example.org"


async def _set(session_factory, email_id, **values):
    async with UnitOfWork(session_factory) as uow:
        await uow.session.execute(update(EmailMessage).where(EmailMessage.id == email_id).values(**values))
        await uow.commit()


async def test_sweep_interrupts_expired_sending_and_requeues_lost_jobs(runtime, session_factory, adapters, fake):
    org_a, box_a = await org_with_mailbox(session_factory, adapters, "A")
    org_b, box_b = await org_with_mailbox(session_factory, adapters, "B")
    conv_a = await email_conversation(session_factory, org_a, box_a)
    conv_b = await email_conversation(session_factory, org_b, box_b)
    _, stuck = await reply(runtime, org_a, conv_a, "stuck")  # a worker died mid-send
    _, live = await reply(runtime, org_a, conv_a, "live")  # being sent right now
    _, lost_queued = await reply(runtime, org_b, conv_b, "lost")  # its job never ran
    _, lost_retry = await reply(runtime, org_b, conv_b, "lost retry")  # its retry job never ran
    _, fresh = await reply(runtime, org_b, conv_b, "fresh")  # its job is still on its way
    _, waiting = await reply(runtime, org_b, conv_b, "waiting")  # backoff not over
    adapters.job_queue.take()
    now = datetime.now(UTC)
    await _set(session_factory, stuck, delivery_status="sending", attempts=1, sending_until=now - timedelta(seconds=5))
    await _set(session_factory, live, delivery_status="sending", attempts=1, sending_until=now + timedelta(seconds=60))
    await _set(session_factory, lost_queued, created_at=now - timedelta(minutes=6))
    await _set(
        session_factory, lost_retry, delivery_status="retry", attempts=2, error_type="smtp_4xx",
        created_at=now - timedelta(hours=1), next_attempt_at=now - timedelta(minutes=6),
    )
    await _set(
        session_factory, waiting, delivery_status="retry", attempts=1, created_at=now - timedelta(hours=1),
        next_attempt_at=now + timedelta(minutes=1),
    )

    assert await sweep_outbound(runtime, now) == (1, 2)
    await drain_pending()

    interrupted = await row(session_factory, stuck)
    assert (interrupted.delivery_status, interrupted.error_type, interrupted.sending_until) == (
        "failed", "interrupted", None,
    )
    assert (await row(session_factory, live)).delivery_status == "sending"
    assert (await row(session_factory, fresh)).delivery_status == "queued"
    assert await error_types(session_factory, org_a) == {("email", "smtp", "interrupted"): 1}
    assert await error_types(session_factory, org_b) == {}
    assert statuses(adapters, org_a)[-1] == ("failed", "interrupted")
    assert sorted(adapters.job_queue.take(), key=lambda j: j[1]["email_message_id"]) == sorted(
        [
            (SEND_EMAIL_JOB, {"organisation_id": str(org_b), "email_message_id": str(lost_queued)}, None),
            (SEND_EMAIL_JOB, {"organisation_id": str(org_b), "email_message_id": str(lost_retry)}, None),
        ],
        key=lambda j: j[1]["email_message_id"],
    )
    assert fake.attempts == []  # the sweep itself never sends

    # The re-enqueued jobs send once each; a second sweep finds nothing.
    assert await send(runtime, org_b, lost_queued) == "sent"
    assert await send(runtime, org_b, lost_retry) == "sent"
    assert await sweep_outbound(runtime, datetime.now(UTC)) == (0, 0)
    assert adapters.job_queue.take() == []


async def test_redelivered_job_inside_claim_is_recovered_by_the_sweep(runtime, session_factory, adapters, fake):
    """A redelivered job lands while the dead run's claim is still live (`skipped`); the sweep marks the row
    `interrupted` once the claim expires, so it never stays `sending`."""
    org_id, box_id = await org_with_mailbox(session_factory, adapters)
    conv_id = await email_conversation(session_factory, org_id, box_id)
    _, email_id = await reply(runtime, org_id, conv_id)
    adapters.job_queue.take()
    now = datetime.now(UTC)
    await _set(session_factory, email_id, delivery_status="sending", attempts=1, sending_until=now + timedelta(seconds=1))

    assert await send(runtime, org_id, email_id) == "skipped"  # the redelivery
    assert (await row(session_factory, email_id)).delivery_status == "sending"
    await asyncio.sleep(1.2)  # the claim expires (database clock)
    assert await sweep_outbound(runtime, datetime.now(UTC)) == (1, 0)
    assert (await row(session_factory, email_id)).error_type == "interrupted"
    assert fake.attempts == []


async def test_sync_mailboxes_tick_runs_the_sweep(runtime, session_factory, adapters):
    from supportcore.knowledge.jobs import dispatch

    org_id, box_id = await org_with_mailbox(session_factory, adapters)
    conv_id = await email_conversation(session_factory, org_id, box_id)
    _, email_id = await reply(runtime, org_id, conv_id)
    adapters.job_queue.take()
    await _set(session_factory, email_id, created_at=datetime.now(UTC) - timedelta(minutes=10))

    await dispatch(runtime, "sync_mailboxes", {})

    assert (SEND_EMAIL_JOB, {"organisation_id": str(org_id), "email_message_id": str(email_id)}, None) in (
        adapters.job_queue.take()
    )
