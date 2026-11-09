from uuid import UUID, uuid4

from sqlalchemy import select

from kb_helpers import create_org, join_org, signup
from supportcore.db.models import AuditLog

RULE_VIEW = {"id", "kind", "builtin_key", "name", "description", "keywords", "action", "enabled", "locked", "position", "params"}


async def _setup(kb_client):
    headers = await signup(kb_client, "owner@x.com")
    return headers, await create_org(kb_client, headers)


async def _rules(client, org_id, headers) -> list[dict]:
    r = await client.get(f"/orgs/{org_id}/risk-rules", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def test_list_seeds_defaults(kb_client):
    headers, org_id = await _setup(kb_client)
    rules = await _rules(kb_client, org_id, headers)
    assert [r["name"] for r in rules][:3] == ["Refunds", "Cancel subscription", "Payment changes"]
    assert len(rules) == 8 and set(rules[0]) == RULE_VIEW
    assert [r["position"] for r in rules] == list(range(8))
    assert {r["builtin_key"] for r in rules if r["kind"] == "builtin"} == {
        "asks_for_human", "low_confidence", "frustration", "repeated_failure", "write_action",
    }


async def test_risk_rules_permissions(kb_client, session_factory):
    headers, org_id = await _setup(kb_client)
    rules = await _rules(kb_client, org_id, headers)
    base = f"/orgs/{org_id}/risk-rules"
    agent = await join_org(kb_client, session_factory, UUID(org_id), "agent@x.com", role_name="Agent")
    assert (await kb_client.get(base, headers=agent)).status_code == 403
    assert (await kb_client.post(base, json={"name": "N"}, headers=agent)).status_code == 403
    assert (await kb_client.patch(f"{base}/{rules[0]['id']}", json={"enabled": False}, headers=agent)).status_code == 403
    assert (await kb_client.delete(f"{base}/{rules[0]['id']}", headers=agent)).status_code == 403
    assert (await kb_client.put(f"{base}/order", json={"ids": []}, headers=agent)).status_code == 403
    # Viewer has analytics.view: may read, may not write.
    viewer = await join_org(kb_client, session_factory, UUID(org_id), "viewer@x.com", role_name="Viewer")
    assert len((await kb_client.get(base, headers=viewer)).json()) == 8
    assert (await kb_client.post(base, json={"name": "N"}, headers=viewer)).status_code == 403
    assert (await kb_client.patch(f"{base}/{rules[0]['id']}", json={"enabled": False}, headers=viewer)).status_code == 403
    # A custom role with bot.configure can write.
    role = await kb_client.post(
        f"/orgs/{org_id}/roles", json={"name": "Botter", "permissions": ["bot.configure"]}, headers=headers
    )
    member = await join_org(kb_client, session_factory, UUID(org_id), "bot@x.com", role_id=UUID(role.json()["id"]))
    assert (await kb_client.get(base, headers=member)).status_code == 200
    assert (await kb_client.post(base, json={"name": "N"}, headers=member)).status_code == 201


async def test_topic_lifecycle_and_audit(kb_client, session_factory):
    headers, org_id = await _setup(kb_client)
    base = f"/orgs/{org_id}/risk-rules"
    r = await kb_client.post(
        base,
        json={"name": "Legal", "description": "Lawsuits", "keywords": ["lawyer", "sue"], "action": "offer_human"},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    topic = r.json()
    assert (topic["kind"], topic["builtin_key"], topic["position"], topic["locked"]) == ("topic", None, 8, False)
    r = await kb_client.patch(
        f"{base}/{topic['id']}", json={"action": "escalate", "keywords": ["lawyer"], "enabled": False}, headers=headers
    )
    assert r.status_code == 200 and (r.json()["action"], r.json()["enabled"]) == ("escalate", False)
    assert (await kb_client.delete(f"{base}/{topic['id']}", headers=headers)).status_code == 204
    assert topic["id"] not in {x["id"] for x in await _rules(kb_client, org_id, headers)}
    async with session_factory() as s:
        actions = (await s.execute(select(AuditLog.action).order_by(AuditLog.created_at))).scalars().all()
    assert [a for a in actions if a.startswith("risk_rule.")] == [
        "risk_rule.created", "risk_rule.updated", "risk_rule.deleted",
    ]


async def test_error_codes(kb_client):
    headers, org_id = await _setup(kb_client)
    base = f"/orgs/{org_id}/risk-rules"
    rules = await _rules(kb_client, org_id, headers)
    builtin = next(r for r in rules if r["builtin_key"] == "frustration")
    locked = next(r for r in rules if r["builtin_key"] == "write_action")
    r = await kb_client.delete(f"{base}/{builtin['id']}", headers=headers)
    assert (r.status_code, r.json()["code"]) == (409, "builtin_rule")
    for call in (
        kb_client.patch(f"{base}/{locked['id']}", json={"enabled": False}, headers=headers),
        kb_client.patch(f"{base}/{locked['id']}", json={"action": "offer_human"}, headers=headers),
        kb_client.delete(f"{base}/{locked['id']}", headers=headers),
    ):
        r = await call
        assert (r.status_code, r.json()["code"]) == (409, "rule_locked")
    r = await kb_client.post(base, json={"name": "N", "keywords": ["x"]}, headers=headers)
    assert (r.status_code, r.json()["code"]) == (422, "invalid_risk_rule")
    r = await kb_client.post(base, json={"name": "N", "action": "nope"}, headers=headers)
    assert (r.status_code, r.json()["code"]) == (422, "invalid_risk_rule")
    r = await kb_client.patch(f"{base}/{builtin['id']}", json={"params": {"x": 1}}, headers=headers)
    assert (r.status_code, r.json()["code"]) == (422, "invalid_risk_rule")


async def test_reorder(kb_client):
    headers, org_id = await _setup(kb_client)
    base = f"/orgs/{org_id}/risk-rules"
    ids = [r["id"] for r in await _rules(kb_client, org_id, headers)]
    r = await kb_client.put(f"{base}/order", json={"ids": [ids[1], ids[0]]}, headers=headers)
    assert r.status_code == 200 and [x["id"] for x in r.json()] == [ids[1], ids[0], *ids[2:]]
    assert [x["id"] for x in await _rules(kb_client, org_id, headers)][:2] == [ids[1], ids[0]]
    r = await kb_client.put(f"{base}/order", json={"ids": [str(uuid4())]}, headers=headers)
    assert r.status_code == 404


async def test_cross_tenant_rule_404(kb_client):
    headers, org_id = await _setup(kb_client)
    other = await signup(kb_client, "other@x.com")
    other_org = await create_org(kb_client, other, "Other")
    foreign = (await _rules(kb_client, other_org, other))[0]
    base = f"/orgs/{org_id}/risk-rules"
    await _rules(kb_client, org_id, headers)
    assert (await kb_client.patch(f"{base}/{foreign['id']}", json={"enabled": False}, headers=headers)).status_code == 404
    assert (await kb_client.delete(f"{base}/{foreign['id']}", headers=headers)).status_code == 404
    assert (await kb_client.put(f"{base}/order", json={"ids": [foreign["id"]]}, headers=headers)).status_code == 404
    # Another organisation's id in the path is not the caller's org: no access at all.
    assert (await kb_client.get(f"/orgs/{other_org}/risk-rules", headers=headers)).status_code in (403, 404)
    assert (await _rules(kb_client, other_org, other))[0]["enabled"] is True
