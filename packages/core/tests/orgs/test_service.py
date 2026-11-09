import pytest
from sqlalchemy import select

from supportcore.db.models import Membership, Organisation, Role, User
from supportcore.errors import NotFoundError
from supportcore.events import DomainEvent, EventBus
from supportcore.orgs.events import OrganisationCreated
from supportcore.orgs.service import OrganisationService, slugify
from supportcore.rbac.defaults import DEFAULT_ROLES


@pytest.fixture
async def user(session) -> User:
    u = User(email="o@x.com", password_hash="x", name="O")
    session.add(u)
    await session.flush()
    return u


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


async def test_create_org_makes_owner_and_system_roles(session, bus, user):
    org = await OrganisationService(session, bus).create(user, "Acme")
    roles = (await session.execute(select(Role).where(Role.organisation_id == org.id))).scalars().all()
    assert {r.name for r in roles} == set(DEFAULT_ROLES)
    assert len(roles) == 4 and all(r.is_system for r in roles)
    owner = next(r for r in roles if r.name == "Owner")
    assert set(owner.permissions) == {p.value for p in DEFAULT_ROLES["Owner"]}
    m = (await session.execute(select(Membership).where(Membership.organisation_id == org.id))).scalar_one()
    assert m.user_id == user.id and m.status == "active" and m.role_id == owner.id
    assert org.created_by_user_id == user.id


async def test_slug_collision_suffixes(session, bus, user):
    svc = OrganisationService(session, bus)
    slugs = [(await svc.create(user, "Acme")).slug for _ in range(3)]
    assert slugs == ["acme", "acme-2", "acme-3"]


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("  Hello,  World!! ", "hello-world"),
        ("Café Zoë", "caf-zo"),
        ("---", "org"),
        ("日本", "org"),
        ("a" * 80, "a" * 50),
    ],
)
def test_slugify(name, slug):
    assert slugify(name) == slug


async def test_create_publishes_event(session, user):
    seen: list[DomainEvent] = []

    async def handler(e: DomainEvent) -> None:
        seen.append(e)

    bus = EventBus()
    bus.subscribe(OrganisationCreated, handler)
    org = await OrganisationService(session, bus).create(user, "Acme")
    assert len(seen) == 1
    ev = seen[0]
    assert isinstance(ev, OrganisationCreated)
    assert ev.organisation_id == org.id and ev.actor_user_id == user.id and ev.organisation_name == "Acme"


async def test_get_for_member(session, bus, user):
    svc = OrganisationService(session, bus)
    org = await svc.create(user, "Acme")
    got_org, membership, role = await svc.get_for_member(user, org.id)
    assert got_org.id == org.id and membership.user_id == user.id and role.name == "Owner"


async def test_get_for_member_non_member_and_deactivated_not_found(session, bus, user):
    svc = OrganisationService(session, bus)
    org = await svc.create(user, "Acme")
    other = User(email="p@x.com", password_hash="x", name="P")
    session.add(other)
    await session.flush()
    with pytest.raises(NotFoundError):
        await svc.get_for_member(other, org.id)
    m = (await session.execute(select(Membership).where(Membership.user_id == user.id))).scalar_one()
    m.status = "deactivated"
    await session.flush()
    with pytest.raises(NotFoundError):
        await svc.get_for_member(user, org.id)
    assert isinstance(org, Organisation)
