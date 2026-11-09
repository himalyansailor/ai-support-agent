from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ChatModelOut(BaseModel):
    provider: str
    model: str
    label: str
    recommended: bool
    requires_credential: bool


class EmbeddingModelOut(BaseModel):
    provider: str
    model: str
    label: str
    dimensions: int | None
    requires_credential: bool
    recommended: bool


class LanguageOut(BaseModel):
    code: str
    label: str


class CatalogOut(BaseModel):
    chat_models: list[ChatModelOut]
    embedding_models: list[EmbeddingModelOut]
    languages: list[LanguageOut]


# Lengths here only bound the request size; CredentialService does the real validation (with codes).
class CreateCredentialRequest(BaseModel):
    provider: str = Field(max_length=50)
    label: str = Field(max_length=500)
    api_key: str = Field(max_length=4096)
    base_url: str | None = Field(default=None, max_length=4096)


class CredentialOut(BaseModel):
    """Never carries the key: only its last 4 characters."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    provider: str
    label: str
    base_url: str | None
    last4: str
    created_at: datetime
    last_validated_at: datetime | None


class AISettingsRequest(BaseModel):
    chat_provider: str = Field(max_length=50)
    chat_model: str = Field(max_length=4096)  # the 200-char limit is enforced (with a code) by the service
    chat_credential_id: UUID | None = None
    temperature: float


class AISettingsOut(BaseModel):
    chat_provider: str
    chat_model: str
    chat_credential_id: UUID | None
    temperature: float
    ready: bool
