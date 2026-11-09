"""Agent actions (Task 6): replies, the status transitions, hand back, notes, tags.

Real Postgres with committed data (the session_factory fixture truncates at teardown), the memory checkpointer,
the scripted chat model, memory presence and a recording memory pub/sub. The tag test runs in the
rollback-isolated `session`.
"""

import asyncio
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update

from supportcore.bot.graph import is_waiting_for_human
from supportcore.bot.runtime import BotRuntime
from supportcore.bot.turns import TurnRunner
from supportcore.conversations.service import ConversationService, InvalidConversationStateError
from supportcore.db.models import BotTurn, Conversation, Membership, Message, Note
from supportcore.db.models.conversation import CONVERSATION_STATUSES
from supportcore.db.uow import UnitOfWork
from supportcore.events import EventBus
from supportcore.inbox.notes import NoteService
from supportcore.inbox.publisher import InboxPublisher
from supportcore.inbox.service import InboxService
from supportcore.inbox.tags import InvalidTagError, TagNameTakenError, TagService, TooManyTagsError
from supportcore.presence.memory_presence import MemoryAgentPresence
from supportcore.pubsub.memory_pubsub import MemoryPubSub
from supportcore.realtime.spawn import drain_pending
from supportcore.testing import script_chat
from supportcore.widget.strings import strings_for

from bot.conftest import MemoryVectorStore, RecordingJobQueue, StubToolExecutor, build_world

NO_TOPICS = json.dumps({"topic_ids": [], "asks_for_human": False, "frustrated": False, "reason": "none"})

# The Global Constraints transition table, written out here (not imported) so the test checks the code.
ALLOWED = {
    ("bot", "open"),  # escalation, claim or "talk to a person"
    ("open", "pending"),  # agent reply
    ("pending", "open"),  # customer message
    ("open", "resolved"),
    ("pending", "resolved"),
    ("open", "bot"),  # hand back
    ("pending", "bot"),
    ("resolved", "open"),  # reopen
    ("bot", "closed"),
    ("open", "closed"),
    ("pending", "closed"),
    ("resolved", "closed"),
}


class RecordingPubSub(MemoryPubSub):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[tuple[str, dict]] = []

    async def publish(self, channel: str, message: dict) -> None:
        self.published.append((channel, message))
        await super().publish(channel, message)

    def on(self, channel: str) -> list[dict]:
        return [m for c, m in self.published if c == channel]


@pytest.fixture(autouse=True)
def _reset_script():
    script_chat([])
    yield
    script_chat([])


@pytest.fixture
def settings(tmp_path):
    from cryptography.fernet import Fernet

    from supportcore.config import Settings
    from supportcore.testing import TEST_DATABASE_URL

    return Settings(
        _env_file=None,
        env="test",
        database_url=TEST_DATABASE_URL,
        redis_url="redis://localhost:6379/0",
        jwt_secret="test-secret-test-secret-test-secret-123",
        smtp_from="noreply@test.local",
        secrets_key=Fernet.generate_key().decode(),
        file_storage="local",
        local_storage_path=str(tmp_path / "uploads"),
        vector_store="chroma",
        rate_limiter="memory",
        pubsub="memory",
        presence="memory",
        checkpointer="memory",
    )


@pytest.fixture
def store():
    return MemoryVectorStore()


@pytest.fixture
async def adapters(settings, store):
    from supportcore.wiring import build_adapters

    built = replace(
        build_adapters(settings),
        pubsub=RecordingPubSub(),
        presence=MemoryAgentPresence(),
        vector_store=store,
        tool_executor=StubToolExecutor(),
        job_queue=RecordingJobQueue(),
    )
    yield built
    await drain_pending()
    await built.aclose()


@pytest.fixture
def pubsub(adapters) -> RecordingPubSub:
    return adapters.pubsub


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def runtime(settings, session_factory, adapters, bus):
    return BotRuntime(settings=settings, session_factory=session_factory, adapters=adapters, events=bus)


@pytest.fixture
async def runner(runtime):
    turns = TurnRunner(runtime)
    yield turns
    await turns.aclose()


@pytest.fixture
async def world(session_factory, adapters, settings, bus, store):
    return await build_world(session_factory, adapters, settings, bus, store)


