"""Knowledge bases, their documents, chunks and indexing jobs.

Versioning: a knowledge base serves one vector collection, `active_version`. A change of embedding
model or chunking while documents exist builds `building_version` alongside it; the `building_*`
columns hold that version's config until the rebuild job swaps it in (Task 11). The plain
`embedding_*` / `chunking` columns always describe the served (active) version.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text  # `text` is also a Chunk column name
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from supportcore.db.base import Base, TenantMixin, TimestampMixin, uuid_pk

DOCUMENT_STATUSES = ("uploaded", "queued", "processing", "indexed", "failed")
JOB_KINDS = ("document", "rebuild")
JOB_STATUSES = ("queued", "running", "succeeded", "failed")


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class KnowledgeBase(TenantMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_bases"
    # Names are unique per organisation, case-insensitively (matches KnowledgeBaseService's check).
    __table_args__ = (
        Index("uq_knowledge_bases_organisation_id_lower_name", "organisation_id", func.lower(sql_text("name")), unique=True),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)

    # Served (active_version) embedding config.
    embedding_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(255), nullable=False)
    embedding_dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding_credential_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provider_credentials.id", ondelete="SET NULL"), nullable=True, index=True
    )
    chunking: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)  # ChunkingParams.model_dump()

    reranker: Mapped[str] = mapped_column(String(64), default="none", server_default="none", nullable=False)
    reranker_params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=sql_text("'{}'::jsonb"), nullable=False
    )

    active_version: Mapped[int] = mapped_column(Integer, default=1, server_default=sql_text("1"), nullable=False)
    building_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Config of building_version; all set while a rebuild is pending, all NULL otherwise.
    building_embedding_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    building_embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    building_embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    building_embedding_credential_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provider_credentials.id", ondelete="SET NULL"), nullable=True, index=True
    )
    building_chunking: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class Document(TenantMixin, TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        # Also serves lookups by knowledge_base_id (leading column).
        UniqueConstraint("knowledge_base_id", "sha256", name="uq_documents_knowledge_base_id_sha256"),
        CheckConstraint(_one_of("status", DOCUMENT_STATUSES), name="ck_documents_status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[str] = mapped_column(String(16), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="uploaded", server_default="uploaded", nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, server_default=sql_text("0"), nullable=False)
    indexed_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class Chunk(TenantMixin, Base):
    __tablename__ = "chunks"
    __table_args__ = (
        Index("ix_chunks_document_id_kb_version_ordinal", "document_id", "kb_version", "ordinal"),
        Index("ix_chunks_knowledge_base_id_kb_version", "knowledge_base_id", "kb_version"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    kb_version: Mapped[int] = mapped_column(Integer, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, default=dict, server_default=sql_text("'{}'::jsonb"), nullable=False
    )


class IndexingJob(TenantMixin, Base):
    __tablename__ = "indexing_jobs"
    __table_args__ = (
        Index("ix_indexing_jobs_knowledge_base_id_queued_at", "knowledge_base_id", sql_text("queued_at DESC")),
        CheckConstraint(_one_of("kind", JOB_KINDS), name="ck_indexing_jobs_kind"),
        CheckConstraint(_one_of("status", JOB_STATUSES), name="ck_indexing_jobs_status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    kb_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="queued", server_default="queued", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=sql_text("0"), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # [{"name", "status", "duration_ms", "detail"}], appended by the indexing pipeline (Task 11).
    steps: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=sql_text("'[]'::jsonb"), nullable=False
    )
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.timezone("utc", func.now()), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
