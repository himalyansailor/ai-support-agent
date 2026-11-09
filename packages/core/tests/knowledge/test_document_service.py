import hashlib
import json

import pytest
from sqlalchemy import select

from supportcore.db.models import AuditLog, Chunk, Document, IndexingJob, Organisation
from supportcore.errors import NotFoundError
from supportcore.knowledge.document_service import (
    DocumentService,
    DuplicateDocumentError,
    InvalidFilenameError,
    UploadTooLargeError,
)
from supportcore.knowledge.events import DocumentDeleted, DocumentUploaded, IndexingStarted
from supportcore.knowledge.kb_service import EmbeddingChoice, collection_name
from supportcore.knowledge.parsing.ports import UnsupportedFileTypeError

MD = b"# Refunds\n\nWe refund within 30 days.\n"


async def test_upload_stores_file_and_row(session, documents, fake_kb, org, owner, storage, seen):
    doc = await documents.upload(fake_kb.id, "refunds.md", MD, owner)
    sha = hashlib.sha256(MD).hexdigest()
    assert (doc.filename, doc.file_type, doc.size_bytes, doc.sha256) == ("refunds.md", "markdown", len(MD), sha)
    assert (doc.status, doc.error, doc.chunk_count, doc.indexed_version) == ("uploaded", None, 0, None)
    assert doc.uploaded_by_user_id == owner.id
    assert doc.storage_key == f"orgs/{org.id}/kbs/{fake_kb.id}/documents/{doc.id}/{sha}"
    assert storage.objects == {doc.storage_key: MD}
    [event] = [e for e in seen if isinstance(e, DocumentUploaded)]
    assert (event.document_id, event.knowledge_base_id, event.filename) == (doc.id, fake_kb.id, "refunds.md")
    assert [d.id for d in await documents.list(fake_kb.id)] == [doc.id]
    assert (await documents.get(doc.id)).id == doc.id


async def test_upload_keeps_only_the_base_filename(documents, fake_kb, owner):
    doc = await documents.upload(fake_kb.id, "../../etc/notes.txt", b"hello", owner)
    assert doc.filename == "notes.txt"


async def test_upload_dedupes_by_sha256(documents, fake_kb, owner, storage):
    await documents.upload(fake_kb.id, "refunds.md", MD, owner)
    with pytest.raises(DuplicateDocumentError, match="refunds.md"):
        await documents.upload(fake_kb.id, "copy.md", MD, owner)
    assert len(storage.objects) == 1


async def test_upload_same_file_into_another_kb_is_fine(kbs, documents, fake_kb, owner, recursive):
    other = await kbs.create(
        name="Other",
        description="",
        embedding_provider="fake",
        embedding_model="fake-embedding",
        embedding_credential_id=None,
        chunking=recursive,
        reranker="none",
        reranker_params={},
        actor=owner,
    )
    await documents.upload(fake_kb.id, "refunds.md", MD, owner)
    await documents.upload(other.id, "refunds.md", MD, owner)


async def test_upload_too_large(documents, fake_kb, owner, storage):
    # kb_settings.max_upload_bytes == 1000; the size check runs before type detection.
    with pytest.raises(UploadTooLargeError):
        await documents.upload(fake_kb.id, "big.exe", b"x" * 1001, owner)
    await documents.upload(fake_kb.id, "exact.txt", b"x" * 1000, owner)
    assert len(storage.objects) == 1


@pytest.mark.parametrize("name", ["", "   ", "/", "\x00"])
async def test_upload_without_filename_is_invalid(documents, fake_kb, owner, storage, name):
    with pytest.raises(InvalidFilenameError):
        await documents.upload(fake_kb.id, name, MD, owner)
    assert storage.objects == {}


async def test_cancelled_upload_still_cleans_storage(session, documents, fake_kb, owner, storage, monkeypatch):
    import asyncio

    async def cancelled_publish(event):
        raise asyncio.CancelledError()

    monkeypatch.setattr(documents._events, "publish", cancelled_publish)
    with pytest.raises(asyncio.CancelledError):
        await documents.upload(fake_kb.id, "a.md", MD, owner)
    await asyncio.sleep(0)  # let the scheduled cleanup task run
    await asyncio.sleep(0)
    assert storage.objects == {}


