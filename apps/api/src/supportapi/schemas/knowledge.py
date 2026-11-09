from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


# Bounds here only limit request size; the services validate (with stable error codes).
class EmbeddingIn(BaseModel):
    provider: str = Field(max_length=50)
    model: str = Field(max_length=1000)
    credential_id: UUID | None = None


class ChunkingIn(BaseModel):
    """Every field is optional: absent ones take the default (create) or keep the current value (patch)."""

    strategy: str | None = Field(default=None, max_length=50)
    chunk_size: int | None = None
    chunk_overlap: int | None = None


class RerankerIn(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    params: dict[str, Any] | None = None


class CreateKnowledgeBaseRequest(BaseModel):
    name: str = Field(max_length=1000)
    description: str | None = Field(default=None, max_length=10000)
    embedding: EmbeddingIn
    chunking: ChunkingIn = ChunkingIn()
    reranker: RerankerIn = RerankerIn()


class UpdateKnowledgeBaseRequest(BaseModel):
    """Partial: only fields present in the body are applied (see `model_fields_set`).

    `description: ""` clears the description. Explicit null for name/description is rejected (422)."""

    name: str | None = Field(default=None, max_length=1000)
    description: str | None = Field(default=None, max_length=10000)
    embedding: EmbeddingIn | None = None
    chunking: ChunkingIn | None = None
    reranker: RerankerIn | None = None

    @model_validator(mode="after")
    def _no_null_text_fields(self) -> "UpdateKnowledgeBaseRequest":
        for field in ("name", "description"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} must not be null (send an empty string to clear the description)")
        return self


class EmbeddingOut(BaseModel):
    provider: str
    model: str
    dimensions: int
    credential_id: UUID | None


class ChunkingOut(BaseModel):
    strategy: str
    chunk_size: int
    chunk_overlap: int


class RerankerOut(BaseModel):
    name: str
    params: dict[str, Any]


class DocumentCountsOut(BaseModel):
    uploaded: int = 0
    queued: int = 0
    processing: int = 0
    indexed: int = 0
    failed: int = 0


class KnowledgeBaseOut(BaseModel):
    id: UUID
    name: str
    description: str
    embedding: EmbeddingOut
    chunking: ChunkingOut
    reranker: RerankerOut
    active_version: int
    building_version: int | None
    document_counts: DocumentCountsOut
    created_at: datetime


class KnowledgeBaseUpdatedOut(KnowledgeBaseOut):
    rebuild_started: bool


class DocumentOut(BaseModel):
    """No storage_key: where the file lives is an internal detail."""

    id: UUID
    filename: str
    file_type: str
    size_bytes: int
    status: str
    error: str | None
    chunk_count: int
    indexed_version: int | None
    uploaded_at: datetime
    updated_at: datetime  # last status change; the web app offers "Stuck? Retry" for an old `queued`


class IndexDocumentsRequest(BaseModel):
    document_ids: list[UUID] | None = Field(default=None, max_length=1000)


class JobRefOut(BaseModel):
    id: UUID
    document_id: UUID | None


class JobsStartedOut(BaseModel):
    jobs: list[JobRefOut]


class ChunkOut(BaseModel):
    id: UUID
    ordinal: int
    text: str
    char_start: int
    char_end: int
    metadata: dict[str, Any]


class ChunkPage(BaseModel):
    items: list[ChunkOut]
    total: int
    version: int


class JobOut(BaseModel):
    id: UUID
    kind: str
    document_id: UUID | None
    document_name: str | None
    status: str
    kb_version: int
    attempts: int
    error: str | None
    steps: list[dict[str, Any]]
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class JobPage(BaseModel):
    items: list[JobOut]
    next_before: str | None
