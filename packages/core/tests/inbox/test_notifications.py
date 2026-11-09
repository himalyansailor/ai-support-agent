"""Notifications (Task 8): who is notified, the payload (never text), unread counts and marking read.
The assignment path is covered in tests/assignment; here the rollback-isolated `session` and a recording publisher."""

import json

import pytest
from sqlalchemy import select

from supportcore.db.models import Notification
from supportcore.inbox.notes import NoteService
from supportcore.inbox.notifications import InvalidNotificationKindError, NotificationService

from .conftest import Builder


class RecordingPublisher:
    def __init__(self) -> None:
        self.sent: list[tuple[object, dict]] = []
        self.notes: list[object] = []

    def note_added(self, session, organisation_id, conversation_id) -> None:
        self.notes.append(conversation_id)

    def notify(self, session, membership_id, payload) -> None:
        self.sent.append((membership_id, payload))


def _token(membership_id, name="Ann") -> str:
    return f"@[{name}]({membership_id})"


async def test_mention_notifies_each_once(session, org_a):
    b = Builder(session, org_a)
    author, ann, bob = await b.member("Author"), await b.member("Ann"), await b.member("Bob")
    conversation = await b.conversation(await b.customer("Dana"), status="open")
    publisher = RecordingPublisher()
    text = f"{_token(ann.id)} {_token(ann.id)} {_token(bob.id, 'Bob')} {_token(author.id, 'Author')} secret-detail"

    await NoteService(session, org_a.id, publisher=publisher).add(conversation.id, author, text)

    rows = (await session.execute(select(Notification).order_by(Notification.membership_id))).scalars().all()
    assert sorted((r.membership_id, r.kind, r.actor_membership_id) for r in rows) == sorted(
        [(ann.id, "mentioned", author.id), (bob.id, "mentioned", author.id)]
    )  # once each, and the author mentioning themselves gets nothing
    assert sorted(m for m, _ in publisher.sent) == sorted([ann.id, bob.id])
    payload = dict(publisher.sent)[ann.id]
    assert payload["type"] == "notification"
    assert payload["notification"]["actor_name"] == "Author" and payload["notification"]["customer_name"] == "Dana"


async def test_payload_has_no_text(session, org_a):
    b = Builder(session, org_a)
    author, ann = await b.member("Author"), await b.member("Ann")
    customer = await b.customer(None, email="visitor@private.test")
    conversation = await b.conversation(customer, [("customer", "my card number is 4242")], status="open")
    publisher = RecordingPublisher()

    await NoteService(session, org_a.id, publisher=publisher).add(
        conversation.id, author, f"{_token(ann.id)} the password is hunter2"
    )

    ((_, payload),) = publisher.sent
    blob = json.dumps(payload)
    for forbidden in ("hunter2", "4242", "password", "visitor@private.test", "@"):
        assert forbidden not in blob
    assert payload["notification"]["customer_name"] == "Visitor"  # no name: a fallback, never the email
    assert set(payload["notification"]) == {
        "id", "kind", "conversation_id", "actor_membership_id", "actor_name", "customer_name", "read_at", "created_at"
    }


async def test_self_notification_and_unknown_kind(session, org_a):
    b = Builder(session, org_a)
    ann = await b.member("Ann")
    conversation = await b.conversation(status="open")
    service = NotificationService(session, org_a.id)
    assert await service.create(kind="assigned", membership_id=ann.id, conversation_id=conversation.id, actor_membership_id=ann.id) is None
    assert await service.unread_count(ann.id) == 0
    with pytest.raises(InvalidNotificationKindError):
        await service.create(kind="bogus", membership_id=ann.id, conversation_id=conversation.id)
    sla = await service.create(kind="sla_breached", membership_id=ann.id, conversation_id=conversation.id)
    assert sla is not None and sla.actor_name is None


async def test_unread_count_and_list(session, org_a):
    b = Builder(session, org_a)
    ann, bob = await b.member("Ann"), await b.member("Bob")
    conversation = await b.conversation(status="open")
    service = NotificationService(session, org_a.id)
    first = await service.create(kind="assigned", membership_id=ann.id, conversation_id=conversation.id, actor_membership_id=bob.id)
    second = await service.create(kind="mentioned", membership_id=ann.id, conversation_id=conversation.id, actor_membership_id=bob.id)
    await service.create(kind="assigned", membership_id=bob.id, conversation_id=conversation.id)

    assert await service.unread_count(ann.id) == 2
    assert [n.id for n in await service.list(ann.id)] == [second.id, first.id]  # newest first
    await service.mark_read(ann.id, [first.id])
    assert await service.unread_count(ann.id) == 1
    assert [n.id for n in await service.list(ann.id, unread_only=True)] == [second.id]
    assert len(await service.list(ann.id)) == 2 and (await service.list(ann.id))[1].read_at is not None


async def test_mark_read_only_own(session, org_a):
    b = Builder(session, org_a)
    ann, bob = await b.member("Ann"), await b.member("Bob")
    conversation = await b.conversation(status="open")
    service = NotificationService(session, org_a.id)
    mine = await service.create(kind="assigned", membership_id=ann.id, conversation_id=conversation.id)
    theirs = await service.create(kind="assigned", membership_id=bob.id, conversation_id=conversation.id)

    assert await service.mark_read(ann.id, [theirs.id]) == 0  # another member's id is ignored
    assert await service.unread_count(bob.id) == 1
    assert await service.mark_read(ann.id, None) == 1  # "all" means all of Ann's
    assert await service.unread_count(ann.id) == 0 and await service.unread_count(bob.id) == 1
    assert mine.id != theirs.id


async def test_other_org_not_visible(session, org_a, org_b):
    a, other = Builder(session, org_a), Builder(session, org_b)
    ann = await a.member("Ann")
    foreign = await other.member("Zed")
    conversation = await a.conversation(status="open")
    await NotificationService(session, org_a.id).create(kind="assigned", membership_id=ann.id, conversation_id=conversation.id)
    # Even with the right membership id, org B's service sees and changes nothing of org A's.
    service_b = NotificationService(session, org_b.id)
    assert await service_b.unread_count(ann.id) == 0 and await service_b.list(ann.id) == []
    assert await service_b.mark_read(ann.id, None) == 0
    assert await NotificationService(session, org_a.id).unread_count(ann.id) == 1
    assert foreign.id != ann.id
