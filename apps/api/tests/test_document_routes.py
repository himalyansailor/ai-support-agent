import io
import zipfile

from kb_helpers import MARKDOWN, create_kb, create_org, signup, upload

DOC_VIEW = {"id", "filename", "file_type", "size_bytes", "status", "error", "chunk_count", "indexed_version", "uploaded_at", "updated_at"}


async def _setup(kb_client):
    headers = await signup(kb_client, "owner@x.com")
    org_id = await create_org(kb_client, headers)
    return headers, org_id, (await create_kb(kb_client, org_id, headers))["id"]


async def test_upload_index_and_chunks_flow(kb_client, kb_app):
    headers, org_id, kb_id = await _setup(kb_client)
    r = await upload(kb_client, org_id, kb_id, headers)
    assert r.status_code == 201
    doc = r.json()
    assert set(doc) == DOC_VIEW
    assert (doc["filename"], doc["file_type"], doc["status"], doc["size_bytes"]) == (
        "refunds.md", "markdown", "uploaded", len(MARKDOWN),
    )
    assert doc["chunk_count"] == 0 and doc["indexed_version"] is None

    r = await kb_client.post(f"/orgs/{org_id}/kbs/{kb_id}/documents/index", json={"document_ids": [doc["id"]]}, headers=headers)
    assert r.status_code == 202
    (job,) = r.json()["jobs"]
    assert job["document_id"] == doc["id"] and job["id"]
    await kb_app.state.job_queue.drain()

    got = (await kb_client.get(f"/orgs/{org_id}/kbs/{kb_id}/documents/{doc['id']}", headers=headers)).json()
    assert (got["status"], got["indexed_version"]) == ("indexed", 1)
    assert got["chunk_count"] > 0
    listed = (await kb_client.get(f"/orgs/{org_id}/kbs/{kb_id}/documents?status=indexed", headers=headers)).json()
    assert [d["id"] for d in listed] == [doc["id"]]
    assert (await kb_client.get(f"/orgs/{org_id}/kbs/{kb_id}/documents?status=failed", headers=headers)).json() == []
    kb = (await kb_client.get(f"/orgs/{org_id}/kbs/{kb_id}", headers=headers)).json()
    assert kb["document_counts"]["indexed"] == 1

    chunks = (await kb_client.get(f"/orgs/{org_id}/kbs/{kb_id}/documents/{doc['id']}/chunks", headers=headers)).json()
    assert chunks["version"] == 1 and chunks["total"] == got["chunk_count"]
    assert chunks["items"] and set(chunks["items"][0]) == {"id", "ordinal", "text", "char_start", "char_end", "metadata"}
    assert "refund" in chunks["items"][0]["text"]
    page = (await kb_client.get(f"/orgs/{org_id}/kbs/{kb_id}/documents/{doc['id']}/chunks?limit=1&offset=0", headers=headers)).json()
    assert len(page["items"]) == 1 and page["total"] == chunks["total"]

    # Reindex while idle queues a job; the response lists it.
    r = await kb_client.post(f"/orgs/{org_id}/kbs/{kb_id}/documents/{doc['id']}/reindex", headers=headers)
    assert r.status_code == 202 and len(r.json()["jobs"]) == 1
    await kb_app.state.job_queue.drain()

    assert (await kb_client.delete(f"/orgs/{org_id}/kbs/{kb_id}/documents/{doc['id']}", headers=headers)).status_code == 204
    await kb_app.state.job_queue.drain()
    assert (await kb_client.get(f"/orgs/{org_id}/kbs/{kb_id}/documents/{doc['id']}", headers=headers)).status_code == 404


async def test_index_without_ids_picks_uploaded_documents(kb_client, kb_app):
    headers, org_id, kb_id = await _setup(kb_client)
    await upload(kb_client, org_id, kb_id, headers, "a.md", MARKDOWN)
    await upload(kb_client, org_id, kb_id, headers, "b.md", MARKDOWN + b"more")
    r = await kb_client.post(f"/orgs/{org_id}/kbs/{kb_id}/documents/index", json={"document_ids": None}, headers=headers)
    assert (r.status_code, len(r.json()["jobs"])) == (202, 2)
    await kb_app.state.job_queue.drain()
    docs = (await kb_client.get(f"/orgs/{org_id}/kbs/{kb_id}/documents", headers=headers)).json()
    assert {d["status"] for d in docs} == {"indexed"}


async def test_upload_duplicate_409(kb_client):
    headers, org_id, kb_id = await _setup(kb_client)
    assert (await upload(kb_client, org_id, kb_id, headers)).status_code == 201
    r = await upload(kb_client, org_id, kb_id, headers, "copy.md")
    assert (r.status_code, r.json()["code"]) == (409, "duplicate_document")


