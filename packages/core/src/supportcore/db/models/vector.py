import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from supportcore.db.base import Base


class VectorCollection(Base):
    """Registry of pgvector collections: name -> dimensions and owner (organisation, knowledge base).

    PgVectorStore checks the owner on every operation. No TenantMixin / foreign keys: rows are only
    read and written by the vector store (never through TenantScopedRepository), and dropping a
    knowledge base's collections is the knowledge service's job (Task 10).

    The vectors themselves live in kb_vectors_{dimensions} tables that PgVectorStore creates lazily;
    those tables are deliberately not models (alembic env.py ignores them)."""

    __tablename__ = "vector_collections"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    organisation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.timezone("utc", func.now()), nullable=False
    )
