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
    )


@pytest.fixture
def cipher(settings):
    from supportcore.wiring import build_adapters

    return build_adapters(settings).cipher


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
def bus(session):
    from supportcore.audit.subscribers import register_audit_subscribers
    from supportcore.events import EventBus

    b = EventBus()
    register_audit_subscribers(b, lambda: session)
    return b


@pytest.fixture
def widgets(session, org, bus, cipher):
    from supportcore.widget.service import WidgetService

    return WidgetService(session, org.id, bus, cipher)