@pytest.fixture
async def owner_mid(session_factory, world):
    async with session_factory() as s:
        return (await s.execute(select(Membership.id).where(Membership.organisation_id == world.org_id))).scalar_one()


@pytest.fixture
def act(session_factory, world, runtime, runner, bus, owner_mid):
    """act(fn): fn(service, locked conversation, owner membership) in one committed unit of work."""

    async def run(fn, conversation_id=None):
        async with UnitOfWork(session_factory) as uow:
            service = ConversationService(
                uow.session, world.org_id, bus, publisher=runtime.publisher(), bot_hand_back=runner.hand_back
            )
            conversation = await service.locked(conversation_id or world.conversation_id)
            member = await uow.session.get(Membership, owner_mid)
            result = await fn(service, conversation, member)
            await uow.commit()
        await drain_pending()
        return result

    return run


@pytest.fixture
def add_note(session_factory, world, owner_mid):
    async def run(text, conversation_id=None):
        async with UnitOfWork(session_factory) as uow:
            member = await uow.session.get(Membership, owner_mid)
            note = await NoteService(uow.session, world.org_id).add(conversation_id or world.conversation_id, member, text)
            await uow.commit()
        await drain_pending()
        return note

    return run


async def _set(session_factory, conversation_id, **values):
    async with session_factory() as s:
        await s.execute(update(Conversation).where(Conversation.id == conversation_id).values(**values))
        await s.commit()


async def _conversation(session_factory, conversation_id) -> Conversation:
    async with session_factory() as s:
        return await s.get(Conversation, conversation_id)


async def _messages(session_factory, conversation_id) -> list[Message]:
    async with session_factory() as s:
        stmt = select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at, Message.id)
        return list((await s.execute(stmt)).scalars())


async def _turns(session_factory, conversation_id) -> list[BotTurn]:
    async with session_factory() as s:
        return list((await s.execute(select(BotTurn).where(BotTurn.conversation_id == conversation_id))).scalars())


def _upserts(pubsub, org_id):
    return [m for m in pubsub.on(f"inbox:{org_id.hex}") if m["type"] == "conversation.upsert"]


# --- replies ---------------------------------------------------------------------------------------------


async def test_agent_reply_sets_first_response_and_pending(session_factory, world, act, pubsub, owner_mid):
    await _set(session_factory, world.conversation_id, status="open")

    first = await act(lambda s, c, m: s.add_agent_message(c, m, "  Hello, I can help  "))

    assert first.role == "agent" and first.text == "Hello, I can help" and first.author_membership_id == owner_mid
    c = await _conversation(session_factory, world.conversation_id)
    assert c.status == "pending"
    assert c.first_response_at == first.created_at and c.last_message_at == first.created_at
    widget = pubsub.on(f"conv:{world.conversation_id.hex}")
    assert widget == [{"type": "message", "message": widget[0]["message"]}]
    assert widget[0]["message"]["id"] == str(first.id) and widget[0]["message"]["role"] == "agent"
    upserts = _upserts(pubsub, world.org_id)
    assert len(upserts) == 1 and upserts[0]["conversation"]["status"] == "pending"

    second = await act(lambda s, c, m: s.add_agent_message(c, m, "One more thing"))
    c = await _conversation(session_factory, world.conversation_id)
    assert c.status == "pending" and c.first_response_at == first.created_at  # only the first reply counts
    assert c.last_message_at == second.created_at


async def test_agent_reply_length_limits(session_factory, world, act):
    from supportcore.conversations.service import InvalidMessageError

    await _set(session_factory, world.conversation_id, status="open")
    for text in ("   ", "x" * 10001):
        with pytest.raises(InvalidMessageError):
            await act(lambda s, c, m, text=text: s.add_agent_message(c, m, text))
    assert (await act(lambda s, c, m: s.add_agent_message(c, m, "x" * 10000))).text == "x" * 10000


async def test_customer_message_reopens_pending(session_factory, world, runner, pubsub):
    await _set(session_factory, world.conversation_id, status="pending")

    posted = await runner.post_customer_message(world.org_id, world.conversation_id, "still broken", None)
    await drain_pending()

    assert posted.status == "open" and posted.created
    assert runner._tasks == set()  # no bot turn
    await runner.drain()
    assert (await _conversation(session_factory, world.conversation_id)).status == "open"
    assert await _turns(session_factory, world.conversation_id) == []
    upserts = _upserts(pubsub, world.org_id)
    assert upserts and upserts[-1]["conversation"]["status"] == "open"


