"""What every Twilio webhook shares: the per-request session, the container, the form, and the signature check
(against `voice_public_url + path + query`, with the token of the account the request names). A bad signature is a
403 with an empty body and a log line with the path only. Nothing here logs a phone number or token."""

from collections.abc import AsyncIterator
from typing import Annotated

import structlog
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from supportcore.db.models import TelephonyAccount
from supportcore.db.session import use_current_session
from supportcore.observability.context import bind_organisation
from supportcore.secrets.ports import SecretDecryptError
from supportcore.voice.telephony.ports import TelephonyAccountRef

from supportvoice.container import VoiceContainer

logger = structlog.get_logger()


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request: commit on success (before the response is sent), roll back on error."""
    async with request.app.state.session_factory() as session:
        with use_current_session(session):
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise


SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]


def get_container(request: Request) -> VoiceContainer:
    return request.app.state.container


ContainerDep = Annotated[VoiceContainer, Depends(get_container)]


async def read_form(request: Request) -> dict[str, str]:
    """The flattened form of a Twilio POST. Anything but a urlencoded body is refused (415) before parsing:
    these endpoints are public, and multipart parsing would spool uploads to disk."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="Unsupported media type")
    return flatten_form(await request.form())


def flatten_form(form) -> dict[str, str]:
    """The form as a plain mapping for signature checks. Twilio signs `key + value` for each distinct value of a
    repeated key, values sorted; the signature helper signs `key + value` once, so repeated values are joined with
    the key between them."""
    values: dict[str, set[str]] = {}
    for key, value in form.multi_items():
        if isinstance(value, str):
            values.setdefault(key, set()).add(value)
    return {key: key.join(sorted(vals)) for key, vals in values.items()}


def xml(body: str) -> Response:
    return Response(content=body, media_type="application/xml")


def forbidden(request: Request) -> Response:
    logger.warning("twilio_signature_invalid", path=request.url.path)
    return Response(status_code=403)


def signed_url(container: VoiceContainer, request: Request) -> str | None:
    base = container.settings.voice_public_url
    if not base:
        return None
    query = request.url.query
    return f"{base}{request.url.path}" + (f"?{query}" if query else "")


def signature_ok(container: VoiceContainer, request: Request, auth_token: str, params: dict[str, str]) -> bool:
    url = signed_url(container, request)
    signature = request.headers.get("X-Twilio-Signature", "")
    if url is None or not signature:
        return False
    return container.adapters.telephony.is_valid_signature(auth_token, url, params, signature)


def auth_token(container: VoiceContainer, account: TelephonyAccount) -> str | None:
    try:
        return container.adapters.cipher.decrypt(account.auth_token_encrypted)
    except SecretDecryptError:
        logger.warning("telephony_token_unreadable", account_id=str(account.id))
        return None


async def verified_account(
    session: AsyncSession, container: VoiceContainer, request: Request, params: dict[str, str]
) -> TelephonyAccount | None:
    """The account named by the form's `AccountSid` when the signature checks out against its token, else None."""
    # Deliberately not tenant-scoped: the Twilio AccountSid is the public identifier that tells us the organisation,
    # exactly like the widget's public_key. The signature (below) then proves the request is Twilio's.
    account = (
        await session.execute(select(TelephonyAccount).where(TelephonyAccount.account_sid == params.get("AccountSid", "")))
    ).scalar_one_or_none()
    if account is None:
        return None
    token = auth_token(container, account)
    if token is None or not signature_ok(container, request, token, params):
        return None
    bind_organisation(account.organisation_id)
    return account


def account_ref(container: VoiceContainer, account: TelephonyAccount) -> TelephonyAccountRef | None:
    """The account as the telephony port takes it (None when its token cannot be read)."""
    token = auth_token(container, account)
    return TelephonyAccountRef(account.account_sid, token) if token is not None else None
