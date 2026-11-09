"""Retrieval: the chunks of one or more knowledge bases that best match a query.

Per knowledge base: embed the query with the KB's active embedding model, search its active
collection (`kb_{id}_v{active_version}`), and hydrate the hits from Postgres (Chunk rows of the active
version and their document's filename; hits without such a row are dropped). Each KB keeps
`top_k` candidates, or `top_k * 4` (at most 80) with a reranker, which then reorders its own
candidates; the vector search asks for ORPHAN_HEADROOM more, so dropped hits (vectors whose chunk
row is gone) do not shorten the result. The merged list is cut to the global `top_k` by rerank score
when every KB with candidates was reranked, else by confidence (cosine score minus the embedding
model's threshold, so models are comparable). A KB whose search or reranker fails is skipped or left
in cosine order, with a warning (`kb_unavailable:<id>`, `reranker_unavailable:<id>`).

Two phases, so no database transaction is held across provider calls: `prepare()` reads the KBs and
decrypts the embedding keys (database only); `run()` embeds the query, searches, hydrates every hit
in one short query, then calls `release` (e.g. the caller's commit) before reranking. `retrieve()`
does both, calling `release` before the first outbound call too.

When the caller passes `on_error` (an integration error recorder), an embedding failure is reported as kind
`embedding` and a reranker failure as kind `rerank`, after the failed call. A vector-store search failure is only
warned about: the error log has no kind for it.

Used by the playground and, in Phase 3, by the bot (several KBs per call). Query text, chunk text and
API keys never go into logs or span attributes.
"""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.analytics.integration_errors import ErrorRecorder, error_type_of
from supportcore.ai.catalog import UnknownModelError, get_embedding_spec
from supportcore.ai.ports import RerankCandidate
from supportcore.config import Settings
from supportcore.db.models import Chunk, Document, KnowledgeBase
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import NotFoundError
from supportcore.events import EventBus
from supportcore.knowledge.embedding import create_embedder, embedding_key
from supportcore.knowledge.kb_service import VersionConfig, collection_name, version_config
from supportcore.knowledge.vectorstore.ports import VectorCollectionNotFoundError, VectorHit
from supportcore.observability.logging import get_logger

if TYPE_CHECKING:
    from supportcore.wiring import Adapters

logger = get_logger(__name__)

MIN_TOP_K, MAX_TOP_K, DEFAULT_TOP_K = 1, 20, 5
RERANK_CANDIDATE_MULTIPLIER = 4
MAX_RERANK_CANDIDATES = 80
ORPHAN_HEADROOM = 5  # extra vector hits fetched per KB, in case some have no chunk row any more
SCORE_DECIMALS = 4
NO_RERANKER = "none"


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: UUID
    knowledge_base_id: UUID
    document_id: UUID
    document_name: str
    ordinal: int
    text: str
    score: float  # cosine similarity, rounded to 4 decimals
    rerank_score: float | None  # None when the KB has no reranker
    metadata: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0  # cosine score minus the KB's embedding model threshold; > 0 is confident

    @property
    def rank_score(self) -> float:
        return self.rerank_score if self.rerank_score is not None else self.score


@dataclass
class RetrievalResult:
    chunks: list[RetrievedChunk]
    timings_ms: dict[str, int]  # embed, search, rerank
    warnings: list[str] = field(default_factory=list)  # kb_unavailable:<kb id>, reranker_unavailable:<kb id>

    @property
    def best_confidence(self) -> float | None:
        """The highest confidence among the chunks; None when nothing was retrieved."""
        return max((c.confidence for c in self.chunks), default=None)


def candidate_count(top_k: int, reranker: str) -> int:
    if reranker == NO_RERANKER:
        return top_k
    return min(top_k * RERANK_CANDIDATE_MULTIPLIER, MAX_RERANK_CANDIDATES)


def fetch_count(top_k: int, reranker: str) -> int:
    """How many hits the vector search asks for: the candidates plus ORPHAN_HEADROOM."""
    return candidate_count(top_k, reranker) + ORPHAN_HEADROOM


def _check_top_k(top_k: int) -> None:
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not MIN_TOP_K <= top_k <= MAX_TOP_K:
        raise ValueError(f"top_k must be an integer between {MIN_TOP_K} and {MAX_TOP_K}")


Release = Callable[[], Awaitable[None]]
_EmbeddingKey = tuple[str, str, UUID | None]  # (provider, model, credential_id)


@dataclass(frozen=True)
class _KbTarget:
    """One KB as retrieval needs it (plain values, usable after the transaction ended)."""

    kb_id: UUID
    version: int
    reranker: str
    reranker_params: dict[str, Any]
    config: VersionConfig
    confidence_threshold: float = 0.0

    @property
    def embedding_key(self) -> _EmbeddingKey:
        return (self.config.provider, self.config.model, self.config.credential_id)


