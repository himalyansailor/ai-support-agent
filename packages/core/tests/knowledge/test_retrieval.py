"""Retrieval (Task 13): embed the query per KB, search its active collection, hydrate chunks, rerank, merge.

Runs on the rollback-isolated `session`; the vector store is an in-memory cosine store so the tests
control exactly which vectors live in which collection.
"""

import hashlib
import math
import uuid
from dataclasses import replace

import pytest

from supportcore.ai.catalog import LANGUAGES
from supportcore.ai.ports import EmbeddingError
from supportcore.ai.embeddings.fake_embedder import FakeEmbedder
from supportcore.db.models import Chunk, Document
from supportcore.errors import NotFoundError
from supportcore.knowledge.kb_service import collection_name
from supportcore.knowledge.retrieval import RetrievalService, RetrievedChunk
from supportcore.knowledge.vectorstore.ports import VectorHit

TIMING_KEYS = {"embed", "search", "rerank"}


class MemoryVectorStore:
    """Cosine nearest-neighbour store: {collection: [(org_id, chunk_id, document_id, vector)]}."""

    def __init__(self) -> None:
        self.collections: dict[str, list[tuple[uuid.UUID, uuid.UUID, uuid.UUID, list[float]]]] = {}
        self.queries: list[tuple[str, int]] = []

    def put(self, name, org_id, chunk_id, document_id, vector) -> None:
        self.collections.setdefault(name, []).append((org_id, chunk_id, document_id, vector))

    async def query(self, name, embedding, *, organisation_id, k, document_ids=None):
        self.queries.append((name, k))
        rows = [r for r in self.collections.get(name, []) if r[0] == organisation_id]

        def cos(v):
            dot = sum(a * b for a, b in zip(v, embedding))
            norm = math.sqrt(sum(a * a for a in v)) * math.sqrt(sum(b * b for b in embedding))
            return dot / norm if norm else 0.0

        hits = [VectorHit(chunk_id=c, document_id=d, score=cos(v), metadata={}) for _o, c, d, v in rows]
        return sorted(hits, key=lambda h: h.score, reverse=True)[:k]


@pytest.fixture
def store() -> MemoryVectorStore:
    return MemoryVectorStore()


@pytest.fixture
def retrieval(session, org, adapters, kb_settings, bus, store):
    return RetrievalService(session, org.id, replace(adapters, vector_store=store), settings=kb_settings, events=bus)


async def _kb(kbs, owner, name="Docs", reranker="none"):
    from supportcore.knowledge.chunking.ports import ChunkingParams

    return await kbs.create(
        name=name,
        description="",
        embedding_provider="fake",
        embedding_model="fake-embedding",
        embedding_credential_id=None,
        chunking=ChunkingParams(strategy="recursive"),
        reranker=reranker,
        reranker_params={},
        actor=owner,
    )


async def _index(session, store, kb, texts, *, filename="faq.md", version=None, store_version=None):
    """Adds a document with one Chunk row per text (at `version`) and its vectors (in `store_version`)."""
    version = version or kb.active_version
    store_version = store_version or version
    doc = Document(
        organisation_id=kb.organisation_id,
        knowledge_base_id=kb.id,
        filename=filename,
        file_type="markdown",
        size_bytes=10,
        sha256=hashlib.sha256(f"{kb.id}{filename}{texts}".encode()).hexdigest(),
        storage_key=f"k/{uuid.uuid4()}",
        status="indexed",
        indexed_version=version,
    )
    session.add(doc)
    await session.flush()
    embedder = FakeEmbedder(dimensions=8)
    chunks = []
    for i, text in enumerate(texts):
        chunk = Chunk(
            organisation_id=kb.organisation_id,
            knowledge_base_id=kb.id,
            document_id=doc.id,
            kb_version=version,
            ordinal=i,
            text=text,
            char_start=0,
            char_end=len(text),
            metadata_={"heading": f"h{i}"},
        )
        session.add(chunk)
        await session.flush()
        store.put(collection_name(kb, store_version), kb.organisation_id, chunk.id, doc.id, await embedder.embed_query(text))
        chunks.append(chunk)
    return doc, chunks


TEXTS = ["refund policy thirty days", "shipping takes three days", "contact support by email"]


