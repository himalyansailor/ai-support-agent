import asyncio
import hashlib
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.db.models import Invite, Membership, User
from supportcore.errors import NotFoundError
from supportcore.events import EventBus
from supportcore.identity.service import AuthService, WeakPasswordError
from supportcore.notifications.console_sender import ConsoleEmailSender
from supportcore.notifications.ports import EmailDeliveryError, EmailMessage
from supportcore.orgs.service import OrganisationService
from supportcore.rbac.checker import PermissionEscalationError
from supportcore.team.events import InviteAccepted, InviteRevoked, MemberInvited
from supportcore.team.invites import (
    AlreadyMemberError,
    InviteAccountExistsError,
    InviteEmailMismatchError,
    InviteInvalidError,
    InviteService,
    InviteSignupDetailsRequiredError,
    accept_invite,
    preview_invite,
)
from supportcore.team.service import OwnerRoleRequiredError

from .conftest import FakeTokens, PlainHasher, billing_adapters, make_user


def _token(message: EmailMessage) -> str:
    match = re.search(r"accept-invite\?token=([A-Za-z0-9_-]+)", message.text)
    assert match, message.text
    return match.group(1)


@pytest.fixture
async def agent_role(role_by_name):
    return await role_by_name("Agent")


@pytest.fixture
def accept(session, bus, auth_service):
    async def run(token, *, current_user=None, name=None, password=None):
        return await accept_invite(
            session, bus, auth_service, token, current_user=current_user, name=name, password=password
        )

    return run


async def test_invite_sends_email_with_link(invites, console_sender, owner_user, agent_role, seen, org, settings):
    invite = await invites.create("  New@Example.com ", agent_role.id, owner_user)
    assert invite.email == "new@example.com" and invite.role_id == agent_role.id
    assert invite.invited_by_user_id == owner_user.id
    (msg,) = console_sender.sent
    assert msg.to == "new@example.com"
    assert msg.subject == "You're invited to join Acme"
    assert "https://app.example.com/accept-invite?token=" in msg.text
    raw = _token(msg)
    assert len(raw) >= 43  # 32 random bytes, urlsafe-base64
    assert invite.token_hash == hashlib.sha256(raw.encode()).hexdigest() and raw not in invite.token_hash
    expected = datetime.now(UTC) + timedelta(seconds=settings.invite_ttl_seconds)
    assert abs((invite.expires_at - expected).total_seconds()) < 60
    (ev,) = [e for e in seen if isinstance(e, MemberInvited)]
    assert ev.email == "new@example.com" and ev.role_id == agent_role.id
    assert ev.organisation_id == org.id and ev.actor_user_id == owner_user.id


async def test_invite_existing_member_is_409(invites, owner_user, agent_role, console_sender):
    with pytest.raises(AlreadyMemberError):
        await invites.create("OWNER@x.com", agent_role.id, owner_user)
    assert console_sender.sent == []


async def test_deactivated_member_can_be_reinvited(invites, owner_user, agent_role, add_member):
    await add_member("gone@x.com", "Viewer", status="deactivated")
    invite = await invites.create("gone@x.com", agent_role.id, owner_user)
    assert invite.email == "gone@x.com"


async def test_reinvite_revokes_previous_pending_invite(invites, owner_user, agent_role, console_sender, seen):
    first = await invites.create("new@x.com", agent_role.id, owner_user)
    second = await invites.create("new@x.com", agent_role.id, owner_user)
    assert first.revoked_at is not None and second.revoked_at is None
    assert [i.id for i in await invites.list_pending()] == [second.id]
    assert [e.invite_id for e in seen if isinstance(e, InviteRevoked)] == [first.id]
    assert len(console_sender.sent) == 2


async def test_invite_role_of_other_org_is_404(session, invites, owner_user):
    other_owner = await make_user(session, "other@x.com")
    other = await OrganisationService(session, EventBus()).create(other_owner, "Other")
    from supportcore.db.models import Role

    foreign = (
        await session.execute(select(Role).where(Role.organisation_id == other.id, Role.name == "Agent"))
    ).scalar_one()
    with pytest.raises(NotFoundError):
        await invites.create("new@x.com", foreign.id, owner_user)


