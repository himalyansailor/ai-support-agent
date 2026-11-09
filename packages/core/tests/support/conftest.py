import pytest


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
def settings_service(session, org, bus):
    from supportcore.support.settings_service import SupportSettingsService

    return SupportSettingsService(session, org.id, bus)
