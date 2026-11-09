"""Inbound ingestion into the inbox (Phase 6, Task 5). Real Postgres with committed data, a local file store in
tmp_path and a recording memory pub/sub."""

import hashlib
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from supportcore.analytics.rollup import compute_hour
from supportcore.config import Settings
from supportcore.db.models import (
    Conversation,
    ConversationEvent,
    Customer,
    EmailAttachment,
    EmailMailbox,
    EmailMessage,
    Membership,
    Message,
    Role,
    SupportSettings,
    User,
)
from supportcore.db.uow import UnitOfWork
from supportcore.events import EventBus
from supportcore.inbox.customers import linked_conversations
from supportcore.inbox.publisher import InboxPublisher
from supportcore.mail.attachments import store_attachments
from supportcore.mail.inbound import InboundEmailService
from supportcore.mail.parse import RawAttachment
from supportcore.mail.sanitize import SanitizedHtml
from supportcore.orgs.service import OrganisationService
from supportcore.presence.memory_presence import MemoryAgentPresence
from supportcore.pubsub.memory_pubsub import MemoryPubSub
from supportcore.realtime.spawn import drain_pending
from supportcore.support.settings_service import SupportSettingsService

from .test_classify import parsed

pytestmark = pytest.mark.asyncio

ALL_DAY = {day: [["00:00", "24:00"]] for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


class RecordingPubSub(MemoryPubSub):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[tuple[str, dict]] = []

    async def publish(self, channel: str, message: dict) -> None:
        self.published.append((channel, message))
        await super().publish(channel, message)

    def on(self, channel: str) -> list[dict]:
        return [m for c, m in self.published if c == channel]


@dataclass
class World:
    org_id: uuid.UUID
    mailbox_id: uuid.UUID
    agent_id: uuid.UUID


@pytest.fixture
def settings(tmp_path):
    from cryptography.fernet import Fernet

    from supportcore.testing import TEST_DATABASE_URL

    return Settings(
        _env_file=None, env="test", database_url=TEST_DATABASE_URL, redis_url="redis://localhost:6379/0",
        jwt_secret="test-secret-test-secret-test-secret-123", smtp_from="noreply@test.local",
        secrets_key=Fernet.generate_key().decode(), file_storage="local", local_storage_path=str(tmp_path / "up"),
        vector_store="chroma", rate_limiter="memory", pubsub="memory", presence="memory", checkpointer="memory",
    )


@pytest.fixture
async def adapters(settings):
    from supportcore.wiring import build_adapters

    built = replace(build_adapters(settings), pubsub=RecordingPubSub(), presence=MemoryAgentPresence())
    yield built
    await drain_pending()
    await built.aclose()


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
async def world(session_factory, bus, adapters) -> World:
    async with UnitOfWork(session_factory) as uow:
        s = uow.session
        owner = User(email="owner@x.com", password_hash="x", name="Owner")
        s.add(owner)
        await s.flush()
        org = await OrganisationService(s, bus).create(owner, "Acme")
        owner_m = (await s.execute(select(Membership).where(Membership.user_id == owner.id))).scalar_one()
        owner_m.agent_status = "offline"
        (await SupportSettingsService(s, org.id, bus).get()).weekly_hours = ALL_DAY
        agent = User(email="ann@x.com", password_hash="x", name="Ann")
        s.add(agent)
        await s.flush()
        role_id = (await s.execute(select(Role.id).where(Role.organisation_id == org.id, Role.name == "Agent"))).scalar_one()
        member = Membership(organisation_id=org.id, user_id=agent.id, role_id=role_id, status="active", agent_status="online")
        s.add(member)
        box = EmailMailbox(
            organisation_id=org.id, address="support@acme.example", imap_host="i", imap_username="u",
            imap_password_encrypted="x", smtp_host="s", smtp_username="u", smtp_password_encrypted="x",
        )
        s.add(box)
        await s.flush()
        await uow.commit()
    await adapters.presence.beat(org.id, member.id)
    return World(org.id, box.id, member.id)


@pytest.fixture
def ingest(session_factory, world, bus, adapters, settings):
    async def run(p, *, html=None, stored=(), skipped=(), uid=1, commit=True):
        async with UnitOfWork(session_factory) as uow:
            mailbox = await uow.session.get(EmailMailbox, world.mailbox_id)
            svc = InboundEmailService(
                uow.session, world.org_id, bus, adapters, settings, InboxPublisher(adapters.pubsub, session_factory)
            )
            result = await svc.ingest(mailbox, p, SanitizedHtml(html, False) if html else None, list(stored), list(skipped), uid)
            if commit:
                await uow.commit()
        await drain_pending()
        return result

    return run


@pytest.fixture
def bounce(session_factory, world, bus, adapters, settings):
    async def run(p, *, commit=True):
        async with UnitOfWork(session_factory) as uow:
            svc = InboundEmailService(
                uow.session, world.org_id, bus, adapters, settings, InboxPublisher(adapters.pubsub, session_factory)
            )
            found = await svc.handle_bounce(p)
            if commit:
                await uow.commit()
        await drain_pending()
        return found

    return run


async def _conv(factory, cid) -> Conversation:
    async with factory() as s:
        return await s.get(Conversation, cid)


async def _seed_chat(factory, world, *, status="bot", email="c@example.com", outbound_id=None):
    """A widget customer with a chat conversation (and, optionally, an outbound email row)."""
    async with UnitOfWork(factory) as uow:
        s = uow.session
        customer = Customer(organisation_id=world.org_id, visitor_id=uuid.uuid4().hex, email=email)
        s.add(customer)
        await s.flush()
        conv = Conversation(organisation_id=world.org_id, customer_id=customer.id, channel="chat", status=status)
        s.add(conv)
        await s.flush()
        if outbound_id:
            msg = Message(organisation_id=world.org_id, conversation_id=conv.id, role="agent", text="hello")
            s.add(msg)
            await s.flush()
            s.add(
                EmailMessage(
                    organisation_id=world.org_id, message_id=msg.id, conversation_id=conv.id, direction="outbound",
                    rfc_message_id=outbound_id, from_address="support@acme.example", delivery_status="sent",
                )
            )
        await uow.commit()
    return customer.id, conv.id


async def test_new_email_creates_open_email_conversation_assigned_round_robin(session_factory, world, ingest):
    before = datetime.now(UTC)
    result = await ingest(parsed(subject="Order late", text="where is it"))
    assert result.created and not result.duplicate
    c = await _conv(session_factory, result.conversation_id)
    assert c.channel == "email" and c.status == "open" and c.subject == "Order late"
    assert c.assignee_membership_id == world.agent_id
    assert c.first_response_due_at is not None and c.first_response_due_at > before
    assert c.resolution_due_at is not None
    async with session_factory() as s:
        row = (await s.execute(select(SupportSettings).where(SupportSettings.organisation_id == world.org_id))).scalar_one()
        assert row.last_assigned_membership_id == world.agent_id
        email = (await s.execute(select(EmailMessage))).scalar_one()
        assert email.direction == "inbound" and email.delivery_status == "received" and email.imap_uid == 1
        msg = await s.get(Message, email.message_id)
        assert msg.role == "customer" and msg.text == "where is it"


async def test_email_conversation_routed_without_system_message(session_factory, ingest, adapters):
    result = await ingest(parsed())
    async with session_factory() as s:
        roles = (await s.execute(select(Message.role).where(Message.conversation_id == result.conversation_id))).scalars().all()
    assert roles == ["customer"]
    assert not [c for c, _ in adapters.pubsub.published if c.startswith("conv:")]


async def test_customer_linked_by_email_across_channels(session_factory, world, ingest):
    customer_id, chat_id = await _seed_chat(session_factory, world, email="Jo@Example.com")
    result = await ingest(parsed(from_address="jo@example.com", message_id="a@x"))
    async with session_factory() as s:
        conv = await s.get(Conversation, result.conversation_id)
        assert conv.customer_id == customer_id
        customers = (await s.execute(select(Customer))).scalars().all()
        assert len(customers) == 1
        linked = await linked_conversations(s, world.org_id, customers[0])
    assert chat_id in {item.id for item in linked}
    assert result.conversation_id in {item.id for item in linked}


async def test_reply_threads_into_existing_and_pending_becomes_open(session_factory, ingest):
    first = await ingest(parsed(message_id="m1@x"))
    async with session_factory() as s:
        (await s.get(Conversation, first.conversation_id)).status = "pending"
        await s.commit()
    second = await ingest(parsed(message_id="m2@x", in_reply_to="m1@x", subject="Re: Hi"), uid=2)
    assert not second.created and second.conversation_id == first.conversation_id
    assert (await _conv(session_factory, first.conversation_id)).status == "open"


async def test_reply_from_other_address_threads_by_headers(session_factory, ingest):
    first = await ingest(parsed(message_id="m1@x"))
    second = await ingest(parsed(message_id="m2@x", in_reply_to="m1@x", from_address="other@example.com", subject="zzz"), uid=2)
    assert second.conversation_id == first.conversation_id
    async with session_factory() as s:
        assert len((await s.execute(select(Customer))).scalars().all()) == 2


async def test_reply_to_resolved_reopens(session_factory, ingest):
    first = await ingest(parsed(message_id="m1@x"))
    async with session_factory() as s:
        c = await s.get(Conversation, first.conversation_id)
        c.status, c.resolved_at = "resolved", datetime.now(UTC)
        stale = datetime(2000, 1, 1, tzinfo=UTC)
        c.resolution_due_at, c.team_entered_at, c.sla_breached = stale, stale, ["resolution"]
        await s.commit()
    before = datetime.now(UTC)
    await ingest(parsed(message_id="m2@x", in_reply_to="m1@x"), uid=2)
    c = await _conv(session_factory, first.conversation_id)
    assert c.status == "open" and c.resolved_at is None
    assert c.resolution_due_at > before and c.team_entered_at >= before and c.sla_breached == []
    assert c.assignee_membership_id is not None
    async with session_factory() as s:
        kinds = (await s.execute(select(ConversationEvent.kind).where(ConversationEvent.conversation_id == c.id))).scalars().all()
    assert "reopened" in kinds


async def test_reply_to_closed_starts_new_conversation(session_factory, ingest):
    first = await ingest(parsed(message_id="m1@x"))
    async with session_factory() as s:
        (await s.get(Conversation, first.conversation_id)).status = "closed"
        await s.commit()
    second = await ingest(parsed(message_id="m2@x", in_reply_to="m1@x"), uid=2)
    assert second.created and second.conversation_id != first.conversation_id
    assert (await _conv(session_factory, second.conversation_id)).status == "open"
    assert (await _conv(session_factory, first.conversation_id)).status == "closed"


async def test_chat_followup_reply_threads_into_chat_conversation(session_factory, world, ingest):
    _, chat_id = await _seed_chat(session_factory, world, outbound_id="out1@acme.example")
    result = await ingest(parsed(message_id="r1@x", in_reply_to="out1@acme.example"))
    assert result.conversation_id == chat_id and not result.created
    c = await _conv(session_factory, chat_id)
    assert c.channel == "chat" and c.status == "open" and c.assignee_membership_id == world.agent_id
    async with session_factory() as s:
        roles = (await s.execute(select(Message.role).where(Message.conversation_id == chat_id))).scalars().all()
    assert "system" not in roles and roles.count("customer") == 1


async def test_duplicate_message_id_ignored(session_factory, ingest, adapters):
    first = await ingest(parsed(message_id="dup@x"))
    published = len(adapters.pubsub.published)
    again = await ingest(parsed(message_id="dup@x"), uid=2)
    assert again.duplicate and not again.created and again.conversation_id == first.conversation_id
    assert again.message_id == first.message_id
    assert len(adapters.pubsub.published) == published
    async with session_factory() as s:
        assert len((await s.execute(select(EmailMessage))).scalars().all()) == 1
        assert len((await s.execute(select(Message))).scalars().all()) == 1


async def test_attachments_stored_with_deterministic_keys_and_rows(session_factory, world, adapters, ingest):
    kept = [
        RawAttachment("a.txt", "text/plain", b"hello", None, False),
        RawAttachment("b.png", "image/png", b"\x89PNG", "cid1", True),
    ]
    stored = await store_attachments(adapters.storage, world.org_id, "att@x", kept)
    again = await store_attachments(adapters.storage, world.org_id, "att@x", kept)
    assert stored == again
    folder = hashlib.sha256(b"att@x").hexdigest()
    assert stored[0].storage_key == f"orgs/{world.org_id}/email/{folder}/0-{hashlib.sha256(b'hello').hexdigest()}"
    assert await adapters.storage.get(stored[1].storage_key) == b"\x89PNG"
    await ingest(parsed(message_id="att@x"), stored=stored, skipped=[{"filename": "c.exe", "size": 3, "reason": "type_not_allowed"}])
    async with session_factory() as s:
        rows = (await s.execute(select(EmailAttachment).order_by(EmailAttachment.created_at, EmailAttachment.filename))).scalars().all()
        email = (await s.execute(select(EmailMessage))).scalar_one()
    assert {(r.filename, r.size_bytes, r.storage_key, r.content_id) for r in rows} == {
        ("a.txt", 5, stored[0].storage_key, None),
        ("b.png", 4, stored[1].storage_key, "cid1"),
    }
    assert all(r.email_message_id == email.id for r in rows)
    assert email.skipped_attachments == [{"filename": "c.exe", "size": 3, "reason": "type_not_allowed"}]


async def test_publishes_after_commit_only(session_factory, world, ingest, adapters):
    inbox = f"inbox:{world.org_id.hex}"
    await ingest(parsed(message_id="rb@x"), commit=False)
    assert adapters.pubsub.on(inbox) == []
    async with session_factory() as s:
        assert (await s.execute(select(EmailMessage))).scalars().all() == []
    result = await ingest(parsed(message_id="ok@x"))
    upserts = [m for m in adapters.pubsub.on(inbox) if m["type"] == "conversation.upsert"]
    assert len(upserts) == 1 and upserts[0]["conversation"]["id"] == str(result.conversation_id)


async def test_email_conversation_counted_under_email_channel(session_factory, world, ingest):
    await ingest(parsed())
    async with session_factory() as s:
        stats = await compute_hour(s, world.org_id, datetime.now(UTC))
    assert stats.channels["email"]["conversations_started"] == 1
    assert "chat" not in stats.channels


async def test_email_entry_not_counted_as_escalation_reason(session_factory, world, ingest):
    await ingest(parsed())
    async with session_factory() as s:
        stats = await compute_hour(s, world.org_id, datetime.now(UTC))
    assert stats.channels["email"]["entered_team"] == 1
    assert [r for r in stats.reasons if r[0] == "escalation"] == []


async def test_bounce_marks_outbound_failed(session_factory, world, bounce, adapters):
    _, chat_id = await _seed_chat(session_factory, world, outbound_id="out2@acme.example")
    assert await bounce(parsed(bounced_message_id="nope@x", is_delivery_report=True)) is False
    assert await bounce(parsed(bounced_message_id=None, is_delivery_report=True)) is False
    assert await bounce(parsed(bounced_message_id="out2@acme.example", is_delivery_report=True)) is True
    async with session_factory() as s:
        row = (await s.execute(select(EmailMessage))).scalar_one()
    assert row.delivery_status == "failed" and row.error_type == "bounce"
    events = [m for m in adapters.pubsub.on(f"inbox:{world.org_id.hex}") if m["type"] == "email.status"]
    assert len(events) == 1 and events[0]["conversation_id"] == str(chat_id)
    assert (events[0]["status"], events[0]["error_type"]) == ("failed", "bounce")


async def test_bounce_rollback_publishes_nothing(session_factory, world, bounce, adapters):
    await _seed_chat(session_factory, world, outbound_id="out3@acme.example")
    assert await bounce(parsed(bounced_message_id="out3@acme.example"), commit=False) is True
    assert [m for m in adapters.pubsub.on(f"inbox:{world.org_id.hex}") if m["type"] == "email.status"] == []
    async with session_factory() as s:
        row = (await s.execute(select(EmailMessage))).scalar_one()
    assert row.delivery_status == "sent"


async def test_newest_last_seen_customer_wins_and_name_filled_only_when_empty(session_factory, world, ingest):
    old, new = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC)
    async with UnitOfWork(session_factory) as uow:
        a = Customer(organisation_id=world.org_id, visitor_id="va", email="Jo@Example.com", last_seen_at=new)
        b = Customer(organisation_id=world.org_id, visitor_id="vb", email="jo@example.com", last_seen_at=old, name="Old")
        uow.session.add_all([a, b])
        await uow.session.flush()
        await uow.commit()
    first = await ingest(parsed(message_id="n1@x", from_address="jo@example.com", from_name="Jo"))
    second = await ingest(parsed(message_id="n2@x", from_address="jo@example.com", from_name="Zed"), uid=2)
    async with session_factory() as s:
        for cid in (first.conversation_id, second.conversation_id):
            assert (await s.get(Conversation, cid)).customer_id == a.id
        a2, b2 = await s.get(Customer, a.id), await s.get(Customer, b.id)
    assert a2.name == "Jo" and a2.last_seen_at > new  # filled when empty, never overwritten by the second mail
    assert b2.name == "Old" and b2.last_seen_at == old


