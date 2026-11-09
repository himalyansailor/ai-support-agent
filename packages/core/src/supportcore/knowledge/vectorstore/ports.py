"""VectorStore port: tenant-scoped nearest-neighbour search over chunk embeddings (cosine)."""

from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from supportcore.errors import DomainError

MIN_K, MAX_K = 1, 200
MAX_DIMENSIONS = 16000
_NOT_FOUND = "Vector collection not found"


class VectorCollectionNotFoundError(DomainError):
    """Upsert into a collection that ensure_collection() never created (or that was dropped)."""

    def __init__(self, message: str = _NOT_FOUND) -> None:
        super().__init__(message)


class VectorCollectionOwnershipError(VectorCollectionNotFoundError):
    """The collection belongs to another organisation / knowledge base, or a record does.

    Subclasses VectorCollectionNotFoundError and uses the same message, so an API response never
    reveals that a collection of that name exists for someone else."""


class VectorDimensionMismatchError(DomainError):
    """A collection exists with other dimensions, or a vector's length differs from the collection's."""


@dataclass
class VectorRecord:
    chunk_id: UUID
    document_id: UUID
    organisation_id: UUID
    knowledge_base_id: UUID
    embedding: list[float]
    metadata: dict[str, Any] = field(default_factory=dict)  # JSON-serialisable


@dataclass
class VectorHit:
    chunk_id: UUID
    document_id: UUID
    score: float  # cosine similarity, 1 - cosine distance; raw (rounding happens in retrieval)
    metadata: dict[str, Any] = field(default_factory=dict)


def check_dimensions(dimensions: Any) -> int:
    if isinstance(dimensions, bool) or not isinstance(dimensions, int) or not 1 <= dimensions <= MAX_DIMENSIONS:
        raise ValueError(f"dimensions must be an integer between 1 and {MAX_DIMENSIONS}")
    return int(dimensions)


def check_uuid(value: Any, field_name: str) -> UUID:
    if not isinstance(value, UUID):
        raise ValueError(f"{field_name} must be a UUID")
    return value


def check_records_owned(records: list["VectorRecord"], organisation_id: UUID, knowledge_base_id: UUID) -> None:
    for rec in records:
        if rec.organisation_id != organisation_id or rec.knowledge_base_id != knowledge_base_id:
            raise VectorCollectionOwnershipError()


def check_k(k: int) -> int:
    if isinstance(k, bool) or not isinstance(k, int) or not MIN_K <= k <= MAX_K:
        raise ValueError(f"k must be an integer between {MIN_K} and {MAX_K}")
    return k


def check_length(embedding: list[float], dimensions: int, collection: str) -> None:
    if len(embedding) != dimensions:
        raise VectorDimensionMismatchError(
            f"Vector has {len(embedding)} dimensions but collection {collection!r} has {dimensions}"
        )


class VectorStore(Protocol):
    """Collections are named kb_{kb_id.hex}_v{version} and are owned by one (organisation, knowledge base).

    Every operation takes the caller's organisation_id and is tenant-checked: a collection owned by
    another organisation (or, for ensure_collection, another knowledge base) raises
    VectorCollectionOwnershipError, and every SQL/where filter also includes organisation_id.
    Unknown collections behave as empty for query/count/delete/drop; upserting into one raises
    VectorCollectionNotFoundError.
    """

    async def ensure_collection(
        self, name: str, dimensions: int, *, organisation_id: UUID, knowledge_base_id: UUID
    ) -> None: ...

    async def upsert(self, name: str, records: list[VectorRecord], *, organisation_id: UUID) -> None: ...

    async def query(
        self,
        name: str,
        embedding: list[float],
        *,
        organisation_id: UUID,
        k: int,
        document_ids: list[UUID] | None = None,
    ) -> list[VectorHit]: ...

    async def delete_documents(self, name: str, document_ids: list[UUID], *, organisation_id: UUID) -> None: ...

    async def drop_collection(self, name: str, *, organisation_id: UUID) -> None: ...

    async def count(self, name: str, *, organisation_id: UUID) -> int: ...
