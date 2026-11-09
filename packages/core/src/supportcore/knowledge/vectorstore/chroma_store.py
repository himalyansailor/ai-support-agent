"""Chroma adapter (HTTP client only; `chromadb-client` package). One Chroma collection per name.

Collection metadata holds the cosine space, the dimensions and the owner (organisation_id,
knowledge_base_id); every operation checks the owner against the caller's organisation_id, like the
pgvector adapter. Each record's ids live in Chroma record metadata as strings so queries and deletes
also filter with `where` on organisation_id; the record's own metadata is stored as a JSON string
because Chroma metadata values must be scalars.

chromadb's async client has no request timeout (its httpx client uses timeout=None), so every call
here is bounded by asyncio.wait_for(REQUEST_TIMEOUT_SECONDS). chromadb is imported lazily so importing
supportcore.wiring stays light.
"""

import asyncio
import json
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import urlparse
from uuid import UUID

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

if TYPE_CHECKING:
    from chromadb.api import AsyncClientAPI
    from chromadb.api.models.AsyncCollection import AsyncCollection

REQUEST_TIMEOUT_SECONDS = 30.0
_DIMENSIONS_KEY = "dimensions"
_ORG_KEY = "organisation_id"
_KB_KEY = "knowledge_base_id"
_METADATA_KEY = "metadata_json"

T = TypeVar("T")


class _Owner:
    def __init__(self, collection: "AsyncCollection") -> None:
        meta = collection.metadata or {}
        try:
            self.dimensions = int(meta[_DIMENSIONS_KEY])
            self.organisation_id = UUID(str(meta[_ORG_KEY]))
            self.knowledge_base_id = UUID(str(meta[_KB_KEY]))
        except (KeyError, ValueError, TypeError):
            # Not created by this adapter: nobody's collection, so never usable through it.
            raise VectorCollectionOwnershipError() from None