@pytest.fixture
async def org_b(session_factory, bus):
    """Another organisation with the same sender, the same inbound Message-ID and the same outbound id."""
    async with UnitOfWork(session_factory) as uow:
        s = uow.session
        owner = User(email="ownerb@x.com", password_hash="x", name="OwnerB")
        s.add(owner)
        await s.flush()
        org = await OrganisationService(s, bus).create(owner, "Beta")
        customer = Customer(organisation_id=org.id, visitor_id="vb", email="C@Example.com")
        s.add(customer)
        await s.flush()
        conv = Conversation(
            organisation_id=org.id, customer_id=customer.id, channel="email", status="open", subject="Hi"
        )
        s.add(conv)
        await s.flush()
        for rfc, direction in (("shared@x", "inbound"), ("outb@x", "outbound")):
            msg = Message(organisation_id=org.id, conversation_id=conv.id, role="customer", text="b")
            s.add(msg)
            await s.flush()
            s.add(
                EmailMessage(
                    organisation_id=org.id, message_id=msg.id, conversation_id=conv.id, direction=direction,
                    rfc_message_id=rfc, from_address="c@example.com",
                    delivery_status="received" if direction == "inbound" else "sent",
                )
            )
        await uow.commit()
    return org.id, customer.id, conv.id


async def _b_untouched(factory, org_b):
    org_id, customer_id, conv_id = org_b
    async with factory() as s:
        customer = await s.get(Customer, customer_id)
        convs = (await s.execute(select(Conversation).where(Conversation.organisation_id == org_id))).scalars().all()
        msgs = (await s.execute(select(Message).where(Message.conversation_id == conv_id))).scalars().all()
        emails = (await s.execute(select(EmailMessage).where(EmailMessage.organisation_id == org_id))).scalars().all()
    assert customer.last_seen_at is None and customer.name is None
    assert [c.id for c in convs] == [conv_id] and len(msgs) == 2
    assert {(e.rfc_message_id, e.delivery_status) for e in emails} == {("shared@x", "received"), ("outb@x", "sent")}


