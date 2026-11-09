"""Indexing pipeline (Task 11): job handlers run through the inline queue against the real test DB.

Everything here really commits (session_factory fixture, truncated at teardown); the pgvector
kb_vectors_* tables are dropped at teardown too. Embeddings come from the test-only fake provider.
"""

import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import func, select, text

from supportcore.ai.embeddings.fake_embedder import FakeEmbedder
from supportcore.ai.ports import EmbedderFactory
from supportcore.config import Settings
from supportcore.db.models import Chunk, Document, IndexingJob, IntegrationEvent, KnowledgeBase, ProviderCredential, User
from supportcore.db.uow import UnitOfWork
from supportcore.events import EventBus
from supportcore.jobs.ports import JobRuntime, UnknownJobError
from supportcore.knowledge.chunking.ports import ChunkingParams
from supportcore.knowledge.document_service import DocumentService
from supportcore.knowledge.jobs import JOB_HANDLERS, dispatch
from supportcore.knowledge.kb_service import EmbeddingChoice, KnowledgeBaseService, collection_name
from supportcore.knowledge.parsing.ports import EMPTY_TEXT_MESSAGE
from supportcore.observability.logging import configure_logging
from supportcore.orgs.service import OrganisationService
from supportcore.registry import ProviderRegistry
from supportcore.testing import TEST_DATABASE_URL
from supportcore.wiring import build_adapters

STEP_NAMES = ["download", "parse", "chunk", "embed", "upsert"]
SECRET_KEY = "sk-test-SECRETSECRETSECRET1234"

# ~3 KB of markdown: several chunks at chunk_size=400.
DOC_TEXT = "# Refunds\n\n" + "\n\n".join(
    f"Paragraph {i}: customers may request a refund within thirty days of purchase. " * 3 for i in range(12)
)


@dataclass
class EmbedderControl:
    """Test double for the embedder factory: fake embedders whose failure can be switched on/off."""

    fail_on: str | None = None
    created: list[dict] = field(default_factory=list)

    def factory(self, settings: Settings) -> EmbedderFactory:
        registry: ProviderRegistry = ProviderRegistry("embedder")
        dims = {"fake-embedding": 8, "fake-embedding-16": 16, "text-embedding-3-small": 1536}

        def make(provider: str):
            def create(*, model: str, api_key, base_url, settings):
                self.created.append({"provider": provider, "model": model, "api_key": api_key, "base_url": base_url})
                return FakeEmbedder(dimensions=dims.get(model, 8), model_id=model, fail_on=self.fail_on)

            return create

        for provider in ("fake", "openai", "openai_compatible"):
            registry.register(provider)(make(provider))
        return EmbedderFactory(registry)


class HeldJobs:
    """Stands in for the queue so enqueued jobs wait until release() hands them to the real one."""

    def __init__(self, queue) -> None:
        self.queue = queue
        self.jobs: list[tuple[str, dict]] = []

    def enqueue(self, name: str, payload: dict, *, countdown: float | None = None) -> None:
        if countdown is not None:
            self.queue.enqueue(name, payload, countdown=countdown)
        else:
            self.jobs.append((name, payload))

    def release(self) -> None:
        for name, payload in self.jobs:
            self.queue.enqueue(name, payload)
        self.jobs = []


@asynccontextmanager
async def held_jobs(runtime):
    held = HeldJobs(runtime.adapters.job_queue)
    runtime.adapters.job_queue = held
    try:
        yield held
    finally:
        runtime.adapters.job_queue = held.queue