async def test_upload_rejects_disguised_type(documents, fake_kb, owner, storage):
    with pytest.raises(UnsupportedFileTypeError):
        await documents.upload(fake_kb.id, "fake.pdf", b"not a pdf", owner)
    assert storage.objects == {}


async def test_upload_to_other_orgs_kb_is_404(session, bus, adapters, kb_settings, fake_kb, owner, storage):
    other = Organisation(name="Other", slug="other-org")
    session.add(other)
    await session.flush()
    service = DocumentService(session, other.id, bus, adapters, kb_settings)
    with pytest.raises(NotFoundError):
        await service.upload(fake_kb.id, "a.md", MD, owner)
    assert storage.objects == {}


async def test_upload_cleans_storage_if_db_insert_fails(documents, fake_kb, owner, storage, monkeypatch):
    first = await documents.upload(fake_kb.id, "refunds.md", MD, owner)

    # Lose the dedupe race: the pre-check sees nothing, so the unique (kb, sha256) index rejects the insert.
    real_find = documents._find_duplicate
    calls = []

    async def racing_find(kb_id, sha256):
        calls.append(sha256)
        return None if len(calls) == 1 else await real_find(kb_id, sha256)

    monkeypatch.setattr(documents, "_find_duplicate", racing_find)
    with pytest.raises(DuplicateDocumentError, match="refunds.md"):
        await documents.upload(fake_kb.id, "copy.md", MD, owner)
    assert list(storage.objects) == [first.storage_key]
    assert len(storage.deleted) == 1


async def test_upload_cleans_storage_if_flush_errors(session, documents, fake_kb, owner, storage, monkeypatch):
    real_flush = session.flush

    async def broken_flush(*args, **kwargs):
        if any(isinstance(o, Document) for o in session.new):
            raise RuntimeError("db down")
        return await real_flush(*args, **kwargs)

    monkeypatch.setattr(session, "flush", broken_flush)
    with pytest.raises(RuntimeError, match="db down"):
        await documents.upload(fake_kb.id, "refunds.md", MD, owner)
    assert storage.objects == {}


async def test_start_indexing_skips_in_flight_documents(session, documents, fake_kb, org, owner, job_queue, seen):
    uploaded = await documents.upload(fake_kb.id, "a.md", b"aaa", owner)
    failed = await documents.upload(fake_kb.id, "b.md", b"bbb", owner)
    queued = await documents.upload(fake_kb.id, "c.md", b"ccc", owner)
    processing = await documents.upload(fake_kb.id, "d.md", b"ddd", owner)
    indexed = await documents.upload(fake_kb.id, "e.md", b"eee", owner)
    failed.status, failed.error = "failed", "boom"
    queued.status, processing.status, indexed.status = "queued", "processing", "indexed"
    await session.commit()
    job_queue.jobs.clear()

    jobs = await documents.start_indexing(fake_kb.id, None, owner)
    assert {j.document_id for j in jobs} == {uploaded.id, failed.id}
    for job in jobs:
        assert (job.kind, job.kb_version, job.status, job.attempts) == ("document", 1, "queued", 0)
    assert (uploaded.status, failed.status, failed.error) == ("queued", "queued", None)
    [event] = [e for e in seen if isinstance(e, IndexingStarted)]
    assert set(event.document_ids) == {uploaded.id, failed.id}

    # Explicit ids: in-flight ones are skipped, indexed ones are re-queued.
    again = await documents.start_indexing(fake_kb.id, [queued.id, processing.id, indexed.id], owner)
    assert [j.document_id for j in again] == [indexed.id]

    assert job_queue.jobs == []
    await session.commit()
    assert sorted(job_queue.jobs, key=lambda j: j[1]["job_id"]) == sorted(
        [("index_document", {"job_id": str(j.id), "organisation_id": str(org.id)}) for j in [*jobs, *again]],
        key=lambda j: j[1]["job_id"],
    )


async def test_start_indexing_uses_building_version(session, kbs, documents, fake_kb, owner):
    doc = await documents.upload(fake_kb.id, "a.md", MD, owner)
    doc.status, doc.indexed_version = "indexed", 1
    await kbs.update(fake_kb.id, embedding=EmbeddingChoice("local", "BAAI/bge-small-en-v1.5", None), actor=owner)
    [job] = await documents.start_indexing(fake_kb.id, [doc.id], owner)
    assert job.kb_version == 2