async def test_other_orgs_customer_with_same_email_is_not_reused(session_factory, world, ingest, org_b):
    result = await ingest(parsed(message_id="own@x", from_address="c@example.com"))
    async with session_factory() as s:
        conv = await s.get(Conversation, result.conversation_id)
        assert conv.customer_id != org_b[1] and conv.organisation_id == world.org_id
        mine = (await s.execute(select(Customer).where(Customer.organisation_id == world.org_id))).scalars().all()
    assert len(mine) == 1
    await _b_untouched(session_factory, org_b)


async def test_other_orgs_identical_message_id_is_not_a_duplicate(session_factory, world, ingest, org_b):
    result = await ingest(parsed(message_id="shared@x"))
    assert result.created and not result.duplicate
    async with session_factory() as s:
        mine = (await s.execute(select(EmailMessage).where(EmailMessage.organisation_id == world.org_id))).scalars().all()
    assert len(mine) == 1 and mine[0].conversation_id == result.conversation_id
    await _b_untouched(session_factory, org_b)


async def test_other_orgs_message_id_is_not_a_header_thread_match(session_factory, world, ingest, org_b):
    result = await ingest(parsed(message_id="own@x", in_reply_to="shared@x", references=("outb@x",), subject="Hi"))
    assert result.created and result.conversation_id != org_b[2]
    await _b_untouched(session_factory, org_b)


