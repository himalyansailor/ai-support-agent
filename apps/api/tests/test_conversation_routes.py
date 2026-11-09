from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from kb_helpers import create_kb, create_org, join_org, signup
from supportcore.db.models import BotTurn, Chunk, Conversation, Customer, Document, Message

T0 = datetime(2026, 1, 1, 12, tzinfo=UTC)


async def _setup(kb_client):
    headers = await signup(kb_client, "owner@x.com")
    return headers, await create_org(kb_client, headers)


async def _conversation(session_factory, org_id, *, status="bot", is_test=False, at=T0, external_id=None, texts=("hi",)):
    async with session_factory() as s:
        customer = Customer(
            organisation_id=UUID(org_id), external_id=external_id, visitor_id=uuid4().hex, email="c@x.com", name="Cee"
        )
        s.add(customer)
        await s.flush()
        conv = Conversation(
            organisation_id=UUID(org_id), customer_id=customer.id, status=status, is_test=is_test, last_message_at=at
        )
        s.add(conv)
        await s.flush()
        for i, text in enumerate(texts):
            s.add(Message(organisation_id=UUID(org_id), conversation_id=conv.id, role="customer", text=text,
                          created_at=at + timedelta(seconds=i)))
        await s.commit()
        return conv.id


async def test_list_filters_status_and_excludes_test_by_default(kb_client, session_factory):
    headers, org_id = await _setup(kb_client)
    long = "x" * 300
    bot = await _conversation(session_factory, org_id, at=T0, texts=("first", long), external_id="u1")
    pending = await _conversation(session_factory, org_id, status="open", at=T0 + timedelta(minutes=1))
    test = await _conversation(session_factory, org_id, is_test=True, at=T0 + timedelta(minutes=2))
    base = f"/orgs/{org_id}/conversations"

    r = await kb_client.get(base, headers=headers)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["id"] for i in items] == [str(pending), str(bot)]  # newest first, test excluded
    assert items[1]["last_message_preview"] == "x" * 120
    assert items[1]["customer"]["identified"] is True and items[0]["customer"]["identified"] is False
    assert set(items[0]) == {"id", "status", "channel", "customer", "last_message_preview", "escalation",
                             "csat_score", "last_message_at", "is_test"}
    assert set(items[0]["customer"]) == {"id", "name", "email", "identified"}

    r = await kb_client.get(base, params={"status": "open"}, headers=headers)
    assert [i["id"] for i in r.json()["items"]] == [str(pending)]
    r = await kb_client.get(base, params={"include_test": "true"}, headers=headers)
    assert str(test) in [i["id"] for i in r.json()["items"]]
    assert (await kb_client.get(base, params={"status": "nope"}, headers=headers)).status_code == 422

    page1 = (await kb_client.get(base, params={"limit": 1, "include_test": "true"}, headers=headers)).json()
    assert [i["id"] for i in page1["items"]] == [str(test)] and page1["next_cursor"]
    page2 = (await kb_client.get(base, params={"limit": 5, "include_test": "true", "cursor": page1["next_cursor"]},
                                 headers=headers)).json()
    assert [i["id"] for i in page2["items"]] == [str(pending), str(bot)] and page2["next_cursor"] is None


async def test_detail_includes_turn_trace_with_rule(kb_client, session_factory):
    headers, org_id = await _setup(kb_client)
    kb = await create_kb(kb_client, org_id, headers)
    conv_id = await _conversation(session_factory, org_id, texts=("refund please",))
    async with session_factory() as s:
        doc = Document(organisation_id=UUID(org_id), knowledge_base_id=UUID(kb["id"]), filename="refunds.md",
                       file_type="md", size_bytes=1, sha256="a" * 64, storage_key="k")
        s.add(doc)
        await s.flush()
        chunk = Chunk(organisation_id=UUID(org_id), knowledge_base_id=UUID(kb["id"]), document_id=doc.id,
                      kb_version=1, ordinal=3, text="t", char_start=0, char_end=1)
        s.add(chunk)
        await s.flush()
        gone = uuid4()
        msg = (await s.execute(Message.__table__.select().where(Message.conversation_id == conv_id))).first()
        s.add(BotTurn(
            organisation_id=UUID(org_id), conversation_id=conv_id, message_id=msg.id, outcome="escalated",
            risk={"action": "escalate", "rule_key": "refunds", "rule_name": "Refunds", "reason": "r", "source": "topic"},
            retrieval={"chunks": [
                {"chunk_id": str(chunk.id), "kb_id": kb["id"], "score": 0.9, "confidence": 0.8, "rerank_score": 0.7},
                {"chunk_id": str(gone), "kb_id": kb["id"], "score": 0.5, "confidence": 0.4, "rerank_score": None},
            ], "warnings": [], "timings_ms": {}},
            tool_calls=[{"tool_id": "t", "name": "n", "status": "ok", "latency_ms": 5}],
            model={"provider": "fake", "model": "m"}, latency_ms=12, trace_id="abc",
        ))
        await s.commit()

    r = await kb_client.get(f"/orgs/{org_id}/conversations/{conv_id}", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [m["text"] for m in body["messages"]] == ["refund please"]
    (turn,) = body["turns"]
    assert turn["risk"]["rule_key"] == "refunds" and turn["outcome"] == "escalated"
    assert turn["latency_ms"] == 12 and turn["trace_id"] == "abc" and turn["tool_calls"][0]["name"] == "n"
    assert turn["message_id"] == str(msg.id)
    hydrated, missing = turn["chunks"]
    assert hydrated["document_name"] == "refunds.md" and hydrated["ordinal"] == 3
    assert (hydrated["score"], hydrated["confidence"], hydrated["rerank_score"]) == (0.9, 0.8, 0.7)
    assert missing["document_name"] is None and missing["ordinal"] is None and missing["chunk_id"] == str(gone)


async def test_cross_tenant_conversation_404(kb_client, session_factory):
    headers, org_id = await _setup(kb_client)
    other = await signup(kb_client, "other@x.com")
    other_org = await create_org(kb_client, other, "Other")
    conv_id = await _conversation(session_factory, other_org)
    assert (await kb_client.get(f"/orgs/{org_id}/conversations/{conv_id}", headers=headers)).status_code == 404
    assert (await kb_client.get(f"/orgs/{org_id}/conversations/{uuid4()}", headers=headers)).status_code == 404
    listed = (await kb_client.get(f"/orgs/{org_id}/conversations", headers=headers)).json()["items"]
    assert listed == []


async def test_viewer_can_read_agent_can_read_no_perm_403(kb_client, session_factory):
    headers, org_id = await _setup(kb_client)
    conv_id = await _conversation(session_factory, org_id)
    base = f"/orgs/{org_id}/conversations"
    for role in ("Viewer", "Agent"):
        member = await join_org(kb_client, session_factory, UUID(org_id), f"{role.lower()}@x.com", role_name=role)
        assert (await kb_client.get(base, headers=member)).status_code == 200
        assert (await kb_client.get(f"{base}/{conv_id}", headers=member)).status_code == 200
    role = await kb_client.post(f"/orgs/{org_id}/roles", json={"name": "KB", "permissions": ["kb.manage"]},
                                headers=headers)
    member = await join_org(kb_client, session_factory, UUID(org_id), "kb@x.com", role_id=UUID(role.json()["id"]))
    assert (await kb_client.get(base, headers=member)).status_code == 403
    assert (await kb_client.get(f"{base}/{conv_id}", headers=member)).status_code == 403
