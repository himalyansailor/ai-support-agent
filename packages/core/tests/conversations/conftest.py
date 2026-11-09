import pytest



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
    )


@pytest.fixture
def adapters(settings):
    from supportcore.wiring import build_adapters

    return build_adapters(settings)


@pytest.fixture
async def owner(session):
    from supportcore.db.models import User

    u = User(email="owner@x.com", password_hash="x", name="Owner")
    session.add(u)
    await session.flush()
    return u


@pytest.fixture
async def org(session, owner):
    from supportcore.events import EventBus
    from supportcore.orgs.service import OrganisationService

    return await OrganisationService(session, EventBus()).create(owner, "Acme")


@pytest.fixture
def bus():
    from supportcore.events import EventBus

    return EventBus()


@pytest.fixture
async def widget(session, org, owner, bus, adapters):
    from supportcore.widget.service import WidgetService

    svc = WidgetService(session, org.id, bus, adapters.cipher)
    return await svc.create("Site", owner)


@pytest.fixture
def widgets(session, org, bus, adapters):
    from supportcore.widget.service import WidgetService

    return WidgetService(session, org.id, bus, adapters.cipher)


@pytest.fixture
def conversations(session, org, bus):
    from supportcore.conversations.service import ConversationService
    from supportcore.inbox.publisher import InboxPublisher
    from supportcore.pubsub.memory_pubsub import MemoryPubSub

    # Rollback-isolated: nothing commits, so the publisher never publishes (end() and contact need one).
    return ConversationService(session, org.id, bus, publisher=InboxPublisher(MemoryPubSub(), None))


@pytest.fixture
def sessions_svc(session, adapters, settings, bus):
    from supportcore.widget.session import WidgetSessionService

    return WidgetSessionService(session, adapters, settings, bus)