async def test_bounce_does_not_match_other_orgs_outbound_row(session_factory, bounce, org_b):
    assert await bounce(parsed(bounced_message_id="outb@x", is_delivery_report=True)) is False
    await _b_untouched(session_factory, org_b)


@pytest.mark.parametrize("status", ["new", "pending", "resolved", "bot"])
async def test_one_conversation_changed_call_per_ingest(session_factory, world, ingest, monkeypatch, status):
    calls = []
    original = InboxPublisher.conversation_changed

    def spy(self, session, organisation_id, conversation_id):
        calls.append(conversation_id)
        return original(self, session, organisation_id, conversation_id)

    monkeypatch.setattr(InboxPublisher, "conversation_changed", spy)
    if status == "new":
        await ingest(parsed(message_id="s1@x"))
    elif status == "bot":
        _, chat_id = await _seed_chat(session_factory, world, outbound_id="o@acme.example")
        await ingest(parsed(message_id="s1@x", in_reply_to="o@acme.example"))
    else:
        first = await ingest(parsed(message_id="s0@x"))
        async with session_factory() as s:
            (await s.get(Conversation, first.conversation_id)).status = status
            await s.commit()
        calls.clear()
        await ingest(parsed(message_id="s1@x", in_reply_to="s0@x"), uid=2)
    assert len(calls) == 1


async def test_summary_contact_email_is_the_sender_after_ingest(session_factory, world, ingest):
    from supportcore.inbox.summary import load_summaries

    result = await ingest(parsed(from_address="jo@example.com", subject="Order late"))
    async with session_factory() as s:
        (summary,) = await load_summaries(s, world.org_id, [result.conversation_id])
    assert summary.contact_email == "jo@example.com" and summary.subject == "Order late"