async def test_agent_reply_does_not_start_bot_turn(session_factory, world, act, runner):
    await _set(session_factory, world.conversation_id, status="open")
    await act(lambda s, c, m: s.add_agent_message(c, m, "Hi, this is Ann"))
    assert runner._tasks == set()
    await runner.drain()
    assert await _turns(session_factory, world.conversation_id) == []
    assert [m.role for m in await _messages(session_factory, world.conversation_id)] == ["agent"]

    # Only `open` and `pending` take agent replies.
    for status in ("bot", "resolved", "closed"):
        await _set(session_factory, world.conversation_id, status=status)
        with pytest.raises(InvalidConversationStateError):
            await act(lambda s, c, m: s.add_agent_message(c, m, "hello"))
    assert runner._tasks == set()


async def test_reply_text_not_logged(session_factory, world, act, add_note, capsys):
    from structlog.testing import capture_logs

    await _set(session_factory, world.conversation_id, status="open")
    with capture_logs() as logs:
        await act(lambda s, c, m: s.add_agent_message(c, m, "SECRET-REPLY-4242"))
        await add_note("SECRET-NOTE-4343")
        await act(lambda s, c, m: s.transition(c, "resolved", m))
    captured = capsys.readouterr()
    dumped = json.dumps(logs, default=str) + captured.out + captured.err
    assert "SECRET-REPLY-4242" not in dumped and "SECRET-NOTE-4343" not in dumped
    assert any(entry["event"] == "agent_replied" for entry in logs)


# --- transitions -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("to", CONVERSATION_STATUSES)
@pytest.mark.parametrize("from_", CONVERSATION_STATUSES)
async def test_transitions_table(from_, to, session_factory, world, act, runner, runtime, owner_mid):
    """Every allowed pair succeeds through the event the table names; every other pair is 409."""
    await _set(session_factory, world.conversation_id, status=from_)
    if (from_, to) not in ALLOWED:
        with pytest.raises(InvalidConversationStateError):
            await act(lambda s, c, m: s.transition(c, to, m))
        assert (await _conversation(session_factory, world.conversation_id)).status == from_
        return
    if (from_, to) == ("bot", "open"):  # claim
        async with UnitOfWork(session_factory) as uow:
            member = await uow.session.get(Membership, owner_mid)
            await runtime.assignment(uow.session, world.org_id).claim(world.conversation_id, member)
            await uow.commit()
        await drain_pending()
    elif (from_, to) == ("open", "pending"):  # agent reply
        await act(lambda s, c, m: s.add_agent_message(c, m, "On it"))
    elif (from_, to) == ("pending", "open"):  # customer message
        await runner.post_customer_message(world.org_id, world.conversation_id, "hello?", None)
        await drain_pending()
    else:
        await act(lambda s, c, m: s.transition(c, to, m))
    assert (await _conversation(session_factory, world.conversation_id)).status == to


@pytest.mark.parametrize(("from_", "to"), [("bot", "open"), ("open", "pending"), ("pending", "open")])
async def test_transition_takes_only_agent_events(from_, to, session_factory, world, act):
    """Entering the team, a reply and a customer message have their own operations; `open` is reopen only."""
    await _set(session_factory, world.conversation_id, status=from_)
    with pytest.raises(InvalidConversationStateError):
        await act(lambda s, c, m: s.transition(c, to, m))


async def test_resolve_publishes_status_to_widget(session_factory, world, act, pubsub):
    await _set(session_factory, world.conversation_id, status="pending")
    before = datetime.now(UTC)
    await act(lambda s, c, m: s.transition(c, "resolved", m))

    c = await _conversation(session_factory, world.conversation_id)
    assert c.status == "resolved" and c.resolved_at is not None and c.resolved_at >= before
    assert pubsub.on(f"conv:{world.conversation_id.hex}") == [{"type": "status", "status": "resolved"}]
    assert _upserts(pubsub, world.org_id)[-1]["conversation"]["status"] == "resolved"


