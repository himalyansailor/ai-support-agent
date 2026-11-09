from uuid import UUID, uuid4

from kb_helpers import FAKE_EMBEDDING, MARKDOWN, create_kb, create_org, join_org, signup, upload

KB_VIEW = {
    "id", "name", "description", "embedding", "chunking", "reranker", "active_version", "building_version",
    "document_counts", "created_at",
}
JOB_VIEW = {
    "id", "kind", "document_id", "document_name", "status", "kb_version", "attempts", "error", "steps",
    "queued_at", "started_at", "finished_at",
}


async def _setup(kb_client):
    headers = await signup(kb_client, "owner@x.com")
    return headers, await create_org(kb_client, headers)


async def test_create_kb_and_list(kb_client):
    headers, org_id = await _setup(kb_client)
    r = await kb_client.post(f"/orgs/{org_id}/kbs", json={"name": "Docs", "embedding": FAKE_EMBEDDING}, headers=headers)
    assert r.status_code == 201
    kb = r.json()
    assert set(kb) == KB_VIEW
    assert kb["embedding"] == {"provider": "fake", "model": "fake-embedding", "dimensions": 8, "credential_id": None}
    assert kb["chunking"] == {"strategy": "recursive", "chunk_size": 1000, "chunk_overlap": 150}
    assert kb["reranker"] == {"name": "none", "params": {}}
    assert (kb["active_version"], kb["building_version"]) == (1, None)
    assert kb["document_counts"] == {"uploaded": 0, "queued": 0, "processing": 0, "indexed": 0, "failed": 0}

    await upload(kb_client, org_id, kb["id"], headers)
    listed = (await kb_client.get(f"/orgs/{org_id}/kbs", headers=headers)).json()
    assert [k["id"] for k in listed] == [kb["id"]]
    assert listed[0]["document_counts"]["uploaded"] == 1
    got = (await kb_client.get(f"/orgs/{org_id}/kbs/{kb['id']}", headers=headers)).json()
    assert got["document_counts"]["uploaded"] == 1

    dup = await kb_client.post(f"/orgs/{org_id}/kbs", json={"name": "docs", "embedding": FAKE_EMBEDDING}, headers=headers)
    assert (dup.status_code, dup.json()["code"]) == (409, "knowledge_base_name_taken")
    bad = await kb_client.post(
        f"/orgs/{org_id}/kbs",
        json={"name": "Bad", "embedding": FAKE_EMBEDDING, "chunking": {"chunk_size": 50}},
        headers=headers,
    )
    assert (bad.status_code, bad.json()["code"]) == (422, "invalid_chunking_params")


async def test_patch_embedding_returns_rebuild_started(kb_client, kb_app):
    headers, org_id = await _setup(kb_client)
    kb = await create_kb(kb_client, org_id, headers)
    url = f"/orgs/{org_id}/kbs/{kb['id']}"

    # Nothing indexed yet: the change applies in place.
    r = await kb_client.patch(url, json={"name": "Renamed", "chunking": {"chunk_size": 500, "chunk_overlap": 50}}, headers=headers)
    assert r.status_code == 200
    assert (r.json()["name"], r.json()["rebuild_started"]) == ("Renamed", False)
    assert r.json()["chunking"] == {"strategy": "recursive", "chunk_size": 500, "chunk_overlap": 50}

    doc = (await upload(kb_client, org_id, kb["id"], headers)).json()
    await kb_client.post(f"{url}/documents/index", json={"document_ids": None}, headers=headers)
    await kb_app.state.job_queue.drain()
    assert (await kb_client.get(f"{url}/documents/{doc['id']}", headers=headers)).json()["status"] == "indexed"

    # Re-sending unchanged values is not a change.
    same = await kb_client.patch(url, json={"chunking": {"chunk_size": 500}, "embedding": FAKE_EMBEDDING}, headers=headers)
    assert (same.status_code, same.json()["rebuild_started"]) == (200, False)

    r = await kb_client.patch(url, json={"chunking": {"chunk_size": 300, "chunk_overlap": 30}}, headers=headers)
    assert r.status_code == 200
    assert r.json()["rebuild_started"] is True
    assert r.json()["building_version"] == 2
    assert r.json()["chunking"]["chunk_size"] == 500  # the active version keeps serving