class HookStorage:
    """Delegating FileStorage that can run an async hook on get() (to act 'during' a job)."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.on_get = None

    async def put(self, key, data, content_type):
        await self.inner.put(key, data, content_type)

    async def get(self, key):
        data = await self.inner.get(key)
        if self.on_get is not None:
            await self.on_get(key)
        return data

    async def delete(self, key):
        await self.inner.delete(key)

    async def delete_prefix(self, prefix):
        return await self.inner.delete_prefix(prefix)


@pytest.fixture
def idx_settings(tmp_path) -> Settings:
    from cryptography.fernet import Fernet

    return Settings(
        _env_file=None,
        env="test",
        database_url=TEST_DATABASE_URL,
        redis_url="redis://localhost:6379/0",
        jwt_secret="test-secret-test-secret-test-secret-123",
        smtp_from="noreply@test.local",
        secrets_key=Fernet.generate_key().decode(),
        file_storage="local",
        local_storage_path=str(tmp_path / "uploads"),
        vector_store="pgvector",
        job_queue="inline",
        embed_batch_size=4,
        allow_private_base_urls=False,
    )


@pytest.fixture
def embedder_control() -> EmbedderControl:
    return EmbedderControl()


async def _drop_vector_tables(engine) -> None:
    async with engine.begin() as conn:
        names = (
            await conn.execute(text("SELECT tablename FROM pg_tables WHERE tablename LIKE 'kb\\_vectors\\_%'"))
        ).scalars()
        for name in list(names):
            assert name.removeprefix("kb_vectors_").isdigit()
            await conn.execute(text(f'DROP TABLE IF EXISTS "{name}"'))
        await conn.execute(text("DELETE FROM vector_collections"))


@pytest.fixture
async def runtime(idx_settings, db_engine, session_factory, embedder_control):
    holder: dict[str, JobRuntime] = {}
    base = build_adapters(idx_settings, engine=db_engine, runtime_factory=lambda: holder["rt"])
    adapters = replace(base, embedders=embedder_control.factory(idx_settings), storage=HookStorage(base.storage))
    holder["rt"] = JobRuntime(settings=idx_settings, session_factory=session_factory, adapters=adapters, events=EventBus())
    yield holder["rt"]
    await adapters.job_queue.drain()
    await _drop_vector_tables(db_engine)


async def drain(runtime: JobRuntime) -> None:
    await runtime.adapters.job_queue.drain()


@dataclass
class World:
    org_id: uuid.UUID
    owner: User
    kb_id: uuid.UUID | None = None
    doc_id: uuid.UUID | None = None


async def make_world(runtime: JobRuntime) -> World:
    async with UnitOfWork(runtime.session_factory) as uow:
        owner = User(email=f"owner-{uuid.uuid4().hex[:8]}@x.com", password_hash="x", name="Owner")
        uow.session.add(owner)
        await uow.session.flush()
        org = await OrganisationService(uow.session, runtime.events).create(owner, "Acme")
        await uow.commit()
    return World(org_id=org.id, owner=owner)


def kbs(runtime: JobRuntime, uow: UnitOfWork, world: World, settings: Settings | None = None) -> KnowledgeBaseService:
    return KnowledgeBaseService(uow.session, world.org_id, runtime.events, runtime.adapters, settings or runtime.settings)


def docs(runtime: JobRuntime, uow: UnitOfWork, world: World) -> DocumentService:
    return DocumentService(uow.session, world.org_id, runtime.events, runtime.adapters, runtime.settings)


async def create_kb(runtime, world, *, model="fake-embedding", provider="fake", credential_id=None, settings=None):
    async with UnitOfWork(runtime.session_factory) as uow:
        kb = await kbs(runtime, uow, world, settings).create(
            name=f"KB {uuid.uuid4().hex[:6]}",
            description="",
            embedding_provider=provider,
            embedding_model=model,
            embedding_credential_id=credential_id,
            chunking=ChunkingParams(strategy="recursive", chunk_size=400, chunk_overlap=0),
            reranker="none",
            reranker_params={},
            actor=world.owner,
        )
        await uow.commit()
    world.kb_id = kb.id
    return kb.id


async def upload(runtime, world, data: bytes, filename: str = "refunds.md") -> uuid.UUID:
    async with UnitOfWork(runtime.session_factory) as uow:
        doc = await docs(runtime, uow, world).upload(world.kb_id, filename, data, world.owner)
        await uow.commit()
    world.doc_id = doc.id
    return doc.id


async def start_indexing(runtime, world, ids=None) -> list[uuid.UUID]:
    async with UnitOfWork(runtime.session_factory) as uow:
        jobs = await docs(runtime, uow, world).start_indexing(world.kb_id, ids, world.owner)
        await uow.commit()
    return [j.id for j in jobs]


async def update_embedding(runtime, world, model: str) -> bool:
    async with UnitOfWork(runtime.session_factory) as uow:
        _kb, rebuild = await kbs(runtime, uow, world).update(
            world.kb_id, embedding=EmbeddingChoice("fake", model), actor=world.owner
        )
        await uow.commit()
    return rebuild


async def load(runtime, model, id_):
    async with runtime.session_factory() as s:
        return await s.get(model, id_)


async def chunk_count(runtime, document_id, version) -> int:
    async with runtime.session_factory() as s:
        stmt = select(func.count()).select_from(Chunk).where(Chunk.document_id == document_id, Chunk.kb_version == version)
        return (await s.execute(stmt)).scalar_one()


async def kb_chunk_count(runtime, kb_id, version) -> int:
    async with runtime.session_factory() as s:
        stmt = select(func.count()).select_from(Chunk).where(Chunk.knowledge_base_id == kb_id, Chunk.kb_version == version)
        return (await s.execute(stmt)).scalar_one()


async def vector_count(runtime, world, version) -> int:
    return await runtime.adapters.vector_store.count(collection_name(world.kb_id, version), organisation_id=world.org_id)


async def latest_job(runtime, document_id=None, kind="document") -> IndexingJob:
    async with runtime.session_factory() as s:
        stmt = select(IndexingJob).where(IndexingJob.kind == kind)
        if document_id is not None:
            stmt = stmt.where(IndexingJob.document_id == document_id)
        stmt = stmt.order_by(IndexingJob.queued_at.desc(), IndexingJob.id)
        return (await s.execute(stmt)).scalars().first()


@pytest.fixture
async def kb_with_doc(runtime) -> World:
    world = await make_world(runtime)
    await create_kb(runtime, world)
    await upload(runtime, world, DOC_TEXT.encode())
    return world


async def index_v1(runtime, world) -> int:
    await start_indexing(runtime, world)
    await drain(runtime)
    doc = await load(runtime, Document, world.doc_id)
    assert doc.status == "indexed", doc.error
    return doc.chunk_count


# --- index_document --------------------------------------------------------------------------------


async def test_index_document_happy_path(kb_with_doc, runtime):
    world = kb_with_doc
    [job_id] = await start_indexing(runtime, world)
    await drain(runtime)

    doc = await load(runtime, Document, world.doc_id)
    assert doc.status == "indexed"
    assert doc.error is None
    assert doc.chunk_count > 1
    assert doc.indexed_version == 1
    assert await chunk_count(runtime, doc.id, 1) == doc.chunk_count
    assert await vector_count(runtime, world, 1) == doc.chunk_count

    job = await load(runtime, IndexingJob, job_id)
    assert job.status == "succeeded"
    assert job.error is None
    assert job.attempts == 1
    assert job.started_at is not None and job.finished_at is not None
    assert [s["name"] for s in job.steps] == STEP_NAMES
    for step in job.steps:
        assert set(step) == {"name", "status", "duration_ms", "detail"}
        assert step["status"] == "succeeded"
        assert isinstance(step["duration_ms"], int) and step["duration_ms"] >= 0
        assert isinstance(step["detail"], str) and len(step["detail"]) <= 120
    details = {s["name"]: s["detail"] for s in job.steps}
    assert details["chunk"] == f"{doc.chunk_count} chunks"
    assert details["embed"].endswith("batches") or details["embed"].endswith("batch")
    # No document text in the job log.
    assert "refund" not in json.dumps(job.steps).lower()

    # Vectors carry the tenant ids and are queryable for this org only.
    hits = await runtime.adapters.vector_store.query(
        collection_name(world.kb_id, 1), [1.0] + [0.0] * 7, organisation_id=world.org_id, k=3
    )
    assert hits and all(h.document_id == doc.id for h in hits)


async def test_empty_text_document_fails_clearly(runtime):
    world = await make_world(runtime)
    await create_kb(runtime, world)
    await upload(runtime, world, b"   \n\n  \n", filename="blank.md")
    [job_id] = await start_indexing(runtime, world)
    await drain(runtime)

    doc = await load(runtime, Document, world.doc_id)
    assert doc.status == "failed"
    assert doc.error == EMPTY_TEXT_MESSAGE
    assert doc.chunk_count == 0
    job = await load(runtime, IndexingJob, job_id)
    assert job.status == "failed"
    assert job.error == EMPTY_TEXT_MESSAGE
    assert [s["name"] for s in job.steps] == ["download", "parse"]
    assert job.steps[-1]["status"] == "failed"
    assert job.steps[-1]["detail"] == EMPTY_TEXT_MESSAGE
    assert await chunk_count(runtime, doc.id, 1) == 0


async def test_failed_job_retry_is_idempotent(runtime, embedder_control):
    world = await make_world(runtime)
    await create_kb(runtime, world)
    await upload(runtime, world, (DOC_TEXT + "\n\nBOOM goes the last paragraph.").encode())

    embedder_control.fail_on = "BOOM"
    [first] = await start_indexing(runtime, world)
    await drain(runtime)
    doc = await load(runtime, Document, world.doc_id)
    assert doc.status == "failed"
    assert "BOOM" in doc.error
    job = await load(runtime, IndexingJob, first)
    assert job.status == "failed"
    assert job.steps[-1]["name"] == "embed" and job.steps[-1]["status"] == "failed"
    assert await chunk_count(runtime, doc.id, 1) == 0
    assert await vector_count(runtime, world, 1) == 0

    embedder_control.fail_on = None
    for _ in range(2):  # retry, then reindex an already indexed document
        async with UnitOfWork(runtime.session_factory) as uow:
            assert await docs(runtime, uow, world).reindex_document(world.doc_id, world.owner) is not None
            await uow.commit()
        await drain(runtime)
        doc = await load(runtime, Document, world.doc_id)
        assert doc.status == "indexed" and doc.error is None
        assert doc.chunk_count > 1
        assert await chunk_count(runtime, doc.id, 1) == doc.chunk_count
        assert await vector_count(runtime, world, 1) == doc.chunk_count


async def test_rerunning_the_same_job_does_not_duplicate(kb_with_doc, runtime):
    """A redelivered message (acks_late) for a job that already finished does nothing."""
    world = kb_with_doc
    [job_id] = await start_indexing(runtime, world)
    await drain(runtime)
    n = (await load(runtime, Document, world.doc_id)).chunk_count

    await dispatch(runtime, "index_document", {"job_id": str(job_id), "organisation_id": str(world.org_id)})
    assert (await load(runtime, IndexingJob, job_id)).attempts == 1
    assert await chunk_count(runtime, world.doc_id, 1) == n
    assert await vector_count(runtime, world, 1) == n


async def _change_kb_in_place(runtime, kb_id, **columns):
    """What an in-place config change does to the KB row, applied directly (KnowledgeBaseService
    only changes a KB in place when no document is queued, processing or indexed)."""
    async with UnitOfWork(runtime.session_factory) as uow:
        kb = await uow.session.get(KnowledgeBase, kb_id)
        for name, value in columns.items():
            setattr(kb, name, value)
        await uow.commit()


async def test_index_document_retargets_to_current_version(runtime):
    """The job was queued for v1, then a dimension change moved the KB to v2 before it started."""
    world = await make_world(runtime)
    await create_kb(runtime, world)
    await upload(runtime, world, DOC_TEXT.encode())
    await drain(runtime)
    async with held_jobs(runtime) as held:  # the job only starts after the change
        [job_id] = await start_indexing(runtime, world)
        await _change_kb_in_place(
            runtime, world.kb_id, embedding_model="fake-embedding-16", embedding_dimensions=16, active_version=2
        )
    held.release()
    await drain(runtime)

    job = await load(runtime, IndexingJob, job_id)
    assert job.status == "succeeded"
    assert job.kb_version == 2
    assert job.steps[0] == {"name": "retarget", "status": "succeeded", "duration_ms": 0, "detail": "v1→v2"}
    assert [s["name"] for s in job.steps[1:]] == STEP_NAMES
    doc = await load(runtime, Document, world.doc_id)
    assert doc.indexed_version == 2
    assert await vector_count(runtime, world, 2) == doc.chunk_count
    assert await chunk_count(runtime, doc.id, 1) == 0


async def test_same_dimension_change_mid_job_rebuilds_with_the_new_config(kb_with_doc, runtime, embedder_control):
    """A same-size model and chunking change lands in place (same version) after the job read its
    config: the store step notices and builds again, so one collection never mixes two models."""
    world = kb_with_doc

    async def change(_key):
        runtime.adapters.storage.on_get = None
        await _change_kb_in_place(
            runtime, world.kb_id, embedding_model="fake-embedding-alt",
            chunking={"strategy": "fixed", "chunk_size": 200, "chunk_overlap": 0},
        )

    runtime.adapters.storage.on_get = change
    [job_id] = await start_indexing(runtime, world)
    await drain(runtime)

    job = await load(runtime, IndexingJob, job_id)
    assert job.status == "succeeded", job.error
    assert job.kb_version == 1
    retargets = [s for s in job.steps if s["name"] == "retarget"]
    assert retargets == [{"name": "retarget", "status": "succeeded", "duration_ms": 0, "detail": "v1: settings changed"}]
    assert [c["model"] for c in embedder_control.created] == ["fake-embedding", "fake-embedding-alt"]
    doc = await load(runtime, Document, world.doc_id)
    assert (doc.status, doc.indexed_version) == ("indexed", 1)
    old_chunks = len(runtime.adapters.chunkers["recursive"].chunk(
        runtime.adapters.parsers["markdown"].parse(DOC_TEXT.encode(), "refunds.md"),
        ChunkingParams(strategy="recursive", chunk_size=400, chunk_overlap=0),
    ))
    new_chunks = len(runtime.adapters.chunkers["fixed"].chunk(
        runtime.adapters.parsers["markdown"].parse(DOC_TEXT.encode(), "refunds.md"),
        ChunkingParams(strategy="fixed", chunk_size=200, chunk_overlap=0),
    ))
    assert new_chunks != old_chunks and doc.chunk_count == new_chunks  # built with the new chunking
    assert await chunk_count(runtime, doc.id, 1) == doc.chunk_count == await vector_count(runtime, world, 1)


async def test_queued_document_makes_config_change_a_rebuild(runtime):
    """Nothing indexed yet, but a document is on its way: changing the model must rebuild, not
    switch in place under the in-flight job."""
    world = await make_world(runtime)
    await create_kb(runtime, world)
    await upload(runtime, world, DOC_TEXT.encode())
    async with held_jobs(runtime) as held:
        await start_indexing(runtime, world)
        assert await update_embedding(runtime, world, "fake-embedding-16") is True
    held.release()
    await drain(runtime)
    await wait_for_rebuild(runtime)
    kb = await load(runtime, KnowledgeBase, world.kb_id)
    assert (kb.active_version, kb.building_version, kb.embedding_model) == (2, None, "fake-embedding-16")
    doc = await load(runtime, Document, world.doc_id)
    assert (doc.status, doc.indexed_version) == ("indexed", 2)
    assert await vector_count(runtime, world, 2) == doc.chunk_count > 0


async def test_deleted_document_job_exits_cleanly(kb_with_doc, runtime):
    world = kb_with_doc
    await start_indexing(runtime, world)
    async with UnitOfWork(runtime.session_factory) as uow:  # deleted before the job runs
        await docs(runtime, uow, world).delete(world.doc_id, world.owner)
        await uow.commit()
    await drain(runtime)  # index_document finds nothing; purge_document runs; nothing raises
    assert await latest_job(runtime) is None
    assert await vector_count(runtime, world, 1) == 0


async def test_document_deleted_mid_job_leaves_no_vectors(kb_with_doc, runtime):
    world = kb_with_doc

    async def delete_now(_key):
        runtime.adapters.storage.on_get = None
        async with UnitOfWork(runtime.session_factory) as uow:
            await docs(runtime, uow, world).delete(world.doc_id, world.owner)
            await uow.commit()

    runtime.adapters.storage.on_get = delete_now
    await start_indexing(runtime, world)
    await drain(runtime)
    assert await load(runtime, Document, world.doc_id) is None
    assert await kb_chunk_count(runtime, world.kb_id, 1) == 0
    assert await vector_count(runtime, world, 1) == 0


async def test_job_payload_for_another_org_touches_nothing(kb_with_doc, runtime):
    """Jobs are loaded through the payload's organisation: a mismatched org finds no job and exits."""
    world = kb_with_doc
    other = await make_world(runtime)
    async with held_jobs(runtime) as held:
        [job_id] = await start_indexing(runtime, world)
    await dispatch(runtime, "index_document", {"job_id": str(job_id), "organisation_id": str(other.org_id)})
    job = await load(runtime, IndexingJob, job_id)
    assert (job.status, job.attempts, job.steps) == ("queued", 0, [])  # invisible to the other org
    assert (await load(runtime, Document, world.doc_id)).status == "queued"
    held.release()
    await drain(runtime)
    assert (await load(runtime, IndexingJob, job_id)).status == "succeeded"