async def test_reopen_and_close(session_factory, world, act, pubsub):
    await _set(session_factory, world.conversation_id, status="resolved", resolved_at=datetime.now(UTC))
    await act(lambda s, c, m: s.transition(c, "open", m))
    c = await _conversation(session_factory, world.conversation_id)
    assert c.status == "open" and c.resolved_at is None

    await act(lambda s, c, m: s.transition(c, "closed", m))
    assert (await _conversation(session_factory, world.conversation_id)).status == "closed"
    assert pubsub.on(f"conv:{world.conversation_id.hex}") == [
        {"type": "status", "status": "open"},
        {"type": "status", "status": "closed"},
    ]
    for to in CONVERSATION_STATUSES:  # closed is final
        with pytest.raises(InvalidConversationStateError):
            await act(lambda s, c, m, to=to: s.transition(c, to, m))


async def test_set_priority(session_factory, world, act, pubsub):
    from supportcore.conversations.service import InvalidPriorityError

    await act(lambda s, c, m: s.set_priority(c, "urgent"))
    assert (await _conversation(session_factory, world.conversation_id)).priority == "urgent"
    assert _upserts(pubsub, world.org_id)[-1]["conversation"]["priority"] == "urgent"
    with pytest.raises(InvalidPriorityError):
        await act(lambda s, c, m: s.set_priority(c, "critical"))


async def _customer_service_call(session_factory, world, runtime, bus, fn):
    """fn(service, conversation) as the widget routes do it: the row locked, one committed unit of work."""
    async with UnitOfWork(session_factory) as uow:
        service = ConversationService(uow.session, world.org_id, bus, publisher=runtime.publisher())
        conversation = await service.locked(world.conversation_id)
        await fn(service, conversation)
        await drain_pending()
        assert _upserts(runtime.adapters.pubsub, world.org_id) == []  # nothing before the commit
        await uow.commit()
    await drain_pending()


async def test_customer_end_while_with_team_is_live_in_inbox(session_factory, world, act, runtime, bus, pubsub, owner_mid):
    """Final review I3: the customer ending an `open` chat reaches the inbox and the watching pane."""
    await _set(session_factory, world.conversation_id, status="open", assignee_membership_id=owner_mid)

    await _customer_service_call(session_factory, world, runtime, bus, lambda s, c: s.end(c))

    c = await _conversation(session_factory, world.conversation_id)
    assert c.status == "resolved" and c.resolved_at is not None
    upserts = _upserts(pubsub, world.org_id)
    assert len(upserts) == 1 and upserts[0]["conversation"]["status"] == "resolved"
    assert pubsub.on(f"conv:{world.conversation_id.hex}") == [{"type": "status", "status": "resolved"}]
    with pytest.raises(InvalidConversationStateError):  # the assignee's next reply
        await act(lambda s, c, m: s.add_agent_message(c, m, "Are you still there?"))


async def test_end_of_ended_conversation_publishes_nothing(session_factory, world, runtime, bus, pubsub):
    await _set(session_factory, world.conversation_id, status="resolved", resolved_at=datetime.now(UTC))
    await _customer_service_call(session_factory, world, runtime, bus, lambda s, c: s.end(c))
    assert _upserts(pubsub, world.org_id) == [] and pubsub.on(f"conv:{world.conversation_id.hex}") == []


async def test_contact_email_publishes_upsert(session_factory, world, runtime, bus, pubsub):
    await _set(session_factory, world.conversation_id, status="open")
    await _customer_service_call(
        session_factory, world, runtime, bus, lambda s, c: s.set_contact_email(c, "later@example.com")
    )
    assert (await _conversation(session_factory, world.conversation_id)).contact_email == "later@example.com"
    upserts = _upserts(pubsub, world.org_id)
    assert len(upserts) == 1
    assert "later@example.com" not in json.dumps(pubsub.on(f"conv:{world.conversation_id.hex}"))


# --- bot-status conversations are live (final review minor 4) --------------------------------------------


