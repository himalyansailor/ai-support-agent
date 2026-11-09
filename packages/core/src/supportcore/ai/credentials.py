"""Org-provided LLM/embedding API keys.

Keys are encrypted with the SecretCipher port before they reach the database. The plaintext key
never appears in events, audit metadata, logs or error messages; callers only ever see `last4`.
"""

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.ai.base_url import ensure_public_base_url
from supportcore.ai.catalog import CREDENTIAL_PROVIDERS
from supportcore.ai.errors import CredentialInvalidError  # re-exported: callers import it from here
from supportcore.ai.events import CredentialCreated, CredentialDeleted
from supportcore.ai.sanitize import sanitize_provider_message
from supportcore.db.models import KnowledgeBase, OrganisationAISettings, ProviderCredential, User, VoiceSettings
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import DomainError, NotFoundError
from supportcore.events import EventBus
from supportcore.observability.logging import get_logger
from supportcore.secrets.ports import SecretCipher, SecretDecryptError

logger = get_logger(__name__)

MIN_KEY_LENGTH = 8
MAX_LABEL_LENGTH = 100
MAX_BASE_URL_LENGTH = 2048


class CredentialInUseError(DomainError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Credential is in use by {reason}")


class CredentialLabelTakenError(DomainError):
    def __init__(self) -> None:
        super().__init__("A credential with this label already exists")


class CredentialUnreadableError(DomainError):
    """The stored key cannot be decrypted (SUPPORT_SECRETS_KEY changed or the ciphertext is corrupt)."""

    def __init__(self) -> None:
        super().__init__("This API key can't be decrypted; delete it and add it again.")


class CredentialValidator(Protocol):
    async def validate(self, provider: str, api_key: str, base_url: str | None) -> None:
        """Checks the key against the provider; raises CredentialInvalidError (sanitised message)."""
        ...


# (session, organisation_id, credential_id) -> a description of what uses it, or None.
InUseCheck = Callable[[AsyncSession, UUID, UUID], Awaitable[str | None]]


async def ai_settings_in_use(session: AsyncSession, organisation_id: UUID, credential_id: UUID) -> str | None:
    settings = TenantScopedRepository(session, OrganisationAISettings, organisation_id)
    stmt = settings.select(OrganisationAISettings.organisation_id).where(
        OrganisationAISettings.chat_credential_id == credential_id
    )
    return "AI settings" if (await session.execute(stmt)).first() is not None else None


async def knowledge_base_in_use(session: AsyncSession, organisation_id: UUID, credential_id: UUID) -> str | None:
    """A KB embeds with the credential, for its served version or the one being rebuilt."""
    kbs = TenantScopedRepository(session, KnowledgeBase, organisation_id)
    stmt = (
        kbs.select(KnowledgeBase.name)
        .where(
            or_(
                KnowledgeBase.embedding_credential_id == credential_id,
                KnowledgeBase.building_embedding_credential_id == credential_id,
            )
        )
        .order_by(KnowledgeBase.created_at, KnowledgeBase.id)
        .limit(1)
    )
    name = (await session.execute(stmt)).scalar_one_or_none()
    return f"knowledge base '{name}'" if name is not None else None


async def voice_settings_in_use(session: AsyncSession, organisation_id: UUID, credential_id: UUID) -> str | None:
    settings = TenantScopedRepository(session, VoiceSettings, organisation_id)
    stmt = settings.select(VoiceSettings.organisation_id).where(
        or_(
            VoiceSettings.stt_credential_id == credential_id,
            VoiceSettings.tts_credential_id == credential_id,
            VoiceSettings.realtime_credential_id == credential_id,
        )
    )
    return "voice settings" if (await session.execute(stmt)).first() is not None else None


# Always run on delete; other features can add theirs via `in_use_checks`.
BUILTIN_IN_USE_CHECKS: tuple[InUseCheck, ...] = (ai_settings_in_use, knowledge_base_in_use, voice_settings_in_use)


def _clean_base_url(provider: str, base_url: str | None) -> str | None:
    if provider != "openai_compatible":
        return None  # hosted providers always use their official endpoint
    url = (base_url or "").strip()
    parts = urlsplit(url) if url else None
    if (
        parts is None
        or parts.scheme not in ("http", "https")
        or not parts.hostname
        or any(c.isspace() for c in url)
        or len(url) > MAX_BASE_URL_LENGTH
    ):
        raise CredentialInvalidError("A valid http(s) base URL is required for OpenAI-compatible providers")
    if "@" in parts.netloc:  # credentials in the URL would be sent to (and logged by) the endpoint
        raise CredentialInvalidError("The base URL must not contain a user name or password")
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise CredentialInvalidError("The base URL must not contain a query string or fragment")
    try:
        parts.port
    except ValueError:
        raise CredentialInvalidError("The base URL has an invalid port") from None
    return url


class CredentialService:
    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        cipher: SecretCipher,
        validator: CredentialValidator,
        events: EventBus,
        *,
        in_use_checks: Sequence[InUseCheck] = (),
        allow_private_base_urls: bool = False,
    ) -> None:
        self._session = session
        self._org_id = organisation_id
        self._cipher = cipher
        self._validator = validator
        self._events = events
        self._in_use_checks = (*BUILTIN_IN_USE_CHECKS, *in_use_checks)
        # Settings.effective_allow_private_base_urls(); False refuses private/loopback base URLs (SSRF).
        self._allow_private_base_urls = allow_private_base_urls
        self._credentials = TenantScopedRepository(session, ProviderCredential, organisation_id)

    async def list(self) -> list[ProviderCredential]:
        return await self._credentials.list(
            order_by=(ProviderCredential.created_at, ProviderCredential.id), limit=1000
        )

    async def get(self, credential_id: UUID) -> ProviderCredential:
        credential = await self._credentials.get(credential_id)
        if credential is None:
            raise NotFoundError("Credential not found")
        return credential

    async def create(
        self, provider: str, label: str, api_key: str, base_url: str | None, actor: User
    ) -> ProviderCredential:
        provider = provider.strip()
        if provider not in CREDENTIAL_PROVIDERS:
            raise CredentialInvalidError(f"Unsupported provider: {provider[:50]}")
        label = label.strip()
        if not label:
            raise CredentialInvalidError("Label is required")
        if len(label) > MAX_LABEL_LENGTH:
            raise CredentialInvalidError(f"Label must be at most {MAX_LABEL_LENGTH} characters")
        key = api_key.strip()
        if not key:
            raise CredentialInvalidError("API key is required")
        if len(key) < MIN_KEY_LENGTH:
            raise CredentialInvalidError("API key looks too short")
        url = _clean_base_url(provider, base_url)
        if url is not None:
            await ensure_public_base_url(url, allow_private=self._allow_private_base_urls)
        await self._ensure_label_free(label)
        await self._validate(provider, key, url)

        credential = ProviderCredential(
            provider=provider,
            label=label,
            encrypted_api_key=self._cipher.encrypt(key),
            base_url=url,
            last4=key[-4:],
            created_by_user_id=actor.id,
            last_validated_at=datetime.now(UTC),
        )
        await self._insert(credential)
        await self._events.publish(
            CredentialCreated(
                organisation_id=self._org_id,
                actor_user_id=actor.id,
                credential_id=credential.id,
                provider=credential.provider,
                label=credential.label,
            )
        )
        return credential

    async def delete(self, credential_id: UUID, actor: User) -> None:
        # Row lock: an AI settings update referencing this credential also locks it, so the
        # in-use checks below cannot race with a concurrent assignment.
        credential = await self._credentials.get(credential_id, for_update=True)
        if credential is None:
            raise NotFoundError("Credential not found")
        for check in self._in_use_checks:
            reason = await check(self._session, self._org_id, credential.id)
            if reason:
                raise CredentialInUseError(reason)
        event = CredentialDeleted(
            organisation_id=self._org_id,
            actor_user_id=actor.id,
            credential_id=credential.id,
            provider=credential.provider,
            label=credential.label,
        )
        await self._credentials.delete(credential)
        await self._events.publish(event)

    def decrypt_key(self, credential: ProviderCredential) -> str:
        """The plaintext key, for adapters only. Never log or return it."""
        if credential.organisation_id != self._org_id:
            raise NotFoundError("Credential not found")
        try:
            return self._cipher.decrypt(credential.encrypted_api_key)
        except SecretDecryptError:
            raise CredentialUnreadableError() from None

    async def _validate(self, provider: str, key: str, base_url: str | None) -> None:
        try:
            await self._validator.validate(provider, key, base_url)
        except CredentialInvalidError as exc:
            message = sanitize_provider_message(str(exc), key) or "The provider rejected this API key"
        except Exception as exc:  # the validator contract is CredentialInvalidError; never leak details
            logger.warning("credential_validation_error", provider=provider, error_type=type(exc).__name__)
            message = "Could not validate the API key with the provider"
        else:
            return
        # Raised outside the except blocks so the provider exception (which may echo the key) is not
        # kept as __context__ for error trackers or tracebacks to render.
        raise CredentialInvalidError(message)

    async def _ensure_label_free(self, label: str) -> None:
        stmt = self._credentials.select(ProviderCredential.id).where(
            func.lower(ProviderCredential.label) == label.lower()
        )
        if (await self._session.execute(stmt)).first() is not None:
            raise CredentialLabelTakenError()

    async def _insert(self, credential: ProviderCredential) -> None:
        """Flush inside a savepoint so a lost race on the unique label becomes CredentialLabelTakenError."""
        try:
            async with self._session.begin_nested():
                await self._credentials.add(credential)
                await self._session.flush()
        except IntegrityError:
            raise CredentialLabelTakenError() from None
