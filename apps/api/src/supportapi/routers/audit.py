from fastapi import APIRouter, Depends, Query
from sqlalchemy import tuple_

from supportapi.deps import OrgContext, SessionDep, require_permission
from supportapi.pagination import decode_cursor, encode_cursor
from supportapi.schemas.audit import ActorOut, AuditLogOut, AuditPage
from supportcore.db.models import AuditLog, User
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.rbac.permissions import Permission

router = APIRouter(tags=["audit"])


@router.get("/orgs/{org_id}/audit-logs", response_model=AuditPage)
async def list_audit_logs(
    session: SessionDep,
    limit: int = Query(50, ge=1, le=100),
    before: str | None = Query(None, max_length=200, description="next_before from the previous page"),
    ctx: OrgContext = Depends(require_permission(Permission.ANALYTICS_VIEW)),
) -> AuditPage:
    stmt = (
        TenantScopedRepository(session, AuditLog, ctx.organisation.id)
        .select(AuditLog, User.name)
        .outerjoin(User, User.id == AuditLog.actor_user_id)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit + 1)  # one extra row tells us whether another page exists
    )
    if before is not None:
        created_at, id = decode_cursor(before)
        # Keyset on (created_at, id), matching the sort, so rows sharing a timestamp are never skipped.
        stmt = stmt.where(tuple_(AuditLog.created_at, AuditLog.id) < tuple_(created_at, id))
    rows = (await session.execute(stmt)).all()
    has_more = len(rows) > limit
    items = [
        AuditLogOut(
            id=log.id,
            action=log.action,
            actor=ActorOut(id=log.actor_user_id, name=name) if log.actor_user_id and name is not None else None,
            target_type=log.target_type,
            target_id=log.target_id,
            metadata=log.metadata_,
            created_at=log.created_at,
        )
        for log, name in rows[:limit]
    ]
    next_before = encode_cursor(items[-1].created_at, items[-1].id) if has_more else None
    return AuditPage(items=items, next_before=next_before)