async def test_only_owner_can_invite_as_owner(session, org, bus, console_sender, settings, add_member, role_by_name, owner_user):
    admin_user, _ = await add_member("admin@x.com", "Admin")
    owner_role = await role_by_name("Owner")
    admin_invites = InviteService(session, org.id, bus, console_sender, settings, adapters=billing_adapters())
    with pytest.raises(OwnerRoleRequiredError):
        await admin_invites.create("new@x.com", owner_role.id, admin_user)
    assert (await admin_invites.create("new@x.com", owner_role.id, owner_user)).role_id == owner_role.id


async def test_failed_email_send_fails_the_invite(session, org, bus, settings, owner_user, agent_role):
    class BrokenSender:
        async def send(self, message: EmailMessage) -> None:
            raise EmailDeliveryError()

    svc = InviteService(session, org.id, bus, BrokenSender(), settings, adapters=billing_adapters())
    with pytest.raises(EmailDeliveryError):
        await svc.create("new@x.com", agent_role.id, owner_user)


async def test_list_pending_excludes_accepted_revoked_expired(session, invites, owner_user, agent_role, console_sender, accept):
    keep = await invites.create("keep@x.com", agent_role.id, owner_user)
    revoked = await invites.create("revoked@x.com", agent_role.id, owner_user)
    await invites.revoke(revoked.id, owner_user)
    expired = await invites.create("expired@x.com", agent_role.id, owner_user)
    expired.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await invites.create("accepted@x.com", agent_role.id, owner_user)
    await accept(_token(console_sender.sent[-1]), name="Acc", password="correct-horse-1")
    assert [i.id for i in await invites.list_pending()] == [keep.id]


async def test_revoke(invites, owner_user, agent_role, console_sender, seen, accept):
    invite = await invites.create("new@x.com", agent_role.id, owner_user)
    await invites.revoke(invite.id, owner_user)
    assert invite.revoked_at is not None
    assert [e.invite_id for e in seen if isinstance(e, InviteRevoked)] == [invite.id]
    with pytest.raises(NotFoundError):
        await invites.revoke(invite.id, owner_user)
    with pytest.raises(InviteInvalidError):
        await accept(_token(console_sender.sent[0]), name="N", password="correct-horse-1")


async def test_revoke_other_org_invite_is_404(session, invites, owner_user, bus, console_sender, settings):
    other_owner = await make_user(session, "other@x.com")
    other = await OrganisationService(session, EventBus()).create(other_owner, "Other")
    from supportcore.db.models import Role

    role = (await session.execute(select(Role).where(Role.organisation_id == other.id, Role.name == "Agent"))).scalar_one()
    foreign = await InviteService(session, other.id, bus, console_sender, settings, adapters=billing_adapters()).create("z@x.com", role.id, other_owner)
    with pytest.raises(NotFoundError):
        await invites.revoke(foreign.id, owner_user)


async def test_preview(session, invites, owner_user, agent_role, console_sender):
    await invites.create("new@x.com", agent_role.id, owner_user)
    preview = await preview_invite(session, _token(console_sender.sent[0]))
    assert (preview.organisation_name, preview.email, preview.role_name) == ("Acme", "new@x.com", "Agent")
    with pytest.raises(InviteInvalidError):
        await preview_invite(session, "not-a-token")


async def test_accept_creates_user_and_membership(session, invites, owner_user, agent_role, console_sender, accept, seen, org):
    invite = await invites.create("New@x.com", agent_role.id, owner_user)
    result = await accept(_token(console_sender.sent[0]), name=" Newbie ", password="correct-horse-1")
    assert result.user.email == "new@x.com" and result.user.name == "Newbie"
    assert result.auth is not None and result.auth.access_token == f"access-{result.user.id}"
    assert result.membership.organisation_id == org.id
    assert result.membership.role_id == agent_role.id and result.membership.status == "active"
    assert invite.accepted_at is not None
    (ev,) = [e for e in seen if isinstance(e, InviteAccepted)]
    assert ev.user_id == result.user.id and ev.membership_id == result.membership.id
    assert ev.organisation_id == org.id


