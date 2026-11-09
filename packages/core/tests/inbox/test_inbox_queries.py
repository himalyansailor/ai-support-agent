"""Inbox lists, counts, detail and the summary shape (Task 5)."""

import pytest
from sqlalchemy import update

from supportcore.db.models import Conversation, ConversationTag, Tag
from supportcore.errors import NotFoundError
from supportcore.inbox.service import InvalidInboxFilterError

from .conftest import BASE_TIME


async def _ids(page):
    return [s.id for s in page.items]


async def test_views_mine_unassigned_bot(service, build):
    ann = await build.member("Ann")
    bob = await build.member("Bob")
    mine_open = await build.conversation(status="open", assignee_membership_id=ann.id)
    mine_pending = await build.conversation(status="pending", assignee_membership_id=ann.id)
    await build.conversation(status="resolved", assignee_membership_id=ann.id)
    await build.conversation(status="open", assignee_membership_id=bob.id)
    free = await build.conversation(status="open")
    bot = await build.conversation(status="bot")
    await build.conversation(status="closed")

    mine = await service.list("mine", viewer_membership_id=ann.id)
    assert await _ids(mine) == [mine_pending.id, mine_open.id]  # newest first
    assert await _ids(await service.list("unassigned", viewer_membership_id=ann.id)) == [free.id]
    assert await _ids(await service.list("bot", viewer_membership_id=ann.id)) == [bot.id]
    assert len((await service.list("all")).items) == 7
    assert (await service.list("mine", viewer_membership_id=None)).items == []
    assert (await service.list("mentions", viewer_membership_id=ann.id)).items == []  # until Task 6
    # An explicit status replaces the view's own.
    resolved = await service.list("mine", status="resolved", viewer_membership_id=ann.id)
    assert len(resolved.items) == 1 and resolved.items[0].status == "resolved"
    with pytest.raises(InvalidInboxFilterError):
        await service.list("nope")
    with pytest.raises(InvalidInboxFilterError):
        await service.list("all", status="waiting")


async def test_assignee_filter_and_paging(service, build):
    ann = await build.member("Ann")
    made = [await build.conversation(status="open", assignee_membership_id=ann.id) for _ in range(3)]
    await build.conversation(status="open")
    assert len((await service.list("all", assignee="none")).items) == 1
    assert len((await service.list("all", assignee="me", viewer_membership_id=ann.id)).items) == 3
    assert len((await service.list("all", assignee=str(ann.id))).items) == 3
    with pytest.raises(InvalidInboxFilterError):
        await service.list("all", assignee="someone")
    first = await service.list("all", assignee="me", viewer_membership_id=ann.id, limit=2)
    assert await _ids(first) == [made[2].id, made[1].id] and first.next_cursor is not None
    rest = await service.list("all", assignee="me", viewer_membership_id=ann.id, limit=2, cursor=first.next_cursor)
    assert await _ids(rest) == [made[0].id] and rest.next_cursor is None


async def test_filters_priority_tag_kb_channel(service, build, session, org_a):
    widget, kb = await build.widget_with_kb()
    urgent = await build.conversation(status="open", priority="urgent", widget_id=widget.id)
    await build.conversation(status="open", priority="normal")
    email = await build.conversation(status="open", channel="email")
    tag = Tag(organisation_id=org_a.id, name="Billing")
    session.add(tag)
    await session.flush()
    session.add(ConversationTag(organisation_id=org_a.id, conversation_id=email.id, tag_id=tag.id))
    test = await build.conversation(status="open", priority="urgent", is_test=True)
    await session.flush()

    assert await _ids(await service.list("all", priority="urgent")) == [urgent.id]
    assert await _ids(await service.list("all", priority="urgent", include_test=True)) == [test.id, urgent.id]
    assert await _ids(await service.list("all", kb_id=kb.id)) == [urgent.id]
    assert await _ids(await service.list("all", channel="email")) == [email.id]
    tagged = await service.list("all", tag_id=tag.id)
    assert await _ids(tagged) == [email.id] and tagged.items[0].tags == [{"id": tag.id, "name": "Billing"}]
    assert (await service.list("all", kb_id=tag.id)).items == []
    with pytest.raises(InvalidInboxFilterError):
        await service.list("all", priority="critical")