async def test_retrieve_orders_and_hydrates(session, kbs, owner, retrieval, store):
    kb = await _kb(kbs, owner)
    doc, chunks = await _index(session, store, kb, TEXTS)
    # A vector whose chunk row is gone is dropped.
    store.put(collection_name(kb, 1), kb.organisation_id, uuid.uuid4(), doc.id, await FakeEmbedder().embed_query(TEXTS[0]))

    result = await retrieval.retrieve([kb.id], "refund policy thirty days", top_k=3)

    assert set(result.timings_ms) == TIMING_KEYS and all(isinstance(v, int) for v in result.timings_ms.values())
    assert store.queries == [(collection_name(kb, 1), 8)]  # top_k + ORPHAN_HEADROOM
    first = result.chunks[0]
    assert isinstance(first, RetrievedChunk)
    assert (first.chunk_id, first.document_id, first.knowledge_base_id) == (chunks[0].id, doc.id, kb.id)
    assert (first.document_name, first.ordinal, first.text) == ("faq.md", 0, TEXTS[0])
    assert first.score == 1.0 and first.rerank_score is None and first.metadata == {"heading": "h0"}
    scores = [c.score for c in result.chunks]
    assert scores == sorted(scores, reverse=True)
    assert all(round(s, 4) == s for s in scores)
    assert len(result.chunks) <= 3 and {c.chunk_id for c in result.chunks} <= {c.id for c in chunks}


async def test_orphan_hits_do_not_shorten_results(session, kbs, owner, retrieval, store):
    kb = await _kb(kbs, owner)
    doc, chunks = await _index(session, store, kb, TEXTS)
    query = "refund policy thirty days"
    vector = await FakeEmbedder().embed_query(query)
    for _ in range(5):  # vectors whose chunk rows are gone, all scoring above every live chunk
        store.put(collection_name(kb, 1), kb.organisation_id, uuid.uuid4(), doc.id, vector)

    result = await retrieval.retrieve([kb.id], query, top_k=3)

    assert [c.chunk_id for c in result.chunks][:1] == [chunks[0].id]
    assert len(result.chunks) == 3 and {c.chunk_id for c in result.chunks} == {c.id for c in chunks}


async def test_reranker_gets_exactly_its_candidates_despite_headroom(session, kbs, owner, adapters, kb_settings, bus, store):
    kb = await _kb(kbs, owner, reranker="fake")
    await _index(session, store, kb, [f"refund topic {i}" for i in range(12)])
    seen: list[int] = []

    class _CountingReranker:
        async def rerank(self, query, candidates, top_n):
            seen.append(len(candidates))
            return []

    class _Rerankers:
        def create(self, name, params):
            return _CountingReranker()

    service = RetrievalService(
        session, kb.organisation_id, replace(adapters, vector_store=store, rerankers=_Rerankers()), settings=kb_settings,
        events=bus,
    )
    await service.retrieve([kb.id], "refund topic", top_k=2)
    assert store.queries == [(collection_name(kb, 1), 13)] and seen == [8]


async def test_release_runs_before_provider_calls_and_after_hydrating(session, kbs, owner, adapters, kb_settings, bus, store):
    kb = await _kb(kbs, owner)
    await _index(session, store, kb, TEXTS)
    order: list[str] = []

    class _Embedders:
        def create(self, provider, model, *, api_key, base_url, settings):
            embedder = FakeEmbedder(dimensions=8, model_id=model)
            original = embedder.embed_query

            async def embed_query(text):
                order.append("embed")
                return await original(text)

            embedder.embed_query = embed_query
            return embedder

    original_execute = session.execute

    async def execute(*args, **kwargs):
        order.append("db")
        return await original_execute(*args, **kwargs)

    async def release():
        order.append("release")

    service = RetrievalService(
        session, kb.organisation_id, replace(adapters, vector_store=store, embedders=_Embedders()), settings=kb_settings,
        events=bus,
    )
    session.execute = execute
    try:
        result = await service.retrieve([kb.id], "refund policy thirty days", top_k=2, release=release)
    finally:
        del session.execute
    assert result.chunks
    # Reads, release, the provider call, one hydrate query, release.
    assert order[-4:] == ["release", "embed", "db", "release"] and set(order[:-4]) == {"db"}


async def test_retrieve_empty_kb_returns_no_chunks(kbs, owner, retrieval):
    kb = await _kb(kbs, owner)
    result = await retrieval.retrieve([kb.id], "anything", top_k=5)
    assert result.chunks == [] and set(result.timings_ms) == TIMING_KEYS


