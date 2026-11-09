"""Documents of a knowledge base: upload (stored in FileStorage), indexing requests, deletion.

Jobs are enqueued with `after_commit` (see kb_service). Payloads:

- index_document  {job_id, organisation_id}
- purge_document  {organisation_id, kb_id, document_id, storage_key, collections: [names]}
"""

from __future__ import annotations  # the `list` method shadows the builtin in later annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import PureWindowsPath
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.billing.entitlements import EntitlementService
from supportcore.config import Settings
from supportcore.db.models import Chunk, Document, IndexingJob, KnowledgeBase, User
from supportcore.db.models.knowledge import DOCUMENT_STATUSES
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import DomainError, NotFoundError
from supportcore.events import EventBus
from supportcore.knowledge.events import DocumentDeleted, DocumentUploaded, IndexingStarted
from supportcore.knowledge.kb_service import collection_name, enqueue_after_commit
from supportcore.knowledge.parsing.ports import detect_file_type
from supportcore.observability.logging import get_logger

if TYPE_CHECKING:
    from supportcore.wiring import Adapters

logger = get_logger(__name__)

# Storage cleanups scheduled from cancelled uploads (kept referenced until they finish).
_cleanup_tasks: set[asyncio.Task] = set()

MAX_FILENAME_LENGTH = 255
MAX_CHUNK_PAGE = 200
# Documents in these states already have a job on the way; indexing requests skip them.
IN_FLIGHT_STATUSES = ("queued", "processing")
# What start_indexing() picks when no ids are given.
INDEXABLE_STATUSES = ("uploaded", "failed")
# A document queued this long with no job running lost its message; reindex_document() retries it
# (the web app offers "Stuck? Retry" after the same time).
STUCK_QUEUED_AFTER = timedelta(minutes=10)
STUCK_QUEUED_MESSAGE = "Stuck in the queue; queued again"

_CONTENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text": "text/plain; charset=utf-8",
    "markdown": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
}


class UploadTooLargeError(DomainError):
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        size = f"{max_bytes / (1024 * 1024):g} MB" if max_bytes >= 1024 * 1024 else f"{max_bytes} bytes"
        super().__init__(f"File is too large (maximum {size})")


class InvalidFilenameError(DomainError):
    """The upload has no usable file name."""


class DuplicateDocumentError(DomainError):
    def __init__(self, existing_filename: str) -> None:
        self.existing_filename = existing_filename
        super().__init__(f"This file is already in the knowledge base as '{existing_filename}'")


def document_storage_key(organisation_id: UUID, kb_id: UUID, document_id: UUID, sha256: str) -> str:
    return f"orgs/{organisation_id}/kbs/{kb_id}/documents/{document_id}/{sha256}"


def _clean_filename(filename: str) -> str:
    """The base name only (no client path, either separator), at most 255 characters keeping the extension."""
    name = PureWindowsPath((filename or "").replace("\x00", "")).name.strip()
    if not name:
        raise InvalidFilenameError("A file name is required")
    if len(name) > MAX_FILENAME_LENGTH:
        stem, dot, ext = name.rpartition(".")
        suffix = f".{ext}" if dot and len(ext) < 20 else ""
        name = (stem if suffix else name)[: MAX_FILENAME_LENGTH - len(suffix)] + suffix
    return name


