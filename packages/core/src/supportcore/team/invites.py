import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.billing.entitlements import EntitlementService
from supportcore.config import Settings
from supportcore.db.models import Invite, Membership, Organisation, Role, User
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import DomainError, NotFoundError
from supportcore.events import EventBus
from supportcore.identity.service import AuthResult, AuthService, normalise_email
from supportcore.notifications.ports import EmailMessage, EmailSender
from supportcore.rbac.checker import ensure_can_grant
from supportcore.team.events import InviteAccepted, InviteRevoked, MemberInvited
from supportcore.team.service import check_can_grant_role, get_actor_role, get_owner_role

if TYPE_CHECKING:
    from supportcore.wiring import Adapters

INVITE_TOKEN_BYTES = 32
_MAX_INVITES = 1000


class AlreadyMemberError(DomainError):
    def __init__(self) -> None:
        super().__init__("This person is already a member of the organisation")


class InviteInvalidError(DomainError):
    """Unknown, expired, accepted or revoked: deliberately indistinguishable to the caller."""

    def __init__(self) -> None:
        super().__init__("This invite is no longer valid")


class InviteEmailMismatchError(DomainError):
    def __init__(self) -> None:
        super().__init__("This invite was sent to a different email address")


class InviteAccountExistsError(DomainError):
    def __init__(self) -> None:
        super().__init__("An account with this email exists; log in to accept the invite.")


class InviteSignupDetailsRequiredError(DomainError):
    def __init__(self) -> None:
        super().__init__("name and password are required to create an account")


@dataclass(frozen=True)
class InvitePreview:
    organisation_name: str
    email: str
    role_name: str


@dataclass(frozen=True)
class AcceptResult:
    user: User
    membership: Membership
    auth: AuthResult | None  # set only when a new account was created


def hash_invite_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _is_pending(invite: Invite, now: datetime) -> bool:
    return invite.accepted_at is None and invite.revoked_at is None and invite.expires_at > now


def _single_line(value: str) -> str:
    return " ".join(value.split())


