"""Which agents can take a conversation right now (Global Constraints: Agent availability)."""

from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.db.models import Membership, Role
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.events import EventBus
from supportcore.presence.ports import AgentPresence
from supportcore.rbac.checker import has_permission
from supportcore.rbac.permissions import Permission
from supportcore.support.calendar import is_open
from supportcore.support.settings_service import SupportSettingsService

_MAX_MEMBERS = 10_000


async def available_agents(
    session: AsyncSession,
    organisation_id: UUID,
    presence: AgentPresence,
    now: datetime,
    present: set[UUID] | None = None,
) -> list[Membership]:
    """Active, online, present members whose role has queries.answer, when the org is within business hours.

    `present` may be passed when the caller already read it (one presence read per request).

    Ordered by `created_at`, then `id`, the order round-robin walks.
    """
    calendar = await SupportSettingsService(session, organisation_id, EventBus()).calendar()
    if not is_open(calendar, now):
        return []
    if present is None:
        present = await presence.present(organisation_id)
    if not present:
        return []
    memberships = TenantScopedRepository(session, Membership, organisation_id)
    candidates = await memberships.list(
        Membership.status == "active",
        Membership.agent_status == "online",
        Membership.id.in_(present),
        order_by=[Membership.created_at, Membership.id],
        limit=_MAX_MEMBERS,
    )
    roles = TenantScopedRepository(session, Role, organisation_id)
    permissions = {
        role.id: role.permissions
        for role in await roles.list(Role.id.in_({m.role_id for m in candidates}), limit=_MAX_MEMBERS)
    }
    return [m for m in candidates if has_permission(permissions.get(m.role_id, ()), Permission.QUERIES_ANSWER)]
