"""Email replies through the inbox routes (Phase 6, Task 7): POST …/messages with `send_email`, and the retry
route. Real commits (raw_client); the session_factory fixture truncates. The job queue records instead of running,
so each test runs `send_email` itself."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import select

from kb_helpers import create_org, join_org, signup
from supportcore.db.models import Conversation, Customer, EmailMailbox, EmailMessage, Message
from supportcore.events import EventBus
from supportcore.jobs.ports import JobRuntime
from supportcore.mail.outbound import send_email
from supportcore.mail.ports import MailSendError
from supportcore.realtime.spawn import drain_pending


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs = []

    def enqueue(self, name, payload, *, countdown=None) -> None:
        self.jobs.append((name, payload))


@pytest.fixture
def test_settings(test_settings):
    return test_settings.model_copy(update={"mail_inbound": "fake", "mail_outbound": "fake"})


@pytest.fixture(autouse=True)
def _truncate(session_factory):
    """Real-commit tests: session_factory truncates every table at teardown."""


@pytest.fixture
def queue(app) -> RecordingQueue:
    container = app.state.container
    recording = RecordingQueue()
    container.adapters = replace(container.adapters, job_queue=recording)
    return recording


@pytest.fixture
def runtime(app, session_factory, test_settings) -> JobRuntime:
    return JobRuntime(
        settings=test_settings, session_factory=session_factory, adapters=app.state.container.adapters, events=EventBus()
    )


async def _setup(client, session_factory, *, mailbox=True):
    owner = await signup(client, "owner@x.com")
    org_id = await create_org(client, owner)
    agent = await join_org(client, session_factory, UUID(org_id), "agent@x.com", role_name="Agent")
    if mailbox:
        r = await client.put(
            f"/orgs/{org_id}/channels/email",
            json={
                "address": "support@acme.example", "imap_host": "imap.acme.example", "imap_username": "u",
                "imap_password": "imap-pw", "smtp_host": "smtp.acme.example", "smtp_username": "u",
                "smtp_password": "smtp-pw",
            },
            headers=owner,
        )
        assert r.status_code == 200, r.text
    return org_id, owner, agent


async def _conversation(session_factory, org_id, *, channel="chat", contact_email=None, inbound_id=None):
    async with session_factory() as s:
        customer = Customer(organisation_id=UUID(org_id), email="cust@example.org")
        s.add(customer)
        await s.flush()
        conversation = Conversation(
            organisation_id=UUID(org_id), customer_id=customer.id, status="open", channel=channel,
            contact_email=contact_email, subject="Printer" if channel == "email" else None,
        )
        s.add(conversation)
        await s.flush()
        if inbound_id:
            message = Message(organisation_id=UUID(org_id), conversation_id=conversation.id, role="customer", text="hi")
            s.add(message)
            await s.flush()
            box = (await s.execute(select(EmailMailbox.id).where(EmailMailbox.organisation_id == UUID(org_id)))).scalar()
            s.add(
                EmailMessage(
                    organisation_id=UUID(org_id), message_id=message.id, conversation_id=conversation.id,
                    mailbox_id=box, direction="inbound", rfc_message_id=inbound_id, subject="Printer",
                    from_address="cust@example.org", delivery_status="received",
                )
            )
        await s.commit()
        return conversation.id


async def test_reply_on_email_conversation_is_emailed(raw_client, session_factory, app, queue, runtime):
    org_id, _, agent = await _setup(raw_client, session_factory)
    cid = await _conversation(session_factory, org_id, channel="email", inbound_id="in1@example.org")

    r = await raw_client.post(f"/orgs/{org_id}/inbox/conversations/{cid}/messages", json={"text": "On it"}, headers=agent)

    assert r.status_code == 201, r.text
    assert [name for name, _ in queue.jobs] == ["send_email"]
    payload = queue.jobs[0][1]
    assert await send_email(runtime, UUID(org_id), UUID(payload["email_message_id"]), datetime.now(UTC)) == "sent"
    await drain_pending()
    sent = app.state.container.adapters.outbound_mail.sent_mime()
    assert len(sent) == 1 and sent[0]["In-Reply-To"] == "<in1@example.org>" and sent[0]["Subject"] == "Re: Printer"
    async with session_factory() as s:
        row = (await s.execute(select(EmailMessage).where(EmailMessage.direction == "outbound"))).scalar_one()
    assert str(row.message_id) == r.json()["id"] and row.delivery_status == "sent"
    # the reply itself carries its email metadata, so the inbox shows delivery state straight away
    assert r.json()["email"]["direction"] == "outbound" and r.json()["email"]["delivery_status"] == "queued"


async def test_chat_reply_emailed_only_with_send_email(raw_client, session_factory, queue):
    org_id, _, agent = await _setup(raw_client, session_factory)
    cid = await _conversation(session_factory, org_id, contact_email="chatter@example.org")
    url = f"/orgs/{org_id}/inbox/conversations/{cid}/messages"

    plain = await raw_client.post(url, json={"text": "widget only"}, headers=agent)
    assert plain.status_code == 201 and plain.json()["email"] is None
    assert queue.jobs == []
    r = await raw_client.post(url, json={"text": "by email too", "send_email": True}, headers=agent)
    assert r.status_code == 201, r.text
    assert r.json()["email"]["direction"] == "outbound"
    assert [name for name, _ in queue.jobs] == ["send_email"]


async def test_send_email_without_contact_or_mailbox_409(raw_client, session_factory, queue):
    org_id, _, agent = await _setup(raw_client, session_factory)
    no_contact = await _conversation(session_factory, org_id)
    r = await raw_client.post(
        f"/orgs/{org_id}/inbox/conversations/{no_contact}/messages", json={"text": "x", "send_email": True},
        headers=agent,
    )
    assert r.status_code == 409 and r.json()["code"] == "email_not_available"

    bare_org, _, bare_agent = await _setup_second(raw_client, session_factory)
    with_contact = await _conversation(session_factory, bare_org, contact_email="chatter@example.org")
    r = await raw_client.post(
        f"/orgs/{bare_org}/inbox/conversations/{with_contact}/messages", json={"text": "x", "send_email": True},
        headers=bare_agent,
    )
    assert r.status_code == 409 and r.json()["code"] == "email_not_available"
    async with session_factory() as s:  # the refused replies wrote nothing
        assert (await s.execute(select(Message.id).where(Message.role == "agent"))).first() is None
    assert queue.jobs == []


async def test_chat_reply_for_identified_customer_without_mailbox_succeeds(raw_client, session_factory, queue):
    """A JWT-identified widget customer has customer.email but left no contact email in the chat: the summary
    offers no email follow-up (contact_email null), and the agent's plain reply is stored, mailbox or not."""
    org_id, _, agent = await _setup(raw_client, session_factory, mailbox=False)
    async with session_factory() as s:
        customer = Customer(organisation_id=UUID(org_id), email="jwt@example.org", external_id="user-42")
        s.add(customer)
        await s.flush()
        conversation = Conversation(organisation_id=UUID(org_id), customer_id=customer.id, status="open", channel="chat")
        s.add(conversation)
        await s.commit()
        cid = conversation.id

    detail = await raw_client.get(f"/orgs/{org_id}/inbox/conversations/{cid}", headers=agent)
    assert detail.status_code == 200, detail.text
    assert detail.json()["summary"]["contact_email"] is None
    assert detail.json()["summary"]["customer"]["email"] == "jwt@example.org"

    r = await raw_client.post(f"/orgs/{org_id}/inbox/conversations/{cid}/messages", json={"text": "Hi"}, headers=agent)
    assert r.status_code == 201, r.text
    assert r.json()["email"] is None and queue.jobs == []


