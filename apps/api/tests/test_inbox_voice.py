"""Call recording through the inbox API (Phase 7a, Task 10): authenticated streaming with Range support.
Real commits (raw_client)."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from kb_helpers import create_org, join_org, signup
from supportcore.db.models import Call, CallRecording, CallTransfer, Conversation, Customer, Membership, Role

DATA = bytes(range(256)) * 4  # 1024 bytes


@pytest.fixture(autouse=True)
def _truncate(session_factory):
    """Real-commit tests: session_factory truncates every table at teardown."""


async def _org(client, email="owner@x.com", name="Acme"):
    owner = await signup(client, email)
    return await create_org(client, owner, name), owner


async def _call_conversation(
    session_factory, org_id, *, recording=True, key="orgs/x/calls/y/z.mp3", source="phone", callback=None,
    phone=None, status="open", is_test=False,
):
    org = UUID(org_id)
    async with session_factory() as s:
        customer = Customer(organisation_id=org, phone=phone)
        s.add(customer)
        await s.flush()
        conv = Conversation(
            organisation_id=org, customer_id=customer.id, status=status, channel="voice", is_test=is_test,
            callback_status=callback, callback_phone=phone if callback else None,
        )
        s.add(conv)
        await s.flush()
        call = Call(
            organisation_id=org, conversation_id=conv.id, source=source, provider_call_sid="CA" + uuid4().hex,
            status="completed", end_reason="handoff_callback" if callback else "caller_hangup", duration_seconds=42,
        )
        s.add(call)
        await s.flush()
        if recording:
            s.add(
                CallRecording(
                    organisation_id=org, call_id=call.id, storage_key=key, size_bytes=len(DATA),
                    duration_seconds=5, delete_after=datetime.now(UTC) + timedelta(days=1),
                )
            )
        await s.commit()
        return conv.id


async def _put(app, key, data=DATA):
    await app.state.container.adapters.storage.put(key, data, "audio/mpeg")


def _url(org_id, cid):
    return f"/orgs/{org_id}/inbox/conversations/{cid}/recording"


async def test_recording_route_streams_and_supports_range(raw_client, session_factory, app):
    org_id, owner = await _org(raw_client)
    cid = await _call_conversation(session_factory, org_id)
    await _put(app, "orgs/x/calls/y/z.mp3")

    full = await raw_client.get(_url(org_id, cid), headers=owner)
    assert full.status_code == 200 and full.content == DATA
    assert full.headers["content-type"] == "audio/mpeg"
    assert full.headers["cache-control"] == "private, no-store"
    assert full.headers["content-disposition"] == "inline"
    assert full.headers["accept-ranges"] == "bytes"

    part = await raw_client.get(_url(org_id, cid), headers={**owner, "Range": "bytes=10-19"})
    assert part.status_code == 206 and part.content == DATA[10:20]
    assert part.headers["content-range"] == f"bytes 10-19/{len(DATA)}"
    assert part.headers["content-length"] == "10"
    assert part.headers["content-type"] == "audio/mpeg"

    open_end = await raw_client.get(_url(org_id, cid), headers={**owner, "Range": "bytes=1000-"})
    assert open_end.status_code == 206 and open_end.content == DATA[1000:]
    assert open_end.headers["content-range"] == f"bytes 1000-1023/{len(DATA)}"

    suffix = await raw_client.get(_url(org_id, cid), headers={**owner, "Range": "bytes=-24"})
    assert suffix.status_code == 206 and suffix.content == DATA[-24:]

    clamped = await raw_client.get(_url(org_id, cid), headers={**owner, "Range": "bytes=1000-5000"})
    assert clamped.status_code == 206 and clamped.content == DATA[1000:]

    beyond = await raw_client.get(_url(org_id, cid), headers={**owner, "Range": "bytes=2000-"})
    assert beyond.status_code == 416 and beyond.headers["content-range"] == f"bytes */{len(DATA)}"

    # A malformed or multi-range header is ignored: the whole object.
    for ignored in ("bytes=0-1,5-6", "bytes=abc", "bytes=-", "bytes=5-2", "bytes=" + "9" * 5000 + "-"):
        r = await raw_client.get(_url(org_id, cid), headers={**owner, "Range": ignored})
        assert r.status_code == 200 and r.content == DATA, ignored
    latin = await raw_client.get(_url(org_id, cid), headers={**owner, "Range": "bytes=\xb2-3".encode("latin-1")})
    assert latin.status_code == 200 and latin.content == DATA  # a non-ASCII digit must not 500
    zero = await raw_client.get(_url(org_id, cid), headers={**owner, "Range": "bytes=-0"})
    assert zero.status_code == 416

    head = await raw_client.head(_url(org_id, cid), headers=owner)
    assert head.status_code == 200 and head.content == b"" and head.headers["content-length"] == str(len(DATA))
    assert head.headers["content-type"] == "audio/mpeg"
    head_range = await raw_client.head(_url(org_id, cid), headers={**owner, "Range": "bytes=10-19"})
    assert head_range.status_code == 206 and head_range.content == b"" and head_range.headers["content-length"] == "10"


async def test_recording_route_streams_large_object_in_chunks(raw_client, session_factory, app):
    org_id, owner = await _org(raw_client)
    key = f"orgs/x/calls/y/{uuid4().hex}.mp3"
    cid = await _call_conversation(session_factory, org_id, key=key)
    big = b"abcdefgh" * (300 * 1024)  # 2.4 MiB: more than one chunk
    await _put(app, key, big)
    storage = app.state.container.adapters.storage
    sizes: list[int] = []
    original = storage.get_range

    async def spy(k, start, end):
        sizes.append(end - start + 1)
        return await original(k, start, end)

    storage.get_range = spy
    try:
        r = await raw_client.get(_url(org_id, cid), headers=owner)
    finally:
        del storage.get_range
    assert r.status_code == 200 and r.content == big
    assert len(sizes) >= 3 and max(sizes) <= 1024 * 1024


async def test_recording_route_404_without_recording_or_object(raw_client, session_factory):
    org_id, owner = await _org(raw_client)
    none = await _call_conversation(session_factory, org_id, recording=False)
    assert (await raw_client.get(_url(org_id, none), headers=owner)).status_code == 404
    missing = await _call_conversation(session_factory, org_id, key=f"orgs/x/calls/y/{uuid4().hex}.mp3")  # row, but the object was never stored
    assert (await raw_client.get(_url(org_id, missing), headers=owner)).status_code == 404


async def test_recording_route_cross_org_404(raw_client, session_factory, app):
    org_id, _ = await _org(raw_client)
    cid = await _call_conversation(session_factory, org_id)
    await _put(app, "orgs/x/calls/y/z.mp3")
    other_org, other_owner = await _org(raw_client, "owner2@x.com", "Other")
    assert (await raw_client.get(_url(other_org, cid), headers=other_owner)).status_code == 404
    assert (await raw_client.get(_url(org_id, cid), headers=other_owner)).status_code == 404


async def test_recording_route_requires_inbox_view(raw_client, session_factory, app):
    org_id, _ = await _org(raw_client)
    cid = await _call_conversation(session_factory, org_id)
    await _put(app, "orgs/x/calls/y/z.mp3")
    async with session_factory() as s:
        role = Role(organisation_id=UUID(org_id), name="nothing", permissions=["kb.manage"], is_system=False)
        s.add(role)
        await s.commit()
        role_id = role.id
    nobody = await join_org(raw_client, session_factory, UUID(org_id), "nobody@x.com", role_id=role_id)
    assert (await raw_client.get(_url(org_id, cid), headers=nobody)).status_code == 403
    viewer = await join_org(raw_client, session_factory, UUID(org_id), "viewer@x.com", role_name="Viewer")
    assert (await raw_client.get(_url(org_id, cid), headers=viewer)).status_code == 200
    assert (await raw_client.get(_url(org_id, cid))).status_code == 401


# --- call meta, callbacks and replies (Task 11) ---------------------------------------------------------------------


def _conv(org_id, cid):
    return f"/orgs/{org_id}/inbox/conversations/{cid}"


async def test_voice_summary_has_call_meta(raw_client, session_factory):
    org_id, owner = await _org(raw_client)
    cid = await _call_conversation(session_factory, org_id, callback="requested", phone="+14155550123")
    plain = await _call_conversation(session_factory, org_id, recording=False, source="browser")
    listed = (await raw_client.get(f"/orgs/{org_id}/inbox/conversations", headers=owner)).json()["items"]
    by_id = {UUID(i["id"]): i for i in listed}
    phone = by_id[cid]
    assert phone["callback_status"] == "requested" and phone["callback_phone"] == "+14155550123"
    assert phone["customer"]["phone"] == "+14155550123"
    call = phone["call"]
    assert set(call) == {
        "id", "source", "mode", "status", "end_reason", "started_at", "duration_seconds", "has_recording"
    }
    assert (call["source"], call["status"], call["end_reason"]) == ("phone", "completed", "handoff_callback")
    assert call["duration_seconds"] == 42 and call["has_recording"] is True
    browser = by_id[plain]
    assert browser["call"]["source"] == "browser" and browser["call"]["has_recording"] is False
    assert browser["callback_status"] is None and browser["callback_phone"] is None
    detail = (await raw_client.get(_conv(org_id, cid), headers=owner)).json()["summary"]
    assert detail["call"] == call and detail["callback_phone"] == "+14155550123"


async def test_chat_summary_has_no_call(raw_client, session_factory):
    org_id, owner = await _org(raw_client)
    async with session_factory() as s:
        customer = Customer(organisation_id=UUID(org_id))
        s.add(customer)
        await s.flush()
        s.add(Conversation(organisation_id=UUID(org_id), customer_id=customer.id, status="open"))
        await s.commit()
    item = (await raw_client.get(f"/orgs/{org_id}/inbox/conversations", headers=owner)).json()["items"][0]
    assert item["call"] is None and item["callback_status"] is None and item["customer"]["phone"] is None


async def test_callback_filter_lists_requested_only(raw_client, session_factory):
    org_id, owner = await _org(raw_client)
    requested = await _call_conversation(session_factory, org_id, callback="requested", phone="+14155550123")
    await _call_conversation(session_factory, org_id, callback="done", phone="+14155550124")
    await _call_conversation(session_factory, org_id)
    base = f"/orgs/{org_id}/inbox"
    r = await raw_client.get(f"{base}/conversations?callback=requested", headers=owner)
    assert [UUID(i["id"]) for i in r.json()["items"]] == [requested]
    assert (await raw_client.get(f"{base}/conversations?callback=done", headers=owner)).status_code == 422
    assert (await raw_client.get(f"{base}/counts", headers=owner)).json()["callbacks"] == 1


async def test_callback_done_transitions_once(raw_client, session_factory):
    org_id, owner = await _org(raw_client)
    cid = await _call_conversation(session_factory, org_id, callback="requested", phone="+14155550123")
    before = (await raw_client.get(_conv(org_id, cid), headers=owner)).json()["summary"]["version"]
    r = await raw_client.post(f"{_conv(org_id, cid)}/callback/done", headers=owner)
    assert r.status_code == 200 and r.json() == {"id": str(cid), "callback_status": "done"}
    summary = (await raw_client.get(_conv(org_id, cid), headers=owner)).json()["summary"]
    assert summary["callback_status"] == "done" and summary["version"] > before
    again = await raw_client.post(f"{_conv(org_id, cid)}/callback/done", headers=owner)
    assert again.status_code == 409 and again.json()["code"] == "invalid_conversation_state"
    assert (await raw_client.get(f"/orgs/{org_id}/inbox/counts", headers=owner)).json()["callbacks"] == 0
    none = await _call_conversation(session_factory, org_id)
    assert (await raw_client.post(f"{_conv(org_id, none)}/callback/done", headers=owner)).status_code == 409


async def test_callback_done_publishes_conversation_changed(raw_client, session_factory, app, monkeypatch):
    org_id, owner = await _org(raw_client)
    cid = await _call_conversation(session_factory, org_id, callback="requested", phone="+14155550123")
    from supportcore.inbox.publisher import InboxPublisher

    seen = []
    original = InboxPublisher.conversation_changed
    monkeypatch.setattr(
        InboxPublisher, "conversation_changed", lambda self, *a, **k: (seen.append(a[-1]), original(self, *a, **k))[1]
    )
    assert (await raw_client.post(f"{_conv(org_id, cid)}/callback/done", headers=owner)).status_code == 200
    assert seen == [cid]


async def test_callback_done_requires_queries_answer(raw_client, session_factory):
    org_id, owner = await _org(raw_client)
    cid = await _call_conversation(session_factory, org_id, callback="requested", phone="+14155550123")
    viewer = await join_org(raw_client, session_factory, UUID(org_id), "viewer@x.com", role_name="Viewer")
    assert (await raw_client.post(f"{_conv(org_id, cid)}/callback/done", headers=viewer)).status_code == 403
    assert (await raw_client.post(f"{_conv(org_id, cid)}/callback/done")).status_code == 401
    agent = await join_org(raw_client, session_factory, UUID(org_id), "agent@x.com", role_name="Agent")
    assert (await raw_client.post(f"{_conv(org_id, cid)}/callback/done", headers=agent)).status_code == 200


async def test_phone_voice_reply_refused(raw_client, session_factory):
    org_id, owner = await _org(raw_client)
    cid = await _call_conversation(session_factory, org_id, callback="requested", phone="+14155550123")
    r = await raw_client.post(f"{_conv(org_id, cid)}/messages", json={"text": "hello"}, headers=owner)
    assert r.status_code == 409 and r.json()["code"] == "voice_not_available"
    assert "call them back" in r.json()["detail"]
    notes = await raw_client.post(f"{_conv(org_id, cid)}/notes", json={"text": "tried"}, headers=owner)
    assert notes.status_code == 201  # notes still work


async def test_browser_voice_reply_allowed(raw_client, session_factory):
    org_id, owner = await _org(raw_client)
    cid = await _call_conversation(session_factory, org_id, source="browser", recording=False)
    r = await raw_client.post(f"{_conv(org_id, cid)}/messages", json={"text": "hello"}, headers=owner)
    assert r.status_code == 201, r.text


async def test_cross_org_voice_conversation_404(raw_client, session_factory):
    org_id, _ = await _org(raw_client)
    cid = await _call_conversation(session_factory, org_id, callback="requested", phone="+14155550123")
    other_org, other_owner = await _org(raw_client, "owner2@x.com", "Other")
    for method, url in (
        ("get", _conv(other_org, cid)),
        ("post", f"{_conv(other_org, cid)}/callback/done"),
        ("post", f"{_conv(other_org, cid)}/messages"),
    ):
        r = await getattr(raw_client, method)(url, headers=other_owner, **({"json": {"text": "x"}} if method == "post" else {}))
        assert r.status_code == 404, (url, r.text)
    assert (await raw_client.get(f"/orgs/{other_org}/inbox/conversations", headers=other_owner)).json()["items"] == []
    assert (await raw_client.get(f"/orgs/{other_org}/inbox/counts", headers=other_owner)).json()["callbacks"] == 0
    cross_done = await raw_client.post(f"{_conv(org_id, cid)}/callback/done", headers=other_owner)
    # The other owner is not a member of this org: the org itself is hidden (404, not 403).
    assert cross_done.status_code == 404


async def test_recording_route_serves_the_inbound_call_when_a_callback_call_exists(raw_client, session_factory, app):
    org_id, owner = await _org(raw_client)
    cid = await _call_conversation(session_factory, org_id)
    await _put(app, "orgs/x/calls/y/z.mp3")
    async with session_factory() as s:  # a callback call on the same conversation, with a recording of its own
        callback = Call(
            organisation_id=UUID(org_id), conversation_id=cid, source="callback", direction="outbound",
            provider_call_sid="CA" + uuid4().hex, status="completed", end_reason="callback",
        )
        s.add(callback)
        await s.flush()
        s.add(
            CallRecording(
                organisation_id=UUID(org_id), call_id=callback.id, storage_key="orgs/x/calls/cb.mp3",
                size_bytes=3, duration_seconds=1, delete_after=datetime.now(UTC) + timedelta(days=1),
            )
        )
        await s.commit()
    await _put(app, "orgs/x/calls/cb.mp3", b"abc")

    res = await raw_client.get(_url(org_id, cid), headers=owner)
    assert res.status_code == 200 and res.content == DATA


# --- calls list and transfer meta (Phase 7b, Task 12) ----------------------------------------------------------------


async def _add_callback_call(
    session_factory, org_id, cid, *, agent=None, started=None, recording_key=None, transfer_for=None
):
    async with session_factory() as s:
        call = Call(
            organisation_id=UUID(org_id), conversation_id=cid, source="callback", direction="outbound",
            provider_call_sid="CA" + uuid4().hex, status="completed", end_reason="callback", duration_seconds=7,
            agent_membership_id=agent, started_at=started or datetime.now(UTC) + timedelta(minutes=5),
        )
        s.add(call)
        await s.flush()
        if recording_key:
            s.add(
                CallRecording(
                    organisation_id=UUID(org_id), call_id=call.id, storage_key=recording_key, size_bytes=3,
                    duration_seconds=1, delete_after=datetime.now(UTC) + timedelta(days=1),
                )
            )
        await s.commit()
        return call.id


async def _membership_id(session_factory, org_id, index=0):
    async with session_factory() as s:
        from sqlalchemy import select

        rows = (await s.execute(select(Membership.id).where(Membership.organisation_id == UUID(org_id)).order_by(Membership.created_at))).scalars().all()
        return rows[index]


async def _inbound_call_id(session_factory, cid):
    async with session_factory() as s:
        from sqlalchemy import select

        return (await s.execute(select(Call.id).where(Call.conversation_id == cid, Call.direction == "inbound"))).scalar_one()


async def test_detail_lists_inbound_and_callback_calls(raw_client, session_factory):
    org_id, owner = await _org(raw_client)
    cid = await _call_conversation(session_factory, org_id)
    agent = await _membership_id(session_factory, org_id)
    callback_id = await _add_callback_call(session_factory, org_id, cid, agent=agent, recording_key="orgs/x/cb.mp3")
    inbound_id = await _inbound_call_id(session_factory, cid)

    body = (await raw_client.get(_conv(org_id, cid), headers=owner)).json()
    calls = body["calls"]
    assert [c["id"] for c in calls] == [str(inbound_id), str(callback_id)]  # ordered by started_at
    assert set(calls[0]) == {
        "id", "direction", "source", "mode", "status", "end_reason", "started_at", "duration_seconds",
        "has_recording", "agent_name", "transfer",
    }
    inbound, callback = calls
    assert (inbound["direction"], inbound["source"], inbound["has_recording"]) == ("inbound", "phone", True)
    assert inbound["agent_name"] is None and inbound["transfer"] is None
    assert (callback["direction"], callback["source"], callback["end_reason"]) == ("outbound", "callback", "callback")
    assert callback["has_recording"] is True and callback["duration_seconds"] == 7
    assert callback["agent_name"]  # the user's display name (signup defaults it)
    # The summary's call stays the inbound one.
    assert body["summary"]["call"]["id"] == str(inbound_id)
    # A chat conversation has no calls.
    async with session_factory() as s:
        customer = Customer(organisation_id=UUID(org_id))
        s.add(customer)
        await s.flush()
        chat = Conversation(organisation_id=UUID(org_id), customer_id=customer.id, status="open")
        s.add(chat)
        await s.commit()
        chat_id = chat.id
    assert (await raw_client.get(_conv(org_id, chat_id), headers=owner)).json()["calls"] == []


async def test_transfer_meta_on_call(raw_client, session_factory):
    org_id, owner = await _org(raw_client)
    cid = await _call_conversation(session_factory, org_id)
    agent = await _membership_id(session_factory, org_id)
    inbound_id = await _inbound_call_id(session_factory, cid)
    async with session_factory() as s:
        s.add(
            CallTransfer(
                organisation_id=UUID(org_id), call_id=inbound_id, conversation_id=cid, status="connected", attempt=2,
                connected_membership_id=agent, tried_membership_ids=[], deadline_at=datetime.now(UTC),
            )
        )
        await s.commit()
    calls = (await raw_client.get(_conv(org_id, cid), headers=owner)).json()["calls"]
    transfer = calls[0]["transfer"]
    assert set(transfer) == {"status", "attempts", "connected_agent_name"}
    assert (transfer["status"], transfer["attempts"]) == ("connected", 2)
    assert transfer["connected_agent_name"]

    other = await _call_conversation(session_factory, org_id)
    other_call = await _inbound_call_id(session_factory, other)
    async with session_factory() as s:
        s.add(
            CallTransfer(
                organisation_id=UUID(org_id), call_id=other_call, conversation_id=other, status="no_answer",
                attempt=3, tried_membership_ids=[], deadline_at=datetime.now(UTC),
            )
        )
        await s.commit()
    transfer = (await raw_client.get(_conv(org_id, other), headers=owner)).json()["calls"][0]["transfer"]
    assert transfer == {"status": "no_answer", "attempts": 3, "connected_agent_name": None}


async def test_recording_by_call_id(raw_client, session_factory, app):
    org_id, owner = await _org(raw_client)
    cid = await _call_conversation(session_factory, org_id)
    await _put(app, "orgs/x/calls/y/z.mp3")
    callback_id = await _add_callback_call(session_factory, org_id, cid, recording_key="orgs/x/calls/cb2.mp3")
    await _put(app, "orgs/x/calls/cb2.mp3", b"abc")
    inbound_id = await _inbound_call_id(session_factory, cid)

    by_callback = await raw_client.get(f"{_url(org_id, cid)}?call_id={callback_id}", headers=owner)
    assert by_callback.status_code == 200 and by_callback.content == b"abc"
    by_inbound = await raw_client.get(f"{_url(org_id, cid)}?call_id={inbound_id}", headers=owner)
    assert by_inbound.status_code == 200 and by_inbound.content == DATA
    default = await raw_client.get(_url(org_id, cid), headers=owner)
    assert default.status_code == 200 and default.content == DATA
    assert (await raw_client.get(f"{_url(org_id, cid)}?call_id=not-a-uuid", headers=owner)).status_code == 422


async def test_recording_call_from_other_conversation_404(raw_client, session_factory, app):
    org_id, owner = await _org(raw_client)
    cid = await _call_conversation(session_factory, org_id)
    await _put(app, "orgs/x/calls/y/z.mp3")
    other = await _call_conversation(session_factory, org_id, key="orgs/x/calls/o.mp3")
    await _put(app, "orgs/x/calls/o.mp3", b"zzz")
    other_call = await _inbound_call_id(session_factory, other)
    assert (await raw_client.get(f"{_url(org_id, cid)}?call_id={other_call}", headers=owner)).status_code == 404
    # A call of another organisation is hidden the same way.
    org2, owner2 = await _org(raw_client, "owner2@x.com", "Other")
    cid2 = await _call_conversation(session_factory, org2, key="orgs/x/calls/p.mp3")
    await _put(app, "orgs/x/calls/p.mp3", b"yyy")
    foreign = await _inbound_call_id(session_factory, cid2)
    assert (await raw_client.get(f"{_url(org_id, cid)}?call_id={foreign}", headers=owner)).status_code == 404
    assert (await raw_client.get(f"{_url(org_id, cid)}?call_id={uuid4()}", headers=owner)).status_code == 404


async def test_summary_call_is_inbound_one(raw_client, session_factory):
    org_id, owner = await _org(raw_client)
    cid = await _call_conversation(session_factory, org_id)
    await _add_callback_call(session_factory, org_id, cid, started=datetime.now(UTC) - timedelta(hours=1))
    inbound_id = await _inbound_call_id(session_factory, cid)
    item = (await raw_client.get(f"/orgs/{org_id}/inbox/conversations", headers=owner)).json()["items"][0]
    assert item["call"]["id"] == str(inbound_id) and item["call"]["mode"] is None
    assert "calls" not in item  # only the detail lists every call