async def test_retrieve_uses_active_version_only(session, kbs, owner, retrieval, store):
    kb = await _kb(kbs, owner)
    await _index(session, store, kb, ["old refund text"], filename="old.md", version=1)
    kb.active_version = 2
    await session.flush()
    _doc, current = await _index(session, store, kb, ["new refund text"], filename="new.md", version=2)
    # A v1 chunk row whose vector sits in the v2 collection is not served either.
    await _index(session, store, kb, ["stray refund text"], filename="stray.md", version=1, store_version=2)

    result = await retrieval.retrieve([kb.id], "refund text", top_k=5)

    assert store.queries == [(collection_name(kb, 2), 10)]
    assert [c.chunk_id for c in result.chunks] == [current[0].id]


async def test_reranker_applied_when_configured(session, kbs, owner, retrieval, store):
    kb = await _kb(kbs, owner, reranker="fake")
    texts = [f"refund topic number {w}" for w in ("one", "two", "three", "four", "five", "six")]
    await _index(session, store, kb, texts)

    query = "refund topic number one"
    by_score = await store.query(
        collection_name(kb, 1), await FakeEmbedder().embed_query(query), organisation_id=kb.organisation_id, k=6
    )
    store.queries.clear()

    result = await retrieval.retrieve([kb.id], query, top_k=2)

    assert store.queries == [(collection_name(kb, 1), 13)]  # top_k * 4 candidates + headroom (all 6 chunks)
    # The fake reranker reverses the candidates: the two weakest vector matches come first.
    assert [c.chunk_id for c in result.chunks] == [h.chunk_id for h in by_score[::-1][:2]]
    assert [c.rerank_score for c in result.chunks] == [6.0, 5.0]
    assert [c.score for c in result.chunks] == [round(h.score, 4) for h in by_score[::-1][:2]]


async def test_rerank_candidates_capped_at_80(kbs, owner, retrieval, store):
    kb = await _kb(kbs, owner, reranker="fake")
    await retrieval.retrieve([kb.id], "q", top_k=20)
    assert store.queries == [(collection_name(kb, 1), 85)]  # 80 candidates + headroom


async def test_merge_across_two_kbs(session, kbs, owner, retrieval, store):
    kb_a = await _kb(kbs, owner, "A")
    kb_b = await _kb(kbs, owner, "B")
    _d, a_chunks = await _index(session, store, kb_a, ["refund policy thirty days", "weather is sunny"], filename="a.md")
    _d, b_chunks = await _index(session, store, kb_b, ["refund policy thirty days please", "unrelated cats"], filename="b.md")

    result = await retrieval.retrieve([kb_a.id, kb_b.id], "refund policy thirty days", top_k=3)

    assert len(result.chunks) == 3
    assert result.chunks[0].chunk_id == a_chunks[0].id and result.chunks[0].document_name == "a.md"
    assert {c.knowledge_base_id for c in result.chunks} == {kb_a.id, kb_b.id}
    assert b_chunks[0].id in {c.chunk_id for c in result.chunks}
    scores = [c.score for c in result.chunks]
    assert scores == sorted(scores, reverse=True)
    assert {name for name, _k in store.queries} == {collection_name(kb_a, 1), collection_name(kb_b, 1)}


async def test_retrieve_other_org_kb_not_found(session, kbs, owner, retrieval, org_b):
    from supportcore.db.models import KnowledgeBase

    mine = await _kb(kbs, owner)
    foreign = KnowledgeBase(
        organisation_id=org_b.id,
        name="Theirs",
        embedding_provider="fake",
        embedding_model="fake-embedding",
        embedding_dimensions=8,
        chunking={},
        active_version=1,
    )
    session.add(foreign)
    await session.flush()
    with pytest.raises(NotFoundError):
        await retrieval.retrieve([mine.id, foreign.id], "q", top_k=5)
    with pytest.raises(NotFoundError):
        await retrieval.retrieve([uuid.uuid4()], "q", top_k=5)


def test_prompt_numbers_context_and_sets_language():
    from supportcore.ai.prompts import build_rag_messages

    chunks = [
        RetrievedChunk(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "refunds.md", 0, "Refunds within 30 days.", 0.9, None, {}),
        RetrievedChunk(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "shipping.md", 2, "Ships in 3 days.", 0.8, None, {}),
    ]
    messages = build_rag_messages("How long for refunds?", chunks, organisation_name="Acme", language="en")

    system, human = messages[0], messages[-1]
    assert system.type == "system" and human.type == "human"
    prompt = system.content
    assert "Acme" in prompt and "[n]" in prompt
    assert "don't know" in prompt.lower() and "human" in prompt.lower()
    assert f'Answer in {dict(LANGUAGES)["en"]} (language code "en")' in prompt
    text = "\n".join(m.content for m in messages)
    assert "[1] refunds.md" in text and "[2] shipping.md" in text
    assert text.index("Refunds within 30 days.") < text.index("Ships in 3 days.")
    assert "How long for refunds?" in human.content