class DocumentService:
    def __init__(
        self, session: AsyncSession, organisation_id: UUID, events: EventBus, adapters: "Adapters", settings: Settings
    ) -> None:
        self._session = session
        self._org_id = organisation_id
        self._events = events
        self._adapters = adapters
        self._settings = settings
        self._kbs = TenantScopedRepository(session, KnowledgeBase, organisation_id)
        self._documents = TenantScopedRepository(session, Document, organisation_id)
        self._chunks = TenantScopedRepository(session, Chunk, organisation_id)
        self._jobs = TenantScopedRepository(session, IndexingJob, organisation_id)

    async def list(self, kb_id: UUID, status: str | None = None) -> list[Document]:
        await self._kb(kb_id)
        where = [Document.knowledge_base_id == kb_id]
        if status is not None:
            if status not in DOCUMENT_STATUSES:
                return []
            where.append(Document.status == status)
        return await self._documents.list(*where, order_by=(Document.created_at, Document.id), limit=10000)

    async def get(self, document_id: UUID, *, for_update: bool = False) -> Document:
        stmt = self._documents.select(for_update=for_update).where(Document.id == document_id)
        if for_update:
            stmt = stmt.execution_options(populate_existing=True)
        document = (await self._session.execute(stmt)).scalar_one_or_none()
        if document is None:
            raise NotFoundError("Document not found")
        return document

    async def upload(self, kb_id: UUID, filename: str, data: bytes, actor: User) -> Document:
        """Order: size, knowledge base, file type (sniffed), duplicate check, the plan's storage, storage.put, row insert.
        If the insert fails the stored object is deleted again (best effort)."""
        max_bytes = self._settings.max_upload_bytes
        if len(data) > max_bytes:
            raise UploadTooLargeError(max_bytes)
        kb = await self._kb(kb_id)
        filename = _clean_filename(filename)
        file_type = detect_file_type(filename, data)
        sha256 = hashlib.sha256(data).hexdigest()
        existing = await self._find_duplicate(kb.id, sha256)
        if existing is not None:
            raise DuplicateDocumentError(existing.filename)
        await EntitlementService(self._session, self._org_id, self._adapters).check_storage(len(data))

        document_id = uuid4()
        key = document_storage_key(self._org_id, kb.id, document_id, sha256)
        await self._adapters.storage.put(key, data, _CONTENT_TYPES[file_type])
        document = Document(
            id=document_id,
            knowledge_base_id=kb.id,
            filename=filename,
            file_type=file_type,
            size_bytes=len(data),
            sha256=sha256,
            storage_key=key,
            status="uploaded",
            chunk_count=0,
            uploaded_by_user_id=actor.id,
        )
        try:
            try:
                async with self._session.begin_nested():
                    await self._documents.add(document)
                    await self._session.flush()
            except IntegrityError:
                # Usually a lost race with a concurrent upload of the same file (unique kb_id + sha256);
                # anything else (e.g. the KB was deleted meanwhile) is re-raised as is.
                winner = await self._find_duplicate(kb.id, sha256)
                if winner is None:
                    raise
                raise DuplicateDocumentError(winner.filename) from None
            await self._events.publish(
                DocumentUploaded(
                    organisation_id=self._org_id,
                    actor_user_id=actor.id,
                    knowledge_base_id=kb.id,
                    document_id=document.id,
                    filename=document.filename,
                    file_type=document.file_type,
                    size_bytes=document.size_bytes,
                )
            )
        except asyncio.CancelledError:
            # Don't await storage in a cancelled task: clean up in a task of its own.
            task = asyncio.ensure_future(self._discard_stored(key))
            _cleanup_tasks.add(task)
            task.add_done_callback(_cleanup_tasks.discard)
            raise
        except Exception:
            await self._discard_stored(key)
            raise
        return document

    async def start_indexing(
        self, kb_id: UUID, document_ids: list[UUID] | None, actor: User, *, retry_stuck: bool = False
    ) -> list[IndexingJob]:
        """Queues one IndexingJob per document: the given ones, or by default every `uploaded` and
        `failed` one. Documents already `queued`/`processing` are skipped, except, with
        `retry_stuck`, one queued for over STUCK_QUEUED_AFTER with no job running: its stuck jobs
        fail and a new one is queued. Jobs target the version being built if a rebuild is pending,
        else the active version."""
        kb = await self._kb(kb_id, for_update=True)  # serialises with KB updates starting a rebuild
        where = [Document.knowledge_base_id == kb.id]
        if document_ids is None:
            where.append(Document.status.in_(INDEXABLE_STATUSES))
        else:
            wanted = set(document_ids)
            where.append(Document.id.in_(wanted))
        stmt = self._documents.select(for_update=True).where(*where).order_by(Document.created_at, Document.id)
        documents = list((await self._session.execute(stmt.execution_options(populate_existing=True))).scalars())
        if document_ids is not None and len(documents) != len(wanted):
            raise NotFoundError("Document not found")

        version = kb.building_version or kb.active_version
        jobs: list[IndexingJob] = []
        for document in documents:
            if document.status in IN_FLIGHT_STATUSES and not (retry_stuck and await self._release_stuck(document)):
                continue
            document.status, document.error = "queued", None
            job = IndexingJob(
                knowledge_base_id=kb.id,
                document_id=document.id,
                kind="document",
                kb_version=version,
                status="queued",
                attempts=0,
                steps=[],
            )
            await self._jobs.add(job)
            jobs.append(job)
        if not jobs:
            return []
        await self._session.flush()
        for job in jobs:
            self._enqueue("index_document", {"job_id": str(job.id), "organisation_id": str(self._org_id)})
        await self._events.publish(
            IndexingStarted(
                organisation_id=self._org_id,
                actor_user_id=actor.id,
                knowledge_base_id=kb.id,
                document_ids=tuple(job.document_id for job in jobs if job.document_id is not None),
            )
        )
        return jobs

    async def reindex_document(self, document_id: UUID, actor: User) -> IndexingJob | None:
        """Queues the document again; None when it is already queued or processing (a document
        stuck in the queue, see STUCK_QUEUED_AFTER, is queued again)."""
        document = await self.get(document_id)
        jobs = await self.start_indexing(document.knowledge_base_id, [document.id], actor, retry_stuck=True)
        return jobs[0] if jobs else None

    async def delete(self, document_id: UUID, actor: User) -> None:
        """Deletes the row (chunks and jobs cascade); after commit a job removes its vectors from the
        active and building collections and deletes the stored file."""
        # Lock order KB -> document, as in start_indexing. The KB lock also keeps a rebuild from
        # starting meanwhile, so the collections listed below are all the document can be in.
        kb_id = (await self.get(document_id)).knowledge_base_id
        kb = await self._kb(kb_id, for_update=True)
        document = await self.get(document_id, for_update=True)
        versions = [kb.active_version] + ([kb.building_version] if kb.building_version is not None else [])
        payload = {
            "organisation_id": str(self._org_id),
            "kb_id": str(kb.id),
            "document_id": str(document.id),
            "storage_key": document.storage_key,
            "collections": [collection_name(kb.id, v) for v in versions],
        }
        event = DocumentDeleted(
            organisation_id=self._org_id,
            actor_user_id=actor.id,
            knowledge_base_id=kb.id,
            document_id=document.id,
            filename=document.filename,
        )
        await self._documents.delete(document)
        await self._events.publish(event)
        self._enqueue("purge_document", payload)

    async def chunks(
        self, document_id: UUID, *, version: int | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[list[Chunk], int, int]:
        """A page of the document's chunks in order, with the total count and the version used.

        Default version: the one the document was last indexed into (`indexed_version`), or the
        KB's active_version if it was never indexed."""
        document = await self.get(document_id)
        if version is None:
            if document.indexed_version is not None:
                version = document.indexed_version
            else:
                version = (await self._kb(document.knowledge_base_id)).active_version
        limit = max(1, min(limit, MAX_CHUNK_PAGE))
        offset = max(0, offset)
        where = (Chunk.document_id == document.id, Chunk.kb_version == version)
        total = (await self._session.execute(self._chunks.select(func.count()).where(*where))).scalar_one()
        items = await self._chunks.list(*where, order_by=(Chunk.ordinal, Chunk.id), limit=limit, offset=offset)
        return items, total, version

    # --- internals ---------------------------------------------------------------------------------

    async def _kb(self, kb_id: UUID, *, for_update: bool = False) -> KnowledgeBase:
        stmt = self._kbs.select(for_update=for_update).where(KnowledgeBase.id == kb_id)
        if for_update:
            stmt = stmt.execution_options(populate_existing=True)
        kb = (await self._session.execute(stmt)).scalar_one_or_none()
        if kb is None:
            raise NotFoundError("Knowledge base not found")
        return kb

    async def _release_stuck(self, document: Document) -> bool:
        """True (and its waiting jobs failed) when the document is `queued`, no job of it is running
        and every queued one was queued over STUCK_QUEUED_AFTER ago. Caller holds the KB and
        document locks (lock order KB -> document -> jobs)."""
        if document.status != "queued":
            return False
        stmt = self._jobs.select(for_update=True).where(
            IndexingJob.document_id == document.id, IndexingJob.status.in_(("queued", "running"))
        )
        waiting = list((await self._session.execute(stmt)).scalars())
        cutoff = datetime.now(UTC) - STUCK_QUEUED_AFTER
        if any(job.status == "running" or job.queued_at >= cutoff for job in waiting):
            return False
        for job in waiting:
            job.status, job.error, job.finished_at = "failed", STUCK_QUEUED_MESSAGE, datetime.now(UTC)
        logger.info("document_stuck_in_queue_requeued", document_id=str(document.id), jobs=len(waiting))
        return True

    async def _find_duplicate(self, kb_id: UUID, sha256: str) -> Document | None:
        # Documents are hard-deleted, so every row is a live document.
        stmt = self._documents.select().where(Document.knowledge_base_id == kb_id, Document.sha256 == sha256)
        return (await self._session.execute(stmt)).scalars().first()

    async def _discard_stored(self, key: str) -> None:
        try:
            await self._adapters.storage.delete(key)
        except Exception as exc:
            logger.warning("document_storage_cleanup_failed", storage_key=key, error_type=type(exc).__name__)

    def _enqueue(self, name: str, payload: dict) -> None:
        enqueue_after_commit(self._session, self._adapters.job_queue, name, payload)