class ChromaVectorStore:
    def __init__(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"Invalid Chroma URL {url!r}")
        self.url = url
        self._host = parsed.hostname
        self._port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self._ssl = parsed.scheme == "https"
        self._client: AsyncClientAPI | None = None

    @staticmethod
    async def _call(awaitable: Awaitable[T]) -> T:
        return await asyncio.wait_for(awaitable, REQUEST_TIMEOUT_SECONDS)

    async def _api(self) -> "AsyncClientAPI":
        if self._client is None:
            import chromadb

            self._client = await self._call(
                chromadb.AsyncHttpClient(host=self._host, port=self._port, ssl=self._ssl)
            )
        return self._client

    async def close(self) -> None:
        """Closes the client's pooled HTTP connections. chromadb exposes no public close for its async
        client, so this uses its internal cleanup; the client reconnects lazily if used again."""
        if self._client is not None:
            cleanup = getattr(getattr(self._client, "_server", None), "_cleanup", None)
            if cleanup is not None:
                await cleanup()
            self._client = None

    async def _get(self, name: str) -> "AsyncCollection | None":
        from chromadb.errors import NotFoundError

        try:
            return await self._call((await self._api()).get_collection(name, embedding_function=None))
        except NotFoundError:
            return None

    async def _owned(self, name: str, organisation_id: UUID) -> "tuple[AsyncCollection, _Owner] | None":
        collection = await self._get(name)
        if collection is None:
            return None
        owner = _Owner(collection)
        if owner.organisation_id != organisation_id:
            raise VectorCollectionOwnershipError()
        return collection, owner

    async def ensure_collection(
        self, name: str, dimensions: int, *, organisation_id: UUID, knowledge_base_id: UUID
    ) -> None:
        dims = check_dimensions(dimensions)
        check_uuid(organisation_id, "organisation_id")
        check_uuid(knowledge_base_id, "knowledge_base_id")
        # get_or_create keeps an existing collection's metadata, so the checks below see the real owner.
        collection = await self._call(
            (await self._api()).get_or_create_collection(
                name,
                metadata={
                    "hnsw:space": "cosine",
                    _DIMENSIONS_KEY: dims,
                    _ORG_KEY: str(organisation_id),
                    _KB_KEY: str(knowledge_base_id),
                },
                embedding_function=None,
            )
        )
        owner = _Owner(collection)
        if owner.organisation_id != organisation_id or owner.knowledge_base_id != knowledge_base_id:
            raise VectorCollectionOwnershipError()
        if owner.dimensions != dims:
            raise VectorDimensionMismatchError(f"Collection {name!r} has {owner.dimensions} dimensions, not {dims}")

    async def upsert(self, name: str, records: list[VectorRecord], *, organisation_id: UUID) -> None:
        check_uuid(organisation_id, "organisation_id")
        owned = await self._owned(name, organisation_id)
        if owned is None:
            raise VectorCollectionNotFoundError()
        if not records:
            return
        collection, owner = owned
        check_records_owned(records, owner.organisation_id, owner.knowledge_base_id)
        for rec in records:
            check_length(rec.embedding, owner.dimensions, name)
        await self._call(
            collection.upsert(
                ids=[str(rec.chunk_id) for rec in records],
                embeddings=[[float(x) for x in rec.embedding] for rec in records],
                metadatas=[
                    {
                        _ORG_KEY: str(rec.organisation_id),
                        _KB_KEY: str(rec.knowledge_base_id),
                        "document_id": str(rec.document_id),
                        _METADATA_KEY: json.dumps(rec.metadata),
                    }
                    for rec in records
                ],
            )
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
        check_k(k)
        check_uuid(organisation_id, "organisation_id")
        owned = await self._owned(name, organisation_id)
        if owned is None or (document_ids is not None and not document_ids):
            return []
        collection, owner = owned
        check_length(embedding, owner.dimensions, name)
        where: dict[str, Any] = {_ORG_KEY: str(organisation_id)}
        if document_ids is not None:
            where = {"$and": [where, {"document_id": {"$in": [str(d) for d in document_ids]}}]}
        result = await self._call(
            collection.query(
                query_embeddings=[[float(x) for x in embedding]],
                n_results=k,
                where=where,
                include=["metadatas", "distances"],
            )
        )
        ids, metadatas, distances = result["ids"][0], result["metadatas"][0], result["distances"][0]
        hits = [
            VectorHit(
                chunk_id=UUID(chunk_id),
                document_id=UUID(str(meta["document_id"])),
                score=1.0 - float(distance),
                metadata=json.loads(str(meta.get(_METADATA_KEY) or "{}")),
            )
            for chunk_id, meta, distance in zip(ids, metadatas, distances, strict=True)
        ]
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits

    async def delete_documents(self, name: str, document_ids: list[UUID], *, organisation_id: UUID) -> None:
        check_uuid(organisation_id, "organisation_id")
        owned = await self._owned(name, organisation_id)
        if owned is None or not document_ids:
            return
        collection, _ = owned
        where = {
            "$and": [
                {_ORG_KEY: str(organisation_id)},
                {"document_id": {"$in": [str(d) for d in document_ids]}},
            ]
        }
        await self._call(collection.delete(where=where))

    async def drop_collection(self, name: str, *, organisation_id: UUID) -> None:
        from chromadb.errors import NotFoundError

        check_uuid(organisation_id, "organisation_id")
        if await self._owned(name, organisation_id) is None:
            return
        try:
            await self._call((await self._api()).delete_collection(name))
        except NotFoundError:
            pass

    async def count(self, name: str, *, organisation_id: UUID) -> int:
        check_uuid(organisation_id, "organisation_id")
        owned = await self._owned(name, organisation_id)
        if owned is None:
            return 0
        collection, _ = owned
        # Every record of an owned collection is the owner's (upsert enforces it).
        return await self._call(collection.count())