def test_parse_citations_variants():
    from supportcore.knowledge.playground import parse_citations

    chunks = [
        RetrievedChunk(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), f"d{i}.md", i, "t", 0.5, None, {}) for i in range(3)
    ]
    ids = [c.chunk_id for c in chunks]
    assert parse_citations("See [2] and [1, 3].", chunks) == [ids[1], ids[0], ids[2]]
    assert parse_citations("Twice [3][3] then [1][2]", chunks) == [ids[2], ids[0], ids[1]]
    assert parse_citations("Out of range [0] [4] [99, 2]", chunks) == [ids[1]]
    assert parse_citations("No citations [a] here", chunks) == []


class _RecordingEmbedders:
    """Embedder factory stand-in that records create() calls and returns fake embedders."""

    def __init__(self) -> None:
        self.created: list[dict] = []

    def create(self, provider, model, *, api_key, base_url, settings):
        self.created.append({"provider": provider, "model": model, "api_key": api_key, "base_url": base_url})
        return FakeEmbedder(dimensions=8, model_id=model)


async def _compatible_kb(session, kbs, owner, adapters, base_url="https://llm.example.com/v1"):
    from supportcore.db.models import ProviderCredential

    kb = await _kb(kbs, owner)
    credential = ProviderCredential(
        organisation_id=kb.organisation_id,
        provider="openai_compatible",
        label="Local",
        encrypted_api_key=adapters.cipher.encrypt("sk-compat-SECRET-1234"),
        base_url=base_url,
        last4="1234",
    )
    session.add(credential)
    await session.flush()
    kb.embedding_provider, kb.embedding_model, kb.embedding_credential_id = "openai_compatible", "my-embed", credential.id
    await session.flush()
    return kb


async def test_compatible_query_embed_checks_base_url_first(session, kbs, owner, adapters, kb_settings, bus, store, monkeypatch):
    import supportcore.knowledge.embedding as embedding
    from supportcore.ai.errors import CredentialInvalidError

    kb = await _compatible_kb(session, kbs, owner, adapters)
    embedders = _RecordingEmbedders()
    service = RetrievalService(
        session, kb.organisation_id, replace(adapters, vector_store=store, embedders=embedders), settings=kb_settings, events=bus
    )
    checked: list[str] = []

    async def guard(url, *, allow_private):
        checked.append(url)
        raise CredentialInvalidError("Base URL must be a public address")

    monkeypatch.setattr(embedding, "ensure_public_base_url", guard)
    with pytest.raises(CredentialInvalidError):
        await service.retrieve([kb.id], "refunds", top_k=5)
    assert checked == ["https://llm.example.com/v1"] and embedders.created == [] and store.queries == []

    async def allow(url, *, allow_private):
        checked.append(url)

    monkeypatch.setattr(embedding, "ensure_public_base_url", allow)
    await service.retrieve([kb.id], "refunds", top_k=5)
    assert embedders.created == [
        {"provider": "openai_compatible", "model": "my-embed", "api_key": "sk-compat-SECRET-1234", "base_url": "https://llm.example.com/v1"}
    ]


async def test_retrieve_with_removed_embedding_key_is_readable_error(session, kbs, owner, retrieval):
    from supportcore.ai.ports import EmbeddingError
    from supportcore.knowledge.embedding import EMBEDDING_KEY_REMOVED

    kb = await _kb(kbs, owner)
    # What deleting the credential leaves behind (the FK is ON DELETE SET NULL).
    kb.embedding_provider, kb.embedding_model, kb.embedding_credential_id = "openai", "text-embedding-3-small", None
    await session.flush()
    with pytest.raises(EmbeddingError) as exc:
        await retrieval.retrieve([kb.id], "refunds", top_k=5)
    assert str(exc.value) == EMBEDDING_KEY_REMOVED


# --- hardening (Phase 3, Task 4) ------------------------------------------------------------------


