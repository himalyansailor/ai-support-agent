"""Saved reply routes (Task 7). Real commits (kb_client); the session_factory fixture truncates."""

from uuid import UUID

from kb_helpers import create_org, join_org, signup


async def _setup(kb_client, session_factory):
    owner = await signup(kb_client, "owner@x.com")
    org_id = await create_org(kb_client, owner)
    agent = await join_org(kb_client, session_factory, UUID(org_id), "agent@x.com", role_name="Agent")
    return org_id, owner, agent


async def test_crud_search_and_errors(kb_client, session_factory):
    org_id, _, agent = await _setup(kb_client, session_factory)
    url = f"/orgs/{org_id}/saved-replies"
    r = await kb_client.post(url, json={"title": "Refunds", "body": "Hi {customer_name}", "shortcut": "refund"}, headers=agent)
    assert r.status_code == 201, r.text
    sr = r.json()
    assert sr["shortcut"] == "refund" and sr["created_by_membership_id"]
    r = await kb_client.post(url, json={"title": "Dup", "body": "b", "shortcut": "refund"}, headers=agent)
    assert r.status_code == 409 and r.json()["code"] == "saved_reply_shortcut_taken"
    r = await kb_client.post(url, json={"title": "Bad", "body": "b", "shortcut": "Bad Cut"}, headers=agent)
    assert r.status_code == 422 and r.json()["code"] == "invalid_saved_reply"
    r = await kb_client.post(url, json={"title": " ", "body": "b"}, headers=agent)
    assert r.status_code == 422 and r.json()["code"] == "invalid_saved_reply"
    await kb_client.post(url, json={"title": "Shipping", "body": "b"}, headers=agent)
    assert len((await kb_client.get(url, headers=agent)).json()) == 2
    r = await kb_client.get(url, params={"q": "REFU"}, headers=agent)
    assert [x["id"] for x in r.json()] == [sr["id"]]
    r = await kb_client.patch(f"{url}/{sr['id']}", json={"title": "Refund policy"}, headers=agent)
    assert r.status_code == 200 and r.json()["title"] == "Refund policy" and r.json()["shortcut"] == "refund"
    r = await kb_client.patch(f"{url}/{sr['id']}", json={"shortcut": None}, headers=agent)
    assert r.json()["shortcut"] is None and r.json()["title"] == "Refund policy"
    r = await kb_client.get(f"{url}/{sr['id']}", headers=agent)
    assert r.status_code == 200 and r.json()["body"] == "Hi {customer_name}"
    assert (await kb_client.delete(f"{url}/{sr['id']}", headers=agent)).status_code == 204
    assert (await kb_client.get(f"{url}/{sr['id']}", headers=agent)).status_code == 404


async def test_routes_permissions_and_cross_tenant(kb_client, session_factory):
    org_id, owner, _ = await _setup(kb_client, session_factory)
    viewer = await join_org(kb_client, session_factory, UUID(org_id), "viewer@x.com", role_name="Viewer")
    url = f"/orgs/{org_id}/saved-replies"
    sr = (await kb_client.post(url, json={"title": "T", "body": "b"}, headers=owner)).json()
    for method, u, body in [
        ("get", url, None),
        ("get", f"{url}/{sr['id']}", None),
        ("post", url, {"title": "N", "body": "b"}),
        ("patch", f"{url}/{sr['id']}", {"title": "X"}),
        ("delete", f"{url}/{sr['id']}", None),
    ]:
        kwargs = {"json": body} if body is not None else {}
        r = await getattr(kb_client, method)(u, headers=viewer, **kwargs)
        assert r.status_code == 403, (method, u, r.text)

    other = await signup(kb_client, "other@x.com")
    other_org = await create_org(kb_client, other, "Other")
    foreign = (await kb_client.post(f"/orgs/{other_org}/saved-replies", json={"title": "F", "body": "b"}, headers=other)).json()
    for method, u, body in [
        ("get", f"{url}/{foreign['id']}", None),
        ("patch", f"{url}/{foreign['id']}", {"title": "X"}),
        ("delete", f"{url}/{foreign['id']}", None),
    ]:
        kwargs = {"json": body} if body is not None else {}
        r = await getattr(kb_client, method)(u, headers=owner, **kwargs)
        assert r.status_code == 404 and r.json()["code"] == "not_found", (method, u, r.text)
    assert (await kb_client.get(f"/orgs/{other_org}/saved-replies", headers=owner)).status_code == 404
    assert len((await kb_client.get(url, headers=owner)).json()) == 1