async def test_upload_too_large_413(kb_client, kb_settings):
    headers, org_id, kb_id = await _setup(kb_client)
    limit = kb_settings.max_upload_bytes
    r = await upload(kb_client, org_id, kb_id, headers, "big.txt", b"a" * (limit + 1))
    assert (r.status_code, r.json()["code"]) == (413, "upload_too_large")
    assert (await upload(kb_client, org_id, kb_id, headers, "exact.txt", b"a" * limit)).status_code == 201


async def test_upload_disguised_type_415(kb_client):
    headers, org_id, kb_id = await _setup(kb_client)
    r = await upload(kb_client, org_id, kb_id, headers, "fake.pdf", b"just some text, not a pdf")
    assert (r.status_code, r.json()["code"]) == (415, "unsupported_file_type")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "hi")
    r = await upload(kb_client, org_id, kb_id, headers, "fake.docx", buf.getvalue())
    assert (r.status_code, r.json()["code"]) == (415, "unsupported_file_type")
    r = await upload(kb_client, org_id, kb_id, headers, "run.exe", b"MZ\x00\x00")
    assert (r.status_code, r.json()["code"]) == (415, "unsupported_file_type")


async def test_upload_missing_or_empty_file_422(kb_client):
    headers, org_id, kb_id = await _setup(kb_client)
    url = f"/orgs/{org_id}/kbs/{kb_id}/documents"
    assert (await kb_client.post(url, headers=headers)).status_code == 422
    assert (await kb_client.post(url, data={"x": "y"}, headers=headers)).status_code == 422
    assert (await upload(kb_client, org_id, kb_id, headers, "empty.txt", b"")).status_code == 422


async def test_document_view_never_exposes_storage_key(kb_client, kb_app):
    headers, org_id, kb_id = await _setup(kb_client)
    doc = (await upload(kb_client, org_id, kb_id, headers)).json()
    await kb_client.post(f"/orgs/{org_id}/kbs/{kb_id}/documents/index", json={}, headers=headers)
    await kb_app.state.job_queue.drain()
    responses = [
        await kb_client.get(f"/orgs/{org_id}/kbs/{kb_id}/documents", headers=headers),
        await kb_client.get(f"/orgs/{org_id}/kbs/{kb_id}/documents/{doc['id']}", headers=headers),
        await kb_client.get(f"/orgs/{org_id}/kbs/{kb_id}/documents/{doc['id']}/chunks", headers=headers),
        await kb_client.get(f"/orgs/{org_id}/kbs/{kb_id}/jobs", headers=headers),
    ]
    for r in responses:
        assert r.status_code == 200
        assert "storage_key" not in r.text and "orgs/" not in r.text and "sha256" not in r.text
    assert (await upload(kb_client, org_id, kb_id, headers, "b.md", MARKDOWN + b"x")).json().keys() == DOC_VIEW


async def test_reindex_retries_a_document_stuck_in_the_queue(kb_client, kb_app, session_factory):
    from datetime import UTC, datetime, timedelta
    from uuid import UUID

    from supportcore.db.models import Document, IndexingJob

    headers, org_id, kb_id = await _setup(kb_client)
    doc = (await upload(kb_client, org_id, kb_id, headers)).json()
    url = f"/orgs/{org_id}/kbs/{kb_id}/documents/{doc['id']}"
    # What a lost enqueue leaves behind: the document and its job queued, no message on the way.
    async with session_factory() as s:
        document = await s.get(Document, UUID(doc["id"]))
        document.status = "queued"
        s.add(IndexingJob(
            organisation_id=document.organisation_id, knowledge_base_id=document.knowledge_base_id,
            document_id=document.id, kind="document", kb_version=1, status="queued",
            queued_at=datetime.now(UTC) - timedelta(minutes=5),
        ))
        await s.commit()
    got = (await kb_client.get(url, headers=headers)).json()
    assert got["status"] == "queued" and got["updated_at"] >= got["uploaded_at"]
    r = await kb_client.post(url + "/reindex", headers=headers)
    assert r.status_code == 202 and r.json()["jobs"] == []  # 5 minutes: not stuck yet

    async with session_factory() as s:
        await s.execute(
            IndexingJob.__table__.update().values(queued_at=datetime.now(UTC) - timedelta(minutes=11))
        )
        await s.commit()
    r = await kb_client.post(url + "/reindex", headers=headers)
    assert r.status_code == 202 and len(r.json()["jobs"]) == 1
    await kb_app.state.job_queue.drain()
    assert (await kb_client.get(url, headers=headers)).json()["status"] == "indexed"
