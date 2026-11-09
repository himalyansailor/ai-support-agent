"""Building a knowledge-base version's embedder: the one place an embedding API key is decrypted.

Steps, shared by indexing and retrieval: look up the version's credential (org-scoped) and decrypt it,
refuse when the model needs a key that is gone, re-check an openai_compatible base URL (SSRF guard)
right before it is used, then create the embedder. The key is only handed to the adapter.
"""

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.ai.base_url import ensure_public_base_url
from supportcore.ai.catalog import UnknownModelError, get_embedding_spec
from supportcore.ai.credentials import CredentialService
from supportcore.ai.ports import Embedder, EmbeddingError
from supportcore.config import Settings
from supportcore.db.models import ProviderCredential
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.events import EventBus
from supportcore.knowledge.kb_service import VersionConfig

if TYPE_CHECKING:
    from supportcore.wiring import Adapters

EMBEDDING_KEY_REMOVED = "The API key for this knowledge base's embedding model was removed"


def requires_credential(config: VersionConfig, *, include_fake: bool) -> bool:
    try:
        return get_embedding_spec(config.provider, config.model, include_fake=include_fake).requires_credential
    except UnknownModelError:
        return False  # the factory reports unknown models


async def embedding_key(
    session: AsyncSession,
    organisation_id: UUID,
    adapters: "Adapters",
    settings: Settings,
    events: EventBus,
    config: VersionConfig,
) -> tuple[str | None, str | None]:
    """(api_key, base_url) for `config`; EmbeddingError(EMBEDDING_KEY_REMOVED) when a needed key is gone."""
    if config.credential_id is None:
        if requires_credential(config, include_fake=settings.env == "test"):
            raise EmbeddingError(EMBEDDING_KEY_REMOVED)
        return None, None
    credential = await TenantScopedRepository(session, ProviderCredential, organisation_id).get(config.credential_id)
    if credential is None:
        raise EmbeddingError(EMBEDDING_KEY_REMOVED)
    api_key = CredentialService(
        session, organisation_id, adapters.cipher, adapters.credential_validator, events
    ).decrypt_key(credential)
    return api_key, credential.base_url


async def create_embedder(
    adapters: "Adapters", settings: Settings, config: VersionConfig, api_key: str | None, base_url: str | None
) -> Embedder:
    """SSRF-checks `base_url` (awaited before anything is sent to it), then creates the embedder."""
    if base_url:
        await ensure_public_base_url(base_url, allow_private=settings.effective_allow_private_base_urls())
    return adapters.embedders.create(config.provider, config.model, api_key=api_key, base_url=base_url, settings=settings)

