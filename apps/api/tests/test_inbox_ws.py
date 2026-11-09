"""The inbox WebSocket (/inbox/v1/ws) the agent dashboard keeps open.

Same harness as test_widget_ws: Starlette's TestClient runs the app on one loop in a background thread,
data is set up through the HTTP API (real commits; the session_factory fixture truncates), adapters are
in memory and the bot uses the scripted chat model.
"""

import asyncio
import time
import uuid
from contextlib import asynccontextmanager

import pytest
import structlog
from sqlalchemy import delete, select, update
from starlette.testclient import TestClient

from supportapi.main import create_app
from supportapi.routers import inbox_ws
from supportapi.websocket import JsonSocket
from supportcore.config import Settings
from supportcore.db.models import Membership, Role, User
from supportcore.inbox.events import inbox_channel
from supportcore.testing import TEST_DATABASE_URL, script_chat
from test_widget_ws import PASSWORD, WEB_ORIGIN, _customer, _db, _owner, _post, closed_with, recv, recv_until

SHOP = "https://shop.example.com"


@pytest.fixture
def inbox_settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        env="test",
        database_url=TEST_DATABASE_URL,
        redis_url="redis://localhost:6379/0",
        jwt_secret="test-secret-test-secret-test-secret-123",
        smtp_from="noreply@test.local",
        web_origin=WEB_ORIGIN,
        file_storage="local",
        local_storage_path=str(tmp_path / "uploads"),
        vector_store="chroma",
        job_queue="inline",
        rate_limiter="memory",
        pubsub="memory",
        checkpointer="memory",
    )


@pytest.fixture
def client(inbox_settings, db_engine, session_factory):
    script_chat([])
    app = create_app(inbox_settings)
    with TestClient(app, base_url="https://testserver") as c:
        yield c
    script_chat([])


def _token(headers: dict) -> str:
    return headers["Authorization"].split()[1]


def _member(client, org_id: str, *, role_name: str | None = None, permissions: list[str] | None = None) -> tuple[dict, str]:
    """A new user in the org with a system role (by name) or a new custom role; (headers, membership id)."""
    email = f"member-{uuid.uuid4().hex[:8]}@x.com"
    signup = _post(client, "/auth/signup", {"email": email, "password": PASSWORD, "name": "Member"})
    headers = {"Authorization": f"Bearer {signup['access_token']}"}

    async def add(s):
        org = uuid.UUID(org_id)
        if role_name is not None:
            role_id = (await s.execute(select(Role.id).where(Role.organisation_id == org, Role.name == role_name))).scalar_one()
        else:
            role = Role(organisation_id=org, name=f"custom-{uuid.uuid4().hex[:6]}", permissions=permissions or [], is_system=False)
            s.add(role)
            await s.flush()
            role_id = role.id
        user = (await s.execute(select(User).where(User.email == email))).scalar_one()
        membership = Membership(organisation_id=org, user_id=user.id, role_id=role_id)
        s.add(membership)
        await s.flush()
        return str(membership.id)

    return headers, _db(client, add, commit=True)


def _owner_membership(client, org_id: str) -> str:
    async def find(s):
        return str(
            (await s.execute(select(Membership.id).where(Membership.organisation_id == uuid.UUID(org_id)))).scalars().first()
        )

    return _db(client, find)


def _connect(client, origin=WEB_ORIGIN):
    return client.websocket_connect("/inbox/v1/ws", headers={"origin": origin})


def _auth(ws, headers: dict, org_id: str) -> dict:
    ws.send_json({"type": "auth", "token": _token(headers), "org_id": org_id})
    ready = recv(ws)
    assert ready["type"] == "ready", ready
    return ready


def _watch(ws, conversation_id: str) -> None:
    ws.send_json({"type": "watch", "conversation_id": conversation_id})
    assert recv(ws) == {"type": "watching", "conversation_id": conversation_id}


def _pong(ws) -> list[dict]:
    """Sends a ping; every frame received before the pong."""
    ws.send_json({"type": "ping"})
    return recv_until(ws, "pong")[:-1]


def _present(client, org_id: str) -> set[str]:
    presence = client.app.state.container.adapters.presence
    return {str(m) for m in client.portal.call(presence.present, uuid.UUID(org_id))}


def _talk_to_human(client, customer_token: str, conversation_id: str) -> None:
    with client.websocket_connect(f"/widget/v1/conversations/{conversation_id}/ws", headers={"origin": WEB_ORIGIN}) as w:
        w.send_json({"type": "auth", "token": customer_token})
        assert recv(w)["type"] == "ready"
        w.send_json({"type": "talk_to_human"})
        recv_until(w, "handoff")