async def test_patch_partial_while_rebuild_pending(kb_client, kb_app):
    """Only fields present in the body reach the service, so a reranker change is fine mid-rebuild."""
    headers, org_id = await _setup(kb_client)
    kb = await create_kb(kb_client, org_id, headers)
    url = f"/orgs/{org_id}/kbs/{kb['id']}"
    await upload(kb_client, org_id, kb["id"], headers)
    await kb_client.post(f"{url}/documents/index", json={"document_ids": None}, headers=headers)
    await kb_app.state.job_queue.drain()

    queue = kb_app.state.job_queue
    held: list = []
    real_enqueue = queue.enqueue
    queue.enqueue = lambda name, payload, **kw: held.append((name, payload))  # keep the rebuild pending
    try:
        r = await kb_client.patch(url, json={"chunking": {"chunk_size": 400, "chunk_overlap": 40}}, headers=headers)
        assert r.json()["rebuild_started"] is True
        again = await kb_client.patch(url, json={"chunking": {"chunk_size": 300}}, headers=headers)
        assert (again.status_code, again.json()["code"]) == (409, "rebuild_in_progress")
        ok = await kb_client.patch(url, json={"reranker": {"name": "none"}}, headers=headers)
        assert (ok.status_code, ok.json()["rebuild_started"]) == (200, False)
        renamed = await kb_client.patch(url, json={"name": "Still fine"}, headers=headers)
        assert renamed.status_code == 200
    finally:
        queue.enqueue = real_enqueue


async def test_delete_kb(kb_client, kb_app):
    headers, org_id = await _setup(kb_client)
    kb = await create_kb(kb_client, org_id, headers)
    r = await kb_client.delete(f"/orgs/{org_id}/kbs/{kb['id']}", headers=headers)
    assert r.status_code == 204
    await kb_app.state.job_queue.drain()
    assert (await kb_client.get(f"/orgs/{org_id}/kbs/{kb['id']}", headers=headers)).status_code == 404