async def test_accept_new_user_requires_name_and_password(invites, owner_user, agent_role, console_sender, accept):
    await invites.create("new@x.com", agent_role.id, owner_user)
    token = _token(console_sender.sent[0])
    with pytest.raises(InviteSignupDetailsRequiredError):
        await accept(token, name=None, password="correct-horse-1")
    with pytest.raises(InviteSignupDetailsRequiredError):
        await accept(token, name="  ", password="correct-horse-1")
    with pytest.raises(WeakPasswordError):
        await accept(token, name="N", password="short")


async def test_accept_as_logged_in_user(session, invites, owner_user, agent_role, console_sender, accept):
    existing = await make_user(session, "exists@x.com")
    await invites.create("Exists@x.com", agent_role.id, owner_user)
    result = await accept(_token(console_sender.sent[0]), current_user=existing)
    assert result.user.id == existing.id and result.auth is None
    assert result.membership.user_id == existing.id


async def test_accept_existing_account_without_login_is_409(session, invites, owner_user, agent_role, console_sender, accept):
    await make_user(session, "exists@x.com")
    await invites.create("exists@x.com", agent_role.id, owner_user)
    with pytest.raises(InviteAccountExistsError, match="log in to accept"):
        await accept(_token(console_sender.sent[0]), name="X", password="correct-horse-1")


async def test_accept_email_mismatch_403(session, invites, owner_user, agent_role, console_sender, accept):
    someone = await make_user(session, "someone@x.com")
    invite = await invites.create("new@x.com", agent_role.id, owner_user)
    with pytest.raises(InviteEmailMismatchError):
        await accept(_token(console_sender.sent[0]), current_user=someone)
    assert invite.accepted_at is None


async def test_invite_expired_is_410(invites, owner_user, agent_role, console_sender, accept, session):
    invite = await invites.create("new@x.com", agent_role.id, owner_user)
    invite.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.flush()
    token = _token(console_sender.sent[0])
    with pytest.raises(InviteInvalidError):
        await accept(token, name="N", password="correct-horse-1")
    with pytest.raises(InviteInvalidError):
        await preview_invite(session, token)


async def test_invite_reuse_is_410(invites, owner_user, agent_role, console_sender, accept, session):
    await invites.create("new@x.com", agent_role.id, owner_user)
    token = _token(console_sender.sent[0])
    result = await accept(token, name="N", password="correct-horse-1")
    with pytest.raises(InviteInvalidError):
        await accept(token, current_user=result.user)
    with pytest.raises(InviteInvalidError):
        await preview_invite(session, token)


async def test_unknown_token_is_410(accept):
    with pytest.raises(InviteInvalidError):
        await accept("nope", name="N", password="correct-horse-1")


async def test_accept_reactivates_deactivated_member_with_invite_role(
    session, invites, owner_user, agent_role, console_sender, accept, add_member
):
    user, m = await add_member("gone@x.com", "Viewer", status="deactivated")
    await invites.create("gone@x.com", agent_role.id, owner_user)
    result = await accept(_token(console_sender.sent[0]), current_user=user)
    assert result.membership.id == m.id
    assert (m.status, m.role_id) == ("active", agent_role.id)


async def test_accept_when_already_active_member_is_409(
    session, invites, owner_user, agent_role, console_sender, accept, add_member
):
    await invites.create("late@x.com", agent_role.id, owner_user)
    user, _ = await add_member("late@x.com", "Viewer")  # became a member some other way meanwhile
    with pytest.raises(AlreadyMemberError):
        await accept(_token(console_sender.sent[0]), current_user=user)