async def test_openai_embedding_uses_decrypted_key_and_never_logs_it(runtime, embedder_control, capsys):
    configure_logging(runtime.settings)
    world = await make_world(runtime)
    async with UnitOfWork(runtime.session_factory) as uow:
        cred = ProviderCredential(
            organisation_id=world.org_id,
            provider="openai",
            label="OpenAI",
            encrypted_api_key=runtime.adapters.cipher.encrypt(SECRET_KEY),
            last4=SECRET_KEY[-4:],
        )
        uow.session.add(cred)
        await uow.commit()
    await create_kb(runtime, world, provider="openai", model="text-embedding-3-small", credential_id=cred.id)
    await upload(runtime, world, DOC_TEXT.encode())
    [job_id] = await start_indexing(runtime, world)
    await drain(runtime)

    assert (await load(runtime, Document, world.doc_id)).status == "indexed"
    used = [c for c in embedder_control.created if c["provider"] == "openai"]
    assert used and all(c["api_key"] == SECRET_KEY for c in used)
    job = await load(runtime, IndexingJob, job_id)
    assert SECRET_KEY not in json.dumps(job.steps)
    assert SECRET_KEY not in capsys.readouterr().out


async def test_openai_compatible_private_base_url_is_refused_before_embedding(runtime, embedder_control):
    world = await make_world(runtime)
    async with UnitOfWork(runtime.session_factory) as uow:
        cred = ProviderCredential(
            organisation_id=world.org_id,
            provider="openai_compatible",
            label="Ollama",
            encrypted_api_key=runtime.adapters.cipher.encrypt(SECRET_KEY),
            base_url="http://127.0.0.1:11434/v1",
            last4=SECRET_KEY[-4:],
        )
        uow.session.add(cred)
        await uow.commit()
    permissive = runtime.settings.model_copy(update={"allow_private_base_urls": True})
    await create_kb(
        runtime, world, provider="openai_compatible", model="nomic-embed", credential_id=cred.id, settings=permissive
    )
    await upload(runtime, world, DOC_TEXT.encode())
    embedder_control.created.clear()
    [job_id] = await start_indexing(runtime, world)
    await drain(runtime)  # runtime settings refuse private base URLs

    doc = await load(runtime, Document, world.doc_id)
    assert doc.status == "failed"
    assert doc.error == "Base URL must be a public address"
    assert embedder_control.created == []
    job = await load(runtime, IndexingJob, job_id)
    assert job.steps[-1]["name"] == "embed" and job.steps[-1]["status"] == "failed"