async def test_start_indexing_unknown_document_404(documents, fake_kb, owner):
    from uuid import uuid4

    with pytest.raises(NotFoundError):
        await documents.start_indexing(fake_kb.id, [uuid4()], owner)


async def test_reindex_document(session, documents, fake_kb, owner):
    doc = await documents.upload(fake_kb.id, "a.md", MD, owner)
    doc.status = "indexed"
    job = await documents.reindex_document(doc.id, owner)
    assert job is not None and job.document_id == doc.id
    assert doc.status == "queued"
    assert await documents.reindex_document(doc.id, owner) is None  # already queued


async def test_reindex_retries_a_document_stuck_in_the_queue(session, documents, fake_kb, owner, job_queue):
    from datetime import UTC, datetime, timedelta

    from supportcore.knowledge.document_service import STUCK_QUEUED_MESSAGE

    doc = await documents.upload(fake_kb.id, "a.md", MD, owner)
    [lost] = await documents.start_indexing(fake_kb.id, [doc.id], owner)
    assert await documents.reindex_document(doc.id, owner) is None  # queued just now: not stuck

    lost.queued_at = datetime.now(UTC) - timedelta(minutes=11)  # its message never arrived
    await session.flush()
    job = await documents.reindex_document(doc.id, owner)
    assert job is not None and job.id != lost.id and job.status == "queued"
    assert (lost.status, lost.error) == ("failed", STUCK_QUEUED_MESSAGE) and lost.finished_at is not None
    assert doc.status == "queued"


async def test_reindex_never_retries_a_document_with_a_running_job(session, documents, fake_kb, owner):
    from datetime import UTC, datetime, timedelta

    doc = await documents.upload(fake_kb.id, "a.md", MD, owner)
    [job] = await documents.start_indexing(fake_kb.id, [doc.id], owner)
    job.queued_at = datetime.now(UTC) - timedelta(hours=1)
    job.status, job.started_at = "running", datetime.now(UTC)
    await session.flush()
    assert await documents.reindex_document(doc.id, owner) is None


async def test_delete_document_enqueues_purge_after_commit(
    session, kbs, documents, fake_kb, org, owner, job_queue, seen
):
    doc = await documents.upload(fake_kb.id, "a.md", MD, owner)
    await documents.start_indexing(fake_kb.id, [doc.id], owner)
    session.add(
        Chunk(
            organisation_id=org.id,
            knowledge_base_id=fake_kb.id,
            document_id=doc.id,
            kb_version=1,
            ordinal=0,
            text="x",
            char_start=0,
            char_end=1,
            metadata_={},
        )
    )
    doc.indexed_version = 1
    _kb, rebuild = await kbs.update(
        fake_kb.id, embedding=EmbeddingChoice("local", "BAAI/bge-small-en-v1.5", None), actor=owner
    )
    assert rebuild is True  # building_version 2 is pending: the purge must cover it too
    await session.commit()
    job_queue.jobs.clear()

    await documents.delete(doc.id, owner)
    assert (await session.execute(select(Document))).first() is None
    assert (await session.execute(select(Chunk))).first() is None
    assert (await session.execute(select(IndexingJob).where(IndexingJob.document_id.is_not(None)))).first() is None
    [event] = [e for e in seen if isinstance(e, DocumentDeleted)]
    assert event.document_id == doc.id
    assert job_queue.jobs == []
    await session.commit()
    assert job_queue.jobs == [
        (
            "purge_document",
            {
                "organisation_id": str(org.id),
                "kb_id": str(fake_kb.id),
                "document_id": str(doc.id),
                "storage_key": doc.storage_key,
                "collections": [collection_name(fake_kb.id, 1), collection_name(fake_kb.id, 2)],
            },
        )
    ]
    with pytest.raises(NotFoundError):
        await documents.get(doc.id)


async def test_delete_document_locks_the_kb(documents, fake_kb, owner, monkeypatch):
    doc = await documents.upload(fake_kb.id, "a.md", MD, owner)
    real_kb = documents._kb
    calls = []

    async def spy(kb_id, *, for_update=False):
        calls.append((kb_id, for_update))
        return await real_kb(kb_id, for_update=for_update)

    monkeypatch.setattr(documents, "_kb", spy)
    await documents.delete(doc.id, owner)
    # Serialises with a KB update starting a rebuild, so no building collection is missed.
    assert calls == [(fake_kb.id, True)]