async def test_concurrent_double_accept_creates_one_membership(db_engine, settings):
    """Two transactions accept the same token at once: the invite row lock makes the loser see it accepted."""
    suffix = uuid.uuid4().hex[:8]
    sender = ConsoleEmailSender()
    async with AsyncSession(db_engine, expire_on_commit=False) as s:
        owner = User(email=f"owner-{suffix}@x.com", password_hash="x", name="O")
        invitee = User(email=f"inv-{suffix}@x.com", password_hash="x", name="I")
        s.add_all([owner, invitee])
        await s.flush()
        org = await OrganisationService(s, EventBus()).create(owner, f"Race {suffix}")
        from supportcore.db.models import Role

        role = (await s.execute(select(Role).where(Role.organisation_id == org.id, Role.name == "Agent"))).scalar_one()
        await InviteService(s, org.id, EventBus(), sender, settings, adapters=billing_adapters()).create(invitee.email, role.id, owner)
        await s.commit()
    token = _token(sender.sent[0])

    async def attempt():
        async with AsyncSession(db_engine, expire_on_commit=False) as s:
            user = await s.get(User, invitee.id)
            auth = AuthService(s, PlainHasher(), FakeTokens(), settings, EventBus())
            try:
                await accept_invite(s, EventBus(), auth, token, current_user=user, name=None, password=None)
                await asyncio.sleep(0.2)  # hold the row lock so the other attempt has to wait on it
                await s.commit()
                return "ok"
            except InviteInvalidError:
                return "invalid"

    try:
        outcomes = await asyncio.gather(attempt(), attempt())
        assert sorted(outcomes) == ["invalid", "ok"]
        async with AsyncSession(db_engine) as s:
            count = len((await s.execute(select(Membership).where(Membership.organisation_id == org.id))).all())
            assert count == 2  # owner + invitee, exactly once
    finally:
        async with AsyncSession(db_engine) as s:
            await s.execute(text("DELETE FROM organisations WHERE id = :id"), {"id": org.id})
            await s.execute(text("DELETE FROM users WHERE email LIKE :p"), {"p": f"%-{suffix}@x.com"})
            await s.commit()


async def test_cannot_revoke_or_supersede_invite_whose_role_exceeds_actor(
    session, org, bus, console_sender, settings, owner_user, role_by_name, team_lead, agent_role
):
    lead_user, _ = await team_lead()
    admin_role = await role_by_name("Admin")
    owners = InviteService(session, org.id, bus, console_sender, settings, adapters=billing_adapters())
    leads = InviteService(session, org.id, bus, console_sender, settings, adapters=billing_adapters())
    admin_invite = await owners.create("new@x.com", admin_role.id, owner_user)
    with pytest.raises(PermissionEscalationError):
        await leads.revoke(admin_invite.id, lead_user)
    with pytest.raises(PermissionEscalationError):
        await leads.create("new@x.com", agent_role.id, lead_user)  # would supersede the Admin invite
    assert admin_invite.revoked_at is None
    # invites within the actor's permissions can still be revoked
    agent_invite = await leads.create("other@x.com", agent_role.id, lead_user)
    await leads.revoke(agent_invite.id, lead_user)
    assert agent_invite.revoked_at is not None


async def test_invite_refused_at_seat_limit_402(session, org, bus, console_sender, settings, owner_user, agent_role):
    from supportcore.billing.errors import PlanLimitError
    from supportcore.billing.fake_billing import FakeBilling
    from supportcore.testing import start_trial_plan

    await start_trial_plan(session, org.id, seats=2)  # the owner holds one seat
    service = InviteService(
        session, org.id, bus, console_sender, settings, adapters=billing_adapters(FakeBilling())
    )
    await service.create("first@x.com", agent_role.id, owner_user)
    await service.create("first@x.com", agent_role.id, owner_user)  # a re-send replaces the pending invite
    with pytest.raises(PlanLimitError) as raised:
        await service.create("second@x.com", agent_role.id, owner_user)
    assert (raised.value.resource, raised.value.limit) == ("seats", 2)
    assert [m.to for m in console_sender.sent] == ["first@x.com", "first@x.com"]  # refused before any email