async def test_unexpected_error_is_readable_and_counted(kb_with_doc, runtime):
    world = kb_with_doc

    async def explode(_key):
        raise ZeroDivisionError("secret internals")

    runtime.adapters.storage.on_get = explode
    labels = {"kind": "document", "status": "failed", "organisation_id": str(world.org_id)}
    before = REGISTRY.get_sample_value("support_indexing_jobs_total", labels) or 0
    [job_id] = await start_indexing(runtime, world)
    await drain(runtime)

    doc = await load(runtime, Document, world.doc_id)
    assert doc.status == "failed"
    assert doc.error == "Indexing failed: ZeroDivisionError"
    job = await load(runtime, IndexingJob, job_id)
    assert job.status == "failed" and job.error == doc.error
    assert job.steps[-1]["name"] == "download" and job.steps[-1]["status"] == "failed"
    assert REGISTRY.get_sample_value("support_indexing_jobs_total", labels) == before + 1


async def test_metrics_count_jobs_chunks_and_embedding_requests(kb_with_doc, runtime):
    world = kb_with_doc
    org = str(world.org_id)
    jobs_before = REGISTRY.get_sample_value(
        "support_indexing_jobs_total", {"kind": "document", "status": "succeeded", "organisation_id": org}
    ) or 0
    embeds_before = REGISTRY.get_sample_value(
        "support_embedding_requests_total", {"provider": "fake", "status": "succeeded"}
    ) or 0
    durations_before = REGISTRY.get_sample_value("support_indexing_job_duration_seconds_count", {"kind": "document"}) or 0
    n = await index_v1(runtime, world)
    assert REGISTRY.get_sample_value(
        "support_indexing_jobs_total", {"kind": "document", "status": "succeeded", "organisation_id": org}
    ) == jobs_before + 1
    assert REGISTRY.get_sample_value("support_indexed_chunks_total", {"organisation_id": org}) == n
    batches = -(-n // runtime.settings.embed_batch_size)
    assert REGISTRY.get_sample_value(
        "support_embedding_requests_total", {"provider": "fake", "status": "succeeded"}
    ) == embeds_before + batches
    assert REGISTRY.get_sample_value(
        "support_indexing_job_duration_seconds_count", {"kind": "document"}
    ) == durations_before + 1


async def test_job_logs_carry_org_context(kb_with_doc, runtime, capsys):
    configure_logging(runtime.settings)
    world = kb_with_doc
    [job_id] = await start_indexing(runtime, world)
    capsys.readouterr()
    await drain(runtime)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    job_lines = [line for line in lines if line.get("job_id") == str(job_id)]
    events = [line["event"] for line in job_lines]
    assert "indexing_job_started" in events and "indexing_job_finished" in events
    assert all(line["organisation_id"] == str(world.org_id) for line in job_lines)
    assert "refund" not in json.dumps(lines).lower()  # no document text in logs


# --- rebuild_kb ------------------------------------------------------------------------------------


async def test_rebuild_keeps_serving_old_version_until_swap(kb_with_doc, runtime):
    world = kb_with_doc
    n = await index_v1(runtime, world)
    async with held_jobs(runtime) as held:  # the rebuild starts once the observer is in place
        assert await update_embedding(runtime, world, "fake-embedding-16") is True

    seen_during: list[tuple] = []

    async def observe(_key):
        kb = await load(runtime, KnowledgeBase, world.kb_id)
        hits = await runtime.adapters.vector_store.query(
            collection_name(world.kb_id, kb.active_version), [1.0] + [0.0] * (kb.embedding_dimensions - 1),
            organisation_id=world.org_id, k=3,
        )
        seen_during.append((kb.active_version, kb.building_version, kb.embedding_model, len(hits)))

    runtime.adapters.storage.on_get = observe
    held.release()
    await drain(runtime)

    assert seen_during == [(1, 2, "fake-embedding", 3)]
    kb = await load(runtime, KnowledgeBase, world.kb_id)
    assert (kb.active_version, kb.building_version) == (2, None)
    assert (kb.embedding_model, kb.embedding_dimensions) == ("fake-embedding-16", 16)
    assert kb.building_embedding_provider is None and kb.building_embedding_model is None
    assert kb.building_embedding_dimensions is None and kb.building_chunking is None
    assert await vector_count(runtime, world, 1) == 0  # old collection dropped
    assert await kb_chunk_count(runtime, world.kb_id, 1) == 0  # old chunks gone
    doc = await load(runtime, Document, world.doc_id)
    assert (doc.status, doc.indexed_version, doc.chunk_count) == ("indexed", 2, n)
    assert await kb_chunk_count(runtime, world.kb_id, 2) == n
    assert await vector_count(runtime, world, 2) == n
    job = await latest_job(runtime, kind="rebuild")
    assert job.status == "succeeded" and job.kb_version == 2 and job.attempts == 1
    names = [s["name"] for s in job.steps]
    assert names == ["cleanup", "ensure_collection", "document", "swap"]
    assert job.steps[-1]["detail"] == "v1→v2"
    assert job.steps[2]["detail"].endswith(f"{n} chunks")


async def test_rebuild_all_failed_does_not_swap(kb_with_doc, runtime, embedder_control):
    world = kb_with_doc
    await upload(runtime, world, b"# Second\n\nBOOM shipping times vary by region.", filename="ship.md")
    await start_indexing(runtime, world)
    await drain(runtime)
    first = (await load(runtime, Document, kb_with_doc.doc_id))
    v1_chunks = await kb_chunk_count(runtime, world.kb_id, 1)
    v1_vectors = await vector_count(runtime, world, 1)
    assert v1_chunks == v1_vectors > 0

    embedder_control.fail_on = " "  # every chunk of every document contains a space: all fail
    assert await update_embedding(runtime, world, "fake-embedding-16") is True
    await drain(runtime)

    kb = await load(runtime, KnowledgeBase, world.kb_id)
    assert (kb.active_version, kb.building_version) == (1, None)
    assert (kb.embedding_model, kb.embedding_dimensions) == ("fake-embedding", 8)
    assert kb.building_embedding_model is None and kb.building_chunking is None
    assert await kb_chunk_count(runtime, world.kb_id, 2) == 0
    assert await vector_count(runtime, world, 2) == 0
    assert await kb_chunk_count(runtime, world.kb_id, 1) == v1_chunks  # still serving v1
    assert await vector_count(runtime, world, 1) == v1_vectors
    doc = await load(runtime, Document, first.id)
    assert (doc.status, doc.indexed_version) == ("indexed", 1)
    job = await latest_job(runtime, kind="rebuild")
    assert job.status == "failed"
    assert "version 1" in job.error
    doc_steps = [s for s in job.steps if s["name"] == "document"]
    assert len(doc_steps) == 2 and all(s["status"] == "failed" for s in doc_steps)


async def test_rebuild_drops_stale_version_and_skips_deleted_documents(kb_with_doc, runtime):
    world = kb_with_doc
    n = await index_v1(runtime, world)
    second = await upload(runtime, world, b"# Other\n\nShipping takes three days.", filename="ship.md")
    await start_indexing(runtime, world)
    await drain(runtime)
    async with held_jobs(runtime) as held:  # the rebuild waits until the leftovers are in place
        assert await update_embedding(runtime, world, "fake-embedding-16") is True

    # Leftovers of an earlier failed rebuild at v2: a stale chunk row and a stale vector.
    from supportcore.knowledge.vectorstore.ports import VectorRecord

    stale_id = uuid.uuid4()
    async with UnitOfWork(runtime.session_factory) as uow:
        uow.session.add(Chunk(
            id=stale_id, organisation_id=world.org_id, knowledge_base_id=world.kb_id, document_id=world.doc_id,
            kb_version=2, ordinal=99, text="stale", char_start=0, char_end=5,
        ))
        await uow.commit()
    name2 = collection_name(world.kb_id, 2)
    store = runtime.adapters.vector_store
    await store.ensure_collection(name2, 16, organisation_id=world.org_id, knowledge_base_id=world.kb_id)
    await store.upsert(name2, [VectorRecord(stale_id, world.doc_id, world.org_id, world.kb_id, [1.0] + [0.0] * 15)],
                       organisation_id=world.org_id)

    async def delete_second(_key):  # the second document is deleted while the rebuild runs
        runtime.adapters.storage.on_get = None
        async with UnitOfWork(runtime.session_factory) as uow:
            await docs(runtime, uow, world).delete(second, world.owner)
            await uow.commit()

    runtime.adapters.storage.on_get = delete_second
    held.release()
    await drain(runtime)

    kb = await load(runtime, KnowledgeBase, world.kb_id)
    assert kb.active_version == 2
    assert await kb_chunk_count(runtime, world.kb_id, 2) == n
    assert await vector_count(runtime, world, 2) == n
    job = await latest_job(runtime, kind="rebuild")
    assert job.status == "succeeded"
    statuses = sorted(s["status"] for s in job.steps if s["name"] == "document")
    assert statuses == ["skipped", "succeeded"]
    assert job.steps[0] == {**job.steps[0], "name": "cleanup", "detail": "removed 1 stale chunk"}


async def test_kb_delete_purges_collections_and_files(kb_with_doc, runtime, idx_settings):
    world = kb_with_doc
    await index_v1(runtime, world)
    async with UnitOfWork(runtime.session_factory) as uow:
        await kbs(runtime, uow, world).delete(world.kb_id, world.owner)
        await uow.commit()
    await drain(runtime)
    assert await vector_count(runtime, world, 1) == 0
    from pathlib import Path

    assert not any(p.is_file() for p in Path(idx_settings.local_storage_path).rglob("*"))


# --- dispatch / queue ------------------------------------------------------------------------------


def test_job_handlers_registered():
    assert set(JOB_HANDLERS) == {
        "ensure_collection",
        "index_document",
        "rebuild_kb",
        "purge_document",
        "purge_kb",
        "close_idle_conversations",
        "check_sla",
        "rollup_analytics",
        "sync_mailboxes",
        "sync_mailbox",
        "send_email",
        "finalise_stale_calls",
        "start_call_recording",
        "fetch_call_recording",
        "purge_call_recordings",
        "sync_billing_plan",
        "report_usage",
        "reconcile_usage_counters",
        "billing_alerts",
    }


async def test_dispatch_unknown_job_raises(runtime):
    with pytest.raises(UnknownJobError):
        await dispatch(runtime, "nope", {})


async def test_inline_queue_drain_awaits_jobs_enqueued_by_jobs():
    from supportcore.jobs.inline_queue import InlineJobQueue

    ran: list[str] = []

    async def fake_dispatch(rt, name, payload):
        ran.append(name)
        if name == "first":
            queue.enqueue("second", {})

    queue = InlineJobQueue(lambda: object(), dispatch=fake_dispatch)
    queue.enqueue("first", {})
    assert ran == []  # scheduled, not run synchronously
    await queue.drain()
    assert ran == ["first", "second"]


async def test_inline_queue_drain_reraises_job_errors():
    from supportcore.jobs.inline_queue import InlineJobQueue

    async def boom(rt, name, payload):
        raise RuntimeError("job crashed")

    queue = InlineJobQueue(lambda: object(), dispatch=boom)
    queue.enqueue("x", {})
    with pytest.raises(RuntimeError, match="job crashed"):
        await queue.drain()
    await queue.drain()  # errors are reported once


def test_build_adapters_inline_requires_runtime_factory(idx_settings):
    with pytest.raises(ValueError, match="runtime"):
        build_adapters(idx_settings)


def test_build_adapters_celery_queue(idx_settings):
    from supportcore.jobs.celery_queue import CeleryJobQueue

    adapters = build_adapters(idx_settings.model_copy(update={"job_queue": "celery", "vector_store": "chroma"}))
    assert isinstance(adapters.job_queue, CeleryJobQueue)
    # A black-holed broker must not stall a request: short connect timeouts, two quick publish retries.
    conf = adapters.job_queue._app.conf
    assert conf.broker_connection_timeout == 5
    assert conf.task_publish_retry is True
    assert conf.task_publish_retry_policy == {"max_retries": 2, "interval_start": 0.2, "interval_step": 0.4, "interval_max": 1}
    assert conf.broker_transport_options == {"socket_connect_timeout": 5, "socket_timeout": 5}


def test_celery_queue_sends_run_job_by_name():
    from supportcore.jobs.celery_queue import CeleryJobQueue

    sent = []

    class FakeCelery:
        def send_task(self, name, args=None, **kw):
            sent.append((name, args))

    CeleryJobQueue(FakeCelery()).enqueue("index_document", {"job_id": "j", "organisation_id": "o"})
    assert sent == [("supportworker.run_job", ["index_document", {"job_id": "j", "organisation_id": "o"}])]


# --- versions that die while a job runs (review fix round 1) ---------------------------------------


async def wait_for_rebuild(runtime, timeout: float = 15.0) -> IndexingJob:
    import asyncio

    for _ in range(int(timeout / 0.05)):
        job = await latest_job(runtime, kind="rebuild")
        if job is not None and job.status in ("succeeded", "failed"):
            return job
        await asyncio.sleep(0.05)
    raise AssertionError("rebuild did not finish")


async def test_document_indexed_during_failed_rebuild_ends_up_served(kb_with_doc, runtime, embedder_control):
    """Reviewer's case: B is indexed (into the building v2) while the rebuild's only document fails.
    The abandon must not leave B in the discarded v2."""
    world = kb_with_doc
    await index_v1(runtime, world)
    embedder_control.fail_on = "refund"  # only the first document fails in the rebuild
    async with held_jobs(runtime) as held:
        assert await update_embedding(runtime, world, "fake-embedding-16") is True
    state = {}

    async def add_second(_key):
        runtime.adapters.storage.on_get = None
        state["b"] = await upload(runtime, world, b"# Ship\n\nShipping takes three days.", filename="ship.md")
        state["jobs"] = await start_indexing(runtime, world, ids=[state["b"]])

    runtime.adapters.storage.on_get = add_second
    held.release()
    await drain(runtime)

    kb = await load(runtime, KnowledgeBase, world.kb_id)
    assert (kb.active_version, kb.building_version) == (1, None)
    b = await load(runtime, Document, state["b"])
    assert (b.status, b.indexed_version) == ("indexed", 1)
    assert await chunk_count(runtime, b.id, 1) == b.chunk_count > 0
    assert await kb_chunk_count(runtime, world.kb_id, 2) == 0
    assert await vector_count(runtime, world, 2) == 0
    assert (await latest_job(runtime, kind="rebuild")).status == "failed"
    a = await load(runtime, Document, world.doc_id)
    assert (a.status, a.indexed_version) == ("indexed", 1)


async def test_document_job_finishing_after_swap_is_retargeted(kb_with_doc, runtime):
    """B's job started at v1; the rebuild swapped v2 in (dropping v1) before B's store step. B must
    land in v2, and v1 must not be re-created."""
    world = kb_with_doc
    n_a = await index_v1(runtime, world)
    b_id = await upload(runtime, world, b"# Ship\n\nShipping takes three days.", filename="ship.md")

    async def rebuild_meanwhile(_key):
        runtime.adapters.storage.on_get = None
        assert await update_embedding(runtime, world, "fake-embedding-16") is True
        assert (await wait_for_rebuild(runtime)).status == "succeeded"

    runtime.adapters.storage.on_get = rebuild_meanwhile
    [job_id] = await start_indexing(runtime, world, ids=[b_id])
    await drain(runtime)

    kb = await load(runtime, KnowledgeBase, world.kb_id)
    assert (kb.active_version, kb.building_version) == (2, None)
    b = await load(runtime, Document, b_id)
    assert (b.status, b.indexed_version) == ("indexed", 2)
    job = await load(runtime, IndexingJob, job_id)
    assert job.status == "succeeded" and job.kb_version == 2
    assert {"name": "retarget", "status": "succeeded", "duration_ms": 0, "detail": "v1→v2"} in job.steps
    assert await kb_chunk_count(runtime, world.kb_id, 1) == 0
    assert await vector_count(runtime, world, 1) == 0  # not re-created
    assert await chunk_count(runtime, b_id, 2) == b.chunk_count
    assert await vector_count(runtime, world, 2) == n_a + b.chunk_count


async def test_swap_requeues_documents_indexed_into_the_old_version(kb_with_doc, runtime):
    """B was indexed into v1 after the rebuild listed its documents: the swap re-queues it at v2."""
    world = kb_with_doc
    n_a = await index_v1(runtime, world)
    b_id = await upload(runtime, world, b"# Ship\n\nShipping takes three days.", filename="ship.md")
    async with held_jobs(runtime) as held:
        assert await update_embedding(runtime, world, "fake-embedding-16") is True

    async def index_b_into_v1(_key):  # during the rebuild's first download: B is not in its list
        runtime.adapters.storage.on_get = None
        async with UnitOfWork(runtime.session_factory) as uow:
            b = await uow.session.get(Document, b_id)
            b.status, b.indexed_version, b.chunk_count = "indexed", 1, 1
            uow.session.add(Chunk(organisation_id=world.org_id, knowledge_base_id=world.kb_id, document_id=b_id,
                                  kb_version=1, ordinal=0, text="x", char_start=0, char_end=1))
            await uow.commit()

    runtime.adapters.storage.on_get = index_b_into_v1
    held.release()
    await drain(runtime)
    b = await load(runtime, Document, b_id)
    assert (b.status, b.indexed_version) == ("indexed", 2)
    assert await chunk_count(runtime, b_id, 2) == b.chunk_count
    assert await vector_count(runtime, world, 2) == n_a + b.chunk_count


async def test_abandon_never_discards_the_active_version(kb_with_doc, runtime):
    """E.g. a duplicate run of a rebuild whose swap fails after the other run swapped: abandoning a
    version that is now active must not touch it (nor flip the finished job)."""
    from supportcore.knowledge.indexing import IndexingService

    world = kb_with_doc
    n = await index_v1(runtime, world)
    assert await update_embedding(runtime, world, "fake-embedding-16") is True
    await drain(runtime)
    job = await latest_job(runtime, kind="rebuild")
    assert job.status == "succeeded"

    await IndexingService(runtime)._abandon(world.org_id, world.kb_id, 2, job.id, "second swap failed")
    kb = await load(runtime, KnowledgeBase, world.kb_id)
    assert kb.active_version == 2
    assert await kb_chunk_count(runtime, world.kb_id, 2) == n
    assert await vector_count(runtime, world, 2) == n
    assert (await load(runtime, IndexingJob, job.id)).status == "succeeded"


async def test_duplicate_rebuild_message_runs_once(kb_with_doc, runtime):
    world = kb_with_doc
    n = await index_v1(runtime, world)
    async with held_jobs(runtime) as held:
        assert await update_embedding(runtime, world, "fake-embedding-16") is True
    [(name, payload)] = [(nm, p) for nm, p in held.jobs if nm == "rebuild_kb"]
    held.jobs.append((name, dict(payload)))  # delivered twice
    held.release()
    await drain(runtime)

    job = await latest_job(runtime, kind="rebuild")
    assert (job.status, job.attempts) == ("succeeded", 1)
    kb = await load(runtime, KnowledgeBase, world.kb_id)
    assert kb.active_version == 2
    assert await kb_chunk_count(runtime, world.kb_id, 2) == n
    assert await vector_count(runtime, world, 2) == n


async def test_failed_reindex_during_rebuild_keeps_document_served(kb_with_doc, runtime, embedder_control):
    world = kb_with_doc
    n = await index_v1(runtime, world)
    async with held_jobs(runtime) as held:
        assert await update_embedding(runtime, world, "fake-embedding-16") is True
    embedder_control.fail_on = "refund"
    [job_id] = await start_indexing(runtime, world, ids=[world.doc_id])  # targets the building v2
    await drain(runtime)

    job = await load(runtime, IndexingJob, job_id)
    assert (job.status, job.kb_version) == ("failed", 2)
    assert "refund" in job.error
    doc = await load(runtime, Document, world.doc_id)
    assert (doc.status, doc.indexed_version, doc.chunk_count) == ("indexed", 1, n)  # v1 still serves it
    assert await vector_count(runtime, world, 1) == n
    held.release()
    await drain(runtime)  # the rebuild fails too; nothing changes for the served version
    doc = await load(runtime, Document, world.doc_id)
    assert (doc.status, doc.indexed_version) == ("indexed", 1)


async def test_recover_stale_jobs(kb_with_doc, runtime):
    from datetime import UTC, datetime, timedelta

    from supportcore.knowledge.indexing import INTERRUPTED
    from supportcore.knowledge.jobs import recover_stale_jobs

    world = kb_with_doc
    await index_v1(runtime, world)
    b_id = await upload(runtime, world, b"# Ship\n\nShipping takes three days.", filename="ship.md")
    c_id = await upload(runtime, world, b"# Other\n\nReturns are free.", filename="other.md")
    async with held_jobs(runtime):  # nothing runs: the jobs are "stuck"
        [stale_doc_job] = await start_indexing(runtime, world, ids=[b_id])
        [fresh_job] = await start_indexing(runtime, world, ids=[c_id])
        assert await update_embedding(runtime, world, "fake-embedding-16") is True
    rebuild = await latest_job(runtime, kind="rebuild")
    long_ago = datetime.now(UTC) - timedelta(hours=2)
    async with UnitOfWork(runtime.session_factory) as uow:
        for job_id, started in ((stale_doc_job, long_ago), (fresh_job, datetime.now(UTC)), (rebuild.id, long_ago)):
            job = await uow.session.get(IndexingJob, job_id)
            job.status, job.started_at, job.attempts = "running", started, 1
        for doc_id in (b_id, c_id):
            (await uow.session.get(Document, doc_id)).status = "processing"
        await uow.commit()

    assert await recover_stale_jobs(runtime) == 2
    job = await load(runtime, IndexingJob, stale_doc_job)
    assert (job.status, job.error) == ("failed", INTERRUPTED)
    b = await load(runtime, Document, b_id)
    assert (b.status, b.error) == ("failed", INTERRUPTED)
    assert (await load(runtime, IndexingJob, fresh_job)).status == "running"
    assert (await load(runtime, Document, c_id)).status == "processing"
    job = await load(runtime, IndexingJob, rebuild.id)
    assert (job.status, job.error) == ("failed", INTERRUPTED)
    kb = await load(runtime, KnowledgeBase, world.kb_id)
    assert (kb.active_version, kb.building_version, kb.building_embedding_model) == (1, None, None)
    assert await recover_stale_jobs(runtime) == 0


async def _age_job(runtime, job_id, minutes: int) -> None:
    from datetime import UTC, datetime, timedelta

    async with UnitOfWork(runtime.session_factory) as uow:
        (await uow.session.get(IndexingJob, job_id)).queued_at = datetime.now(UTC) - timedelta(minutes=minutes)
        await uow.commit()


async def test_recovery_requeues_document_jobs_lost_before_the_queue(kb_with_doc, runtime):
    from supportcore.knowledge.jobs import recover_stale_jobs

    world = kb_with_doc
    a_id = world.doc_id
    b_id = await upload(runtime, world, b"# Ship\n\nShipping takes three days.", filename="ship.md")
    async with held_jobs(runtime):  # the enqueues are lost: these messages never reach the queue
        [lost] = await start_indexing(runtime, world, ids=[a_id])
        [recent] = await start_indexing(runtime, world, ids=[b_id])
    await _age_job(runtime, lost, 11)

    assert await recover_stale_jobs(runtime) == 1
    await drain(runtime)
    assert (await load(runtime, IndexingJob, lost)).status == "succeeded"
    assert (await load(runtime, Document, a_id)).status == "indexed"
    assert (await load(runtime, IndexingJob, recent)).status == "queued"  # not 10 minutes yet
    # Sent again at most once per 10 minutes, and finished jobs are left alone.
    assert await recover_stale_jobs(runtime) == 0


async def test_recovery_requeues_a_lost_rebuild(kb_with_doc, runtime):
    from supportcore.knowledge.jobs import recover_stale_jobs

    world = kb_with_doc
    await index_v1(runtime, world)
    async with held_jobs(runtime):
        assert await update_embedding(runtime, world, "fake-embedding-16") is True
    rebuild = await latest_job(runtime, kind="rebuild")
    await _age_job(runtime, rebuild.id, 30)

    assert await recover_stale_jobs(runtime) == 1
    await drain(runtime)
    assert (await wait_for_rebuild(runtime)).status == "succeeded"
    kb = await load(runtime, KnowledgeBase, world.kb_id)
    assert (kb.active_version, kb.building_version) == (2, None)


class FakeQueueTimeout(Exception):
    pass


async def test_job_timeout_is_recorded_as_timed_out(kb_with_doc, runtime):
    from supportcore.jobs.ports import register_job_timeout_error
    from supportcore.knowledge.indexing import TIMED_OUT

    register_job_timeout_error(FakeQueueTimeout)
    world = kb_with_doc

    async def time_out(_key):
        raise FakeQueueTimeout()

    runtime.adapters.storage.on_get = time_out
    [job_id] = await start_indexing(runtime, world)
    await drain(runtime)
    assert (await load(runtime, Document, world.doc_id)).error == TIMED_OUT
    assert (await load(runtime, IndexingJob, job_id)).error == TIMED_OUT


async def test_fail_job_marks_a_running_job_failed(kb_with_doc, runtime):
    from supportcore.knowledge.indexing import TIMED_OUT
    from supportcore.knowledge.jobs import fail_job

    world = kb_with_doc
    async with held_jobs(runtime):
        [job_id] = await start_indexing(runtime, world)
    payload = {"job_id": str(job_id), "organisation_id": str(world.org_id)}
    await fail_job(runtime, "index_document", payload, TIMED_OUT)
    assert (await load(runtime, IndexingJob, job_id)).status == "failed"
    assert (await load(runtime, Document, world.doc_id)).error == TIMED_OUT
    await fail_job(runtime, "purge_kb", {}, TIMED_OUT)  # no job row: nothing to do


# --- review fix round 2 ----------------------------------------------------------------------------


class SoftLimit(Exception):
    """Stands in for Celery's SoftTimeLimitExceeded."""


async def test_soft_time_limit_inside_rebuild_abandons_it(kb_with_doc, runtime):
    """Reviewer's case: the time limit hits during the rebuild's first document. The rebuild must stop
    and keep serving v1, not record one failed document, carry on and swap without it."""
    from supportcore.jobs.ports import register_job_timeout_error
    from supportcore.knowledge.indexing import TIMED_OUT

    register_job_timeout_error(SoftLimit)
    world = kb_with_doc
    await index_v1(runtime, world)
    b = await upload(runtime, world, b"# Ship\n\nShipping takes three days.", filename="ship.md")
    await start_indexing(runtime, world, ids=[b])
    await drain(runtime)
    async with held_jobs(runtime) as held:
        assert await update_embedding(runtime, world, "fake-embedding-16") is True
    calls = []

    async def soft_limit_once(key):
        calls.append(key)
        if len(calls) == 1:
            raise SoftLimit()

    runtime.adapters.storage.on_get = soft_limit_once
    held.release()
    await drain(runtime)

    assert len(calls) == 1  # stopped at the first document
    job = await latest_job(runtime, kind="rebuild")
    assert (job.status, job.error) == ("failed", TIMED_OUT)
    assert job.steps[-1]["name"] == "document" and job.steps[-1]["status"] == "failed"
    kb = await load(runtime, KnowledgeBase, world.kb_id)
    assert (kb.active_version, kb.building_version, kb.embedding_model) == (1, None, "fake-embedding")
    for doc_id in (world.doc_id, b):
        doc = await load(runtime, Document, doc_id)
        assert (doc.status, doc.indexed_version) == ("indexed", 1)
    assert await kb_chunk_count(runtime, world.kb_id, 2) == 0
    assert await vector_count(runtime, world, 2) == 0


async def test_soft_time_limit_fails_a_document_job_as_timed_out(kb_with_doc, runtime):
    from supportcore.jobs.ports import register_job_timeout_error
    from supportcore.knowledge.indexing import TIMED_OUT

    register_job_timeout_error(SoftLimit)
    world = kb_with_doc

    async def soft_limit(_key):
        raise SoftLimit()

    runtime.adapters.storage.on_get = soft_limit
    [job_id] = await start_indexing(runtime, world)
    await drain(runtime)
    job = await load(runtime, IndexingJob, job_id)
    assert (job.status, job.error, job.steps[-1]["status"]) == ("failed", TIMED_OUT, "failed")
    assert (await load(runtime, Document, world.doc_id)).error == TIMED_OUT


async def _set_job(runtime, job_id, **fields):
    async with UnitOfWork(runtime.session_factory) as uow:
        job = await uow.session.get(IndexingJob, job_id)
        for key, value in fields.items():
            setattr(job, key, value)
        await uow.commit()


async def test_rebuild_redelivered_while_lease_held_is_rescheduled(kb_with_doc, runtime):
    from datetime import UTC, datetime

    world = kb_with_doc
    await index_v1(runtime, world)
    async with held_jobs(runtime) as held:
        assert await update_embedding(runtime, world, "fake-embedding-16") is True
    [(name, payload)] = held.jobs
    job = await latest_job(runtime, kind="rebuild")
    await _set_job(runtime, job.id, status="running", started_at=datetime.now(UTC), attempts=1)  # another run

    await dispatch(runtime, name, payload)
    [(d_name, d_payload, countdown)] = runtime.adapters.job_queue.delayed
    assert (d_name, d_payload) == ("rebuild_kb", payload)
    assert 1800 < countdown <= 1800 + 31  # just past the lease
    job = await load(runtime, IndexingJob, job.id)
    assert (job.status, job.attempts) == ("running", 1)  # untouched, and the message is not lost


async def test_rebuild_message_takes_over_an_expired_lease(kb_with_doc, runtime):
    from datetime import UTC, datetime, timedelta

    world = kb_with_doc
    n = await index_v1(runtime, world)
    async with held_jobs(runtime) as held:
        assert await update_embedding(runtime, world, "fake-embedding-16") is True
    job = await latest_job(runtime, kind="rebuild")
    await _set_job(runtime, job.id, status="running", started_at=datetime.now(UTC) - timedelta(hours=1), attempts=1)
    held.release()  # the rescheduled message arrives after the lease expired
    await drain(runtime)
    job = await load(runtime, IndexingJob, job.id)
    assert (job.status, job.attempts) == ("succeeded", 2)
    assert (await load(runtime, KnowledgeBase, world.kb_id)).active_version == 2
    assert await vector_count(runtime, world, 2) == n
    assert runtime.adapters.job_queue.delayed == []


async def test_document_job_gives_up_after_three_attempts(kb_with_doc, runtime):
    from supportcore.knowledge.indexing import FAILED_REPEATEDLY

    world = kb_with_doc
    async with held_jobs(runtime) as held:
        [job_id] = await start_indexing(runtime, world)
    await _set_job(runtime, job_id, status="running", attempts=3)  # three runs died mid-job
    held.release()
    await drain(runtime)
    job = await load(runtime, IndexingJob, job_id)
    assert (job.status, job.error, job.attempts) == ("failed", FAILED_REPEATEDLY, 3)
    doc = await load(runtime, Document, world.doc_id)
    assert (doc.status, doc.error) == ("failed", FAILED_REPEATEDLY)
    assert await chunk_count(runtime, world.doc_id, 1) == 0


def test_celery_queue_passes_countdown():
    from supportcore.jobs.celery_queue import CeleryJobQueue

    sent = []

    class FakeCelery:
        def send_task(self, name, args=None, **kw):
            sent.append((name, args, kw))

    CeleryJobQueue(FakeCelery()).enqueue("rebuild_kb", {"job_id": "j"}, countdown=90)
    assert sent == [("supportworker.run_job", ["rebuild_kb", {"job_id": "j"}], {"countdown": 90})]


# --- integration error log (Phase 5, Task 3) ---


async def test_indexing_embedding_failure_recorded(runtime, embedder_control):
    world = await make_world(runtime)
    await create_kb(runtime, world)
    await upload(runtime, world, (DOC_TEXT + "\n\nBOOM goes the last paragraph.").encode())
    embedder_control.fail_on = "BOOM"

    await start_indexing(runtime, world)
    await drain(runtime)

    async with runtime.session_factory() as s:
        rows = list((await s.execute(select(IntegrationEvent))).scalars())
    assert [(r.organisation_id, r.kind, r.provider, r.error_type, r.count) for r in rows] == [
        (world.org_id, "embedding", "fake", "EmbeddingError", 1)
    ]
    assert "BOOM" not in repr([(r.provider, r.error_type) for r in rows])