async def test_email_conversation_contact_falls_back_to_customer_email(raw_client, session_factory, queue):
    org_id, _, agent = await _setup(raw_client, session_factory)
    cid = await _conversation(session_factory, org_id, channel="email")  # an older row: no contact_email

    detail = await raw_client.get(f"/orgs/{org_id}/inbox/conversations/{cid}", headers=agent)

    assert detail.json()["summary"]["contact_email"] == "cust@example.org"


async def _setup_second(client, session_factory):
    owner = await signup(client, "owner2@x.com")
    org_id = await create_org(client, owner, "NoMailbox")
    agent = await join_org(client, session_factory, UUID(org_id), "agent2@x.com", role_name="Agent")
    return org_id, owner, agent


async def _failed_email(raw_client, session_factory, app, queue, runtime):
    org_id, owner, agent = await _setup(raw_client, session_factory)
    cid = await _conversation(session_factory, org_id, channel="email", inbound_id="in1@example.org")
    r = await raw_client.post(f"/orgs/{org_id}/inbox/conversations/{cid}/messages", json={"text": "x"}, headers=agent)
    mid = r.json()["id"]
    email_id = UUID(queue.jobs.pop()[1]["email_message_id"])
    return org_id, owner, agent, cid, mid, email_id


async def test_retry_route_requeues_failed_and_409_otherwise(raw_client, session_factory, app, queue, runtime):
    org_id, _, agent, cid, mid, email_id = await _failed_email(raw_client, session_factory, app, queue, runtime)
    url = f"/orgs/{org_id}/inbox/conversations/{cid}/messages/{mid}/email/retry"

    r = await raw_client.post(url, headers=agent)  # still queued
    assert r.status_code == 409 and r.json()["code"] == "email_not_retryable"

    app.state.container.adapters.outbound_mail.fail_next(MailSendError("permanent", "smtp_5xx"))
    assert await send_email(runtime, UUID(org_id), email_id, datetime.now(UTC)) == "failed"
    await drain_pending()
    r = await raw_client.post(url, headers=agent)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["delivery_status"] == "queued" and body["direction"] == "outbound" and body["error_type"] is None
    assert queue.jobs == [("send_email", {"organisation_id": org_id, "email_message_id": str(email_id)})]

    r = await raw_client.post(f"/orgs/{org_id}/inbox/conversations/{cid}/messages/{uuid.uuid4()}/email/retry", headers=agent)
    assert r.status_code == 404 and r.json()["code"] == "not_found"