class InviteService:
    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        events: EventBus,
        email_sender: EmailSender,
        settings: Settings,
        *,
        adapters: "Adapters | Any",
    ) -> None:
        """`adapters`: the billing provider and usage counter the seat limit is checked with."""
        self._session = session
        self._org_id = organisation_id
        self._events = events
        self._email_sender = email_sender
        self._settings = settings
        self._adapters = adapters
        self._invites = TenantScopedRepository(session, Invite, organisation_id)
        self._roles = TenantScopedRepository(session, Role, organisation_id)
        self._memberships = TenantScopedRepository(session, Membership, organisation_id)

    async def create(self, email: str, role_id: UUID, actor: User) -> Invite:
        email = normalise_email(email)
        role = await self._roles.get(role_id)
        if role is None:
            raise NotFoundError("Role not found")
        owner_role = await get_owner_role(self._session, self._org_id)
        actor_role = await get_actor_role(self._session, self._org_id, actor)
        check_can_grant_role(actor_role, role, owner_role)
        if await self._has_active_membership(email):
            raise AlreadyMemberError()

        now = datetime.now(UTC)
        superseded = await self.list_pending(email=email)
        for old in superseded:  # check all before revoking any
            await self._ensure_can_manage(old, actor_role)
        if not superseded:  # a re-send replaces the pending invite, which already holds the seat
            await EntitlementService(self._session, self._org_id, self._adapters).check_capacity("seats")
        for old in superseded:
            old.revoked_at = now
            await self._events.publish(
                InviteRevoked(organisation_id=self._org_id, actor_user_id=actor.id, invite_id=old.id)
            )

        raw = secrets.token_urlsafe(INVITE_TOKEN_BYTES)
        invite = Invite(
            email=email,
            role_id=role.id,
            token_hash=hash_invite_token(raw),
            invited_by_user_id=actor.id,
            expires_at=now + timedelta(seconds=self._settings.invite_ttl_seconds),
        )
        await self._invites.add(invite)
        await self._session.flush()
        await self._events.publish(
            MemberInvited(
                organisation_id=self._org_id,
                actor_user_id=actor.id,
                invite_id=invite.id,
                email=email,
                role_id=role.id,
            )
        )
        # Last step: if delivery fails the error propagates and the caller's transaction rolls back.
        await self._email_sender.send(await self._invite_email(invite, raw, actor))
        return invite

    async def list_pending(self, *, email: str | None = None) -> list[Invite]:
        where = [Invite.accepted_at.is_(None), Invite.revoked_at.is_(None), Invite.expires_at > datetime.now(UTC)]
        if email is not None:
            where.append(Invite.email == normalise_email(email))
        return await self._invites.list(*where, order_by=Invite.created_at.desc(), limit=_MAX_INVITES)

    async def revoke(self, invite_id: UUID, actor: User) -> None:
        invite = await self._invites.get(invite_id)
        if invite is None or invite.accepted_at is not None or invite.revoked_at is not None:
            raise NotFoundError("Invite not found")
        await self._ensure_can_manage(invite, await get_actor_role(self._session, self._org_id, actor))
        invite.revoked_at = datetime.now(UTC)
        await self._session.flush()
        await self._events.publish(
            InviteRevoked(organisation_id=self._org_id, actor_user_id=actor.id, invite_id=invite.id)
        )

    async def _ensure_can_manage(self, invite: Invite, actor_role: Role | None) -> None:
        """Revoking (or superseding) an invite needs every permission its role would grant."""
        role = await self._roles.get(invite.role_id)
        assert role is not None  # guaranteed by the FK
        ensure_can_grant(actor_role.permissions if actor_role is not None else (), role.permissions)

    async def _has_active_membership(self, email: str) -> bool:
        user = (await self._session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            return False
        found = await self._memberships.list(
            Membership.user_id == user.id, Membership.status == "active", limit=1
        )
        return bool(found)

    async def _invite_email(self, invite: Invite, raw_token: str, actor: User) -> EmailMessage:
        org = await self._session.get(Organisation, self._org_id)
        assert org is not None
        org_name = _single_line(org.name)
        link = f"{self._settings.web_origin.rstrip('/')}/accept-invite?token={raw_token}"
        text = (
            f"{_single_line(actor.name)} has invited you to join {org_name}.\n\n"
            f"Accept the invite: {link}\n\n"
            f"This link expires on {invite.expires_at:%Y-%m-%d %H:%M} UTC."
        )
        return EmailMessage(to=invite.email, subject=f"You're invited to join {org_name}", text=text)


# --- token-addressed operations (public; the token identifies the organisation) ---------


async def _find_by_token(session: AsyncSession, raw_token: str, *, for_update: bool = False) -> Invite | None:
    # Sanctioned exception to "tenant queries go through TenantScopedRepository": the token is the
    # only key the caller has (it identifies the organisation), and it is a 256-bit secret.
    stmt = select(Invite).where(Invite.token_hash == hash_invite_token(raw_token))
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one_or_none()


async def preview_invite(session: AsyncSession, raw_token: str) -> InvitePreview:
    invite = await _find_by_token(session, raw_token)
    if invite is None or not _is_pending(invite, datetime.now(UTC)):
        raise InviteInvalidError()
    org = await session.get(Organisation, invite.organisation_id)
    role = await TenantScopedRepository(session, Role, invite.organisation_id).get(invite.role_id)
    if org is None or role is None:
        raise InviteInvalidError()
    return InvitePreview(organisation_name=org.name, email=invite.email, role_name=role.name)


async def accept_invite(
    session: AsyncSession,
    events: EventBus,
    auth_service: AuthService,
    raw_token: str,
    *,
    current_user: User | None,
    name: str | None,
    password: str | None,
) -> AcceptResult:
    # Row lock: a concurrent accept of the same token waits here, then sees accepted_at set.
    invite = await _find_by_token(session, raw_token, for_update=True)
    now = datetime.now(UTC)
    if invite is None or not _is_pending(invite, now):
        raise InviteInvalidError()

    memberships = TenantScopedRepository(session, Membership, invite.organisation_id)
    auth: AuthResult | None = None
    if current_user is not None:
        if normalise_email(current_user.email) != invite.email:
            raise InviteEmailMismatchError()
        user = current_user
    else:
        existing = (await session.execute(select(User).where(User.email == invite.email))).scalar_one_or_none()
        if existing is not None:
            raise InviteAccountExistsError()
        if not name or not name.strip() or not password:
            raise InviteSignupDetailsRequiredError()
        auth = await auth_service.signup(invite.email, password, name)
        user = auth.user

    found = await memberships.list(Membership.user_id == user.id, limit=1)
    if found:
        membership = found[0]
        if membership.status == "active":
            raise AlreadyMemberError()
        membership.status = "active"
        membership.role_id = invite.role_id
    else:
        membership = Membership(user_id=user.id, role_id=invite.role_id, status="active")
        await memberships.add(membership)
    invite.accepted_at = now
    await session.flush()

    await events.publish(
        InviteAccepted(
            organisation_id=invite.organisation_id,
            actor_user_id=user.id,
            user_id=user.id,
            membership_id=membership.id,
        )
    )
    return AcceptResult(user=user, membership=membership, auth=auth)