# --- connection and auth ---------------------------------------------------------------------------


def test_rejects_foreign_origin_4403(client):
    with _connect(client, origin=SHOP) as ws:
        assert closed_with(ws) == 4403


def test_requires_auth_frame_4401(client, monkeypatch):
    headers, org_id, _widget = _owner(client)
    with _connect(client) as ws:  # a first frame that is not auth
        ws.send_json({"type": "ping"})
        assert closed_with(ws) == 4401
    with _connect(client) as ws:  # an invalid token
        ws.send_json({"type": "auth", "token": "not-a-token", "org_id": org_id})
        assert closed_with(ws) == 4401
    with _connect(client) as ws:  # no org id
        ws.send_json({"type": "auth", "token": _token(headers)})
        assert closed_with(ws) == 4401
    monkeypatch.setattr(inbox_ws, "AUTH_TIMEOUT_SECONDS", 0.2)
    with _connect(client) as ws:  # nothing within the timeout
        assert closed_with(ws) == 4401


def test_widget_token_rejected_4401(client):
    _headers, org_id, widget = _owner(client)
    customer_token, _conversation_id = _customer(client, widget["public_key"])
    with _connect(client) as ws:
        ws.send_json({"type": "auth", "token": customer_token, "org_id": org_id})
        assert closed_with(ws) == 4401


def test_no_inbox_permission_4403(client):
    _headers, org_id, _widget = _owner(client)
    caller, _ = _member(client, org_id, permissions=["calls.take"])
    with _connect(client) as ws:
        ws.send_json({"type": "auth", "token": _token(caller), "org_id": org_id})
        assert closed_with(ws) == 4403
    other_headers, _other_org, _ = _owner(client)  # not a member of org_id
    with _connect(client) as ws:
        ws.send_json({"type": "auth", "token": _token(other_headers), "org_id": org_id})
        assert closed_with(ws) == 4403


def test_ready_with_counts(client):
    headers, org_id, widget = _owner(client)
    token, conversation_id = _customer(client, widget["public_key"])
    _customer(client, widget["public_key"])
    _talk_to_human(client, token, conversation_id)  # one open, unassigned; one bot
    with _connect(client) as ws:
        ready = _auth(ws, headers, org_id)
    assert ready == {
        "type": "ready",
        "membership_id": _owner_membership(client, org_id),
        "counts": {"mine": 0, "unassigned": 1, "bot": 1, "callbacks": 0},
    }
    viewer, viewer_mid = _member(client, org_id, role_name="Viewer")  # analytics.view alone may watch the inbox
    with _connect(client) as ws:
        assert _auth(ws, viewer, org_id)["membership_id"] == viewer_mid


# --- relayed events --------------------------------------------------------------------------------


def test_receives_upsert_after_assignment(client):
    owner, org_id, widget = _owner(client)
    agent, agent_mid = _member(client, org_id, role_name="Agent")
    _token_c, conversation_id = _customer(client, widget["public_key"])
    with _connect(client) as ws:
        _auth(ws, agent, org_id)
        r = client.put(
            f"/orgs/{org_id}/inbox/conversations/{conversation_id}/assignee", json={"membership_id": agent_mid}, headers=owner
        )
        assert r.status_code == 200, r.text
        frames = []
        while not ({"conversation.upsert", "notification"} <= {f["type"] for f in frames}):
            frames.append(recv(ws))
        assert _pong(ws) == []  # one upsert, one notification
    upsert = next(f for f in frames if f["type"] == "conversation.upsert")
    assert upsert["conversation"]["id"] == conversation_id
    assert upsert["conversation"]["assignee"]["membership_id"] == agent_mid
    assert upsert["conversation"]["status"] == "open"
    notification = next(f for f in frames if f["type"] == "notification")
    assert notification["notification"]["kind"] == "assigned"


def test_note_added_relayed_without_text(client):
    owner, org_id, widget = _owner(client)
    _token_c, conversation_id = _customer(client, widget["public_key"])
    with _connect(client) as ws:
        _auth(ws, owner, org_id)
        r = client.post(
            f"/orgs/{org_id}/inbox/conversations/{conversation_id}/notes", json={"text": "SECRET-NOTE-77"}, headers=owner
        )
        assert r.status_code == 201, r.text
        frames = recv_until(ws, "note.added")
    assert frames[-1] == {"type": "note.added", "conversation_id": conversation_id}
    assert all("SECRET-NOTE-77" not in str(f) for f in frames)


