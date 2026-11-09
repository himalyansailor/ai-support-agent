"""The chat model an organisation's bot (and the playground) talks to.

`load_chat_setup` is database-only (the AI settings, the decrypted key, the organisation) and returns
plain values, so the caller can end its transaction before any provider call. `build_chat_model`
SSRF-checks a custom base URL and creates the LangChain model. Keys never go into logs or spans.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from langchain_core.language_models.chat_models import BaseChatModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.ai.base_url import ensure_public_base_url
from supportcore.ai.credentials import CredentialService
from supportcore.ai.settings_service import OrganisationAISettingsService
from supportcore.config import Settings
from supportcore.db.models import Organisation, ProviderCredential
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import NotFoundError
from supportcore.events import EventBus

if TYPE_CHECKING:
    from supportcore.wiring import Adapters


@dataclass
class ChatSetup:
    """The chat model call as read from the database. Holds the decrypted key: keep it only for the
    duration of one run and never log it."""

    provider: str
    model: str
    temperature: float
    api_key: str | None
    base_url: str | None
    organisation_name: str
    language: str

    def __repr__(self) -> str:
        return (
            f"ChatSetup(provider={self.provider!r}, model={self.model!r}, temperature={self.temperature!r}, "
            f"api_key={'***' if self.api_key else None}, base_url={self.base_url!r}, "
            f"organisation_name={self.organisation_name!r}, language={self.language!r})"
        )


async def load_chat_setup(
    session: AsyncSession, organisation_id: UUID, adapters: "Adapters", settings: Settings, events: EventBus
) -> ChatSetup | None:
    """Database reads only. None when the bot is not ready (no chat model, or its key is missing)."""
    ai = OrganisationAISettingsService(session, organisation_id, events, include_fake=settings.env == "test")
    row = await ai.get()
    if not await ai.is_ready(row):
        return None
    api_key: str | None = None
    base_url: str | None = None
    if row.chat_credential_id is not None:
        credential = await TenantScopedRepository(session, ProviderCredential, organisation_id).get(
            row.chat_credential_id
        )
        if credential is None:  # is_ready() just saw it; deleted in between
            raise NotFoundError("The chat model's API key was removed")
        api_key = CredentialService(
            session, organisation_id, adapters.cipher, adapters.credential_validator, events
        ).decrypt_key(credential)
        base_url = credential.base_url
    organisation = (
        await session.execute(select(Organisation).where(Organisation.id == organisation_id))
    ).scalar_one_or_none()
    if organisation is None:
        raise NotFoundError("Organisation not found")
    return ChatSetup(
        provider=row.chat_provider,
        model=row.chat_model,
        temperature=row.temperature,
        api_key=api_key,
        base_url=base_url,
        organisation_name=organisation.name,
        language=organisation.language,
    )


async def check_chat_base_url(settings: Settings, setup: ChatSetup) -> None:
    """The SSRF pre-check: raises CredentialInvalidError for a base URL that is not allowed."""
    if setup.base_url:
        await ensure_public_base_url(setup.base_url, allow_private=settings.effective_allow_private_base_urls())


def create_chat_model(
    adapters: "Adapters", setup: ChatSetup, *, temperature: float | None = None, timeout: float
) -> BaseChatModel:
    """Creates the model without the SSRF pre-check (callers run `check_chat_base_url` first)."""
    return adapters.chat_models.create(
        setup.provider,
        setup.model,
        api_key=setup.api_key,
        base_url=setup.base_url,
        temperature=setup.temperature if temperature is None else temperature,
        timeout=timeout,
    )


async def build_chat_model(
    adapters: "Adapters", settings: Settings, setup: ChatSetup, *, temperature: float | None = None, timeout: float
) -> BaseChatModel:
    """`check_chat_base_url` then `create_chat_model`. `temperature` overrides the organisation's."""
    await check_chat_base_url(settings, setup)
    return create_chat_model(adapters, setup, temperature=temperature, timeout=timeout)
