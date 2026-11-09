"""Indexing pipeline: stored file -> parse -> chunk -> embed -> vector store + Chunk rows.

Runs in background jobs (supportcore.knowledge.jobs), each inside `job_context(organisation_id,
job_id)`. Every database step uses its own short UnitOfWork and commits, so the business sees the
job move from queued to running, each step as it finishes, and the outcome. The file storage and the
embedding provider are never called with a transaction open. The vector store is: the store step
(ensure collection, delete the document's old vectors, upsert) and a rebuild's cleanup run under the
KB's and the document's row locks, so a swap, abandon or delete cannot interleave with them.

Job log (`IndexingJob.steps`): `{"name", "status", "duration_ms", "detail"}` per step. `detail` is
short and human-readable ("12 chunks", "3 batches", an error message). Document text, chunk text and
API keys never go into steps, logs, span attributes or metric labels.

Idempotency: storing a document into a version first deletes that document's Chunk rows and vectors
for the version, under the document's row lock. A job that already finished (a redelivered message)
is skipped.

Versions: a document is only ever written into a *live* version (active_version or building_version).
The store step locks the KB, then the document (the order a document delete uses: KB -> document ->
jobs), and re-checks the version under that lock; swaps and abandons take the same KB lock. A
document job whose version died meanwhile is retargeted to the current version and built again with
that version's config (at most MAX_RETARGETS times); so is one whose version's config changed in
place (same dimensions) after the job read it. Documents left indexed in a version that a swap
or an abandon removes are re-queued at the version that stays.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.ai.ports import Embedder, EmbeddingError
from supportcore.db.models import Chunk, Document, IndexingJob, KnowledgeBase, Organisation
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.analytics.integration_errors import error_type_of, record_integration_error
from supportcore.db.uow import UnitOfWork
from supportcore.errors import DomainError
from supportcore.jobs.ports import JOB_TIME_LIMIT_SECONDS, JobRuntime, is_job_timeout
from supportcore.knowledge.chunking.ports import ChunkDraft, ChunkingParams
from supportcore.knowledge.embedding import create_embedder, embedding_key
from supportcore.knowledge.kb_service import VersionConfig, collection_name, enqueue_after_commit, version_config
from supportcore.knowledge.vectorstore.ports import VectorRecord
from supportcore.observability.context import job_context
from supportcore.observability.logging import get_logger
from supportcore.observability.metrics import (
    EMBEDDING_REQUESTS,
    INDEXED_CHUNKS,
    INDEXING_JOB_DURATION,
    INDEXING_JOBS,
)
from supportcore.storage.ports import StoredFileNotFoundError

logger = get_logger(__name__)
tracer = trace.get_tracer("supportcore.indexing")

DOCUMENT_GONE = "Document no longer exists"
FILE_MISSING = "The uploaded file could not be found in storage"
NO_CHUNKS = "No text chunks could be made from this document"
TIMED_OUT = "Indexing timed out"
INTERRUPTED = "Indexing was interrupted; retry"
VERSION_CHURN = "The knowledge base kept changing while this document was indexed; index it again"
REBUILD_SUPERSEDED = "This rebuild is no longer pending"
MAX_RETARGETS = 2
MAX_DOCUMENT_ATTEMPTS = 3  # a message that keeps killing its worker is not run a fourth time
FAILED_REPEATEDLY = "Indexing failed repeatedly; check the file and retry"
LEASE_RETRY_MARGIN_SECONDS = 30
# A job still `queued` this long after it was (last) sent has lost its message: send it again.
LOST_JOB_AFTER = timedelta(minutes=10)
MAX_ERROR_LENGTH = 500
MAX_DETAIL_LENGTH = 200
FINISHED_STATUSES = ("succeeded", "failed")

Step = dict[str, Any]
Recorder = Callable[[Step], Awaitable[None]]


def readable_error(exc: BaseException) -> str:
    """A message safe to show the business: domain messages as written, never a stack trace."""
    if is_job_timeout(exc):
        return TIMED_OUT
    if isinstance(exc, StoredFileNotFoundError):
        return FILE_MISSING  # its own message carries the storage key
    if isinstance(exc, DomainError):
        message = " ".join(str(exc).split())
        if message:
            return message[:MAX_ERROR_LENGTH]
    return f"Indexing failed: {type(exc).__name__}"


def _now() -> datetime:
    return datetime.now(UTC)


def _ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _step(name: str, status: str, duration_ms: int, detail: str) -> Step:
    return {"name": name, "status": status, "duration_ms": duration_ms, "detail": detail[:MAX_DETAIL_LENGTH]}


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {plural or word + 's'}"


def _size(n_bytes: int) -> str:
    if n_bytes < 1024:
        return _plural(n_bytes, "byte")
    if n_bytes < 1024 * 1024:
        return f"{n_bytes / 1024:.1f} KB"
    return f"{n_bytes / (1024 * 1024):.1f} MB"


class _StepFailed(Exception):
    """A pipeline step failed; `message` is readable (already recorded on the job)."""

    def __init__(self, step: str, message: str, cause: BaseException) -> None:
        super().__init__(message)
        self.step = step
        self.message = message
        self.error_type = type(cause).__name__


class _Gone(Exception):
    """The job, document or knowledge base was deleted while the job ran."""


class _VersionChanged(Exception):
    """The target version is no longer live; `version`/`config` are the KB's current target."""

    def __init__(self, version: int, config: VersionConfig) -> None:
        super().__init__(version)
        self.version = version
        self.config = config