async def test_bot_conversation_messages_publish_upserts(session_factory, world, runner, pubsub):
    """A customer message and the bot's reply each publish one upsert, after their own commit."""
    from supportcore.ai.chat.scripted_chat import CLASSIFIER_MARKER

    started, gate = asyncio.Event(), asyncio.Event()

    async def hook(messages):
        if messages and CLASSIFIER_MARKER in str(messages[0].content):
            return
        started.set()
        await gate.wait()

    script_chat(["Shipping takes three days [1]"], classifier=[NO_TOPICS], hook=hook)
    await runner.post_customer_message(world.org_id, world.conversation_id, "shipping takes how long?", None)
    await asyncio.wait_for(started.wait(), 10)
    await drain_pending()
    upserts = _upserts(pubsub, world.org_id)
    assert len(upserts) == 1
    assert upserts[0]["conversation"]["status"] == "bot"
    assert upserts[0]["conversation"]["last_message_role"] == "customer"

    gate.set()
    await asyncio.wait_for(runner.drain(), 20)
    await drain_pending()
    upserts = _upserts(pubsub, world.org_id)
    assert len(upserts) == 2 and upserts[1]["conversation"]["last_message_role"] == "bot"
    assert upserts[1]["conversation"]["version"] > upserts[0]["conversation"]["version"]


async def test_new_conversation_publishes_upsert(session_factory, world, runtime, bus, pubsub):
    async with UnitOfWork(session_factory) as uow:
        service = ConversationService(uow.session, world.org_id, bus, publisher=runtime.publisher())
        customer_id = (await uow.session.get(Conversation, world.conversation_id)).customer_id
        created = await service.create(customer_id, None, "en", False)
        await drain_pending()
        assert _upserts(pubsub, world.org_id) == []
        await uow.commit()
    await drain_pending()
    upserts = _upserts(pubsub, world.org_id)
    assert [u["conversation"]["id"] for u in upserts] == [str(created.id)]
    assert upserts[0]["conversation"]["status"] == "bot"


async def test_idle_close_publishes_upsert_and_status(session_factory, world, runtime, pubsub):
    from datetime import timedelta

    from supportcore.conversations.jobs import close_idle_conversations

    await _set(session_factory, world.conversation_id, last_message_at=datetime.now(UTC) - timedelta(hours=2))
    assert await close_idle_conversations(runtime) == 1
    await drain_pending()
    assert pubsub.on(f"conv:{world.conversation_id.hex}") == [{"type": "status", "status": "resolved"}]
    upserts = _upserts(pubsub, world.org_id)
    assert len(upserts) == 1 and upserts[0]["conversation"]["status"] == "resolved"


# --- hand back -------------------------------------------------------------------------------------------


async def test_hand_back_resumes_interrupted_graph_and_bot_answers_next(session_factory, world, act, runner, pubsub):
    script_chat([])
    await runner.post_customer_message(world.org_id, world.conversation_id, "I want a refund", None)
    await runner.drain()
    await drain_pending()
    graph = await runner.graph()
    assert await is_waiting_for_human(graph, world.conversation_id)
    assert (await _conversation(session_factory, world.conversation_id)).status == "open"
    widget_before = len(pubsub.on(f"conv:{world.conversation_id.hex}"))

    await act(lambda s, c, m: s.transition(c, "bot", m))

    assert not await is_waiting_for_human(graph, world.conversation_id)
    c = await _conversation(session_factory, world.conversation_id)
    assert c.status == "bot" and c.assignee_membership_id is None
    assert c.escalation is None and c.first_response_due_at is None and c.resolution_due_at is None
    messages = await _messages(session_factory, world.conversation_id)
    assert messages[-1].role == "system" and messages[-1].text == strings_for("en")["back_to_bot"]
    widget = pubsub.on(f"conv:{world.conversation_id.hex}")[widget_before:]
    assert [e["type"] for e in widget] == ["message", "agent", "status"]
    assert widget[0]["message"]["text"] == strings_for("en")["back_to_bot"]
    assert widget[1] == {"type": "agent", "agent": None}
    assert widget[2] == {"type": "status", "status": "bot"}
    assert _upserts(pubsub, world.org_id)[-1]["conversation"]["status"] == "bot"

    # The bot answers the next message (not the one before the hand back).
    script_chat(["Shipping takes three days [1]"], classifier=[NO_TOPICS])
    await runner.post_customer_message(world.org_id, world.conversation_id, "shipping takes how long?", None)
    await runner.drain()
    turns = await _turns(session_factory, world.conversation_id)
    assert sorted(t.outcome for t in turns) == ["answered", "escalated"]
    replies = [m for m in await _messages(session_factory, world.conversation_id) if m.role == "bot"]
    assert [m.text for m in replies] == ["Shipping takes three days [1]"]
    assert not await is_waiting_for_human(graph, world.conversation_id)


