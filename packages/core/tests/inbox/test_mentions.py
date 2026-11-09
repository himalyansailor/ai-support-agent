"""Mentions in notes (Task 6): the `@[name](membership uuid)` token, org scoping, the cap and the mentions view.
Runs in the rollback-isolated `session` (nothing is published from notes)."""

import uuid

from sqlalchemy import select

from supportcore.db.models import Mention
from supportcore.inbox.mentions import MAX_MENTIONS, parse_mentions
from supportcore.inbox.notes import NoteService

from .conftest import Builder


def _token(membership_id, name="Ann") -> str:
    return f"@[{name}]({membership_id})"


def test_mentions_parsed_and_capped():
    members = [uuid.uuid4() for _ in range(25)]
    unknown = uuid.uuid4()
    text = " ".join(
        [_token(unknown), _token(members[0]), _token(members[0], "Ann again"), "@[broken](not-a-uuid)", "@Ann"]
        + [_token(m, f"Agent {i}") for i, m in enumerate(members[1:])]
    )

    found = parse_mentions(text, set(members))

    assert MAX_MENTIONS == 20
    assert found == members[:20]  # order of appearance, deduplicated, unknown ids skipped, capped at 20
    assert parse_mentions("no mentions here", set(members)) == []
    assert parse_mentions(_token(str(members[3]).upper()), set(members)) == [members[3]]
    assert parse_mentions("@[](%s)" % members[4], set(members)) == []  # a token needs a display name


async def test_cross_org_mention_ignored(session, org_a, org_b):
    a, b = Builder(session, org_a), Builder(session, org_b)
    author = await a.member("Author")
    teammate = await a.member("Teammate")
    outsider = await b.member("Outsider")
    conversation = await a.conversation(status="open")
    text = f"{_token(teammate.id, 'Teammate')} and {_token(outsider.id, 'Outsider')} and {_token(teammate.id)}"

    note = await NoteService(session, org_a.id).add(conversation.id, author, text)

    # Unknown (other-org) ids stay as plain text; our member's tokens carry their real name.
    assert note.text == text.replace(_token(teammate.id), _token(teammate.id, "Teammate"))
    assert _token(outsider.id, "Outsider") in note.text
    rows = (await session.execute(select(Mention))).scalars().all()
    assert [(m.membership_id, m.note_id, m.conversation_id) for m in rows] == [(teammate.id, note.id, conversation.id)]
    assert all(m.organisation_id == org_a.id for m in rows)


async def test_mentions_view_lists_mentioned_conversations(session, org_a, service):
    a = Builder(session, org_a)
    author, ann, bob = await a.member("Author"), await a.member("Ann"), await a.member("Bob")
    mentioned = await a.conversation(status="open")
    resolved = await a.conversation(status="resolved")
    await a.conversation(status="open")  # nobody mentioned
    notes = NoteService(session, org_a.id)
    await notes.add(mentioned.id, author, f"{_token(ann.id)} can you look?")
    await notes.add(resolved.id, author, f"fyi {_token(ann.id)}")

    page = await service.list("mentions", viewer_membership_id=ann.id)
    assert [s.id for s in page.items] == [resolved.id, mentioned.id]  # any status, newest activity first
    assert (await service.list("mentions", viewer_membership_id=bob.id)).items == []
    assert (await service.list("mentions", viewer_membership_id=None)).items == []
    filtered = await service.list("mentions", status="open", viewer_membership_id=ann.id)
    assert [s.id for s in filtered.items] == [mentioned.id]


