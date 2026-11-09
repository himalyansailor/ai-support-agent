import pytest

from supportcore.errors import NotFoundError
from supportcore.rbac.checker import PermissionEscalationError
from supportcore.team.events import MemberUpdated
from supportcore.team.service import (
    InvalidMemberStatusError,
    InvalidPhoneNumberError,
    LastOwnerError,
    OwnerRoleRequiredError,
)

from .conftest import make_user


async def test_list_members(members, owner_user, owner_membership, add_member):
    agent_user, agent_m = await add_member("agent@x.com", "Agent")
    views = await members.list()
    by_id = {v.membership_id: v for v in views}
    assert set(by_id) == {owner_membership.id, agent_m.id}
    owner = by_id[owner_membership.id]
    assert (owner.user_id, owner.name, owner.email, owner.role_name, owner.status) == (
        owner_user.id, "Olivia", "owner@x.com", "Owner", "active"
    )
    assert owner.phone_number is None and owner.agent_status == "offline"
    assert by_id[agent_m.id].role_name == "Agent"


async def test_list_members_excludes_other_orgs(session, members, owner_membership):
    from supportcore.events import EventBus
    from supportcore.orgs.service import OrganisationService

    other = await make_user(session, "other@x.com")
    await OrganisationService(session, EventBus()).create(other, "Other")
    assert [v.membership_id for v in await members.list()] == [owner_membership.id]


async def test_cannot_remove_last_owner(members, owner_membership, owner_user, role_by_name):
    with pytest.raises(LastOwnerError):
        await members.update(owner_membership.id, status="deactivated", actor=owner_user)
    viewer = await role_by_name("Viewer")
    with pytest.raises(LastOwnerError):
        await members.update(owner_membership.id, role_id=viewer.id, actor=owner_user)


async def test_can_demote_owner_when_another_active_owner_exists(
    members, owner_membership, owner_user, add_member, role_by_name
):
    await add_member("owner2@x.com", "Owner")
    admin = await role_by_name("Admin")
    view = await members.update(owner_membership.id, role_id=admin.id, actor=owner_user)
    assert view.role_name == "Admin"


async def test_deactivated_owner_does_not_count(members, owner_membership, owner_user, add_member):
    await add_member("owner2@x.com", "Owner", status="deactivated")
    with pytest.raises(LastOwnerError):
        await members.update(owner_membership.id, status="deactivated", actor=owner_user)


async def test_update_role_status_phone_publishes_changes(
    members, owner_user, add_member, role_by_name, seen
):
    _, m = await add_member("agent@x.com", "Agent")
    viewer = await role_by_name("Viewer")
    agent = await role_by_name("Agent")
    view = await members.update(
        m.id, role_id=viewer.id, status="deactivated", phone_number="+14155550123", actor=owner_user
    )
    assert (view.role_id, view.role_name, view.status, view.phone_number) == (
        viewer.id, "Viewer", "deactivated", "+14155550123"
    )
    (ev,) = [e for e in seen if isinstance(e, MemberUpdated)]
    assert ev.membership_id == m.id and ev.actor_user_id == owner_user.id
    assert ev.changes == {
        "role_id": {"from": str(agent.id), "to": str(viewer.id)},
        "status": {"from": "active", "to": "deactivated"},
        "phone_number": {"from": None, "to": "+14155550123"},
    }


async def test_phone_can_be_cleared_and_unset_means_unchanged(members, owner_user, add_member):
    _, m = await add_member("agent@x.com", "Agent")
    await members.update(m.id, phone_number="+14155550123", actor=owner_user)
    assert (await members.update(m.id, status="active", actor=owner_user)).phone_number == "+14155550123"
    assert (await members.update(m.id, phone_number=None, actor=owner_user)).phone_number is None


async def test_phone_with_spaces_is_normalised(members, owner_user, add_member):
    _, m = await add_member("agent@x.com", "Agent")
    assert (await members.update(m.id, phone_number="+1 415 555 0123", actor=owner_user)).phone_number == "+14155550123"


@pytest.mark.parametrize("phone", ["14155550123", "+0123456789", "+12345", "+1234567890123456"])
async def test_invalid_phone_rejected(members, owner_user, add_member, phone):
    _, m = await add_member("agent@x.com", "Agent")
    with pytest.raises(InvalidPhoneNumberError):
        await members.update(m.id, phone_number=phone, actor=owner_user)


async def test_invalid_status_rejected(members, owner_user, add_member):
    _, m = await add_member("agent@x.com", "Agent")
    with pytest.raises(InvalidMemberStatusError):
        await members.update(m.id, status="banned", actor=owner_user)


async def test_role_of_other_org_is_not_found(session, members, owner_user, add_member):
    from sqlalchemy import select

    from supportcore.db.models import Role
    from supportcore.events import EventBus
    from supportcore.orgs.service import OrganisationService

    _, m = await add_member("agent@x.com", "Agent")
    other_owner = await make_user(session, "other@x.com")
    other = await OrganisationService(session, EventBus()).create(other_owner, "Other")
    foreign_role = (
        await session.execute(select(Role).where(Role.organisation_id == other.id, Role.name == "Viewer"))
    ).scalar_one()
    with pytest.raises(NotFoundError):
        await members.update(m.id, role_id=foreign_role.id, actor=owner_user)


async def test_membership_of_other_org_is_not_found(session, members, owner_user):
    from sqlalchemy import select

    from supportcore.db.models import Membership
    from supportcore.events import EventBus
    from supportcore.orgs.service import OrganisationService

    other_owner = await make_user(session, "other@x.com")
    other = await OrganisationService(session, EventBus()).create(other_owner, "Other")
    foreign = (await session.execute(select(Membership).where(Membership.organisation_id == other.id))).scalar_one()
    with pytest.raises(NotFoundError):
        await members.update(foreign.id, status="deactivated", actor=owner_user)


async def test_non_owner_cannot_grant_or_touch_owner(members, owner_membership, add_member, role_by_name):
    admin_user, admin_m = await add_member("admin@x.com", "Admin")
    _, agent_m = await add_member("agent@x.com", "Agent")
    owner = await role_by_name("Owner")
    viewer = await role_by_name("Viewer")
    with pytest.raises(OwnerRoleRequiredError):
        await members.update(admin_m.id, role_id=owner.id, actor=admin_user)
    with pytest.raises(OwnerRoleRequiredError):
        await members.update(agent_m.id, role_id=owner.id, actor=admin_user)
    with pytest.raises(OwnerRoleRequiredError):
        await members.update(owner_membership.id, role_id=viewer.id, actor=admin_user)
    with pytest.raises(OwnerRoleRequiredError):
        await members.update(owner_membership.id, status="deactivated", actor=admin_user)
    # admins may still manage non-owner members
    assert (await members.update(agent_m.id, role_id=viewer.id, actor=admin_user)).role_name == "Viewer"


async def test_cannot_change_member_whose_current_role_exceeds_actor(members, add_member, role_by_name, team_lead):
    lead_user, _ = await team_lead()
    _, admin_m = await add_member("admin@x.com", "Admin")
    agent = await role_by_name("Agent")
    with pytest.raises(PermissionEscalationError):
        await members.update(admin_m.id, role_id=agent.id, actor=lead_user)  # downgrade a superior
    with pytest.raises(PermissionEscalationError):
        await members.update(admin_m.id, status="deactivated", actor=lead_user)
    # members within the actor's own permissions remain manageable
    _, agent_m = await add_member("agent@x.com", "Agent")
    assert (await members.update(agent_m.id, status="deactivated", actor=lead_user)).status == "deactivated"