async def test_hand_back_without_interrupt(session_factory, world, act, runner, runtime, owner_mid, pubsub):
    """A bot conversation a person claimed has no interrupt to resume: hand back just returns it to the bot."""
    async with UnitOfWork(session_factory) as uow:
        member = await uow.session.get(Membership, owner_mid)
        await runtime.assignment(uow.session, world.org_id).claim(world.conversation_id, member)
        await uow.commit()
    await drain_pending()
    assert (await _conversation(session_factory, world.conversation_id)).assignee_membership_id == owner_mid
    assert not await is_waiting_for_human(await runner.graph(), world.conversation_id)

    c = await _conversation(session_factory, world.conversation_id)
    assert c.first_response_due_at is not None  # the claim started the SLA clock
    await act(lambda s, c, m: s.add_agent_message(c, m, "Hi there"))
    replied_at = (await _conversation(session_factory, world.conversation_id)).first_response_at

    await act(lambda s, c, m: s.transition(c, "bot", m))

    c = await _conversation(session_factory, world.conversation_id)
    assert c.status == "bot" and c.assignee_membership_id is None
    assert c.escalation is None and c.first_response_due_at is None and c.resolution_due_at is None
    assert c.first_response_at == replied_at is not None  # kept as history
    texts = [m.text for m in await _messages(session_factory, world.conversation_id) if m.role == "system"]
    assert texts[-1] == strings_for("en")["back_to_bot"]
    assert pubsub.on(f"conv:{world.conversation_id.hex}")[-1] == {"type": "status", "status": "bot"}


# --- notes -----------------------------------------------------------------------------------------------


async def test_note_not_visible_to_widget(session_factory, world, add_note, pubsub, adapters, owner_mid):
    await _set(session_factory, world.conversation_id, status="open")
    note = await add_note("  check the refund  ")

    assert note.text == "check the refund" and note.author_membership_id == owner_mid
    assert pubsub.on(f"conv:{world.conversation_id.hex}") == []  # never on the widget channel
    assert await _messages(session_factory, world.conversation_id) == []  # not a message
    async with session_factory() as s:
        assert (await s.execute(select(Note.id))).scalars().all() == [note.id]
        detail = await InboxService(s, world.org_id, adapters).detail(world.conversation_id)
    assert [(n.id, n.text, n.author_name) for n in detail.notes] == [(note.id, "check the refund", "Owner")]


async def test_note_length_limits(add_note):
    from supportcore.inbox.notes import InvalidNoteError

    for text in ("  ", "x" * 5001):
        with pytest.raises(InvalidNoteError):
            await add_note(text)
    assert (await add_note("x" * 5000)).text == "x" * 5000


# --- tags ------------------------------------------------------------------------------------------------


async def test_tags_unique_case_insensitive_and_max_10(session, org_a, org_b):
    from supportcore.db.models import Customer

    publisher = InboxPublisher(MemoryPubSub(), None)  # nothing commits here: nothing is published
    tags = TagService(session, org_a.id, publisher=publisher)
    billing = await tags.create("  Billing ")
    assert billing.name == "Billing"
    with pytest.raises(TagNameTakenError):
        await tags.create("billing")
    other = await tags.create("Shipping")
    with pytest.raises(TagNameTakenError):
        await tags.rename(other.id, "BILLING")
    await session.refresh(other)  # the failed rename's savepoint rolled back (and expired) it
    assert other.name == "Shipping"
    assert (await tags.rename(billing.id, "billing")).name == "billing"  # its own name, another case
    for bad in ("", "   ", "x" * 41):
        with pytest.raises(InvalidTagError):
            await tags.create(bad)
    # Another organisation may use the same name.
    assert (await TagService(session, org_b.id, publisher=publisher).create("Billing")).name == "Billing"
    foreign = await TagService(session, org_b.id, publisher=publisher).create("Foreign")

    customer = Customer(organisation_id=org_a.id, visitor_id=uuid.uuid4().hex)
    session.add(customer)
    await session.flush()
    conversation = Conversation(organisation_id=org_a.id, customer_id=customer.id, status="open")
    session.add(conversation)
    await session.flush()

    many = [billing, other] + [await tags.create(f"t{i}") for i in range(9)]
    with pytest.raises(TooManyTagsError):
        await tags.set_tags(conversation.id, [t.id for t in many])  # 11
    assert [t.id for t in await tags.set_tags(conversation.id, [t.id for t in many[:10]] + [many[0].id])] == [
        t.id for t in sorted(many[:10], key=lambda t: t.name.lower())
    ]  # duplicates collapse
    assert [t.name for t in await tags.set_tags(conversation.id, [other.id])] == ["Shipping"]  # replaces
    with pytest.raises(InvalidTagError):
        await tags.set_tags(conversation.id, [foreign.id])  # another org's tag is unknown here
    with pytest.raises(InvalidTagError):
        await tags.set_tags(conversation.id, [uuid.uuid4()])

    names = [t.name for t in await tags.list()]
    assert names == sorted(names, key=str.lower) and "Foreign" not in names
    await tags.delete(other.id)
    assert "Shipping" not in [t.name for t in await tags.list()]


