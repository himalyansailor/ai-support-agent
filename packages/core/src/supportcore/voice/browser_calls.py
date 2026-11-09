"""Browser (widget) calls: whether a widget can offer one, and the access token + call ticket that start it.

The access token lets the customer's browser register with Twilio Voice (identity `customer-{customer_id}`); the
ticket rides along as the call's `ticket` parameter, and the voice process's browser webhook verifies it (see
`voice.tokens`). Building the access token is a local signing operation: no Twilio request, so the caller's database
session may stay open. Neither the token nor the ticket is ever logged.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.config import Settings
from supportcore.db.models import TelephonyAccount
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.events import EventBus
from supportcore.secrets.ports import SecretDecryptError
from supportcore.voice.errors import VoiceNotAvailableError
from supportcore.voice.settings_service import VoiceSettingsService
from supportcore.voice.telephony.ports import BrowserCalling
from supportcore.voice.tokens import issue_call_ticket

if TYPE_CHECKING:
    from supportcore.wiring import Adapters

__all__ = ["ACCESS_TOKEN_TTL_SECONDS", "BrowserCallToken", "browser_calls_available", "issue_browser_call"]

ACCESS_TOKEN_TTL_SECONDS = 600


@dataclass(frozen=True)
class BrowserCallToken:
    token: str = field(repr=False)
    ticket: str = field(repr=False)
    expires_in: int = ACCESS_TOKEN_TTL_SECONDS


def _service(session: AsyncSession, organisation_id: UUID, settings: Settings, events: EventBus) -> VoiceSettingsService:
    return VoiceSettingsService(
        session,
        organisation_id,
        events,
        include_fake=settings.env == "test",
        voice_public_url=settings.voice_public_url,
    )


async def browser_calls_available(
    session: AsyncSession, organisation_id: UUID, widget_id: UUID, settings: Settings, events: EventBus
) -> bool:
    """Browser calling is ready, and not limited to a different widget (`browser_widget_id` NULL = every widget)."""
    service = _service(session, organisation_id, settings, events)
    # Read-only (public routes): an organisation without a settings row has voice disabled.
    if not (await service.readiness(create=False)).browser:
        return False
    row = await service._load(False)  # readiness just saw it
    return row is not None and row.browser_widget_id in (None, widget_id)


async def issue_browser_call(
    session: AsyncSession,
    organisation_id: UUID,
    widget_id: UUID,
    customer_id: UUID,
    adapters: "Adapters",
    settings: Settings,
    events: EventBus,
) -> BrowserCallToken:
    """VoiceNotAvailableError unless browser calls are ready for this widget."""
    if not await browser_calls_available(session, organisation_id, widget_id, settings, events):
        raise VoiceNotAvailableError("Voice calls are not available right now")
    account = (
        await session.execute(TenantScopedRepository(session, TelephonyAccount, organisation_id).select())
    ).scalar_one_or_none()
    if account is None or not (account.api_key_sid and account.api_key_secret_encrypted and account.twiml_app_sid):
        raise VoiceNotAvailableError("Voice calls are not available right now")
    try:
        secret = adapters.cipher.decrypt(account.api_key_secret_encrypted)
    except SecretDecryptError:
        raise VoiceNotAvailableError("Voice calls are not available right now") from None
    token = adapters.telephony.browser_access_token(
        account.account_sid,
        BrowserCalling(api_key_sid=account.api_key_sid, api_key_secret=secret, twiml_app_sid=account.twiml_app_sid),
        f"customer-{customer_id}",
        ACCESS_TOKEN_TTL_SECONDS,
    )
    ticket, _jti = issue_call_ticket(settings, organisation_id, widget_id, customer_id)
    return BrowserCallToken(token=token, ticket=ticket)
