"""Shared analytics fixtures: the bot-runtime world (an organisation with an owner, a widget and a customer, the
bot runtime and the conversation service) used by the event-log and pricing tests. Real Postgres with committed
data (the session_factory fixture truncates at teardown), memory presence and pub/sub."""

import uuid
from dataclasses import dataclass, replace

import pytest
from sqlalchemy import select

from supportcore.assignment.service import AssignmentService
from supportcore.bot.runtime import BotRuntime
from supportcore.conversations.service import ConversationService
from supportcore.db.models import Customer, Membership, User
from supportcore.db.uow import UnitOfWork
from supportcore.events import EventBus
from supportcore.inbox.publisher import InboxPublisher
from supportcore.orgs.service import OrganisationService
from supportcore.presence.memory_presence import MemoryAgentPresence
from supportcore.pubsub.memory_pubsub import MemoryPubSub
from supportcore.realtime.spawn import drain_pending
from supportcore.support.settings_service import SupportSettingsService

ALL_DAY = {day: [["00:00", "24:00"]] for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


@dataclass
class World:
    org_id: uuid.UUID
    customer_id: uuid.UUID
    owner_membership_id: uuid.UUID
    widget_id: uuid.UUID


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
async def adapters(settings):
    from supportcore.wiring import build_adapters

    built = replace(build_adapters(settings), pubsub=MemoryPubSub(), presence=MemoryAgentPresence())
    yield built
    await drain_pending()
    await built.aclose()


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def runtime(settings, session_factory, adapters, bus):
    return BotRuntime(settings=settings, session_factory=session_factory, adapters=adapters, events=bus)


@pytest.fixture
async def world(session_factory, bus, adapters):
    from supportcore.widget.service import WidgetService

    async with UnitOfWork(session_factory) as uow:
        s = uow.session
        owner = User(email="owner@x.com", password_hash="x", name="Owner")
        s.add(owner)
        await s.flush()
        org = await OrganisationService(s, bus).create(owner, "Acme")
        membership = (await s.execute(select(Membership).where(Membership.user_id == owner.id))).scalar_one()
        membership.agent_status = "offline"
        (await SupportSettingsService(s, org.id, bus).get()).weekly_hours = ALL_DAY
        widget = await WidgetService(s, org.id, bus, adapters.cipher).create("Site", owner)
        customer = Customer(organisation_id=org.id, visitor_id=uuid.uuid4().hex)
        s.add(customer)
        await s.flush()
        await uow.commit()
    return World(org.id, customer.id, membership.id, widget.id)


@pytest.fixture
def act(session_factory, world, runtime, bus):
    """act(fn, conversation_id=None): fn(service, conversation-or-None, owner membership) in one committed unit
    of work (the conversation is locked when an id is given)."""

    async def run(fn, conversation_id=None):
        async with UnitOfWork(session_factory) as uow:
            service = ConversationService(
                uow.session,
                world.org_id,
                bus,
                publisher=runtime.publisher(),
                bot_hand_back=lambda _cid: _false(),
            )
            conversation = await service.locked(conversation_id) if conversation_id else None
            member = await uow.session.get(Membership, world.owner_membership_id)
            result = await fn(service, conversation, member)
            await uow.commit()
        await drain_pending()
        return result

    return run


async def _false() -> bool:
    return False


@pytest.fixture
def make_conversation(act, world):
    async def make(status="bot", *, is_test=False, **fields):
        async def create(service, _c, _m):
            c = await service.create(world.customer_id, world.widget_id, "en", is_test)
            c.status = status
            for name, value in fields.items():
                setattr(c, name, value)
            return c.id

        return await act(create)

    return make


@pytest.fixture
def assignment(session_factory, world, adapters, settings, bus):
    def make(session):
        return AssignmentService(
            session, world.org_id, bus, adapters, settings, publisher=InboxPublisher(adapters.pubsub, session_factory)
        )

    return make


