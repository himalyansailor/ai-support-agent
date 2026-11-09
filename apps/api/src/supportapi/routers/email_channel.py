"""The support mailbox: IMAP/SMTP settings, connection test and sync now (channels.manage)."""

from fastapi import APIRouter, Depends, Response

from supportapi.container import Container
from supportapi.deps import OrgContext, SessionDep, build_mailbox_service, get_container, require_permission
from supportapi.schemas.email_channel import ConnectionTestOut, MailboxOut, SaveMailboxRequest, TestMailboxRequest
from supportcore.db.models import EmailMailbox
from supportcore.rbac.permissions import Permission
from supportcore.ratelimit.ports import enforce

router = APIRouter(tags=["email-channel"])

SYNC_NOW_PER_ORG = (1, 10)  # one per 10 seconds
TEST_PER_ORG = (5, 60)


def mailbox_out(box: EmailMailbox) -> MailboxOut:
    return MailboxOut(
        **{name: getattr(box, name) for name in MailboxOut.model_fields if not name.endswith("_password_set")},
        imap_password_set=bool(box.imap_password_encrypted),
        smtp_password_set=bool(box.smtp_password_encrypted),
    )


@router.get("/orgs/{org_id}/channels/email", response_model=MailboxOut | None)
async def get_mailbox(
    session: SessionDep,
    ctx: OrgContext = Depends(require_permission(Permission.CHANNELS_MANAGE)),
    container: Container = Depends(get_container),
) -> MailboxOut | None:
    box = await build_mailbox_service(session, ctx.organisation.id, container).get()
    return mailbox_out(box) if box is not None else None


@router.put("/orgs/{org_id}/channels/email", response_model=MailboxOut)
async def save_mailbox(
    session: SessionDep,
    body: SaveMailboxRequest,
    ctx: OrgContext = Depends(require_permission(Permission.CHANNELS_MANAGE)),
    container: Container = Depends(get_container),
) -> MailboxOut:
    service = build_mailbox_service(session, ctx.organisation.id, container)
    return mailbox_out(await service.save(ctx.user, **body.model_dump(exclude_unset=True)))


@router.delete("/orgs/{org_id}/channels/email", status_code=204)
async def disconnect_mailbox(
    session: SessionDep,
    ctx: OrgContext = Depends(require_permission(Permission.CHANNELS_MANAGE)),
    container: Container = Depends(get_container),
) -> Response:
    await build_mailbox_service(session, ctx.organisation.id, container).disconnect(ctx.user)
    return Response(status_code=204)


@router.post("/orgs/{org_id}/channels/email/test", response_model=ConnectionTestOut)
async def test_mailbox(
    session: SessionDep,
    body: TestMailboxRequest,
    ctx: OrgContext = Depends(require_permission(Permission.CHANNELS_MANAGE)),
    container: Container = Depends(get_container),
) -> ConnectionTestOut:
    await enforce(container.adapters.rate_limiter, f"org:{ctx.organisation.id}:mail-test", TEST_PER_ORG)
    service = build_mailbox_service(session, ctx.organisation.id, container)
    return ConnectionTestOut(**await service.test(body.model_dump(exclude_none=True)))


@router.post("/orgs/{org_id}/channels/email/sync", status_code=202)
async def sync_mailbox_now(
    session: SessionDep,
    ctx: OrgContext = Depends(require_permission(Permission.CHANNELS_MANAGE)),
    container: Container = Depends(get_container),
) -> Response:
    await enforce(container.adapters.rate_limiter, f"org:{ctx.organisation.id}:mail-sync", SYNC_NOW_PER_ORG)
    await build_mailbox_service(session, ctx.organisation.id, container).request_sync()
    return Response(status_code=202)
