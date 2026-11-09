import math
from typing import Any
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.ai.catalog import UnknownModelError, chat_models, get_chat_spec
from supportcore.ai.events import AISettingsUpdated
from supportcore.db.models import OrganisationAISettings, ProviderCredential, User
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import DomainError, NotFoundError
from supportcore.events import EventBus

DEFAULT_TEMPERATURE = 0.2
MAX_MODEL_NAME_LENGTH = 200


class CredentialProviderMismatchError(DomainError):
    def __init__(self, credential_provider: str, provider: str) -> None:
        super().__init__(f"Credential is for provider {credential_provider!r}, not {provider!r}")


class InvalidModelNameError(DomainError):
    def __init__(self) -> None:
        super().__init__(f"Model name must be 1-{MAX_MODEL_NAME_LENGTH} characters")


class InvalidTemperatureError(DomainError):
    def __init__(self) -> None:
        super().__init__("Temperature must be between 0 and 1")


def _recommended_chat_model() -> tuple[str, str]:
    spec = next(m for m in chat_models() if m.recommended)
    return spec.provider, spec.model


class OrganisationAISettingsService:
    def __init__(
        self, session: AsyncSession, organisation_id: UUID, events: EventBus, *, include_fake: bool = False
    ) -> None:
        self._session = session
        self._org_id = organisation_id
        self._events = events
        self._include_fake = include_fake  # the test-only `fake` chat provider (settings.env == "test")
        self._settings = TenantScopedRepository(session, OrganisationAISettings, organisation_id)
        self._credentials = TenantScopedRepository(session, ProviderCredential, organisation_id)

    async def get(self, *, for_update: bool = False) -> OrganisationAISettings:
        """The org's settings; the recommended defaults (no credential) are created on first access."""
        row = await self._load(for_update)
        if row is None:
            provider, model = _recommended_chat_model()
            # ON CONFLICT DO NOTHING: concurrent first reads must not fail on the primary key.
            await self._session.execute(
                insert(OrganisationAISettings)
                .values(
                    organisation_id=self._org_id,
                    chat_provider=provider,
                    chat_model=model,
                    chat_credential_id=None,
                    temperature=DEFAULT_TEMPERATURE,
                )
                .on_conflict_do_nothing(index_elements=[OrganisationAISettings.organisation_id])
            )
            row = await self._load(for_update)
            assert row is not None
        return row

    async def update(
        self,
        chat_provider: str,
        chat_model: str,
        chat_credential_id: UUID | None,
        temperature: float,
        actor: User,
    ) -> OrganisationAISettings:
        provider, model = chat_provider.strip(), chat_model.strip()
        if not 1 <= len(model) <= MAX_MODEL_NAME_LENGTH:
            raise InvalidModelNameError()
        get_chat_spec(provider, model, include_fake=self._include_fake)  # raises UnknownModelError
        if isinstance(temperature, bool) or not (isinstance(temperature, (int, float)) and math.isfinite(temperature) and 0 <= temperature <= 1):
            raise InvalidTemperatureError()
        if chat_credential_id is not None:
            # FOR UPDATE pairs with CredentialService.delete so a credential cannot be deleted
            # while it is being assigned here.
            credential = await self._credentials.get(chat_credential_id, for_update=True)
            if credential is None:
                raise NotFoundError("Credential not found")
            if credential.provider != provider:
                raise CredentialProviderMismatchError(credential.provider, provider)

        row = await self.get(for_update=True)
        new_values: dict[str, Any] = {
            "chat_provider": provider,
            "chat_model": model,
            "chat_credential_id": chat_credential_id,
            "temperature": float(temperature),
        }
        changes: dict[str, Any] = {}
        for name, value in new_values.items():
            old = getattr(row, name)
            if old != value:
                changes[name] = {"from": _audit_value(old), "to": _audit_value(value)}
                setattr(row, name, value)
        if changes:
            await self._session.flush()
            await self._events.publish(
                AISettingsUpdated(organisation_id=self._org_id, actor_user_id=actor.id, changes=changes)
            )
        return row

    async def is_ready(self, row: OrganisationAISettings) -> bool:
        """A chat model is chosen and, when its provider needs a key, a credential for that provider is set."""
        if not row.chat_provider or not row.chat_model:
            return False
        try:
            spec = get_chat_spec(row.chat_provider, row.chat_model, include_fake=self._include_fake)
        except UnknownModelError:
            return False
        if not spec.requires_credential:
            return True
        if row.chat_credential_id is None:
            return False
        credential = await self._credentials.get(row.chat_credential_id)
        return credential is not None and credential.provider == row.chat_provider

    async def _load(self, for_update: bool) -> OrganisationAISettings | None:
        stmt = self._settings.select(for_update=for_update)
        if for_update:
            stmt = stmt.execution_options(populate_existing=True)  # refresh after acquiring the lock
        return (await self._session.execute(stmt)).scalar_one_or_none()


def _audit_value(value: Any) -> Any:
    return str(value) if isinstance(value, UUID) else value
