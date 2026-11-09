from types import SimpleNamespace

import pytest
from sqlalchemy import select

from supportcore.billing.dev_billing import DevBilling
from supportcore.billing.memory_counter import MemoryUsageCounter
from supportcore.config import Settings
from supportcore.db.models import Membership, Role, User
from supportcore.events import DomainEvent, EventBus
from supportcore.identity.service import AuthService
from supportcore.notifications.console_sender import ConsoleEmailSender
from supportcore.orgs.service import OrganisationService
from supportcore.team.invites import InviteService
from supportcore.team.service import MemberService
from supportcore.testing import TEST_DATABASE_URL


def billing_adapters(provider=None) -> SimpleNamespace:
    """What InviteService's seat check reads: the billing provider (default dev: no limits) and the usage counter."""
    return SimpleNamespace(billing=provider or DevBilling(), usage_counter=MemoryUsageCounter())


class PlainHasher:
    def hash(self, password: str) -> str:
        return "plain:" + password

    def verify(self, hash: str, password: str) -> bool:
        return hash == "plain:" + password


class FakeTokens:
    def issue(self, user_id) -> str:
        return f"access-{user_id}"

    def decode(self, token: str):
        raise NotImplementedError


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        database_url=TEST_DATABASE_URL,
        redis_url="redis://localhost:6379/0",
        jwt_secret="test-secret-test-secret-test-secret-123",
        smtp_from="noreply@test.local",
        web_origin="https://app.example.com",
    )


@pytest.fixture
def seen() -> list[DomainEvent]:
    return []


@pytest.fixture
def bus(seen) -> EventBus:
    b = EventBus()

    async def handler(e: DomainEvent) -> None:
        seen.append(e)

    b.subscribe(DomainEvent, handler)
    return b


@pytest.fixture
def console_sender() -> ConsoleEmailSender:
    return ConsoleEmailSender()


async def make_user(session, email: str, name: str | None = None) -> User:
    u = User(email=email, password_hash="plain:correct-horse-1", name=name or email)
    session.add(u)
    await session.flush()
    return u


@pytest.fixture
async def owner_user(session) -> User:
    return await make_user(session, "owner@x.com", "Olivia")


@pytest.fixture
async def org(session, owner_user):
    return await OrganisationService(session, EventBus()).create(owner_user, "Acme")


@pytest.fixture
async def owner_membership(session, org, owner_user) -> Membership:
    return (
        await session.execute(
            select(Membership).where(Membership.organisation_id == org.id, Membership.user_id == owner_user.id)
        )
    ).scalar_one()


@pytest.fixture
def role_by_name(session, org):
    async def find(name: str) -> Role:
        return (
            await session.execute(select(Role).where(Role.organisation_id == org.id, Role.name == name))
        ).scalar_one()

    return find


@pytest.fixture
def add_member(session, org, role_by_name):
    async def add(email: str, role_name: str, status: str = "active") -> tuple[User, Membership]:
        user = await make_user(session, email)
        role = await role_by_name(role_name)
        m = Membership(organisation_id=org.id, user_id=user.id, role_id=role.id, status=status)
        session.add(m)
        await session.flush()
        return user, m

    return add


@pytest.fixture
def members(session, org, bus) -> MemberService:
    return MemberService(session, org.id, bus)


@pytest.fixture
def invites(session, org, bus, console_sender, settings) -> InviteService:
    return InviteService(session, org.id, bus, console_sender, settings, adapters=billing_adapters())


@pytest.fixture
def auth_service(session, settings, bus) -> AuthService:
    return AuthService(session, PlainHasher(), FakeTokens(), settings, bus)


@pytest.fixture
def team_lead(session, org, add_member):
    """A non-Owner who holds team.manage but fewer permissions than Admin."""

    async def make(email: str = "lead@x.com") -> tuple[User, Membership]:
        if (await session.execute(select(Role).where(Role.organisation_id == org.id, Role.name == "Lead"))).first() is None:
            session.add(Role(organisation_id=org.id, name="Lead", permissions=["team.manage", "queries.answer", "calls.take"]))
            await session.flush()
        return await add_member(email, "Lead")

    return make