@dataclass(frozen=True)
class _Target:
    """One document to build into one version (plain values: no ORM objects cross sessions)."""

    organisation_id: UUID
    knowledge_base_id: UUID
    document_id: UUID
    filename: str
    file_type: str
    storage_key: str
    version: int
    config: VersionConfig

    @property
    def collection(self) -> str:
        return collection_name(self.knowledge_base_id, self.version)


class IndexingService:
    def __init__(self, runtime: JobRuntime) -> None:
        self._rt = runtime
        self._settings = runtime.settings
        self._adapters = runtime.adapters

    # --- document jobs -----------------------------------------------------------------------------

    async def index_document(self, job_id: UUID, organisation_id: UUID) -> None:
        """Runs one `kind="document"` job. Never raises: failures are recorded on the job and the
        document, logged and counted."""
        started = time.monotonic()
        with tracer.start_as_current_span(
            "indexing.index_document", attributes={"organisation.id": str(organisation_id), "job.id": str(job_id)}
        ) as span:
            try:
                target = await self._start_document_job(job_id, organisation_id)
            except Exception as exc:
                logger.exception("indexing_job_start_failed", kind="document", error_type=type(exc).__name__)
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                return
            if target is None:
                return
            span.set_attribute("knowledge_base.id", str(target.knowledge_base_id))
            span.set_attribute("kb.version", target.version)
            logger.info(
                "indexing_job_started",
                kind="document",
                knowledge_base_id=str(target.knowledge_base_id),
                document_id=str(target.document_id),
                kb_version=target.version,
            )
            status, chunks, error_step, error_type = "failed", 0, None, None
            record = self._job_recorder(job_id, organisation_id)
            try:
                chunks = await self._build_retargeting(job_id, target, record)
                await self._finish_job(job_id, organisation_id, "succeeded", None)
                status = "succeeded"
            except _StepFailed as failure:
                error_step, error_type = failure.step, failure.error_type
                await self._safely(self._fail_document_job(job_id, target, failure.message))
            except _Gone:
                error_type = "Gone"
                logger.info("indexing_document_gone", document_id=str(target.document_id))
                await self._safely(self._fail_document_job(job_id, target, DOCUMENT_GONE))
            except Exception as exc:  # a bug or an infrastructure error outside the steps
                error_type = type(exc).__name__
                logger.exception("indexing_job_crashed", kind="document", error_type=error_type)
                await self._safely(self._fail_document_job(job_id, target, readable_error(exc)))
            if status != "succeeded":
                span.set_status(Status(StatusCode.ERROR, error_type or "failed"))
            self._finished("document", status, organisation_id, started, chunks=chunks, error_step=error_step,
                           error_type=error_type)

    async def _build_retargeting(self, job_id: UUID, target: _Target, record: Recorder) -> int:
        """Builds the document; if its version dies before the store step, retargets the job to the
        KB's current version (recorded as a "retarget" step) and builds again with that config."""
        for attempt in range(MAX_RETARGETS + 1):
            try:
                return await self._build(target, record)
            except _VersionChanged as changed:
                if attempt == MAX_RETARGETS:
                    await record(_step("retarget", "failed", 0, VERSION_CHURN))
                    raise _StepFailed("retarget", VERSION_CHURN, changed) from None
                await self._retarget_job(job_id, target.organisation_id, target.version, changed.version)
                target = replace(target, version=changed.version, config=changed.config)
        raise AssertionError("unreachable")

    async def _retarget_job(self, job_id: UUID, org: UUID, old: int, new: int) -> None:
        async with UnitOfWork(self._rt.session_factory) as uow:
            job = await TenantScopedRepository(uow.session, IndexingJob, org).get(job_id, for_update=True)
            if job is None:
                raise _Gone()
            job.kb_version = new
            detail = f"v{old}→v{new}" if old != new else f"v{new}: settings changed"
            job.steps = [*job.steps, _step("retarget", "succeeded", 0, detail)]
            await uow.commit()

    async def _start_document_job(self, job_id: UUID, org: UUID) -> _Target | None:
        """Marks the job running (attempts + 1) and the document processing, and commits. Returns
        None when there is nothing to do (job gone or already finished, or its document gone)."""
        async with UnitOfWork(self._rt.session_factory) as uow:
            s = uow.session
            jobs = TenantScopedRepository(s, IndexingJob, org)
            # Lock order document -> job, as when a document delete cascades to its jobs (KB -> document
            # -> job); locking the job first deadlocked with a concurrent delete.
            job = await jobs.get(job_id)
            document = None
            if job is not None and job.kind == "document" and job.document_id is not None:
                document = await TenantScopedRepository(s, Document, org).get(job.document_id, for_update=True)
                stmt = jobs.select(for_update=True).where(IndexingJob.id == job_id)
                job = (await s.execute(stmt.execution_options(populate_existing=True))).scalar_one_or_none()
            if job is None or job.kind != "document":
                logger.info("indexing_job_missing", kind="document")
                return None
            if job.status in FINISHED_STATUSES:
                logger.info("indexing_job_already_finished", kind="document", status=job.status)
                return None
            kb = await TenantScopedRepository(s, KnowledgeBase, org).get(job.knowledge_base_id)
            if kb is None or document is None:
                job.status, job.error, job.finished_at = "failed", DOCUMENT_GONE, _now()
                await uow.commit()
                INDEXING_JOBS.labels(kind="document", status="failed", organisation_id=str(org)).inc()
                logger.info("indexing_document_gone")
                return None

            if job.attempts >= MAX_DOCUMENT_ATTEMPTS:
                job.status, job.error, job.finished_at = "failed", FAILED_REPEATEDLY, _now()
                _fail_or_keep_served(document, kb, job.kb_version, FAILED_REPEATEDLY)
                await uow.commit()
                INDEXING_JOBS.labels(kind="document", status="failed", organisation_id=str(org)).inc()
                logger.warning("indexing_job_gave_up", kind="document", attempts=job.attempts)
                return None

            steps: list[Step] = []  # a new attempt starts a new log
            target_version = kb.building_version or kb.active_version
            if job.kb_version != target_version:
                steps.append(_step("retarget", "succeeded", 0, f"v{job.kb_version}→v{target_version}"))
                job.kb_version = target_version
            job.steps = steps
            job.status, job.error = "running", None
            job.attempts += 1
            job.started_at, job.finished_at = _now(), None
            document.status, document.error = "processing", None
            target = _Target(
                organisation_id=org,
                knowledge_base_id=kb.id,
                document_id=document.id,
                filename=document.filename,
                file_type=document.file_type,
                storage_key=document.storage_key,
                version=target_version,
                config=version_config(kb, target_version),
            )
            await uow.commit()
            return target

    def _job_recorder(self, job_id: UUID, org: UUID) -> Recorder:
        async def record(step: Step) -> None:
            async with UnitOfWork(self._rt.session_factory) as uow:
                job = await TenantScopedRepository(uow.session, IndexingJob, org).get(job_id, for_update=True)
                if job is None:
                    raise _Gone()
                job.steps = [*job.steps, step]
                await uow.commit()

        return record

    async def _finish_job(self, job_id: UUID, org: UUID, status: str, error: str | None) -> None:
        async with UnitOfWork(self._rt.session_factory) as uow:
            job = await TenantScopedRepository(uow.session, IndexingJob, org).get(job_id, for_update=True)
            if job is None:
                raise _Gone()
            job.status, job.error, job.finished_at = status, error, _now()
            await uow.commit()

    async def _fail_document_job(self, job_id: UUID, target: _Target, message: str) -> None:
        await self._fail_document(job_id, target.organisation_id, target.knowledge_base_id, target.document_id, message)

    async def _fail_document(self, job_id: UUID, org: UUID, kb_id: UUID, document_id: UUID, message: str) -> None:
        """Fails the job. The document fails too, unless the active version still serves an earlier
        index of it (e.g. a re-index into the version being built failed): then it stays `indexed` and
        the failure is only on the job."""
        async with UnitOfWork(self._rt.session_factory) as uow:
            s = uow.session
            kb = await TenantScopedRepository(s, KnowledgeBase, org).get(kb_id)
            document = await TenantScopedRepository(s, Document, org).get(document_id, for_update=True)
            job = await TenantScopedRepository(s, IndexingJob, org).get(job_id, for_update=True)
            if document is not None and document.status in ("processing", "queued"):
                _fail_or_keep_served(document, kb, job.kb_version if job is not None else None, message)
            if job is not None:
                job.status, job.error, job.finished_at = "failed", message, _now()
            await uow.commit()

    # --- rebuilds ----------------------------------------------------------------------------------

    async def rebuild_kb(self, kb_id: UUID, version: int, organisation_id: UUID, job_id: UUID) -> None:
        """Builds `version` (the KB's building_version) from every document while active_version keeps
        serving, then swaps it in. Never raises: failures are recorded on the job, logged, counted."""
        started = time.monotonic()
        org = organisation_id
        with tracer.start_as_current_span(
            "indexing.rebuild_kb",
            attributes={"organisation.id": str(org), "job.id": str(job_id), "knowledge_base.id": str(kb_id),
                        "kb.version": version},
        ) as span:
            try:
                start = await self._start_rebuild(kb_id, version, org, job_id)
            except Exception as exc:
                logger.exception("indexing_job_start_failed", kind="rebuild", error_type=type(exc).__name__)
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                return
            if start is None:
                return
            config, document_ids = start
            logger.info("indexing_job_started", kind="rebuild", knowledge_base_id=str(kb_id), kb_version=version,
                        documents=len(document_ids))
            record = self._job_recorder(job_id, org)
            status, chunks, error_step, error_type = "failed", 0, None, None
            try:
                await self._step("cleanup", record, lambda: self._drop_version(org, kb_id, version))
                await self._step("ensure_collection", record, lambda: self._ensure_collection(org, kb_id, version, config))
                results: dict[UUID, tuple[str, int | str]] = {}
                for document_id in document_ids:
                    outcome = await self._rebuild_document(org, kb_id, document_id, version, config, record)
                    if outcome is not None:
                        results[document_id] = outcome
                succeeded = {d: n for d, (st, n) in results.items() if st == "succeeded"}
                failed = {d: msg for d, (st, msg) in results.items() if st == "failed"}
                chunks = sum(int(n) for n in succeeded.values())
                if succeeded or not failed:
                    await self._swap(org, kb_id, version, job_id, succeeded, failed, total=len(results))
                    status = "succeeded"
                else:
                    error_type = "AllDocumentsFailed"
                    await self._abandon(org, kb_id, version, job_id, None)
            except _StepFailed as failure:
                error_step, error_type = failure.step, failure.error_type
                await self._safely(self._abandon(org, kb_id, version, job_id, failure.message))
            except _Gone:
                error_type = "Gone"
                logger.info("indexing_knowledge_base_gone")
            except Exception as exc:
                error_type = type(exc).__name__
                logger.exception("indexing_job_crashed", kind="rebuild", error_type=error_type)
                await self._safely(self._abandon(org, kb_id, version, job_id, readable_error(exc)))
            if status != "succeeded":
                span.set_status(Status(StatusCode.ERROR, error_type or "failed"))
            self._finished("rebuild", status, org, started, chunks=chunks, error_step=error_step, error_type=error_type)

    async def _start_rebuild(
        self, kb_id: UUID, version: int, org: UUID, job_id: UUID
    ) -> tuple[VersionConfig, list[UUID]] | None:
        retry_in: float | None = None
        async with UnitOfWork(self._rt.session_factory) as uow:
            s = uow.session
            kb = await TenantScopedRepository(s, KnowledgeBase, org).get(kb_id, for_update=True)  # lock order: KB first
            job = await TenantScopedRepository(s, IndexingJob, org).get(job_id, for_update=True)
            if kb is None or job is None or job.kind != "rebuild":
                logger.info("indexing_job_missing", kind="rebuild")
                return None
            if job.status in FINISHED_STATUSES:
                logger.info("indexing_job_already_finished", kind="rebuild", status=job.status)
                return None
            if job.status == "running" and job.started_at is not None and job.started_at > _lease_cutoff():
                # Another run holds it (a duplicate or redelivered message). Two runs building one
                # version would wipe each other's work, so this message comes back once the lease has
                # expired: by then the run finished, was failed by recover_stale_jobs(), or is stale
                # and this message takes over.
                retry_in = (job.started_at - _lease_cutoff()).total_seconds() + LEASE_RETRY_MARGIN_SECONDS
            elif kb.building_version != version:
                job.status, job.error, job.finished_at = "failed", REBUILD_SUPERSEDED, _now()
                await uow.commit()
                INDEXING_JOBS.labels(kind="rebuild", status="failed", organisation_id=str(org)).inc()
                return None
            else:
                config = version_config(kb, version)
                # Documents that were ever asked to be indexed; merely uploaded ones stay as they are.
                stmt = (
                    TenantScopedRepository(s, Document, org)
                    .select(Document.id)
                    .where(Document.knowledge_base_id == kb_id, Document.status != "uploaded")
                    .order_by(Document.created_at, Document.id)
                )
                document_ids = list((await s.execute(stmt)).scalars())
                job.steps, job.status, job.error = [], "running", None
                job.attempts += 1
                job.started_at, job.finished_at = _now(), None
                await uow.commit()
                return config, document_ids
        # Lease held: nothing was written; enqueue only once the transaction is closed.
        payload = {"kb_id": str(kb_id), "organisation_id": str(org), "version": version, "job_id": str(job_id)}
        self._adapters.job_queue.enqueue("rebuild_kb", payload, countdown=max(1.0, retry_in))
        logger.info("indexing_job_lease_held", kind="rebuild", retry_in_seconds=int(retry_in))
        return None

    async def _rebuild_document(
        self, org: UUID, kb_id: UUID, document_id: UUID, version: int, config: VersionConfig, record: Recorder
    ) -> tuple[str, int | str] | None:
        """Builds one document into `version`; records one "document" step. None: it was deleted."""
        started = time.monotonic()
        async with self._rt.session_factory() as s:
            document = await TenantScopedRepository(s, Document, org).get(document_id)
            if document is not None:
                target = _Target(org, kb_id, document.id, document.filename, document.file_type,
                                 document.storage_key, version, config)
        if document is None:
            await record({**_step("document", "skipped", 0, "Document was deleted"), "document_id": str(document_id)})
            return None
        steps: list[Step] = []

        async def keep(step: Step) -> None:
            steps.append(step)

        try:
            n = await self._build(target, keep, update_document=False)
        except _VersionChanged:
            raise _StepFailed("document", REBUILD_SUPERSEDED, RuntimeError()) from None
        except _Gone:
            await record({**_step("document", "skipped", _ms(started), f"{target.filename}: deleted"),
                          "document_id": str(document_id)})
            return None
        except _StepFailed as failure:
            await record({**_step("document", "failed", _ms(started), f"{target.filename}: {failure.message}"),
                          "document_id": str(document_id)})
            return "failed", failure.message
        except Exception as exc:
            if is_job_timeout(exc):  # recorded, then the rebuild stops (abandon, no swap)
                await record({**_step("document", "failed", _ms(started), f"{target.filename}: {TIMED_OUT}"),
                              "document_id": str(document_id)})
            raise
        await record({**_step("document", "succeeded", _ms(started), f"{target.filename}: {_plural(n, 'chunk')}"),
                      "document_id": str(document_id)})
        return "succeeded", n

    async def _swap(
        self,
        org: UUID,
        kb_id: UUID,
        version: int,
        job_id: UUID,
        succeeded: dict[UUID, int],
        failed: dict[UUID, str],
        *,
        total: int,
    ) -> None:
        """Serves `version`: config copied from building_*, documents updated, old chunks deleted, all
        in one commit; the old collection is dropped after it."""
        started = time.monotonic()
        async with UnitOfWork(self._rt.session_factory) as uow:
            s = uow.session
            kb = await TenantScopedRepository(s, KnowledgeBase, org).get(kb_id, for_update=True)
            if kb is None:
                raise _Gone()
            if kb.building_version != version:
                raise RuntimeError("The rebuild version changed while it was being built")
            old = kb.active_version
            kb.embedding_provider = kb.building_embedding_provider
            kb.embedding_model = kb.building_embedding_model
            kb.embedding_dimensions = kb.building_embedding_dimensions
            kb.embedding_credential_id = kb.building_embedding_credential_id
            kb.chunking = kb.building_chunking
            kb.active_version = version
            _clear_building(kb)

            documents = TenantScopedRepository(s, Document, org)
            ids = list(succeeded) + list(failed)
            if ids:
                for document in await documents.list(Document.id.in_(ids), limit=len(ids), for_update=True):
                    if document.id in succeeded:
                        document.status, document.error = "indexed", None
                        document.chunk_count, document.indexed_version = succeeded[document.id], version
                    else:
                        document.status, document.error = "failed", failed[document.id]
                        document.chunk_count, document.indexed_version = 0, None
            # Indexed into the old version by a document job after the rebuild listed its documents.
            await self._requeue_stranded(s, org, kb, old, exclude=set(ids))
            await _delete_chunks(s, org, Chunk.knowledge_base_id == kb_id, Chunk.kb_version == old)

            job = await TenantScopedRepository(s, IndexingJob, org).get(job_id, for_update=True)
            if job is not None:
                job.steps = [*job.steps, _step("swap", "succeeded", _ms(started), f"v{old}→v{version}")]
                job.status, job.finished_at = "succeeded", _now()
                job.error = f"{len(failed)} of {total} documents failed to index" if failed else None
            await uow.commit()
        await self._drop_collection_quietly(org, collection_name(kb_id, old))

    async def _abandon(self, org: UUID, kb_id: UUID, version: int, job_id: UUID, error: str | None) -> None:
        """No swap: the KB keeps serving active_version; the building version's config, chunks and
        collection are discarded and the job fails.

        Only a version that is still `building_version` is discarded; anything else (in particular the
        active version, e.g. after another run already swapped it in) is left untouched."""
        async with UnitOfWork(self._rt.session_factory) as uow:
            s = uow.session
            kb = await TenantScopedRepository(s, KnowledgeBase, org).get(kb_id, for_update=True)
            discard = kb is not None and kb.building_version == version
            if discard:
                _clear_building(kb)
                # Document jobs may have indexed into the version being built; serve them again.
                await self._requeue_stranded(s, org, kb, version, exclude=set())
                await _delete_chunks(s, org, Chunk.knowledge_base_id == kb_id, Chunk.kb_version == version)
            job = await TenantScopedRepository(s, IndexingJob, org).get(job_id, for_update=True)
            if job is not None and job.status not in FINISHED_STATUSES:
                job.status, job.finished_at = "failed", _now()
                active = kb.active_version if kb is not None else version - 1
                job.error = error or f"Every document failed to index; the knowledge base still uses version {active}"
            await uow.commit()
        if discard:
            await self._drop_collection_quietly(org, collection_name(kb_id, version))

    async def _requeue_stranded(
        self, s: AsyncSession, org: UUID, kb: KnowledgeBase, dead_version: int, *, exclude: set[UUID]
    ) -> None:
        """Documents indexed into `dead_version` (about to be deleted) get a new job at the version
        that stays; documents with a job already on the way are left to it (it retargets)."""
        stmt = TenantScopedRepository(s, Document, org).select(for_update=True).where(
            Document.knowledge_base_id == kb.id,
            Document.indexed_version == dead_version,
            Document.status.not_in(("queued", "processing")),
        )
        stranded = [d for d in (await s.execute(stmt)).scalars() if d.id not in exclude]
        if not stranded:
            return
        live = kb.building_version or kb.active_version
        jobs = TenantScopedRepository(s, IndexingJob, org)
        for document in stranded:
            document.status, document.error = "queued", None
            document.chunk_count, document.indexed_version = 0, None
            job = IndexingJob(
                knowledge_base_id=kb.id, document_id=document.id, kind="document", kb_version=live,
                status="queued", attempts=0, steps=[],
            )
            await jobs.add(job)
            await s.flush()
            enqueue_after_commit(
                s, self._adapters.job_queue, "index_document", {"job_id": str(job.id), "organisation_id": str(org)}
            )
        logger.info("indexing_documents_requeued", knowledge_base_id=str(kb.id), documents=len(stranded),
                    kb_version=live)

    async def _drop_version(self, org: UUID, kb_id: UUID, version: int) -> tuple[None, str]:
        """Removes what an earlier failed rebuild may have left at `version` (only while it is still the
        building version, under the KB lock)."""
        async with UnitOfWork(self._rt.session_factory) as uow:
            kb = await TenantScopedRepository(uow.session, KnowledgeBase, org).get(kb_id, for_update=True)
            if kb is None:
                raise _Gone()
            if kb.building_version != version:
                raise _RebuildSupersededError(REBUILD_SUPERSEDED)
            await self._adapters.vector_store.drop_collection(collection_name(kb_id, version), organisation_id=org)
            removed = await _delete_chunks(
                uow.session, org, Chunk.knowledge_base_id == kb_id, Chunk.kb_version == version
            )
            await uow.commit()
        return None, f"removed {_plural(removed, 'stale chunk')}" if removed else "nothing to clean up"

    async def _drop_collection_quietly(self, org: UUID, name: str) -> None:
        try:
            await self._adapters.vector_store.drop_collection(name, organisation_id=org)
        except Exception as exc:  # the KB is consistent; an orphan collection is dropped with the KB
            logger.warning("indexing_drop_collection_failed", collection=name, error_type=type(exc).__name__)

    # --- the pipeline ------------------------------------------------------------------------------

    async def _build(self, t: _Target, record: Recorder, *, update_document: bool = True) -> int:
        """download -> parse -> chunk -> embed -> upsert for one document into one version; returns the
        chunk count. Raises _StepFailed (recorded) or _Gone."""
        data: bytes = await self._step("download", record, lambda: self._download(t))
        parsed = await self._step("parse", record, lambda: self._parse(t, data))
        drafts: list[ChunkDraft] = await self._step("chunk", record, lambda: self._chunk(t, parsed))
        vectors: list[list[float]] = await self._step("embed", record, lambda: self._embed(t, drafts))
        return await self._step("upsert", record, lambda: self._store(t, drafts, vectors, update_document))

    async def _step(self, name: str, record: Recorder, run: Callable[[], Awaitable[tuple[Any, str]]]) -> Any:
        started = time.monotonic()
        with tracer.start_as_current_span(f"indexing.{name}", record_exception=False) as span:
            try:
                result, detail = await run()
            except (_Gone, _VersionChanged):
                raise
            except Exception as exc:
                message = readable_error(exc)
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                await record(_step(name, "failed", _ms(started), message))
                if is_job_timeout(exc):
                    raise  # the whole job is out of time: never "one document failed, carry on"
                raise _StepFailed(name, message, exc) from None
        await record(_step(name, "succeeded", _ms(started), detail))
        return result

    async def _download(self, t: _Target) -> tuple[bytes, str]:
        data = await self._adapters.storage.get(t.storage_key)
        return data, _size(len(data))

    async def _parse(self, t: _Target, data: bytes):
        parser = self._adapters.parsers[t.file_type]
        parsed = await asyncio.to_thread(parser.parse, data, t.filename)
        detail = _plural(len(parsed.text), "character")
        if parsed.pages:
            detail += f", {_plural(len(parsed.pages), 'page')}"
        return parsed, detail

    async def _chunk(self, t: _Target, parsed) -> tuple[list[ChunkDraft], str]:
        params = ChunkingParams(**t.config.chunking)
        chunker = self._adapters.chunkers[params.strategy]
        drafts = await asyncio.to_thread(chunker.chunk, parsed, params)
        if not drafts:
            raise _NoChunksError(NO_CHUNKS)
        return drafts, _plural(len(drafts), "chunk")

    async def _embed(self, t: _Target, drafts: list[ChunkDraft]) -> tuple[list[list[float]], str]:
        try:
            return await self._embed_batches(t, drafts)
        except (_Gone, _VersionChanged):
            raise
        except Exception as exc:  # recorded in its own unit of work, after the failed call
            await record_integration_error(
                self._rt.session_factory, t.organisation_id, "embedding", t.config.provider, error_type_of(exc)
            )
            raise

    async def _embed_batches(self, t: _Target, drafts: list[ChunkDraft]) -> tuple[list[list[float]], str]:
        provider = t.config.provider
        embedder = await self._embedder(t)
        size = max(1, self._settings.embed_batch_size)
        vectors: list[list[float]] = []
        batches = 0
        for i in range(0, len(drafts), size):
            batch = [d.text for d in drafts[i : i + size]]
            try:
                out = await embedder.embed_documents(batch)
            except Exception:
                EMBEDDING_REQUESTS.labels(provider=provider, status="failed").inc()
                raise
            EMBEDDING_REQUESTS.labels(provider=provider, status="succeeded").inc()
            if len(out) != len(batch) or any(len(v) != t.config.dimensions for v in out):
                raise EmbeddingError(
                    f"The embedding model returned unexpected vectors (expected {t.config.dimensions} dimensions)"
                )
            vectors.extend(out)
            batches += 1
        return vectors, _plural(batches, "batch", "batches")

    async def _embedder(self, t: _Target) -> Embedder:
        """Builds the version's embedder (see supportcore.knowledge.embedding). The credential is read
        in a short session that is closed before the SSRF check and before anything is embedded."""
        async with self._rt.session_factory() as s:  # opens no connection when there is no credential
            api_key, base_url = await embedding_key(
                s, t.organisation_id, self._adapters, self._settings, self._rt.events, t.config
            )
        return await create_embedder(self._adapters, self._settings, t.config, api_key, base_url)

    async def _ensure_collection(self, org: UUID, kb_id: UUID, version: int, config: VersionConfig) -> tuple[None, str]:
        await self._adapters.vector_store.ensure_collection(
            collection_name(kb_id, version), config.dimensions, organisation_id=org, knowledge_base_id=kb_id
        )
        return None, _plural(config.dimensions, "dimension")

    async def _store(
        self, t: _Target, drafts: list[ChunkDraft], vectors: list[list[float]], update_document: bool
    ) -> tuple[int, str]:
        """Replaces the document's chunks and vectors in t.version, holding the KB's and the document's
        row locks. Raises _VersionChanged when t.version is no longer live (for a rebuild, no longer
        the building version), so a dead version is never written or re-created."""
        org, store = t.organisation_id, self._adapters.vector_store
        async with UnitOfWork(self._rt.session_factory) as uow:
            s = uow.session
            # FOR SHARE: stores (and uploads, whose FK check takes KEY SHARE) run side by side, while a
            # swap, abandon, KB update or delete (FOR UPDATE) waits for them and vice versa.
            kb_stmt = (
                TenantScopedRepository(s, KnowledgeBase, org)
                .select()
                .where(KnowledgeBase.id == t.knowledge_base_id)
                .with_for_update(read=True)
            )
            kb = (await s.execute(kb_stmt)).scalar_one_or_none()
            if kb is None:
                raise _Gone()
            live = (kb.building_version,) if not update_document else (kb.active_version, kb.building_version)
            # A live version whose config changed in place (same dimensions, so same version) since
            # the job read it counts as changed too: one collection never mixes two models/chunkings.
            if t.version not in live or version_config(kb, t.version) != t.config:
                current = kb.building_version or kb.active_version
                raise _VersionChanged(current, version_config(kb, current))
            document = await TenantScopedRepository(s, Document, org).get(t.document_id, for_update=True)
            if document is None:
                raise _Gone()
            await self._ensure_collection(org, t.knowledge_base_id, t.version, t.config)  # idempotent
            await _delete_chunks(s, org, Chunk.document_id == t.document_id, Chunk.kb_version == t.version)
            await store.delete_documents(t.collection, [t.document_id], organisation_id=org)
            chunks = TenantScopedRepository(s, Chunk, org)
            rows = []
            for draft in drafts:
                row = Chunk(
                    knowledge_base_id=t.knowledge_base_id,
                    document_id=t.document_id,
                    kb_version=t.version,
                    ordinal=draft.ordinal,
                    text=draft.text,
                    char_start=draft.char_start,
                    char_end=draft.char_end,
                    metadata_=dict(draft.metadata),
                )
                await chunks.add(row)
                rows.append(row)
            await s.flush()  # assigns ids
            records = [
                VectorRecord(
                    chunk_id=row.id,
                    document_id=t.document_id,
                    organisation_id=org,
                    knowledge_base_id=t.knowledge_base_id,
                    embedding=vector,
                    metadata={"ordinal": row.ordinal, "kb_version": t.version},
                )
                for row, vector in zip(rows, vectors, strict=True)
            ]
            await store.upsert(t.collection, records, organisation_id=org)
            if update_document:
                document.status, document.error = "indexed", None
                document.chunk_count, document.indexed_version = len(rows), t.version
            await uow.commit()
        INDEXED_CHUNKS.labels(organisation_id=str(org)).inc(len(rows))
        return len(rows), _plural(len(rows), "vector")

    # --- interrupted jobs --------------------------------------------------------------------------

    async def fail_interrupted(self, job_id: UUID, organisation_id: UUID, message: str) -> bool:
        """Fails a job that cannot finish (timed out, worker lost): document jobs fail (see
        _fail_document), rebuilds take the abandon path. False if the job is gone or finished."""
        async with self._rt.session_factory() as s:
            job = await TenantScopedRepository(s, IndexingJob, organisation_id).get(job_id)
            if job is None or job.status in FINISHED_STATUSES:
                return False
            kind, kb_id, document_id, version = job.kind, job.knowledge_base_id, job.document_id, job.kb_version
        if kind == "rebuild":
            await self._abandon(organisation_id, kb_id, version, job_id, message)
        elif document_id is not None:
            await self._fail_document(job_id, organisation_id, kb_id, document_id, message)
        INDEXING_JOBS.labels(kind=kind, status="failed", organisation_id=str(organisation_id)).inc()
        logger.warning("indexing_job_interrupted", kind=kind, reason=message)
        return True

    async def recover_stale_jobs(self, *, older_than: timedelta | None = None) -> int:
        """Fails every job still `running` that started longer ago than the job time limit: its
        worker died or was killed. Runs periodically (celery beat, each worker process, the API in
        inline mode). Returns how many."""
        cutoff = _now() - (older_than if older_than is not None else timedelta(seconds=JOB_TIME_LIMIT_SECONDS))
        async with self._rt.session_factory() as s:
            organisation_ids = list((await s.execute(select(Organisation.id))).scalars())
        recovered = 0
        for org in organisation_ids:
            async with self._rt.session_factory() as s:
                stale = await TenantScopedRepository(s, IndexingJob, org).list(
                    IndexingJob.status == "running", IndexingJob.started_at < cutoff, limit=1000
                )
                stale_ids = [job.id for job in stale]
            for job_id in stale_ids:
                with job_context(org, str(job_id)):
                    if await self.fail_interrupted(job_id, org, INTERRUPTED):
                        recovered += 1
        return recovered

    async def requeue_lost_jobs(self, *, older_than: timedelta = LOST_JOB_AFTER) -> int:
        """Sends again every job still `queued` LOST_JOB_AFTER after it was queued (or last sent):
        its message was lost (e.g. the broker was down when the request committed). `queued_at` is
        moved to now, so a job is sent at most once per interval. Safe to repeat: a job that already
        finished is skipped by its handler, and a running rebuild holds a lease. Returns how many."""
        cutoff = _now() - older_than
        async with self._rt.session_factory() as s:
            organisation_ids = list((await s.execute(select(Organisation.id))).scalars())
        requeued = 0
        for org in organisation_ids:
            with job_context(org, "requeue_lost_jobs"):
                requeued += await self._requeue_lost_jobs_of(org, cutoff)
        return requeued

    async def _requeue_lost_jobs_of(self, org: UUID, cutoff: datetime) -> int:
        async with UnitOfWork(self._rt.session_factory) as uow:
            stmt = (
                TenantScopedRepository(uow.session, IndexingJob, org)
                .select()
                .where(IndexingJob.status == "queued", IndexingJob.queued_at < cutoff)
                .with_for_update(skip_locked=True)  # a job being started right now is not lost
                .order_by(IndexingJob.queued_at)
                .limit(1000)
            )
            lost = list((await uow.session.execute(stmt)).scalars())
            for job in lost:
                job.queued_at = _now()
                if job.kind == "rebuild":
                    name, payload = "rebuild_kb", {
                        "kb_id": str(job.knowledge_base_id), "organisation_id": str(org),
                        "version": job.kb_version, "job_id": str(job.id),
                    }
                else:
                    name, payload = "index_document", {"job_id": str(job.id), "organisation_id": str(org)}
                enqueue_after_commit(uow.session, self._adapters.job_queue, name, payload)
            await uow.commit()
        if lost:
            logger.warning("indexing_lost_jobs_requeued", jobs=len(lost), job_ids=[str(j.id) for j in lost][:20])
        return len(lost)

    # --- helpers -----------------------------------------------------------------------------------

    async def _safely(self, work: Awaitable[None]) -> None:
        """Recording a failure must not raise out of the job (e.g. the rows were deleted meanwhile)."""
        try:
            await work
        except _Gone:
            pass
        except Exception as exc:
            logger.exception("indexing_failure_not_recorded", error_type=type(exc).__name__)

    def _finished(
        self,
        kind: str,
        status: str,
        org: UUID,
        started: float,
        *,
        chunks: int,
        error_step: str | None,
        error_type: str | None,
    ) -> None:
        seconds = time.monotonic() - started
        INDEXING_JOBS.labels(kind=kind, status=status, organisation_id=str(org)).inc()
        INDEXING_JOB_DURATION.labels(kind=kind).observe(seconds)
        fields: dict[str, Any] = {"kind": kind, "status": status, "duration_ms": int(seconds * 1000), "chunks": chunks}
        if error_type is not None:
            fields.update(error_step=error_step, error_type=error_type)
        (logger.info if status == "succeeded" else logger.warning)("indexing_job_finished", **fields)