async def test_retry_route_requires_queries_answer(raw_client, session_factory, app, queue, runtime):
    org_id, _, _, cid, mid, email_id = await _failed_email(raw_client, session_factory, app, queue, runtime)
    app.state.container.adapters.outbound_mail.fail_next(MailSendError("permanent", "smtp_5xx"))
    assert await send_email(runtime, UUID(org_id), email_id, datetime.now(UTC)) == "failed"
    await drain_pending()
    viewer = await join_org(raw_client, session_factory, UUID(org_id), "viewer@x.com", role_name="Viewer")
    url = f"/orgs/{org_id}/inbox/conversations/{cid}/messages/{mid}/email/retry"

    assert (await raw_client.post(url, headers=viewer)).status_code == 403
    r = await raw_client.post(
        f"/orgs/{org_id}/inbox/conversations/{cid}/messages", json={"text": "x", "send_email": True}, headers=viewer
    )
    assert r.status_code == 403
    async with session_factory() as s:
        assert (await s.get(EmailMessage, email_id)).delivery_status == "failed"
    assert queue.jobs == []


async def test_reply_route_cross_tenant_404(raw_client, session_factory, app, queue, runtime):
    org_id, _, _, cid, mid, email_id = await _failed_email(raw_client, session_factory, app, queue, runtime)
    app.state.container.adapters.outbound_mail.fail_next(MailSendError("permanent", "smtp_5xx"))
    assert await send_email(runtime, UUID(org_id), email_id, datetime.now(UTC)) == "failed"
    await drain_pending()
    other_org, other_owner, _ = await _setup_second(raw_client, session_factory)

    r = await raw_client.post(
        f"/orgs/{other_org}/inbox/conversations/{cid}/messages", json={"text": "x", "send_email": True},
        headers=other_owner,
    )
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    r = await raw_client.post(f"/orgs/{other_org}/inbox/conversations/{cid}/messages/{mid}/email/retry", headers=other_owner)
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    # The owning organisation's path, by a non-member.
    r = await raw_client.post(f"/orgs/{org_id}/inbox/conversations/{cid}/messages/{mid}/email/retry", headers=other_owner)
    assert r.status_code == 404
    async with session_factory() as s:
        assert (await s.get(EmailMessage, email_id)).delivery_status == "failed"
    assert queue.jobs == []