async def test_chunks_default_to_served_version(session, documents, fake_kb, org, owner):
    doc = await documents.upload(fake_kb.id, "a.md", MD, owner)
    for version, count in ((1, 3), (2, 2)):
        for ordinal in range(count):
            session.add(
                Chunk(
                    organisation_id=org.id,
                    knowledge_base_id=fake_kb.id,
                    document_id=doc.id,
                    kb_version=version,
                    ordinal=ordinal,
                    text=f"v{version}-{ordinal}",
                    char_start=0,
                    char_end=1,
                    metadata_={},
                )
            )
    await session.flush()
    items, total, version = await documents.chunks(doc.id)  # indexed_version is None -> KB active_version (1)
    assert total == 3 and [c.text for c in items] == ["v1-0", "v1-1", "v1-2"]
    doc.indexed_version = 2
    items, total, _ = await documents.chunks(doc.id, limit=1, offset=1)
    assert total == 2 and [c.text for c in items] == ["v2-1"]
    items, total, _ = await documents.chunks(doc.id, version=1, limit=2)
    assert total == 3 and [c.text for c in items] == ["v1-0", "v1-1"]


async def test_list_filters_by_status(session, documents, fake_kb, owner):
    a = await documents.upload(fake_kb.id, "a.md", b"aaa", owner)
    b = await documents.upload(fake_kb.id, "b.md", b"bbb", owner)
    b.status = "failed"
    await session.flush()
    assert [d.id for d in await documents.list(fake_kb.id, status="failed")] == [b.id]
    assert [d.id for d in await documents.list(fake_kb.id, status="uploaded")] == [a.id]


async def test_audit_actions_written(session, documents, fake_kb, org, owner):
    doc = await documents.upload(fake_kb.id, "a.md", MD, owner)
    await documents.start_indexing(fake_kb.id, None, owner)
    await documents.delete(doc.id, owner)
    rows = list((await session.execute(select(AuditLog).where(AuditLog.organisation_id == org.id))).scalars())
    by_action = {r.action: r for r in rows}
    assert {"kb.created", "document.uploaded", "kb.indexing_started", "document.deleted"} <= set(by_action)
    assert (by_action["document.uploaded"].target_type, by_action["document.uploaded"].target_id) == (
        "document",
        str(doc.id),
    )
    assert by_action["document.uploaded"].metadata_["filename"] == "a.md"
    assert (by_action["document.deleted"].target_type, by_action["document.deleted"].target_id) == (
        "document",
        str(doc.id),
    )
    started = by_action["kb.indexing_started"]
    assert (started.target_type, started.target_id) == ("knowledge_base", str(fake_kb.id))
    assert started.metadata_["document_ids"] == [str(doc.id)]
    for row in rows:
        assert "storage_key" not in json.dumps(row.metadata_)


async def test_upload_refused_over_storage_402(session, org, bus, adapters, kb_settings, fake_kb, owner, storage):
    from dataclasses import replace

    from supportcore.billing.errors import PlanLimitError
    from supportcore.billing.fake_billing import FakeBilling
    from supportcore.testing import start_trial_plan

    await start_trial_plan(session, org.id, storage_mb=1)
    settings = kb_settings.model_copy(update={"max_upload_bytes": 2_000_000})
    service = DocumentService(session, org.id, bus, replace(adapters, billing=FakeBilling()), settings)
    await service.upload(fake_kb.id, "refunds.md", MD, owner)
    # Raw bytes, not rounded megabytes: exactly the rest of the 1,000,000 bytes still fits.
    rest = b"# Other\n" + b"x" * (1_000_000 - len(MD) - len(b"# Other\n"))
    await service.upload(fake_kb.id, "other.md", rest, owner)
    with pytest.raises(PlanLimitError) as raised:
        await service.upload(fake_kb.id, "one-more.md", b"#", owner)
    assert (raised.value.resource, raised.value.limit) == ("storage_mb", 1)
    assert len(storage.objects) == 2  # refused before the storage put