class _NoChunksError(DomainError):
    pass


def _fail_or_keep_served(document: Document, kb: KnowledgeBase | None, job_version: int | None, message: str) -> None:
    """A failed job fails its document, unless the active version still serves an earlier index of it
    and the job targeted another version (a re-index into the version being built): then the
    document stays `indexed` and the failure is only on the job."""
    still_served = (
        kb is not None
        and document.indexed_version is not None
        and document.indexed_version == kb.active_version
        and job_version != kb.active_version
    )
    if still_served:
        document.status = "indexed"
    else:
        document.status, document.error = "failed", message


class _RebuildSupersededError(DomainError):
    pass


def _lease_cutoff() -> datetime:
    return _now() - timedelta(seconds=JOB_TIME_LIMIT_SECONDS)


def _clear_building(kb: KnowledgeBase) -> None:
    kb.building_version = None
    kb.building_embedding_provider = None
    kb.building_embedding_model = None
    kb.building_embedding_dimensions = None
    kb.building_embedding_credential_id = None
    kb.building_chunking = None


async def _delete_chunks(session: AsyncSession, org: UUID, *where: Any) -> int:
    """Deletes the org's chunks matching `where` (ids picked through the tenant-scoped repository)."""
    ids = TenantScopedRepository(session, Chunk, org).select(Chunk.id).where(*where)
    result = await session.execute(delete(Chunk).where(Chunk.id.in_(ids)))
    return result.rowcount or 0