def test_email_status_relayed_to_inbox_viewers(client):
    """A delivery-status change (published by the outbound worker on the org's inbox channel) reaches every
    inbox viewer live, without a watch: the pane updates "Queued" -> "Sent" / "Failed" with no reload."""
    _owner_h, org_id, _widget = _owner(client)
    viewer, _viewer_mid = _member(client, org_id, role_name="Viewer")
    conversation_id, message_id = str(uuid.uuid4()), str(uuid.uuid4())
    event = {
        "type": "email.status",
        "conversation_id": conversation_id,
        "message_id": message_id,
        "status": "failed",
        "error_type": "auth_failed",
    }
    pubsub = client.app.state.container.adapters.pubsub
    with _connect(client) as ws:
        _auth(ws, viewer, org_id)
        client.portal.call(pubsub.publish, inbox_channel(uuid.UUID(org_id)), event)
        frames = recv_until(ws, "email.status")
    assert frames[-1] == event


def test_watch_streams_customer_message_and_bot_tokens(client):
    owner, org_id, widget = _owner(client)
    customer_token, conversation_id = _customer(client, widget["public_key"])
    script_chat(["Your order ships tomorrow"])
    with _connect(client) as ws:
        _auth(ws, owner, org_id)
        _watch(ws, conversation_id)
        with client.websocket_connect(
            f"/widget/v1/conversations/{conversation_id}/ws", headers={"origin": WEB_ORIGIN}
        ) as widget_socket:
            widget_socket.send_json({"type": "auth", "token": customer_token})
            assert recv(widget_socket)["type"] == "ready"
            widget_socket.send_json({"type": "message", "text": "where is my order?", "client_id": "c-1"})
            recv_until(widget_socket, "bot_done")
        frames = [f for f in recv_until(ws, "bot_done") if f["type"] != "conversation.upsert"]

    assert all(f["conversation_id"] == conversation_id for f in frames)
    messages = [f["message"] for f in frames if f["type"] == "message"]
    assert [(m["role"], m["text"]) for m in messages] == [
        ("customer", "where is my order?"),
        ("bot", "Your order ships tomorrow"),
    ]
    assert "".join(f["delta"] for f in frames if f["type"] == "bot_token") == "Your order ships tomorrow"


def test_unwatch_and_rewatch_replace_the_subscription(client):
    owner, org_id, widget = _owner(client)
    _t1, first = _customer(client, widget["public_key"])
    t2, second = _customer(client, widget["public_key"])
    pubsub = client.app.state.container.adapters.pubsub
    from supportcore.pubsub.ports import conversation_channel

    def listeners(cid):
        return pubsub.listener_count(conversation_channel(uuid.UUID(cid)))

    with _connect(client) as ws:
        _auth(ws, owner, org_id)
        _watch(ws, first)
        assert listeners(first) == 1
        _watch(ws, second)  # replaces the first watch
        assert (listeners(first), listeners(second)) == (0, 1)
        ws.send_json({"type": "unwatch"})
        assert _pong(ws) == []
        assert listeners(second) == 0
        _talk_to_human(client, t2, second)  # no longer watched: only the upsert arrives
        frames = recv_until(ws, "conversation.upsert")
        assert [f["type"] for f in frames] == ["conversation.upsert"]
        ws.send_json({"type": "watch", "conversation_id": "not-a-uuid"})
        assert recv(ws) == {"type": "error", "code": "invalid_frame"}


def test_watch_other_org_conversation_not_found(client):
    owner, org_id, _widget = _owner(client)
    _other_owner, _other_org, other_widget = _owner(client)
    _token_c, foreign = _customer(client, other_widget["public_key"])
    with _connect(client) as ws:
        _auth(ws, owner, org_id)
        ws.send_json({"type": "watch", "conversation_id": foreign})
        assert recv(ws) == {"type": "error", "code": "not_found"}
        ws.send_json({"type": "watch", "conversation_id": str(uuid.uuid4())})
        assert recv(ws) == {"type": "error", "code": "not_found"}
        assert _pong(ws) == []  # the socket stays open


def test_inbox_socket_org_isolation(client):
    owner_a, org_a, _widget_a = _owner(client)
    owner_b, org_b, widget_b = _owner(client)
    token_b, conversation_b = _customer(client, widget_b["public_key"])
    with _connect(client) as ws_a, _connect(client) as ws_b:
        _auth(ws_a, owner_a, org_a)
        _auth(ws_b, owner_b, org_b)
        _talk_to_human(client, token_b, conversation_b)
        upsert = recv_until(ws_b, "conversation.upsert")[-1]
        assert upsert["conversation"]["id"] == conversation_b
        assert _pong(ws_a) == []  # org A saw nothing of org B
    with _connect(client) as ws:  # nor can org A's owner open org B's inbox
        ws.send_json({"type": "auth", "token": _token(owner_a), "org_id": org_b})
        assert closed_with(ws) == 4403