async def test_counts_match_lists(service, build):
    ann = await build.member("Ann")
    for status, assignee in [("open", ann), ("pending", ann), ("resolved", ann), ("open", None), ("bot", None)]:
        await build.conversation(status=status, assignee_membership_id=assignee.id if assignee else None)
    await build.conversation(status="open", assignee_membership_id=ann.id, is_test=True)
    counts = await service.counts(ann.id)
    assert counts == {"mine": 2, "unassigned": 1, "bot": 1, "callbacks": 0}
    for view in ("mine", "unassigned", "bot"):
        listed = await service.list(view, viewer_membership_id=ann.id, limit=100)
        assert len(listed.items) == counts[view]
    assert (await service.counts(None))["mine"] == 0


async def test_summary_shape_and_version(service, build, session):
    ann = await build.member("Ann")
    customer = await build.customer(name="Cara", email="cara@x.com", external_id="u-1")
    long_text = "x" * 300
    c = await build.conversation(
        customer,
        [("customer", "hello"), ("bot", long_text)],
        status="open",
        priority="high",
        assignee_membership_id=ann.id,
        sla_breached=["first_response"],
        escalation={"action": "escalate"},
        first_response_at=BASE_TIME,
    )
    s = await service.summary(c.id)
    assert s.assignee == {"membership_id": ann.id, "name": "Ann"}
    assert s.customer == {"id": customer.id, "name": "Cara", "email": "cara@x.com", "phone": None, "identified": True}
    assert s.last_message_role == "bot" and s.last_message_preview == "x" * 120
    assert s.sla["breached"] == ["first_response"] and s.priority == "high" and s.channel == "chat"
    assert s.escalation == {"action": "escalate"} and not s.is_test and not s.unread_for_assignee
    assert s.version == c.version == 1
    payload = s.to_payload()
    assert payload["id"] == str(c.id) and payload["assignee"]["membership_id"] == str(ann.id)
    assert payload["last_message_at"] == c.last_message_at.isoformat() and payload["version"] == s.version
    # the client shows a first-response countdown only while it is unmet
    assert s.sla["first_response_at"] == BASE_TIME
    assert payload["sla"]["first_response_at"] == BASE_TIME.isoformat()
    with pytest.raises(NotFoundError):
        await service.summary(ann.id)


async def test_unread_for_assignee_flag(service, build):
    ann = await build.member("Ann")
    waiting = await build.conversation(
        None, [("customer", "hi"), ("agent", "hello"), ("customer", "any news?")], status="open", assignee_membership_id=ann.id
    )
    answered = await build.conversation(
        None, [("customer", "hi"), ("agent", "on it")], status="pending", assignee_membership_id=ann.id
    )
    empty = await build.conversation(None, [], status="open")
    assert (await service.summary(waiting.id)).unread_for_assignee is True
    assert (await service.summary(answered.id)).unread_for_assignee is False
    assert (await service.summary(empty.id)).unread_for_assignee is False


async def test_detail_includes_history_linked_by_email(service, build, make_service, org_b, session):
    cara = await build.customer(name="Cara", email="Cara@X.com")
    same_row = await build.conversation(cara, [("customer", "first")], status="resolved")
    other_customer = await build.customer(email="cara@x.com")  # same email, different case, other row
    by_email = await build.conversation(other_customer, [("customer", "second")], status="resolved")
    contact = await build.conversation(await build.customer(), [("customer", "third")], contact_email="CARA@x.com")
    stranger = await build.conversation(await build.customer(email="dan@x.com"), [("customer", "nope")])
    current = await build.conversation(cara, [("customer", "now"), ("bot", "reply")], status="open")
    foreign_build = type(build)(session, org_b)
    await foreign_build.conversation(await foreign_build.customer(email="cara@x.com"), [("customer", "other org")])

    detail = await service.detail(current.id)
    assert detail.summary.id == current.id
    assert [m.text for m in detail.messages] == ["now", "reply"] and detail.has_more is False
    assert detail.notes == [] and detail.turns == []
    assert {h.id for h in detail.history} == {same_row.id, by_email.id, contact.id}
    assert stranger.id not in {h.id for h in detail.history}
    assert [h.last_message_at for h in detail.history] == sorted((h.last_message_at for h in detail.history), reverse=True)
    with pytest.raises(NotFoundError):  # another organisation's conversation looks missing
        await make_service(org_b).detail(current.id)


