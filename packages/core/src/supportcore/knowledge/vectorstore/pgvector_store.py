"""pgvector adapter: one physical table per embedding dimension, many collections per table.

SQL safety: every value is a bound parameter. The only interpolated identifiers/types are built from
a validated int (`kb_vectors_{dim}`, `vector({dim})` / `halfvec({dim})`); collection names, ids and
metadata never reach the SQL text.

Vectors are sent as pgvector text literals ('[0.1,0.2,...]') in a bound *text* parameter and cast
server-side (`CAST(CAST(:v AS text) AS vector(dim))`), so no pgvector Python package or asyncpg codec
is needed. Above 2000 dimensions (pgvector's HNSW limit for `vector`) the column is `halfvec`; HNSW on
halfvec supports up to 4000 dimensions, above which the table has no ANN index (exact scan).

Tenancy: the `vector_collections` registry row records the owning organisation and knowledge base.
Every operation checks it against the caller's organisation_id first, and every statement on the
vector tables also filters on `organisation_id`.
"""

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from supportcore.knowledge.vectorstore.ports import (
    VectorCollectionNotFoundError,
    VectorCollectionOwnershipError,
    VectorDimensionMismatchError,
    VectorHit,
    VectorRecord,
    check_dimensions,
    check_k,
    check_length,
    check_records_owned,
    check_uuid,
)

HALFVEC_ABOVE = 2000  # vector HNSW limit
HNSW_MAX_DIMENSIONS = 4000  # halfvec HNSW limit
TABLE_PREFIX = "kb_vectors_"
_UUID_ARRAY = ARRAY(PG_UUID(as_uuid=True))


@dataclass(frozen=True)
class _Layout:
    dimensions: int
    table: str
    column_type: str  # e.g. vector(384) / halfvec(3072)
    ops: str  # HNSW operator class for cosine distance


@dataclass(frozen=True)
class _Collection:
    layout: _Layout
    organisation_id: UUID
    knowledge_base_id: UUID


def _layout(dimensions: Any) -> _Layout:
    dim = check_dimensions(dimensions)
    kind = "halfvec" if dim > HALFVEC_ABOVE else "vector"
    return _Layout(dim, f"{TABLE_PREFIX}{dim}", f"{kind}({dim})", f"{kind}_cosine_ops")