class _FixedStore:
    """Vector store returning fixed scores per collection (and raising for chosen collections)."""

    def __init__(self, docs: dict[str, tuple[list, uuid.UUID]], scores: dict[str, float], failing=()) -> None:
        self.docs, self.scores, self.failing = docs, scores, set(failing)

    async def query(self, name, embedding, *, organisation_id, k, document_ids=None):
        if name in self.failing:
            raise RuntimeError("vector store down")
        chunks, doc_id = self.docs[name]
        return [VectorHit(chunk_id=c.id, document_id=doc_id, score=self.scores[name], metadata={}) for c in chunks]


async def test_failing_kb_is_skipped_with_warning(session, kbs, owner, adapters, kb_settings, bus, store):
    kb_a = await _kb(kbs, owner, "A")
    kb_b = await _kb(kbs, owner, "B")
    _d, a_chunks = await _index(session, store, kb_a, ["refund policy thirty days"], filename="a.md")
    await _index(session, store, kb_b, ["refund policy thirty days"], filename="b.md")
    real_query = store.query

    async def query(name, embedding, **kwargs):
        if name == collection_name(kb_b, 1):
            raise RuntimeError("vector store down")
        return await real_query(name, embedding, **kwargs)

    store.query = query
    service = RetrievalService(session, kb_a.organisation_id, replace(adapters, vector_store=store), settings=kb_settings, events=bus)

    result = await service.retrieve([kb_a.id, kb_b.id], "refund policy thirty days", top_k=3)

    assert [c.chunk_id for c in result.chunks] == [a_chunks[0].id]
    assert result.warnings == [f"kb_unavailable:{kb_b.id}"]


async def test_unlisted_reranker_skips_rerank_with_warning(session, kbs, owner, retrieval, store):
    kb = await _kb(kbs, owner)
    kb.reranker, kb.reranker_params = "cross_encoder", {"model": "evil/model"}
    await session.flush()
    await _index(session, store, kb, TEXTS)

    result = await retrieval.retrieve([kb.id], "refund policy thirty days", top_k=3)

    scores = [c.score for c in result.chunks]
    assert len(result.chunks) == 3 and scores == sorted(scores, reverse=True)
    assert all(c.rerank_score is None for c in result.chunks)
    assert result.warnings == [f"reranker_unavailable:{kb.id}"]


async def _two_fixed_kbs(session, kbs, owner, adapters, kb_settings, bus, store, scores, reranker="none"):
    kb_a = await _kb(kbs, owner, "A", reranker=reranker)
    kb_b = await _kb(kbs, owner, "B", reranker=reranker)
    docs = {}
    for kb, texts in ((kb_a, ["a one", "a two"]), (kb_b, ["b one", "b two", "b three"])):
        doc, chunks = await _index(session, store, kb, texts, filename=f"{kb.name}.md")
        docs[collection_name(kb, 1)] = (chunks, doc.id)
    fixed = _FixedStore(docs, {collection_name(kb_a, 1): scores[0], collection_name(kb_b, 1): scores[1]})
    service = RetrievalService(
        session, kb_a.organisation_id, replace(adapters, vector_store=fixed), settings=kb_settings, events=bus
    )
    return service, kb_a, kb_b


async def test_merge_orders_by_confidence_when_not_all_reranked(session, kbs, owner, adapters, kb_settings, bus, store, monkeypatch):
    from supportcore.ai import catalog
    from supportcore.knowledge import retrieval as retrieval_module

    service, kb_a, kb_b = await _two_fixed_kbs(session, kbs, owner, adapters, kb_settings, bus, store, (0.5, 0.6))
    thresholds = iter([0.0, 0.35])  # KB A (fake spec), then KB B (patched spec)
    real = catalog.get_embedding_spec
    monkeypatch.setattr(
        retrieval_module,
        "get_embedding_spec",
        lambda *a, **kw: replace(real(*a, **kw), confidence_threshold=next(thresholds)),
    )

    result = await service.retrieve([kb_a.id, kb_b.id], "q", top_k=5)

    assert result.chunks[0].knowledge_base_id == kb_a.id
    assert [(c.knowledge_base_id == kb_a.id, c.score, c.confidence) for c in result.chunks][:2] == [
        (True, 0.5, 0.5),
        (True, 0.5, 0.5),
    ]
    assert {c.confidence for c in result.chunks if c.knowledge_base_id == kb_b.id} == {0.25}
    assert result.best_confidence == 0.5


