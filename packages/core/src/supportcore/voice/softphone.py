"""Agents' browser softphones: who is registered right now, and the access token that lets a tab receive calls.

A softphone heartbeat marks the membership present for `SOFTPHONE_TTL_SECONDS` in its own presence namespace (separate
from the agent's online presence). The agent token is a local signing operation (no Twilio request); it is never logged.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.config import Settings
from supportcore.db.models import Membership, TelephonyAccount
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.secrets.ports import SecretDecryptError
from supportcore.voice.browser_calls import ACCESS_TOKEN_TTL_SECONDS
from supportcore.voice.errors import VoiceNotAvailableError
from supportcore.voice.telephony.ports import BrowserCalling

if TYPE_CHECKING:
    from supportcore.wiring import Adapters

__all__ = [
    "SOFTPHONE_TTL_SECONDS",
    "AgentCallToken",
    "agent_identity",
    "beat_softphone",
    "clear_softphone",
    "issue_agent_token",
]

SOFTPHONE_TTL_SECONDS = 60


@dataclass(frozen=True)
class AgentCallToken:
    token: str = field(repr=False)
    identity: str
    expires_in: int = ACCESS_TOKEN_TTL_SECONDS


def agent_identity(membership_id: UUID) -> str:
    return f"agent-{membership_id}"


async def beat_softphone(adapters: "Adapters", organisation_id: UUID, membership_id: UUID) -> None:
    await adapters.softphones.beat(organisation_id, membership_id, SOFTPHONE_TTL_SECONDS)


async def clear_softphone(adapters: "Adapters", organisation_id: UUID, membership_id: UUID) -> None:
    await adapters.softphones.clear(organisation_id, membership_id)


async def issue_agent_token(
    session: AsyncSession,
    organisation_id: UUID,
    membership: Membership,
    adapters: "Adapters",
    settings: Settings,  # kept for symmetry with issue_browser_call; nothing agent-specific to read yet
) -> AgentCallToken:
    """An access token with incoming calls allowed. VoiceNotAvailableError without a telephony account or when it
    has no browser calling set up."""
    account = (
        await session.execute(TenantScopedRepository(session, TelephonyAccount, organisation_id).select())
    ).scalar_one_or_none()
    if account is None or not (account.api_key_sid and account.api_key_secret_encrypted and account.twiml_app_sid):
        raise VoiceNotAvailableError("Browser calling is not set up")
    try:
        secret = adapters.cipher.decrypt(account.api_key_secret_encrypted)
    except SecretDecryptError:
        raise VoiceNotAvailableError("Browser calling is not set up") from None
    identity = agent_identity(membership.id)
    token = adapters.telephony.browser_access_token(
        account.account_sid,
        BrowserCalling(api_key_sid=account.api_key_sid, api_key_secret=secret, twiml_app_sid=account.twiml_app_sid),
        identity,
        ACCESS_TOKEN_TTL_SECONDS,
        incoming=True,
    )
    return AgentCallToken(token=token, identity=identity, expires_in=ACCESS_TOKEN_TTL_SECONDS)
