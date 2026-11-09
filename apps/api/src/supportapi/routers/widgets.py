"""Widgets: admin CRUD and the identity secret (channels.manage)."""

from uuid import UUID

from fastapi import APIRouter, Depends, Response

from supportapi.container import Container
from supportapi.deps import (
    OrgContext,
    SessionDep,
    build_widget_service,
    get_container,
    require_any_permission,
    require_permission,
)
from supportapi.schemas.widgets import CreateWidgetRequest, IdentitySecretOut, PreviewTicketOut, UpdateWidgetRequest, WidgetOut
from supportcore.db.models import Widget
from supportcore.rbac.permissions import Permission
from supportcore.widget.service import WidgetService

router = APIRouter(tags=["widgets"])


def snippet(web_origin: str, public_key: str) -> str:
    return f'<script src="{web_origin}/widget/v1.js" data-key="{public_key}" async></script>'


async def widget_out(service: WidgetService, widget: Widget, container: Container) -> WidgetOut:
    return WidgetOut(
        id=widget.id,
        name=widget.name,
        public_key=widget.public_key,
        enabled=widget.enabled,
        allowed_domains=widget.allowed_domains,
        appearance=widget.appearance,
        csat_enabled=widget.csat_enabled,
        identity_required=widget.identity_required,
        identity_secret_last4=widget.identity_secret_last4,  # never the secret itself
        knowledge_base_ids=await service.knowledge_base_ids(widget.id),
        snippet=snippet(container.settings.web_origin, widget.public_key),
        created_at=widget.created_at,
    )


@router.get("/orgs/{org_id}/widgets", response_model=list[WidgetOut])
async def list_widgets(
    session: SessionDep,
    ctx: OrgContext = Depends(require_permission(Permission.CHANNELS_MANAGE)),
    container: Container = Depends(get_container),
) -> list[WidgetOut]:
    service = build_widget_service(session, ctx.organisation.id, container)
    return [await widget_out(service, w, container) for w in await service.list()]


@router.post("/orgs/{org_id}/widgets", response_model=WidgetOut, status_code=201)
async def create_widget(
    session: SessionDep,
    body: CreateWidgetRequest,
    ctx: OrgContext = Depends(require_permission(Permission.CHANNELS_MANAGE)),
    container: Container = Depends(get_container),
) -> WidgetOut:
    service = build_widget_service(session, ctx.organisation.id, container)
    widget = await service.create(body.name, ctx.user)
    await session.refresh(widget)  # server-generated timestamps
    return await widget_out(service, widget, container)


@router.get("/orgs/{org_id}/widgets/{widget_id}", response_model=WidgetOut)
async def get_widget(
    session: SessionDep,
    widget_id: UUID,
    ctx: OrgContext = Depends(require_permission(Permission.CHANNELS_MANAGE)),
    container: Container = Depends(get_container),
) -> WidgetOut:
    service = build_widget_service(session, ctx.organisation.id, container)
    return await widget_out(service, await service.get(widget_id), container)


@router.patch("/orgs/{org_id}/widgets/{widget_id}", response_model=WidgetOut)
async def update_widget(
    session: SessionDep,
    widget_id: UUID,
    body: UpdateWidgetRequest,
    ctx: OrgContext = Depends(require_permission(Permission.CHANNELS_MANAGE)),
    container: Container = Depends(get_container),
) -> WidgetOut:
    service = build_widget_service(session, ctx.organisation.id, container)
    widget = await service.update(widget_id, actor=ctx.user, **body.model_dump(exclude_unset=True))
    await session.refresh(widget)  # onupdate timestamp
    return await widget_out(service, widget, container)


@router.delete("/orgs/{org_id}/widgets/{widget_id}", status_code=204)
async def delete_widget(
    session: SessionDep,
    widget_id: UUID,
    ctx: OrgContext = Depends(require_permission(Permission.CHANNELS_MANAGE)),
    container: Container = Depends(get_container),
) -> Response:
    await build_widget_service(session, ctx.organisation.id, container).delete(widget_id, ctx.user)
    return Response(status_code=204)


@router.post("/orgs/{org_id}/widgets/{widget_id}/identity-secret", response_model=IdentitySecretOut)
async def rotate_identity_secret(
    session: SessionDep,
    widget_id: UUID,
    ctx: OrgContext = Depends(require_permission(Permission.CHANNELS_MANAGE)),
    container: Container = Depends(get_container),
) -> IdentitySecretOut:
    """The only response that ever carries the secret; it cannot be read again."""
    _, secret = await build_widget_service(session, ctx.organisation.id, container).rotate_identity_secret(
        widget_id, ctx.user
    )
    return IdentitySecretOut(secret=secret)


@router.post("/orgs/{org_id}/widgets/{widget_id}/preview-ticket", response_model=PreviewTicketOut)
async def preview_ticket(
    session: SessionDep,
    widget_id: UUID,
    ctx: OrgContext = Depends(require_any_permission(Permission.CHANNELS_MANAGE, Permission.BOT_CONFIGURE)),
    container: Container = Depends(get_container),
) -> PreviewTicketOut:
    """A 5-minute ticket the dashboard hands the widget iframe to start a preview (test) session."""
    widget = await build_widget_service(session, ctx.organisation.id, container).get(widget_id)  # 404 if foreign
    return PreviewTicketOut(ticket=container.widget_tokens.issue_preview_ticket(ctx.organisation.id, widget.id))
