"""The integration error log: failures of models, embedders, rerankers, tools and the classifier, counted per
UTC hour (analytics.view). `from` / `to` are dates in the organisation's support timezone, both inclusive;
they filter on `last_seen_at`. Newest `last_seen_at` first."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import tuple_

from supportapi.container import Container
from supportapi.deps import OrgContext, SessionDep, build_support_settings_service, get_container, require_permission
from supportapi.pagination import decode_cursor, encode_cursor
from supportapi.schemas.integration_errors import IntegrationErrorOut, IntegrationErrorPage
from supportcore.analytics.definitions import date_range_utc
from supportcore.analytics.integration_errors import KINDS
from supportcore.db.models import IntegrationEvent
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.rbac.permissions import Permission

router = APIRouter(tags=["integration-errors"])


@router.get("/orgs/{org_id}/integration-errors", response_model=IntegrationErrorPage)
async def list_integration_errors(
    session: SessionDep,
    from_: date | None = Query(None, alias="from"),
    to: date | None = Query(None),
    kind: str | None = Query(None, max_length=16),
    limit: int = Query(50, ge=1, le=100),
    before: str | None = Query(None, max_length=200, description="next_before from the previous page"),
    ctx: OrgContext = Depends(require_permission(Permission.ANALYTICS_VIEW)),
    container: Container = Depends(get_container),
) -> IntegrationErrorPage:
    if kind is not None and kind not in KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of: {', '.join(KINDS)}")
    settings = await build_support_settings_service(session, ctx.organisation.id, container).get()
    lo, hi = date_range_utc(settings.timezone, from_, to)
    stmt = (
        TenantScopedRepository(session, IntegrationEvent, ctx.organisation.id)
        .select(IntegrationEvent)
        .where(IntegrationEvent.last_seen_at >= lo, IntegrationEvent.last_seen_at < hi)
        .order_by(IntegrationEvent.last_seen_at.desc(), IntegrationEvent.id.desc())
        .limit(limit + 1)
    )
    if kind is not None:
        stmt = stmt.where(IntegrationEvent.kind == kind)
    if before is not None:
        seen_at, id = decode_cursor(before)
        stmt = stmt.where(tuple_(IntegrationEvent.last_seen_at, IntegrationEvent.id) < tuple_(seen_at, id))
    rows = list((await session.execute(stmt)).scalars())
    has_more = len(rows) > limit
    items = [IntegrationErrorOut.model_validate(r, from_attributes=True) for r in rows[:limit]]
    next_before = encode_cursor(items[-1].last_seen_at, items[-1].id) if has_more else None
    return IntegrationErrorPage(items=items, next_before=next_before)
