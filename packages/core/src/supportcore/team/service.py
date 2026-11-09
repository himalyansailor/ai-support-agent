import re
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.db.models import Membership, Role, User
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import DomainError, NotFoundError
from supportcore.events import EventBus
from supportcore.rbac.checker import ensure_can_grant
from supportcore.team.events import MemberUpdated

OWNER_ROLE_NAME = "Owner"
MEMBER_STATUSES = ("active", "deactivated")
AGENT_STATUSES = ("online", "away", "offline")
E164_PATTERN = re.compile(r"^\+[1-9]\d{6,14}$")
CALL_PREFERENCES = ("browser", "phone")
_PHONE_FORMATTING = re.compile(r"[\s\-()\[\]]")
_MAX_MEMBERS = 10_000


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


UNSET: Final = _Unset()
"""Marks an optional field that was not supplied (distinct from an explicit None)."""


class LastOwnerError(DomainError):
    def __init__(self) -> None:
        super().__init__("The organisation must keep at least one active Owner")


class OwnerRoleRequiredError(DomainError):
    def __init__(self) -> None:
        super().__init__("Only an Owner can grant the Owner role or change an Owner's membership")


class InvalidPhoneNumberError(DomainError):
    def __init__(self) -> None:
        super().__init__("phone_number must be in E.164 format, e.g. +14155550123")


class InvalidCallPreferenceError(DomainError):
    def __init__(self) -> None:
        super().__init__(f"call_preference must be one of: {', '.join(CALL_PREFERENCES)}")


class InvalidMemberStatusError(DomainError):
    def __init__(self) -> None:
        super().__init__(f"status must be one of: {', '.join(MEMBER_STATUSES)}")


class InvalidAgentStatusError(DomainError):
    def __init__(self) -> None:
        super().__init__(f"status must be one of: {', '.join(AGENT_STATUSES)}")


@dataclass(frozen=True)
class MemberView:
    membership_id: UUID
    user_id: UUID
    name: str
    email: str
    role_id: UUID
    role_name: str
    status: str
    phone_number: str | None
    agent_status: str
    call_preference: str


async def get_owner_role(session: AsyncSession, organisation_id: UUID) -> Role:
    """The org's system Owner role (created with the organisation)."""
    found = await TenantScopedRepository(session, Role, organisation_id).list(
        Role.is_system.is_(True), Role.name == OWNER_ROLE_NAME, limit=1
    )
    if not found:  # every organisation is created with one; fail closed
        raise NotFoundError("Owner role not found")
    return found[0]


async def get_actor_role(session: AsyncSession, organisation_id: UUID, user: User) -> Role | None:
    """The role of the user's ACTIVE membership in the org, or None."""
    found = await TenantScopedRepository(session, Membership, organisation_id).list(
        Membership.user_id == user.id, Membership.status == "active", limit=1
    )
    if not found:
        return None
    return await TenantScopedRepository(session, Role, organisation_id).get(found[0].role_id)


def check_can_grant_role(actor_role: Role | None, role: Role, owner_role: Role) -> None:
    """Granting a role needs every permission it carries; granting Owner additionally needs an Owner."""
    if role.id == owner_role.id and (actor_role is None or actor_role.id != owner_role.id):
        raise OwnerRoleRequiredError()
    ensure_can_grant(actor_role.permissions if actor_role is not None else (), role.permissions)