async def test_set_tags_bumps_version_and_publishes_one_upsert(session_factory, world, pubsub):
    """Final review I1: a tag change must bump `conversations.version`, or clients drop the upsert as stale."""
    publisher = InboxPublisher(pubsub, session_factory)
    async with UnitOfWork(session_factory) as uow:
        tag = await TagService(uow.session, world.org_id, publisher=publisher).create("VIP")
        await uow.commit()
    before = (await _conversation(session_factory, world.conversation_id)).version

    async with UnitOfWork(session_factory) as uow:
        await TagService(uow.session, world.org_id, publisher=publisher).set_tags(world.conversation_id, [tag.id])
        await drain_pending()
        assert _upserts(pubsub, world.org_id) == []  # not before the commit
        await uow.commit()
    await drain_pending()

    after = (await _conversation(session_factory, world.conversation_id)).version
    assert after > before
    upserts = _upserts(pubsub, world.org_id)
    assert len(upserts) == 1 and upserts[0]["conversation"]["version"] == after

    async with UnitOfWork(session_factory) as uow:  # the same set again: no change, no bump, no upsert
        await TagService(uow.session, world.org_id, publisher=publisher).set_tags(world.conversation_id, [tag.id])
        await uow.commit()
    await drain_pending()
    assert (await _conversation(session_factory, world.conversation_id)).version == after
    assert len(_upserts(pubsub, world.org_id)) == 1


async def test_tags_org_limit_200(session, org_a, monkeypatch):
    import supportcore.inbox.tags as tags_module

    assert tags_module.MAX_TAGS_PER_ORG == 200 and tags_module.MAX_TAGS_PER_CONVERSATION == 10
    monkeypatch.setattr(tags_module, "MAX_TAGS_PER_ORG", 3)
    tags = TagService(session, org_a.id, publisher=InboxPublisher(MemoryPubSub(), None))
    for name in ("a", "b", "c"):
        await tags.create(name)
    with pytest.raises(TooManyTagsError):
        await tags.create("d")


# --- note.added (Task 12 fix: notes are live in the dashboard) -------------------------------------------------


async def test_note_added_published_after_commit_without_text(session_factory, world, owner_mid, pubsub):
    publisher = InboxPublisher(pubsub, session_factory)
    channel = f"inbox:{world.org_id.hex}"

    # rolled back: nothing goes out
    async with UnitOfWork(session_factory) as uow:
        member = await uow.session.get(Membership, owner_mid)
        await NoteService(uow.session, world.org_id, publisher=publisher).add(world.conversation_id, member, "draft-4444")
        await drain_pending()
        assert [m for m in pubsub.on(channel) if m["type"] == "note.added"] == []
        await uow.session.rollback()
    await drain_pending()
    assert [m for m in pubsub.on(channel) if m["type"] == "note.added"] == []

    async with UnitOfWork(session_factory) as uow:
        member = await uow.session.get(Membership, owner_mid)
        await NoteService(uow.session, world.org_id, publisher=publisher).add(
            world.conversation_id, member, "SECRET-NOTE-4545"
        )
        await uow.commit()
    await drain_pending()
    events = [m for m in pubsub.on(channel) if m["type"] == "note.added"]
    assert events == [{"type": "note.added", "conversation_id": str(world.conversation_id)}]
    assert "SECRET-NOTE-4545" not in json.dumps(pubsub.on(channel))
