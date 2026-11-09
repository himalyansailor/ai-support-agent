from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.db.models import Membership, Role, User
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import DomainError, NotFoundError
from supportcore.events import EventBus
from supportcore.rbac.checker import ensure_can_grant
from supportcore.rbac.defaults import DEFAULT_ROLES
from supportcore.rbac.permissions import validate_permissions
from supportcore.roles.events import RoleCreated, RoleDeleted, RoleUpdated

_SYSTEM_ORDER = {name: i for i, name in enumerate(DEFAULT_ROLES)}


class SystemRoleImmutableError(DomainError):
    def __init__(self) -> None:
        super().__init__("System roles cannot be changed or deleted")


class RoleInUseError(DomainError):
    def __init__(self, member_count: int) -> None:
        self.member_count = member_count
        super().__init__(f"Role is assigned to {member_count} member(s)")


class RoleNameTakenError(DomainError):
    def __init__(self) -> None:
        super().__init__("A role with this name already exists")


def _clean_permissions(values: Iterable[str]) -> list[str]:
    """Validate, then dedupe preserving order."""
    return list(dict.fromkeys(p.value for p in validate_permissions(values)))


class RoleService:
    def __init__(self, session: AsyncSession, organisation_id: UUID, events: EventBus) -> None:
        self._session = session
        self._org_id = organisation_id
        self._events = events
        self._roles = TenantScopedRepository(session, Role, organisation_id)

    async def list(self) -> list[Role]:
        roles = await self._roles.list(limit=1000)
        return sorted(roles, key=lambda r: (not r.is_system, _SYSTEM_ORDER.get(r.name, 0), r.name))

    async def member_counts(self) -> dict[UUID, int]:
        memberships = TenantScopedRepository(self._session, Membership, self._org_id)
        stmt = memberships.select(Membership.role_id, func.count()).group_by(Membership.role_id)
        return {role_id: n for role_id, n in (await self._session.execute(stmt)).all()}

    async def create(
        self, name: str, permissions: Iterable[str], actor: User, *, actor_permissions: Iterable[str]
    ) -> Role:
        name = name.strip()
        perms = _clean_permissions(permissions)
        ensure_can_grant(actor_permissions, perms)
        await self._ensure_name_free(name)
        role = Role(name=name, permissions=perms, is_system=False)
        await self._flush_unique(role)
        await self._events.publish(
            RoleCreated(
                organisation_id=self._org_id,
                actor_user_id=actor.id,
                role_id=role.id,
                role_name=role.name,
                permissions=list(role.permissions),
            )
        )
        return role

    async def update(
        self,
        role_id: UUID,
        name: str | None,
        permissions: Iterable[str] | None,
        actor: User,
        *,
        actor_permissions: Iterable[str],
    ) -> Role:
        role = await self._get_mutable(role_id)
        ensure_can_grant(actor_permissions, role.permissions)  # cannot edit a role above one's own
        if name is not None:
            name = name.strip()
            await self._ensure_name_free(name, exclude=role.id)
        perms = _clean_permissions(permissions) if permissions is not None else None
        if perms is not None:
            ensure_can_grant(actor_permissions, perms)
        if name is not None:
            role.name = name
        if perms is not None:
            role.permissions = perms
        await self._flush_unique()
        await self._events.publish(
            RoleUpdated(
                organisation_id=self._org_id,
                actor_user_id=actor.id,
                role_id=role.id,
                role_name=role.name,
                permissions=list(role.permissions),
            )
        )
        return role

    async def delete(self, role_id: UUID, actor: User, *, actor_permissions: Iterable[str]) -> None:
        role = await self._get_mutable(role_id)
        ensure_can_grant(actor_permissions, role.permissions)  # cannot delete a role above one's own
        in_use = (await self.member_counts()).get(role.id, 0)
        if in_use:
            raise RoleInUseError(in_use)
        event = RoleDeleted(
            organisation_id=self._org_id,
            actor_user_id=actor.id,
            role_id=role.id,
            role_name=role.name,
            permissions=list(role.permissions),
        )
        await self._roles.delete(role)
        await self._events.publish(event)

    async def _get_mutable(self, role_id: UUID) -> Role:
        role = await self._roles.get(role_id)
        if role is None:
            raise NotFoundError("Role not found")
        if role.is_system:
            raise SystemRoleImmutableError()
        return role

    async def _ensure_name_free(self, name: str, exclude: UUID | None = None) -> None:
        stmt = self._roles.select(Role.id).where(func.lower(Role.name) == name.lower())
        if exclude is not None:
            stmt = stmt.where(Role.id != exclude)
        if (await self._session.execute(stmt)).first() is not None:
            raise RoleNameTakenError()

    async def _flush_unique(self, new: Role | None = None) -> None:
        """Flush inside a savepoint so a lost race on the unique constraint becomes RoleNameTakenError."""
        try:
            async with self._session.begin_nested():
                if new is not None:
                    await self._roles.add(new)
                await self._session.flush()
        except IntegrityError:
            raise RoleNameTakenError() from None