async def test_merge_orders_by_rerank_when_all_reranked(session, kbs, owner, adapters, kb_settings, bus, store):
    service, kb_a, kb_b = await _two_fixed_kbs(
        session, kbs, owner, adapters, kb_settings, bus, store, (0.9, 0.1), reranker="fake"
    )

    result = await service.retrieve([kb_a.id, kb_b.id], "q", top_k=5)

    assert result.warnings == []
    assert [c.rerank_score for c in result.chunks] == [3.0, 2.0, 2.0, 1.0, 1.0]
    assert result.chunks[0].knowledge_base_id == kb_b.id  # lower cosine, higher rerank score


async def test_best_confidence_none_when_no_chunks(kbs, owner, retrieval):
    kb = await _kb(kbs, owner)
    result = await retrieval.retrieve([kb.id], "anything", top_k=5)
    assert result.chunks == [] and result.best_confidence is None and result.warnings == []


def test_unknown_embedding_spec_threshold_defaults_to_zero():
    from supportcore.knowledge.retrieval import _confidence_threshold

    assert _confidence_threshold("local", "no/such-model") == 0.0
    assert _confidence_threshold("local", "BAAI/bge-small-en-v1.5") == 0.60


async def test_failing_embedder_skips_its_kbs_when_multi_kb(session, kbs, owner, adapters, kb_settings, bus, store):
    from supportcore.knowledge.chunking.ports import ChunkingParams

    kb_a = await _kb(kbs, owner, "A")
    kb_b = await kbs.create(
        name="B", description="", embedding_provider="fake", embedding_model="fake-embedding-16",
        embedding_credential_id=None, chunking=ChunkingParams(strategy="recursive"), reranker="none",
        reranker_params={}, actor=owner,
    )
    _d, a_chunks = await _index(session, store, kb_a, ["refund policy thirty days"], filename="a.md")

    class _Embedders:
        def create(self, provider, model, *, api_key, base_url, settings):
            if model == "fake-embedding-16":
                raise RuntimeError("embedder down")
            return FakeEmbedder(dimensions=8, model_id=model)

    service = RetrievalService(
        session, kb_a.organisation_id, replace(adapters, vector_store=store, embedders=_Embedders()),
        settings=kb_settings, events=bus,
    )
    result = await service.retrieve([kb_a.id, kb_b.id], "refund policy thirty days", top_k=3)
    assert [c.chunk_id for c in result.chunks] == [a_chunks[0].id]
    assert result.warnings == [f"kb_unavailable:{kb_b.id}"]
    assert store.queries == [(collection_name(kb_a, 1), 8)]

    # A single KB: the error still propagates.
    with pytest.raises(RuntimeError):
        await service.retrieve([kb_b.id], "q", top_k=3)


# --- integration error log (Phase 5, Task 3) ---


async def test_retrieval_embedding_failure_reported(session, kbs, owner, adapters, kb_settings, bus, store, monkeypatch):
    kb = await _kb(kbs, owner)
    await _index(session, store, kb, TEXTS)
    seen = []

    async def on_error(kind, provider, error_type):
        seen.append((kind, provider, error_type))

    async def failing(*args, **kwargs):
        raise EmbeddingError("down")

    monkeypatch.setattr("supportcore.knowledge.retrieval.create_embedder", failing)
    service = RetrievalService(
        session, kb.organisation_id, replace(adapters, vector_store=store), settings=kb_settings, events=bus, on_error=on_error
    )
    with pytest.raises(EmbeddingError):  # a single KB has nothing to fall back on: the caller handles it
        await service.retrieve([kb.id], "refund", top_k=3)
    assert seen == [("embedding", "fake", "EmbeddingError")]


async def test_retrieval_reranker_failure_reported(session, kbs, owner, adapters, kb_settings, bus, store):
    kb = await _kb(kbs, owner)
    kb.reranker, kb.reranker_params = "cross_encoder", {"model": "evil/model"}  # unusable reranker config
    await session.flush()
    await _index(session, store, kb, TEXTS)
    seen = []

    async def on_error(kind, provider, error_type):
        seen.append((kind, provider, error_type))

    service = RetrievalService(
        session, kb.organisation_id, replace(adapters, vector_store=store), settings=kb_settings, events=bus, on_error=on_error
    )
    result = await service.retrieve([kb.id], "refund policy thirty days", top_k=3)
    assert result.warnings == [f"reranker_unavailable:{kb.id}"]
    assert len(seen) == 1 and seen[0][:2] == ("rerank", "cross_encoder")