async def test_jobs_log_shows_steps(kb_client, kb_app):
    headers, org_id = await _setup(kb_client)
    kb = await create_kb(kb_client, org_id, headers)
    doc = (await upload(kb_client, org_id, kb["id"], headers)).json()
    started = await kb_client.post(f"/orgs/{org_id}/kbs/{kb['id']}/documents/index", json={}, headers=headers)
    assert started.status_code == 202
    await kb_app.state.job_queue.drain()

    r = await kb_client.get(f"/orgs/{org_id}/kbs/{kb['id']}/jobs", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["next_before"] is None
    (job,) = body["items"]
    assert set(job) == JOB_VIEW
    assert (job["kind"], job["status"], job["document_name"], job["document_id"]) == (
        "document", "succeeded", "refunds.md", doc["id"],
    )
    assert [s["name"] for s in job["steps"]] == ["download", "parse", "chunk", "embed", "upsert"]
    assert "storage_key" not in r.text and "orgs/" not in r.text
    assert "Customers may request" not in r.text  # no chunk text in the log

    # Keyset pagination: newest first, one per page.
    await kb_client.post(f"/orgs/{org_id}/kbs/{kb['id']}/documents/{doc['id']}/reindex", headers=headers)
    await kb_app.state.job_queue.drain()
    page1 = (await kb_client.get(f"/orgs/{org_id}/kbs/{kb['id']}/jobs?limit=1", headers=headers)).json()
    assert len(page1["items"]) == 1 and page1["next_before"]
    page2 = (
        await kb_client.get(f"/orgs/{org_id}/kbs/{kb['id']}/jobs?limit=1&before={page1['next_before']}", headers=headers)
    ).json()
    assert len(page2["items"]) == 1 and page2["items"][0]["id"] != page1["items"][0]["id"]
    assert page2["next_before"] is None
    bad = await kb_client.get(f"/orgs/{org_id}/kbs/{kb['id']}/jobs?before=garbage", headers=headers)
    assert bad.status_code == 422


async def test_cross_tenant_kb_and_document_404(kb_client, kb_app):
    headers, org_id = await _setup(kb_client)
    kb = await create_kb(kb_client, org_id, headers)
    doc = (await upload(kb_client, org_id, kb["id"], headers)).json()
    await kb_client.post(f"/orgs/{org_id}/kbs/{kb['id']}/documents/index", json={}, headers=headers)
    await kb_app.state.job_queue.drain()

    other = await signup(kb_client, "other@x.com")
    other_org = await create_org(kb_client, other, "Other")
    own_kb = await create_kb(kb_client, other_org, other, "Mine")
    kb_id, doc_id = kb["id"], doc["id"]

    # Someone else's org id: not a member.
    for path in (
        f"/orgs/{org_id}/kbs/{kb_id}",
        f"/orgs/{org_id}/kbs/{kb_id}/documents",
        f"/orgs/{org_id}/kbs/{kb_id}/documents/{doc_id}",
        f"/orgs/{org_id}/kbs/{kb_id}/documents/{doc_id}/chunks",
        f"/orgs/{org_id}/kbs/{kb_id}/jobs",
    ):
        assert (await kb_client.get(path, headers=other)).status_code == 404, path
    # Own org, foreign ids.
    base = f"/orgs/{other_org}/kbs"
    for path in (
        f"{base}/{kb_id}",
        f"{base}/{kb_id}/documents",
        f"{base}/{kb_id}/documents/{doc_id}",
        f"{base}/{kb_id}/documents/{doc_id}/chunks",
        f"{base}/{kb_id}/jobs",
        f"{base}/{own_kb['id']}/documents/{doc_id}",  # foreign document under my own KB
        f"{base}/{own_kb['id']}/documents/{doc_id}/chunks",
    ):
        assert (await kb_client.get(path, headers=other)).status_code == 404, path
    assert (await kb_client.patch(f"{base}/{kb_id}", json={"name": "x"}, headers=other)).status_code == 404
    assert (await kb_client.delete(f"{base}/{kb_id}", headers=other)).status_code == 404
    assert (await kb_client.delete(f"{base}/{own_kb['id']}/documents/{doc_id}", headers=other)).status_code == 404
    assert (await kb_client.post(f"{base}/{own_kb['id']}/documents/{doc_id}/reindex", headers=other)).status_code == 404
    assert (await upload(kb_client, other_org, kb_id, other)).status_code == 404
    r = await kb_client.post(f"{base}/{own_kb['id']}/documents/index", json={"document_ids": [doc_id]}, headers=other)
    assert r.status_code == 404
    assert (await kb_client.get(f"{base}/{uuid4()}", headers=other)).status_code == 404
    # Nothing leaked or changed for the owner.
    assert (await kb_client.get(f"/orgs/{org_id}/kbs/{kb_id}", headers=headers)).json()["name"] == "Docs"


async def test_agent_cannot_upload_403(kb_client, session_factory):
    headers, org_id = await _setup(kb_client)
    kb = await create_kb(kb_client, org_id, headers)
    agent = await join_org(kb_client, session_factory, UUID(org_id), "agent@x.com", role_name="Agent")
    url = f"/orgs/{org_id}/kbs"
    assert (await upload(kb_client, org_id, kb["id"], agent)).status_code == 403
    assert (await kb_client.get(url, headers=agent)).status_code == 403
    assert (await kb_client.post(url, json={"name": "N", "embedding": FAKE_EMBEDDING}, headers=agent)).status_code == 403


async def test_viewer_with_bot_configure_can_read_not_write(kb_client, session_factory):
    headers, org_id = await _setup(kb_client)
    kb = await create_kb(kb_client, org_id, headers)
    doc = (await upload(kb_client, org_id, kb["id"], headers)).json()
    role = await kb_client.post(
        f"/orgs/{org_id}/roles", json={"name": "Configurer", "permissions": ["bot.configure"]}, headers=headers
    )
    assert role.status_code == 201, role.text
    member = await join_org(kb_client, session_factory, UUID(org_id), "cfg@x.com", role_id=UUID(role.json()["id"]))
    base = f"/orgs/{org_id}/kbs"
    for path in (base, f"{base}/{kb['id']}", f"{base}/{kb['id']}/documents", f"{base}/{kb['id']}/documents/{doc['id']}",
                 f"{base}/{kb['id']}/documents/{doc['id']}/chunks", f"{base}/{kb['id']}/jobs"):
        assert (await kb_client.get(path, headers=member)).status_code == 200, path
    assert (await kb_client.post(base, json={"name": "N", "embedding": FAKE_EMBEDDING}, headers=member)).status_code == 403
    assert (await kb_client.patch(f"{base}/{kb['id']}", json={"name": "N"}, headers=member)).status_code == 403
    assert (await kb_client.delete(f"{base}/{kb['id']}", headers=member)).status_code == 403
    assert (await upload(kb_client, org_id, kb["id"], member, "other.md", MARKDOWN + b"x")).status_code == 403
    assert (await kb_client.post(f"{base}/{kb['id']}/documents/index", json={}, headers=member)).status_code == 403
    assert (await kb_client.post(f"{base}/{kb['id']}/documents/{doc['id']}/reindex", headers=member)).status_code == 403
    assert (await kb_client.delete(f"{base}/{kb['id']}/documents/{doc['id']}", headers=member)).status_code == 403


async def test_patch_description_empty_clears_and_null_is_422(kb_client):
    headers, org_id = await _setup(kb_client)
    kb = await create_kb(kb_client, org_id, headers, description="About refunds")
    url = f"/orgs/{org_id}/kbs/{kb['id']}"
    r = await kb_client.patch(url, json={"description": ""}, headers=headers)
    assert (r.status_code, r.json()["description"]) == (200, "")
    assert (await kb_client.patch(url, json={"description": None}, headers=headers)).status_code == 422
    assert (await kb_client.patch(url, json={"name": None}, headers=headers)).status_code == 422
    assert (await kb_client.get(url, headers=headers)).json()["description"] == ""