class MemberService:
    def __init__(self, session: AsyncSession, organisation_id: UUID, events: EventBus) -> None:
        self._session = session
        self._org_id = organisation_id
        self._events = events
        self._memberships = TenantScopedRepository(session, Membership, organisation_id)
        self._roles = TenantScopedRepository(session, Role, organisation_id)

    async def list(self) -> list[MemberView]:
        memberships = await self._memberships.list(limit=_MAX_MEMBERS)
        roles = {r.id: r for r in await self._roles.list(limit=_MAX_MEMBERS)}
        user_ids = [m.user_id for m in memberships]
        users = {
            u.id: u for u in (await self._session.execute(select(User).where(User.id.in_(user_ids)))).scalars()
        } if user_ids else {}
        views = [self._view(m, users[m.user_id], roles[m.role_id]) for m in memberships]
        return sorted(views, key=lambda v: (v.name.lower(), v.email))

    async def update(
        self,
        membership_id: UUID,
        *,
        role_id: UUID | None = None,
        status: str | None = None,
        phone_number: str | None | _Unset = UNSET,
        call_preference: str | _Unset = UNSET,
        actor: User,
    ) -> MemberView:
        membership = await self._memberships.get(membership_id)
        if membership is None:
            raise NotFoundError("Member not found")
        if status is not None and status not in MEMBER_STATUSES:
            raise InvalidMemberStatusError()
        if phone_number is not UNSET and phone_number is not None:
            # Spaces, dashes and brackets are formatting; what is stored is the bare E.164 number.
            phone_number = _PHONE_FORMATTING.sub("", phone_number)
            if not E164_PATTERN.fullmatch(phone_number):
                raise InvalidPhoneNumberError()
        if call_preference is not UNSET and call_preference not in CALL_PREFERENCES:
            raise InvalidCallPreferenceError()
        new_role = None
        if role_id is not None:
            new_role = await self._roles.get(role_id)
            if new_role is None:
                raise NotFoundError("Role not found")

        owner_role = await get_owner_role(self._session, self._org_id)
        actor_role = await get_actor_role(self._session, self._org_id, actor)
        new_role_id = new_role.id if new_role is not None else membership.role_id
        new_status = status if status is not None else membership.status
        was_owner = membership.role_id == owner_role.id
        if was_owner and (new_role_id != membership.role_id or new_status != membership.status):
            if actor_role is None or actor_role.id != owner_role.id:
                raise OwnerRoleRequiredError()
        if new_role is not None and new_role.id != membership.role_id:
            check_can_grant_role(actor_role, new_role, owner_role)
        if new_role_id != membership.role_id or new_status != membership.status:
            # Changing someone's role or status needs every permission they hold now, so a
            # team manager cannot demote or deactivate a member more privileged than themselves.
            current_role = await self._roles.get(membership.role_id)
            assert current_role is not None  # guaranteed by the FK
            ensure_can_grant(actor_role.permissions if actor_role is not None else (), current_role.permissions)
        if was_owner and membership.status == "active" and not (new_role_id == owner_role.id and new_status == "active"):
            await self._ensure_another_active_owner(membership, owner_role)

        changes: dict[str, Any] = {}
        if new_role_id != membership.role_id:
            changes["role_id"] = {"from": str(membership.role_id), "to": str(new_role_id)}
            membership.role_id = new_role_id
        if new_status != membership.status:
            changes["status"] = {"from": membership.status, "to": new_status}
            membership.status = new_status
        if phone_number is not UNSET and phone_number != membership.phone_number:
            changes["phone_number"] = {"from": membership.phone_number, "to": phone_number}
            membership.phone_number = phone_number
        if call_preference is not UNSET and call_preference != membership.call_preference:
            changes["call_preference"] = {"from": membership.call_preference, "to": call_preference}
            membership.call_preference = call_preference
        if changes:
            await self._session.flush()
            await self._events.publish(
                MemberUpdated(
                    organisation_id=self._org_id,
                    actor_user_id=actor.id,
                    membership_id=membership.id,
                    changes=changes,
                )
            )
        return await self._view_of(membership)

    async def _ensure_another_active_owner(self, membership: Membership, owner_role: Role) -> None:
        # Lock every active Owner membership so two concurrent demotions cannot both see "another owner".
        stmt = self._memberships.select(Membership.id, for_update=True).where(
            Membership.role_id == owner_role.id, Membership.status == "active"
        )
        owner_ids = set((await self._session.execute(stmt)).scalars())
        if not owner_ids - {membership.id}:
            raise LastOwnerError()

    async def _view_of(self, membership: Membership) -> MemberView:
        user = await self._session.get(User, membership.user_id)
        role = await self._roles.get(membership.role_id)
        assert user is not None and role is not None  # guaranteed by FKs
        return self._view(membership, user, role)

    @staticmethod
    def _view(m: Membership, user: User, role: Role) -> MemberView:
        return MemberView(
            membership_id=m.id,
            user_id=user.id,
            name=user.name,
            email=user.email,
            role_id=role.id,
            role_name=role.name,
            status=m.status,
            phone_number=m.phone_number,
            agent_status=m.agent_status,
            call_preference=m.call_preference,
        )