async def test_detail_lists_notes_with_mentions(session, org_a, service):
    a = Builder(session, org_a)
    author, ann = await a.member("Author"), await a.member("Ann")
    conversation = await a.conversation(status="open", texts=[("customer", "help")])
    notes = NoteService(session, org_a.id)
    first = await notes.add(conversation.id, author, "first")
    second = await notes.add(conversation.id, author, f"ping {_token(ann.id)}")

    detail = await service.detail(conversation.id)

    assert [n.id for n in detail.notes] == [first.id, second.id]
    assert detail.notes[1].author_name == "Author" and detail.notes[1].author_membership_id == author.id
    assert [(m.membership_id, m.name) for m in detail.notes[1].mentions] == [(ann.id, "Ann")]
    assert detail.notes[0].mentions == []
    assert [m.role for m in detail.messages] == ["customer"]  # notes are not messages


async def test_mention_display_name_comes_from_the_database(session, org_a):
    a = Builder(session, org_a)
    author, intern = await a.member("Author"), await a.member("Ian Intern")
    conversation = await a.conversation(status="open")
    unknown = uuid.uuid4()

    note = await NoteService(session, org_a.id).add(
        conversation.id, author, f"{_token(intern.id, 'CEO')} and {_token(unknown, 'CEO')}"
    )

    expected = f"@[Ian Intern]({intern.id}) and {_token(unknown, 'CEO')}"  # unknown ids stay as typed
    assert note.text == expected
    assert [(m.membership_id, m.name) for m in note.mentions] == [(intern.id, "Ian Intern")]
    listed = await NoteService(session, org_a.id).list(conversation.id)
    assert listed[0].text == expected and [m.name for m in listed[0].mentions] == ["Ian Intern"]
    assert note.author_name == "Author"


async def test_mentions_count_only_active_members(session, org_a):
    a = Builder(session, org_a)
    author, active, suspended = await a.member("Author"), await a.member("Active"), await a.member("Gone")
    suspended.status = "suspended"
    await session.flush()
    conversation = await a.conversation(status="open")
    text = f"{_token(active.id, 'Active')} {_token(suspended.id, 'Gone')}"

    note = await NoteService(session, org_a.id).add(conversation.id, author, text)

    assert note.text == text
    rows = (await session.execute(select(Mention.membership_id))).scalars().all()
    assert rows == [active.id]


async def test_notes_beyond_the_listing_cap(session, org_a, service, monkeypatch):
    import supportcore.inbox.notes as notes_module

    monkeypatch.setattr(notes_module, "MAX_LISTED_NOTES", 3)
    a = Builder(session, org_a)
    author = await a.member("Author")
    conversation = await a.conversation(status="open")
    notes = NoteService(session, org_a.id)
    added = [await notes.add(conversation.id, author, f"note {i}") for i in range(5)]  # past the cap: fine

    detail = await service.detail(conversation.id)
    assert [n.text for n in detail.notes] == ["note 2", "note 3", "note 4"]  # the newest, oldest first
    assert added[-1].text == "note 4"


async def test_mentions_count_only_members_who_can_view_the_inbox(session, org_a):
    """Final review minor 5: a member whose role cannot open the inbox gets no Mention and no notification
    (it would carry the customer's name); their token stays plain text."""
    from supportcore.db.models import Notification

    a = Builder(session, org_a)
    author = await a.member("Author")
    viewers = [
        await a.member("Answers", permissions=("queries.answer",)),
        await a.member("Analyst", permissions=("analytics.view",)),
        await a.member("Builder", permissions=("bot.configure",)),
    ]
    outsider = await a.member("Billing", permissions=("billing.manage",))
    conversation = await a.conversation(status="open")
    text = " ".join(_token(m.id, "x") for m in [*viewers, outsider])

    note = await NoteService(session, org_a.id).add(conversation.id, author, text)

    assert [m.membership_id for m in note.mentions] == [m.id for m in viewers]
    assert _token(outsider.id, "x") in note.text  # left as written
    rows = (await session.execute(select(Mention.membership_id))).scalars().all()
    assert sorted(rows) == sorted(m.id for m in viewers)
    notified = (await session.execute(select(Notification.membership_id))).scalars().all()
    assert outsider.id not in notified and sorted(notified) == sorted(m.id for m in viewers)