def _literal(embedding: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def _check_name(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise ValueError("collection name must be a non-empty string")
    return name


class PgVectorStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def _collection(self, conn: AsyncConnection, name: str, *, lock: str = "") -> _Collection | None:
        """lock: "" | "FOR SHARE" | "FOR UPDATE" (fixed strings, never user input)."""
        assert lock in ("", "FOR SHARE", "FOR UPDATE")
        row = (
            await conn.execute(
                text(
                    "SELECT dimensions, organisation_id, knowledge_base_id FROM vector_collections"
                    f" WHERE name = :name {lock}"
                ),
                {"name": name},
            )
        ).one_or_none()
        if row is None:
            return None
        return _Collection(_layout(row.dimensions), row.organisation_id, row.knowledge_base_id)

    async def _owned(
        self, conn: AsyncConnection, name: str, organisation_id: UUID, *, lock: str = ""
    ) -> _Collection | None:
        """The collection if it exists and belongs to organisation_id; None if absent; raises otherwise."""
        coll = await self._collection(conn, name, lock=lock)
        if coll is not None and coll.organisation_id != organisation_id:
            raise VectorCollectionOwnershipError()
        return coll

    async def ensure_collection(
        self, name: str, dimensions: int, *, organisation_id: UUID, knowledge_base_id: UUID
    ) -> None:
        _check_name(name)
        lay = _layout(dimensions)
        check_uuid(organisation_id, "organisation_id")
        check_uuid(knowledge_base_id, "knowledge_base_id")

        def check(coll: _Collection) -> None:
            # Ownership first, so a foreign collection's dimensions are never revealed.
            if coll.organisation_id != organisation_id or coll.knowledge_base_id != knowledge_base_id:
                raise VectorCollectionOwnershipError()
            if coll.layout.dimensions != lay.dimensions:
                raise VectorDimensionMismatchError(
                    f"Collection {name!r} has {coll.layout.dimensions} dimensions, not {lay.dimensions}"
                )

        async with self.engine.begin() as conn:
            existing = await self._collection(conn, name)
            if existing is not None:
                check(existing)
                return
            # Serialises concurrent DDL on the same table; released at commit/rollback.
            await conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:t))"), {"t": lay.table})
            t = lay.table
            await conn.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {t} ("
                    " id uuid NOT NULL,"
                    " collection text NOT NULL,"
                    " organisation_id uuid NOT NULL,"
                    " knowledge_base_id uuid NOT NULL,"
                    " document_id uuid NOT NULL,"
                    f" embedding {lay.column_type} NOT NULL,"
                    " metadata jsonb NOT NULL DEFAULT '{}'::jsonb,"
                    f" CONSTRAINT {t}_pkey PRIMARY KEY (collection, id))"
                )
            )
            if lay.dimensions <= HNSW_MAX_DIMENSIONS:
                await conn.execute(
                    text(f"CREATE INDEX IF NOT EXISTS {t}_embedding_hnsw ON {t} USING hnsw (embedding {lay.ops})")
                )
            await conn.execute(
                text(f"CREATE INDEX IF NOT EXISTS {t}_collection_org_idx ON {t} (collection, organisation_id)")
            )
            await conn.execute(
                text(f"CREATE INDEX IF NOT EXISTS {t}_collection_document_idx ON {t} (collection, document_id)")
            )
            await conn.execute(
                text(
                    "INSERT INTO vector_collections (name, dimensions, organisation_id, knowledge_base_id)"
                    " VALUES (:name, :dims, :org, :kb) ON CONFLICT (name) DO NOTHING"
                ),
                {"name": name, "dims": lay.dimensions, "org": organisation_id, "kb": knowledge_base_id},
            )
            # A concurrent ensure_collection for the same name may have won with another owner/dims.
            winner = await self._collection(conn, name)
            assert winner is not None
            check(winner)

    async def upsert(self, name: str, records: list[VectorRecord], *, organisation_id: UUID) -> None:
        _check_name(name)
        check_uuid(organisation_id, "organisation_id")
        async with self.engine.begin() as conn:
            # FOR SHARE: a concurrent drop_collection (FOR UPDATE) waits for this upsert, or vice versa.
            coll = await self._owned(conn, name, organisation_id, lock="FOR SHARE")
            if coll is None:
                raise VectorCollectionNotFoundError()
            if not records:
                return
            check_records_owned(records, coll.organisation_id, coll.knowledge_base_id)
            lay = coll.layout
            for rec in records:
                check_length(rec.embedding, lay.dimensions, name)
            params = [
                {
                    "id": rec.chunk_id,
                    "collection": name,
                    "org": rec.organisation_id,
                    "kb": rec.knowledge_base_id,
                    "doc": rec.document_id,
                    "embedding": _literal(rec.embedding),
                    "metadata": json.dumps(rec.metadata),
                }
                for rec in records
            ]
            # organisation_id / knowledge_base_id are never updated on conflict (they are the collection
            # owner's, checked above); the WHERE makes a conflicting foreign row a no-op, not a takeover.
            await conn.execute(
                text(
                    f"INSERT INTO {lay.table} AS t"
                    " (id, collection, organisation_id, knowledge_base_id, document_id, embedding, metadata)"
                    " VALUES (:id, :collection, :org, :kb, :doc,"
                    f" CAST(CAST(:embedding AS text) AS {lay.column_type}), CAST(CAST(:metadata AS text) AS jsonb))"
                    " ON CONFLICT (collection, id) DO UPDATE SET"
                    " document_id = EXCLUDED.document_id,"
                    " embedding = EXCLUDED.embedding,"
                    " metadata = EXCLUDED.metadata"
                    " WHERE t.organisation_id = EXCLUDED.organisation_id"
                    " AND t.knowledge_base_id = EXCLUDED.knowledge_base_id"
                ),
                params,
            )

    async def query(
        self,
        name: str,
        embedding: list[float],
        *,
        organisation_id: UUID,
        k: int,
        document_ids: list[UUID] | None = None,
    ) -> list[VectorHit]:
        _check_name(name)
        check_k(k)
        check_uuid(organisation_id, "organisation_id")
        async with self.engine.begin() as conn:
            coll = await self._owned(conn, name, organisation_id)
            if coll is None or (document_ids is not None and not document_ids):
                return []
            lay = coll.layout
            check_length(embedding, lay.dimensions, name)
            # Filtered HNSW scans keep going until k rows pass the filters (pgvector >= 0.8), and
            # ef_search must be at least k or the index returns fewer rows than asked for.
            await conn.execute(text("SELECT set_config('hnsw.iterative_scan', 'strict_order', true)"))
            await conn.execute(text("SELECT set_config('hnsw.ef_search', :ef, true)"), {"ef": str(max(40, k))})
            distance = f"embedding <=> CAST(CAST(:q AS text) AS {lay.column_type})"
            sql = (
                f"SELECT id, document_id, metadata, 1 - ({distance}) AS score FROM {lay.table}"
                " WHERE collection = :name AND organisation_id = :org"
            )
            params: dict[str, Any] = {"q": _literal(embedding), "name": name, "org": organisation_id, "k": k}
            binds = []
            if document_ids is not None:
                sql += " AND document_id = ANY(:doc_ids)"
                params["doc_ids"] = list(document_ids)
                binds.append(bindparam("doc_ids", type_=_UUID_ARRAY))
            sql += f" ORDER BY {distance} LIMIT :k"
            rows = (await conn.execute(text(sql).bindparams(*binds), params)).all()
        return [
            VectorHit(
                chunk_id=row.id,
                document_id=row.document_id,
                score=float(row.score),
                metadata=json.loads(row.metadata) if isinstance(row.metadata, str) else dict(row.metadata),
            )
            for row in rows
        ]

    async def delete_documents(self, name: str, document_ids: list[UUID], *, organisation_id: UUID) -> None:
        _check_name(name)
        check_uuid(organisation_id, "organisation_id")
        async with self.engine.begin() as conn:
            coll = await self._owned(conn, name, organisation_id, lock="FOR SHARE")
            if coll is None or not document_ids:
                return
            await conn.execute(
                text(
                    f"DELETE FROM {coll.layout.table}"
                    " WHERE collection = :name AND organisation_id = :org AND document_id = ANY(:doc_ids)"
                ).bindparams(bindparam("doc_ids", type_=_UUID_ARRAY)),
                {"name": name, "org": organisation_id, "doc_ids": list(document_ids)},
            )

    async def drop_collection(self, name: str, *, organisation_id: UUID) -> None:
        _check_name(name)
        check_uuid(organisation_id, "organisation_id")
        async with self.engine.begin() as conn:
            coll = await self._owned(conn, name, organisation_id, lock="FOR UPDATE")
            if coll is None:
                return
            # Every row of an owned collection is the owner's (upsert enforces it), so drop them all.
            await conn.execute(text(f"DELETE FROM {coll.layout.table} WHERE collection = :name"), {"name": name})
            await conn.execute(text("DELETE FROM vector_collections WHERE name = :name"), {"name": name})

    async def count(self, name: str, *, organisation_id: UUID) -> int:
        _check_name(name)
        check_uuid(organisation_id, "organisation_id")
        async with self.engine.connect() as conn:
            coll = await self._owned(conn, name, organisation_id)
            if coll is None:
                return 0
            return (
                await conn.execute(
                    text(
                        f"SELECT count(*) FROM {coll.layout.table} WHERE collection = :name AND organisation_id = :org"
                    ),
                    {"name": name, "org": organisation_id},
                )
            ).scalar_one()