@dataclass
class RetrievalPlan:
    """What `prepare()` read. Holds decrypted keys: keep it only for the duration of one call."""

    targets: list[_KbTarget]
    keys: dict[_EmbeddingKey, tuple[str | None, str | None]]  # -> (api_key, base_url)


def _confidence_threshold(provider: str, model: str) -> float:
    """The embedding model's confidence threshold; 0.0 for a model the catalog no longer knows."""
    try:
        return get_embedding_spec(provider, model, include_fake=True).confidence_threshold
    except UnknownModelError:
        return 0.0


def _round(score: float | None) -> float | None:
    return None if score is None else round(float(score), SCORE_DECIMALS)


class _Clock:
    def __init__(self) -> None:
        self.ms = {"embed": 0.0, "search": 0.0, "rerank": 0.0}

    def add(self, name: str, started: float) -> None:
        self.ms[name] += (time.monotonic() - started) * 1000

    def as_ints(self) -> dict[str, int]:
        return {k: max(0, int(v)) for k, v in self.ms.items()}


class RetrievalService:
    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        adapters: "Adapters",
        *,
        settings: Settings,
        events: EventBus,
        on_error: ErrorRecorder | None = None,
    ) -> None:
        self._on_error = on_error
        self._session = session
        self._org_id = organisation_id
        self._adapters = adapters
        self._settings = settings
        self._events = events
        self._kbs = TenantScopedRepository(session, KnowledgeBase, organisation_id)
        self._chunks = TenantScopedRepository(session, Chunk, organisation_id)

    async def retrieve(
        self, kb_ids: list[UUID], query: str, top_k: int, *, release: Release | None = None
    ) -> RetrievalResult:
        """Raises NotFoundError if any KB is not the organisation's, ValueError for a bad top_k.
        `release` (optional) is awaited once the database reads are done, before the first provider
        call, and again after hydrating."""
        _check_top_k(top_k)
        plan = await self.prepare(kb_ids)
        if release is not None:
            await release()
        return await self.run(plan, query, top_k, release=release)

    async def prepare(self, kb_ids: list[UUID]) -> RetrievalPlan:
        """Database reads only: the KBs (NotFoundError if any is not the organisation's) and the
        decrypted key of each embedding config."""
        targets = []
        for kb in await self._load_kbs(kb_ids):
            config = version_config(kb, kb.active_version)
            targets.append(
                _KbTarget(
                    kb.id,
                    kb.active_version,
                    kb.reranker,
                    dict(kb.reranker_params or {}),
                    config,
                    _confidence_threshold(config.provider, config.model),
                )
            )
        keys: dict[_EmbeddingKey, tuple[str | None, str | None]] = {}
        for target in targets:
            if target.embedding_key not in keys:
                keys[target.embedding_key] = await embedding_key(
                    self._session, self._org_id, self._adapters, self._settings, self._events, target.config
                )
        return RetrievalPlan(targets=targets, keys=keys)

    async def run(
        self, plan: RetrievalPlan, query: str, top_k: int, *, release: Release | None = None
    ) -> RetrievalResult:
        """Embeds, searches and hydrates (one short query, then `release`), then reranks."""
        _check_top_k(top_k)
        clock = _Clock()
        # Query vectors per embedding config, so KBs on the same model embed the query once.
        vectors: dict[_EmbeddingKey, list[float]] = {}
        failed_keys: set[_EmbeddingKey] = set()
        warnings: list[str] = []
        for target in plan.targets:
            key = target.embedding_key
            if key in vectors or key in failed_keys:
                continue
            started = time.monotonic()
            api_key, base_url = plan.keys[key]
            try:
                embedder = await create_embedder(self._adapters, self._settings, target.config, api_key, base_url)
                vectors[key] = await embedder.embed_query(query)
            except Exception as exc:
                await self._report("embedding", target.config.provider, exc)
                if len(plan.targets) == 1:  # a single KB: nothing to fall back on
                    raise
                failed_keys.add(key)
                for failed in (t for t in plan.targets if t.embedding_key == key):
                    logger.warning("retrieval_kb_failed", kb_id=str(failed.kb_id), error_type=type(exc).__name__)
                    warnings.append(f"kb_unavailable:{failed.kb_id}")
            clock.add("embed", started)

        started = time.monotonic()
        hits: list[list[VectorHit]] = []
        for target in plan.targets:
            if target.embedding_key in failed_keys:
                hits.append([])
                continue
            try:
                hits.append(await self._search(target, vectors[target.embedding_key], fetch_count(top_k, target.reranker)))
            except Exception as exc:  # one unavailable KB must not take the others down
                logger.warning("retrieval_kb_failed", kb_id=str(target.kb_id), error_type=type(exc).__name__)
                warnings.append(f"kb_unavailable:{target.kb_id}")
                hits.append([])
        hydrated = await self._hydrate(plan.targets, hits)
        clock.add("search", started)
        if release is not None:
            await release()

        merged: list[RetrievedChunk] = []
        all_reranked = True  # every KB that had candidates produced rerank scores
        for target, candidates in zip(plan.targets, hydrated, strict=True):
            candidates = candidates[: candidate_count(top_k, target.reranker)]
            if not candidates:
                continue
            reranked = False
            if target.reranker != NO_RERANKER:
                started = time.monotonic()
                try:
                    candidates = await self._rerank(target, query, candidates, top_k)
                    reranked = True
                except Exception as exc:  # unusable reranker (bad config) or a failing one: keep cosine order
                    logger.warning(
                        "retrieval_reranker_failed", kb_id=str(target.kb_id), error_type=type(exc).__name__
                    )
                    warnings.append(f"reranker_unavailable:{target.kb_id}")
                    await self._report("rerank", target.reranker, exc)
                clock.add("rerank", started)
            all_reranked = all_reranked and reranked
            merged.extend(candidates)

        # Rerank scores only compare within one reranker, so they decide the merge only when every
        # KB was reranked; otherwise the model-independent confidence does.
        merged.sort(key=(lambda c: c.rank_score) if all_reranked else (lambda c: c.confidence), reverse=True)
        return RetrievalResult(chunks=merged[:top_k], timings_ms=clock.as_ints(), warnings=warnings)

    # --- internals ---------------------------------------------------------------------------------

    async def _report(self, kind: str, provider: str, exc: Exception) -> None:
        if self._on_error is not None:
            await self._on_error(kind, provider, error_type_of(exc))

    async def _load_kbs(self, kb_ids: list[UUID]) -> list[KnowledgeBase]:
        wanted = list(dict.fromkeys(kb_ids))  # dedupe, keep order
        if not wanted:
            return []
        found = {kb.id: kb for kb in await self._kbs.list(KnowledgeBase.id.in_(wanted), limit=len(wanted))}
        if len(found) != len(wanted):
            raise NotFoundError("Knowledge base not found")
        return [found[kb_id] for kb_id in wanted]

    async def _search(self, target: _KbTarget, vector: list[float], k: int) -> list[VectorHit]:
        try:
            return await self._adapters.vector_store.query(
                collection_name(target.kb_id, target.version), vector, organisation_id=self._org_id, k=k
            )
        except VectorCollectionNotFoundError:  # nothing indexed yet
            return []

    async def _hydrate(self, targets: list[_KbTarget], hits: list[list[VectorHit]]) -> list[list[RetrievedChunk]]:
        """Per KB, its hits in score order as chunks of its active version; hits without such a row
        are dropped. One query for every KB."""
        ids = [h.chunk_id for kb_hits in hits for h in kb_hits]
        rows: dict[UUID, tuple[Chunk, str]] = {}
        if ids:
            stmt = (
                self._chunks.select(Chunk, Document.filename)
                # The chunk is org-scoped by the repository; its document must be the same org's.
                .join(Document, (Document.id == Chunk.document_id) & (Document.organisation_id == Chunk.organisation_id))
                .where(Chunk.id.in_(ids), Chunk.knowledge_base_id.in_([t.kb_id for t in targets]))
            )
            rows = {chunk.id: (chunk, filename) for chunk, filename in (await self._session.execute(stmt)).all()}
        out: list[list[RetrievedChunk]] = []
        for target, kb_hits in zip(targets, hits, strict=True):
            chunks: list[RetrievedChunk] = []
            for hit in sorted(kb_hits, key=lambda h: h.score, reverse=True):
                row = rows.get(hit.chunk_id)
                if row is None:
                    continue
                chunk, filename = row
                if chunk.knowledge_base_id != target.kb_id or chunk.kb_version != target.version:
                    continue
                chunks.append(
                    RetrievedChunk(
                        chunk_id=chunk.id,
                        knowledge_base_id=target.kb_id,
                        document_id=chunk.document_id,
                        document_name=filename,
                        ordinal=chunk.ordinal,
                        text=chunk.text,
                        score=_round(hit.score),
                        rerank_score=None,
                        metadata=dict(chunk.metadata_ or {}),
                        confidence=_round(_round(hit.score) - target.confidence_threshold),
                    )
                )
            out.append(chunks)
        return out

    async def _rerank(
        self, target: _KbTarget, query: str, candidates: list[RetrievedChunk], top_k: int
    ) -> list[RetrievedChunk]:
        reranker = self._adapters.rerankers.create(target.reranker, dict(target.reranker_params))
        by_id = {c.chunk_id: c for c in candidates}
        results = await reranker.rerank(
            query, [RerankCandidate(c.chunk_id, c.text, c.score) for c in candidates], top_k
        )
        return [replace(by_id[r.chunk_id], rerank_score=_round(r.rerank_score)) for r in results if r.chunk_id in by_id]