def test_two_agents_see_messages_once(client):
    owner, org_id, widget = _owner(client)
    agent, _ = _member(client, org_id, role_name="Agent")
    customer_token, conversation_id = _customer(client, widget["public_key"])
    _talk_to_human(client, customer_token, conversation_id)  # with the team: no bot turn
    with _connect(client) as first, _connect(client) as second:
        _auth(first, owner, org_id)
        _auth(second, agent, org_id)
        for ws in (first, second):
            _pong(ws)  # drain the talk_to_human upsert
            _watch(ws, conversation_id)
        r = client.post(
            f"/orgs/{org_id}/inbox/conversations/{conversation_id}/messages", json={"text": "Hello from the team"}, headers=owner
        )
        assert r.status_code == 201, r.text
        for ws in (first, second):
            frames = recv_until(ws, "message")
            frames += _pong(ws)
            assert [f["message"]["text"] for f in frames if f["type"] == "message"] == ["Hello from the team"]


# --- presence and limits ---------------------------------------------------------------------------


def _wait_until(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_ping_refreshes_presence(client):
    _owner_headers, org_id, _widget = _owner(client)
    agent, agent_mid = _member(client, org_id, role_name="Agent")
    viewer, _ = _member(client, org_id, role_name="Viewer")
    presence = client.app.state.container.adapters.presence
    clock = [1_000_000.0]
    presence._now = lambda: clock[0]
    key = (uuid.UUID(org_id), uuid.UUID(agent_mid))
    with _connect(client) as ws:
        _auth(ws, agent, org_id)  # beats on auth
        assert _present(client, org_id) == {agent_mid}
        clock[0] += 50
        assert _pong(ws) == []  # and on every ping
        clock[0] += 50
        assert _present(client, org_id) == {agent_mid}
        clock[0] += 61
        assert _present(client, org_id) == set()  # no ping for over the TTL
    with _connect(client) as tab, _connect(client) as other_tab:
        _auth(tab, agent, org_id)
        _auth(other_tab, agent, org_id)
        assert inbox_ws._presence_sockets[key] == 2
        tab.close()
        _wait_until(lambda: inbox_ws._presence_sockets[key] == 1)  # the server has processed the close
        assert _present(client, org_id) == {agent_mid}  # another socket of the membership is still open
    _wait_until(lambda: key not in inbox_ws._presence_sockets)
    assert _present(client, org_id) == set()  # the membership's last socket closed: cleared
    with _connect(client) as ws:
        _auth(ws, viewer, org_id)  # cannot be assigned: no presence
        _pong(ws)
        assert _present(client, org_id) == set()


def _count_beats(client, monkeypatch) -> list:
    presence = client.app.state.container.adapters.presence
    beats = []
    original = presence.beat

    async def beat(*args, **kwargs):
        beats.append(args)
        await original(*args, **kwargs)

    monkeypatch.setattr(presence, "beat", beat)
    return beats


def test_removed_member_ping_closes_4403(client, monkeypatch):
    _owner_headers, org_id, _widget = _owner(client)
    agent, agent_mid = _member(client, org_id, role_name="Agent")
    beats = _count_beats(client, monkeypatch)
    with _connect(client) as ws:
        _auth(ws, agent, org_id)
        assert len(beats) == 1

        async def remove(s):
            await s.execute(delete(Membership).where(Membership.id == uuid.UUID(agent_mid)))

        _db(client, remove, commit=True)
        ws.send_json({"type": "ping"})
        assert closed_with(ws) == 4403
    assert len(beats) == 1  # not beaten after removal
    assert _present(client, org_id) == set()


def test_deactivated_user_ping_closes_4401(client, monkeypatch):
    _owner_headers, org_id, _widget = _owner(client)
    agent, agent_mid = _member(client, org_id, role_name="Agent")
    beats = _count_beats(client, monkeypatch)
    with _connect(client) as ws:
        _auth(ws, agent, org_id)
        assert len(beats) == 1

        async def deactivate(s):
            user_id = (await s.execute(select(Membership.user_id).where(Membership.id == uuid.UUID(agent_mid)))).scalar_one()
            await s.execute(update(User).where(User.id == user_id).values(is_active=False))

        _db(client, deactivate, commit=True)
        ws.send_json({"type": "ping"})
        assert closed_with(ws) == 4401
    assert len(beats) == 1
    assert _present(client, org_id) == set()


def test_frame_budget_1008(client, monkeypatch):
    monkeypatch.setattr(inbox_ws, "FRAME_BUDGET", (5, 60))
    headers, org_id, _widget = _owner(client)
    with _connect(client) as ws:
        _auth(ws, headers, org_id)
        for _ in range(5):
            assert _pong(ws) == []
        ws.send_text("garbage")  # every frame counts, valid or not
        assert closed_with(ws) == 1008


def test_oversize_frame_closes_1009(client):
    headers, org_id, _widget = _owner(client)
    with _connect(client) as ws:
        _auth(ws, headers, org_id)
        ws.send_text('{"type": "ping", "pad": "' + "x" * 16400 + '"}')
        assert closed_with(ws) == 1009


def test_connects_per_ip_limited_1013(client, monkeypatch):
    monkeypatch.setattr(inbox_ws, "CONNECTS_PER_IP", (2, 60))
    for _ in range(2):
        with _connect(client) as ws:
            ws.send_json({"type": "ping"})
            assert closed_with(ws) == 4401
    with _connect(client) as ws:
        assert closed_with(ws) == 1013


def test_relay_failure_closes_1011(client):
    from test_widget_ws import _BrokenPubSub

    headers, org_id, _widget = _owner(client)
    adapters = client.app.state.container.adapters
    original = adapters.pubsub
    adapters.pubsub = _BrokenPubSub()
    try:
        with _connect(client) as ws:
            _auth(ws, headers, org_id)
            assert closed_with(ws) == 1011
    finally:
        adapters.pubsub = original
    with _connect(client) as ws:  # the app is fine
        _auth(ws, headers, org_id)
        assert _pong(ws) == []


class _EndingPubSub:
    """Every subscription ends cleanly at once (e.g. Redis closed the connection on shutdown)."""

    async def publish(self, channel, message):
        return None

    def subscribe(self, channel):
        async def nothing():
            return
            yield

        @asynccontextmanager
        async def cm():
            yield nothing()

        return cm()


def test_relay_clean_end_closes_1012(client):
    headers, org_id, _widget = _owner(client)
    adapters = client.app.state.container.adapters
    original = adapters.pubsub
    adapters.pubsub = _EndingPubSub()
    try:
        with _connect(client) as ws:
            _auth(ws, headers, org_id)
            assert closed_with(ws) == 1012
    finally:
        adapters.pubsub = original


class _FailOnSignalPubSub:
    """The inbox channel's subscription fails once `failed` is set; other channels are the real pub/sub."""

    def __init__(self, inner):
        self.inner = inner
        self.fail = asyncio.Event()

    async def publish(self, channel, message):
        await self.inner.publish(channel, message)

    def subscribe(self, channel):
        if not channel.startswith("inbox:"):
            return self.inner.subscribe(channel)
        fail = self.fail

        async def failing():
            await fail.wait()
            raise ConnectionError("lost")
            yield

        @asynccontextmanager
        async def cm():
            yield failing()

        return cm()


def test_send_after_relay_closed_is_not_logged_as_failure(client, monkeypatch):
    """The relay closes 1011 while a ping is being handled: the pong then cannot be sent. That is the
    socket being closed, not a second failure."""
    headers, org_id, _widget = _owner(client)
    adapters = client.app.state.container.adapters
    original = adapters.pubsub
    failing = _FailOnSignalPubSub(original)
    real_beat = adapters.presence.beat
    calls = []

    async def beat(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:  # the ping's beat: the relay fails and closes the socket meanwhile
            failing.fail.set()
            await asyncio.sleep(0.1)
        await real_beat(*args, **kwargs)

    real_close = JsonSocket.close

    async def slow_close(self, code):  # the relay is still finishing its close when the pong is sent
        await real_close(self, code)
        if code == 1011:
            await asyncio.sleep(0.4)

    monkeypatch.setattr(adapters.presence, "beat", beat)
    monkeypatch.setattr(JsonSocket, "close", slow_close)
    adapters.pubsub = failing
    try:
        with structlog.testing.capture_logs() as logs:
            with _connect(client) as ws:
                _auth(ws, headers, org_id)
                ws.send_json({"type": "ping"})
                assert closed_with(ws) == 1011
                time.sleep(0.3)  # the server is still handling the ping (keep the session open meanwhile)
        events = [entry.get("event") for entry in logs]
        assert "inbox_ws_relay_failed" in events
        assert "inbox_ws_failed" not in events
    finally:
        adapters.pubsub = original
