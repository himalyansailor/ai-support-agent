from fastapi import APIRouter, Depends
from sqlalchemy import select

from supportapi.deps import SessionDep, get_current_user
from supportapi.schemas.auth import MeResponse, MembershipOut, UserOut
from supportcore.db.models import Membership, Organisation, Role, User

router = APIRouter(tags=["me"])


@router.get("/me", response_model=MeResponse)
async def me(session: SessionDep, user: User = Depends(get_current_user)) -> MeResponse:
    # User-scoped (spans the user's own organisations), so not a TenantScopedRepository query.
    stmt = (
        select(Membership.organisation_id, Organisation.name, Role.name, Role.permissions)
        .join(Organisation, Organisation.id == Membership.organisation_id)
        .join(Role, (Role.id == Membership.role_id) & (Role.organisation_id == Membership.organisation_id))
        .where(Membership.user_id == user.id, Membership.status == "active")
        .order_by(Organisation.name)
    )
    rows = (await session.execute(stmt)).all()
    memberships = [
        MembershipOut(organisation_id=org_id, organisation_name=org_name, role_name=role_name, permissions=perms)
        for org_id, org_name, role_name, perms in rows
    ]
    return MeResponse(user=UserOut.model_validate(user), memberships=memberships)