async def test_linked_conversations_capped_at_20(build, session, org_a):
    from supportcore.inbox.customers import linked_conversations

    cara = await build.customer(email="cara@x.com")
    for _ in range(25):
        await build.conversation(cara)
    assert len(await linked_conversations(session, org_a.id, cara)) == 20


async def test_messages_window_and_has_more(service, build):
    texts = [("customer", f"m{i}") for i in range(205)]
    c = await build.conversation(None, texts, status="open")
    detail = await service.detail(c.id)
    assert len(detail.messages) == 200 and detail.has_more is True
    assert detail.messages[0].text == "m5" and detail.messages[-1].text == "m204"  # oldest first
    oldest = detail.messages[0].created_at
    older = await service.messages_before(c.id, oldest, limit=3)
    assert [m.text for m in older.items] == ["m2", "m3", "m4"] and older.has_more is True
    last = await service.messages_before(c.id, older.items[0].created_at)
    assert [m.text for m in last.items] == ["m0", "m1"] and last.has_more is False
    with pytest.raises(NotFoundError):
        await service.messages_before(c.customer_id, BASE_TIME)


async def test_unread_ignores_system_messages_and_needs_assignee(service, build):
    ann = await build.member("Ann")
    c = await build.conversation(
        None,
        [("customer", "need help"), ("system", "Ann has joined")],
        status="open",
        assignee_membership_id=ann.id,
    )
    s = await service.summary(c.id)
    assert s.unread_for_assignee is True
    assert s.last_message_role == "customer" and s.last_message_preview == "need help"
    unassigned = await build.conversation(None, [("customer", "hello"), ("system", "note")], status="open")
    assert (await service.summary(unassigned.id)).unread_for_assignee is False
    only_system = await build.conversation(None, [("system", "joined")], status="open", assignee_membership_id=ann.id)
    s = await service.summary(only_system.id)
    assert s.last_message_preview is None and s.last_message_role is None and s.unread_for_assignee is False


async def test_tags_never_leak_across_organisations(service, build, session, org_a, org_b):
    mine = Tag(organisation_id=org_a.id, name="Mine")
    foreign = Tag(organisation_id=org_b.id, name="Foreign")
    session.add_all([mine, foreign])
    await session.flush()
    c = await build.conversation(status="open")
    session.add_all(
        [
            ConversationTag(organisation_id=org_a.id, conversation_id=c.id, tag_id=mine.id),
            ConversationTag(organisation_id=org_a.id, conversation_id=c.id, tag_id=foreign.id),  # corrupt row
        ]
    )
    await session.flush()
    assert (await service.summary(c.id)).tags == [{"id": mine.id, "name": "Mine"}]


async def test_version_increases_on_every_committed_change(session_factory, org_a_committed):
    """Two separate committed transactions, one ORM change and one Core UPDATE (as the turn lease does)."""
    from datetime import timedelta

    org_id, cid = org_a_committed
    versions = []
    async with session_factory() as s:
        versions.append((await s.get(Conversation, cid)).version)
    async with session_factory() as s:
        (await s.get(Conversation, cid)).priority = "high"
        await s.commit()
    async with session_factory() as s:
        versions.append((await s.get(Conversation, cid)).version)
    async with session_factory() as s:
        await s.execute(
            update(Conversation).where(Conversation.id == cid).values(turn_lease_until=BASE_TIME + timedelta(days=1))
        )
        await s.commit()
    async with session_factory() as s:
        versions.append((await s.get(Conversation, cid)).version)
    assert versions[0] == 1 and versions[0] < versions[1] < versions[2]
