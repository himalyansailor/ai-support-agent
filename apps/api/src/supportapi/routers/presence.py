"""Agent status (own) and the org's agent list with presence and availability."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from supportapi.container import Container
from supportapi.deps import OrgContext, SessionDep, get_container, require_any_permission, require_permission
from supportcore.assignment.availability import available_agents
from supportcore.db.models import Membership, Role, User
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.presence.events import set_agent_status
from supportcore.rbac.checker import has_permission
from supportcore.rbac.permissions import Permission

router = APIRouter(tags=["presence"])


class AgentStatusRequest(BaseModel):
    status: str  # validated by the domain (422 invalid_agent_status)


class AgentStatusOut(BaseModel):
    membership_id: UUID
    agent_status: str


class AgentOut(BaseModel):
    membership_id: UUID
    name: str
    agent_status: str
    present: bool
    available: bool


@router.put("/orgs/{org_id}/me/agent-status", response_model=AgentStatusOut)
async def put_agent_status(
    session: SessionDep,
    body: AgentStatusRequest,
    ctx: OrgContext = Depends(require_permission(Permission.QUERIES_ANSWER)),
    container: Container = Depends(get_container),
) -> AgentStatusOut:
    if ctx.membership is None:  # a platform admin who is not a member has no status to set
        raise HTTPException(status_code=403, detail="Not a member of this organisation")
    await set_agent_status(session, ctx.membership, body.status, container.adapters.pubsub)
    return AgentStatusOut(membership_id=ctx.membership.id, agent_status=ctx.membership.agent_status)


@router.get("/orgs/{org_id}/agents", response_model=list[AgentOut])
async def list_agents(
    session: SessionDep,
    ctx: OrgContext = Depends(require_any_permission(Permission.QUERIES_ANSWER, Permission.TEAM_MANAGE)),
    container: Container = Depends(get_container),
) -> list[AgentOut]:
    """Active members whose role can answer queries, oldest first."""
    org_id = ctx.organisation.id
    members = TenantScopedRepository(session, Membership, org_id)
    stmt = (
        members.select(Membership, User.name, Role.permissions)
        .join(User, User.id == Membership.user_id)
        .join(Role, (Role.id == Membership.role_id) & (Role.organisation_id == Membership.organisation_id))
        .where(Membership.status == "active")
        .order_by(Membership.created_at, Membership.id)
    )
    rows = [r for r in (await session.execute(stmt)).all() if has_permission(r[2], Permission.QUERIES_ANSWER)]
    presence = container.adapters.presence
    present = await presence.present(org_id)
    available = {m.id for m in await available_agents(session, org_id, presence, datetime.now(UTC), present=present)}
    return [
        AgentOut(
            membership_id=m.id, name=name, agent_status=m.agent_status, present=m.id in present, available=m.id in available
        )
        for m, name, _ in rows
    ]
