import pytest


@pytest.fixture
def settings():
    from supportcore.config import Settings

    return Settings(_env_file=None, env="test", jwt_secret="test-secret-test-secret-test-secret-123")


@pytest.fixture
def cipher():
    from cryptography.fernet import Fernet

    from supportcore.secrets.fernet_cipher import FernetCipher

    return FernetCipher(Fernet.generate_key().decode())


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
def tools(session, org, bus, cipher, settings):
    from supportcore.tools.service import BusinessToolService

    return BusinessToolService(session, org.id, bus, cipher, settings)
