import pytest

from supportcore.db.models import Membership, Role, User
from supportcore.errors import NotFoundError
from supportcore.events import DomainEvent, EventBus
from supportcore.orgs.service import OrganisationService
from supportcore.rbac.checker import PermissionEscalationError
from supportcore.rbac.permissions import ALL_PERMISSIONS, InvalidPermissionError

ALL = frozenset(p.value for p in ALL_PERMISSIONS)
from supportcore.roles.events import RoleCreated, RoleDeleted, RoleUpdated
from supportcore.roles.service import (
    RoleInUseError,
    RoleNameTakenError,
    RoleService,
    SystemRoleImmutableError,
)


@pytest.fixture
async def user(session) -> User:
    u = User(email="o@x.com", password_hash="x", name="O")
    session.add(u)
    await session.flush()
    return u


@pytest.fixture
def seen() -> list[DomainEvent]:
    return []


@pytest.fixture
async def bus(seen) -> EventBus:
    b = EventBus()

    async def handler(e: DomainEvent) -> None:
        seen.append(e)

    for cls in (RoleCreated, RoleUpdated, RoleDeleted):
        b.subscribe(cls, handler)
    return b


@pytest.fixture
async def org(session, user):
    return await OrganisationService(session, EventBus()).create(user, "Acme")


@pytest.fixture
def svc(session, bus, org) -> RoleService:
    return RoleService(session, org.id, bus)


async def test_list_system_first_then_custom_by_name(svc, user):
    await svc.create("Zed", ["kb.manage"], user, actor_permissions=ALL)
    await svc.create("Alpha", ["kb.manage"], user, actor_permissions=ALL)
    names = [r.name for r in await svc.list()]
    assert names == ["Owner", "Admin", "Agent", "Viewer", "Alpha", "Zed"]


async def test_create_custom_role_dedupes_and_publishes(svc, user, org, seen):
    role = await svc.create("  Support Lead ", ["kb.manage", "calls.take", "kb.manage"], user, actor_permissions=ALL)
    assert role.name == "Support Lead" and not role.is_system
    assert role.permissions == ["kb.manage", "calls.take"]
    assert role.organisation_id == org.id
    (ev,) = seen
    assert isinstance(ev, RoleCreated)
    assert ev.role_id == role.id and ev.role_name == "Support Lead"
    assert ev.permissions == ["kb.manage", "calls.take"]
    assert ev.actor_user_id == user.id and ev.organisation_id == org.id


async def test_duplicate_name_case_insensitive(svc, user):
    await svc.create("Lead", [], user, actor_permissions=ALL)
    with pytest.raises(RoleNameTakenError):
        await svc.create("lead", [], user, actor_permissions=ALL)
    with pytest.raises(RoleNameTakenError):
        await svc.create("agent", [], user, actor_permissions=ALL)


async def test_unknown_permission(svc, user):
    with pytest.raises(InvalidPermissionError):
        await svc.create("X", ["nope"], user, actor_permissions=ALL)


async def test_update_partial_and_rename_conflict(svc, user, seen):
    role = await svc.create("Lead", ["kb.manage"], user, actor_permissions=ALL)
    other = await svc.create("Other", [], user, actor_permissions=ALL)
    updated = await svc.update(role.id, None, ["calls.take"], user, actor_permissions=ALL)
    assert updated.name == "Lead" and updated.permissions == ["calls.take"]
    renamed = await svc.update(role.id, " Boss ", None, user, actor_permissions=ALL)
    assert renamed.name == "Boss" and renamed.permissions == ["calls.take"]
    assert isinstance(seen[-1], RoleUpdated) and seen[-1].role_name == "Boss"
    with pytest.raises(RoleNameTakenError):
        await svc.update(other.id, "boss", None, user, actor_permissions=ALL)
    # renaming to its own name in a different case is fine
    assert (await svc.update(role.id, "BOSS", None, user, actor_permissions=ALL)).name == "BOSS"


async def test_system_role_immutable(svc, session, org, user):
    from sqlalchemy import select

    agent = (await session.execute(select(Role).where(Role.organisation_id == org.id, Role.name == "Agent"))).scalar_one()
    with pytest.raises(SystemRoleImmutableError):
        await svc.update(agent.id, "X", None, user, actor_permissions=ALL)
    with pytest.raises(SystemRoleImmutableError):
        await svc.delete(agent.id, user, actor_permissions=ALL)


async def test_delete_in_use_and_unused(svc, session, user, seen):
    role = await svc.create("Lead", [], user, actor_permissions=ALL)
    u2 = User(email="b@x.com", password_hash="x", name="B")
    session.add(u2)
    await session.flush()
    m = Membership(organisation_id=role.organisation_id, user_id=u2.id, role_id=role.id)
    session.add(m)
    await session.flush()
    with pytest.raises(RoleInUseError, match=r"1 member"):
        await svc.delete(role.id, user, actor_permissions=ALL)
    await session.delete(m)
    await session.flush()
    await svc.delete(role.id, user, actor_permissions=ALL)
    assert isinstance(seen[-1], RoleDeleted) and seen[-1].role_id == role.id
    with pytest.raises(NotFoundError):
        await svc.delete(role.id, user, actor_permissions=ALL)


async def test_other_org_role_not_found(svc, session, bus):
    u = User(email="c@x.com", password_hash="x", name="C")
    session.add(u)
    await session.flush()
    other_org = await OrganisationService(session, EventBus()).create(u, "Other")
    other_role = await RoleService(session, other_org.id, bus).create("Mine", [], u, actor_permissions=ALL)
    with pytest.raises(NotFoundError):
        await svc.update(other_role.id, "x", None, u, actor_permissions=ALL)
    with pytest.raises(NotFoundError):
        await svc.delete(other_role.id, u, actor_permissions=ALL)


async def test_cannot_grant_permissions_actor_lacks(svc, user):
    admin_perms = {"kb.manage", "team.manage"}
    with pytest.raises(PermissionEscalationError):
        await svc.create("Boss", ["kb.manage", "billing.manage"], user, actor_permissions=admin_perms)
    role = await svc.create("Kb", ["kb.manage"], user, actor_permissions=admin_perms)
    with pytest.raises(PermissionEscalationError):
        await svc.update(role.id, None, ["org.manage"], user, actor_permissions=admin_perms)
    # renaming does not grant anything
    assert (await svc.update(role.id, "KB", None, user, actor_permissions=admin_perms)).name == "KB"


async def test_cannot_edit_or_delete_role_whose_current_permissions_exceed_actor(svc, user):
    lead_perms = {"team.manage", "queries.answer"}
    billing = await svc.create("Billing", ["billing.manage"], user, actor_permissions=ALL)
    with pytest.raises(PermissionEscalationError):
        await svc.update(billing.id, None, [], user, actor_permissions=lead_perms)  # strip a superior role
    with pytest.raises(PermissionEscalationError):
        await svc.update(billing.id, "Renamed", None, user, actor_permissions=lead_perms)
    with pytest.raises(PermissionEscalationError):
        await svc.delete(billing.id, user, actor_permissions=lead_perms)
    own = await svc.create("Helpers", ["queries.answer"], user, actor_permissions=lead_perms)
    assert (await svc.update(own.id, "Help", None, user, actor_permissions=lead_perms)).name == "Help"
    await svc.delete(own.id, user, actor_permissions=lead_perms)
