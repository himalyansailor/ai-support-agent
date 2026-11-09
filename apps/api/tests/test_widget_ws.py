"""The widget WebSocket (/widget/v1/conversations/{id}/ws), the bot turns it starts, and idle close.

Starlette's TestClient runs the app (with its lifespan) on one event loop in a background thread;
the tests are synchronous and set up their data through the HTTP API, so everything really commits
(the `session_factory` fixture truncates every table at teardown). Adapters are in memory: pub/sub,
rate limiter, checkpointer; jobs run inline; the bot uses the scripted chat model.
"""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import anyio
import pytest
import structlog
from sqlalchemy import select, update
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from supportapi.main import create_app
from supportapi.routers import widget_ws
from supportcore.config import Settings
from supportcore.conversations.jobs import close_idle_conversations
from supportcore.db.models import BotTurn, Conversation, Message, Widget
from supportcore.testing import TEST_DATABASE_URL, script_chat
from supportcore.widget.strings import strings_for

WEB_ORIGIN = "http://localhost:3000"
SHOP = "https://shop.example.com"
PASSWORD = "correct-horse-1"


@pytest.fixture
def ws_settings(tmp_path) -> Settings:
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
def client(ws_settings, db_engine, session_factory):
    script_chat([])
    app = create_app(ws_settings)
    with TestClient(app, base_url="https://testserver") as c:
        yield c
    script_chat([])


@dataclass
class Chat:
    org_id: str
    widget_key: str
    token: str
    conversation_id: str


def _post(client, path, body=None, headers=None, status=(200, 201)):
    r = client.post(path, json=body, headers=headers)
    assert r.status_code in status, r.text
    return r.json()


def _owner(client) -> tuple[dict, str, dict]:
    email = f"owner-{uuid.uuid4().hex[:8]}@x.com"
    signup = _post(client, "/auth/signup", {"email": email, "password": PASSWORD, "name": "Owner"})
    headers = {"Authorization": f"Bearer {signup['access_token']}"}
    org_id = _post(client, "/orgs", {"name": "Acme"}, headers)["id"]
    r = client.put(
        f"/orgs/{org_id}/ai/settings",
        json={"chat_provider": "scripted", "chat_model": "scripted-chat", "temperature": 0.2},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    widget = _post(client, f"/orgs/{org_id}/widgets", {"name": "Site"}, headers)
    r = client.patch(f"/orgs/{org_id}/widgets/{widget['id']}", json={"allowed_domains": ["shop.example.com"]}, headers=headers)
    assert r.status_code == 200, r.text
    return headers, org_id, widget


def _customer(client, widget_key: str) -> tuple[str, str]:
    """A new anonymous customer's token and conversation id."""
    session = _post(
        client, "/widget/v1/sessions", {"key": widget_key, "parent_origin": SHOP, "visitor_id": uuid.uuid4().hex}
    )
    token = session["token"]
    conversation = _post(client, "/widget/v1/conversations", headers={"Authorization": f"Bearer {token}"})
    # The new conversation's inbox upsert goes out after the response: let it go before a test subscribes.
    from supportcore.realtime.spawn import drain_pending

    client.portal.call(drain_pending)
    return token, conversation["id"]


def _chat(client) -> Chat:
    _headers, org_id, widget = _owner(client)
    token, conversation_id = _customer(client, widget["public_key"])
    return Chat(org_id, widget["public_key"], token, conversation_id)


def _connect(client, conversation_id, origin=WEB_ORIGIN):
    return client.websocket_connect(f"/widget/v1/conversations/{conversation_id}/ws", headers={"origin": origin})


async def _receive(stream, timeout):
    with anyio.fail_after(timeout):
        return await stream.receive()


def recv(ws, timeout: float = 10) -> dict:
    """The next frame as JSON; raises WebSocketDisconnect on close, TimeoutError after `timeout`."""
    message = ws.portal.call(_receive, ws._send_rx, timeout)
    ws._raise_on_close(message)
    return json.loads(message["text"])


def recv_until(ws, frame_type: str, timeout: float = 15) -> list[dict]:
    frames = []
    while True:
        frame = recv(ws, timeout)
        frames.append(frame)
        if frame["type"] == frame_type:
            return frames


def closed_with(ws) -> int:
    with pytest.raises(WebSocketDisconnect) as info:
        for _ in range(20):
            recv(ws, 10)
    return info.value.code


def _auth(ws, token) -> dict:
    ws.send_json({"type": "auth", "token": token})
    ready = recv(ws)
    assert ready["type"] == "ready", ready
    return ready


def _db(client, fn, *, commit=False):
    """Runs `fn(session)` on the app's loop with a session from the app's engine."""
    factory = client.app.state.session_factory

    async def run():
        async with factory() as s:
            result = await fn(s)
            if commit:
                await s.commit()
            return result

    return client.portal.call(run)


# --- connection and auth ---------------------------------------------------------------------------


def test_ws_rejects_foreign_origin_4403(client):
    chat = _chat(client)
    with _connect(client, chat.conversation_id, origin=SHOP) as ws:
        assert closed_with(ws) == 4403


def test_ws_requires_auth_frame_4401(client):
    chat = _chat(client)
    with _connect(client, chat.conversation_id) as ws:  # a first frame that is not auth
        ws.send_json({"type": "ping"})
        assert closed_with(ws) == 4401
    with _connect(client, chat.conversation_id) as ws:  # an invalid token
        ws.send_json({"type": "auth", "token": "not-a-token"})
        assert closed_with(ws) == 4401


def test_ws_auth_frame_timeout_4401(client, monkeypatch):
    monkeypatch.setattr(widget_ws, "AUTH_TIMEOUT_SECONDS", 0.2)
    chat = _chat(client)
    with _connect(client, chat.conversation_id) as ws:
        assert closed_with(ws) == 4401


def test_ws_rejects_dashboard_access_token_4401(client):
    headers, _org_id, widget = _owner(client)
    _token, conversation_id = _customer(client, widget["public_key"])
    with _connect(client, conversation_id) as ws:
        ws.send_json({"type": "auth", "token": headers["Authorization"].split()[1]})
        assert closed_with(ws) == 4401


def test_ws_disabled_widget_4401(client):
    headers, org_id, widget = _owner(client)
    token, conversation_id = _customer(client, widget["public_key"])
    r = client.patch(f"/orgs/{org_id}/widgets/{widget['id']}", json={"enabled": False}, headers=headers)
    assert r.status_code == 200, r.text
    with _connect(client, conversation_id) as ws:
        ws.send_json({"type": "auth", "token": token})
        assert closed_with(ws) == 4401


@pytest.mark.parametrize("change", ["disable", "delete", "identity_required"])
@pytest.mark.parametrize("frame", [{"type": "message", "text": "hello", "client_id": "c-1"}, {"type": "talk_to_human"}])
def test_open_socket_stops_when_widget_turned_off(client, change, frame):
    headers, org_id, widget = _owner(client)
    token, conversation_id = _customer(client, widget["public_key"])
    path = f"/orgs/{org_id}/widgets/{widget['id']}"
    with _connect(client, conversation_id) as ws:
        _auth(ws, token)
        if change == "disable":
            assert client.patch(path, json={"enabled": False}, headers=headers).status_code == 200
        elif change == "delete":
            assert client.delete(path, headers=headers).status_code == 204
        else:  # the anonymous session no longer qualifies

            async def require_identity(s):
                await s.execute(update(Widget).where(Widget.id == uuid.UUID(widget["id"])).values(identity_required=True))

            _db(client, require_identity, commit=True)
        ws.send_json(frame)
        assert recv(ws) == {"type": "error", "code": "widget_unavailable"}
        assert closed_with(ws) == 4401

    async def stored(s):
        return list((await s.execute(select(Message).where(Message.conversation_id == uuid.UUID(conversation_id)))).scalars())

    assert all(m.role != "customer" for m in _db(client, stored))
    if change != "delete":
        assert _db(client, lambda s: s.get(Conversation, uuid.UUID(conversation_id))).status == "bot"


def test_ws_other_customers_conversation_4404(client):
    chat = _chat(client)
    other_token, _other_conversation = _customer(client, chat.widget_key)
    with _connect(client, chat.conversation_id) as ws:
        ws.send_json({"type": "auth", "token": other_token})
        assert closed_with(ws) == 4404
    with _connect(client, str(uuid.uuid4())) as ws:
        ws.send_json({"type": "auth", "token": chat.token})
        assert closed_with(ws) == 4404


def test_ws_ping_pong_and_ready_status(client):
    chat = _chat(client)
    with _connect(client, chat.conversation_id) as ws:
        assert _auth(ws, chat.token) == {"type": "ready", "status": "bot"}
        ws.send_json({"type": "ping"})
        assert recv(ws) == {"type": "pong"}


# --- messages and the bot --------------------------------------------------------------------------


def test_ws_message_roundtrip_bot_reply(client):
    chat = _chat(client)
    script_chat(["Your order ships tomorrow"])
    with _connect(client, chat.conversation_id) as ws:
        _auth(ws, chat.token)
        ws.send_json({"type": "message", "text": "  where is my order?  ", "client_id": "c-1"})
        frames = recv_until(ws, "bot_done")

    types = [f["type"] for f in frames]
    assert types[0] == "message" and frames[0]["message"]["role"] == "customer"
    assert frames[0]["message"]["text"] == "where is my order?" and frames[0]["message"]["client_id"] == "c-1"
    assert "bot_token" in types
    bot = [f["message"] for f in frames if f["type"] == "message" and f["message"]["role"] == "bot"]
    assert [m["text"] for m in bot] == ["Your order ships tomorrow"]
    assert "".join(f["delta"] for f in frames if f["type"] == "bot_token") == "Your order ships tomorrow"
    assert frames[-1]["type"] == "bot_done" and isinstance(frames[-1]["offer_human"], bool)

    def stored(s):
        return s.execute(select(BotTurn).where(BotTurn.conversation_id == uuid.UUID(chat.conversation_id)))

    turns = list(_db(client, lambda s: _scalars(stored(s))))
    assert len(turns) == 1 and turns[0].reply_message_id is not None


async def _scalars(result_coro):
    return list((await result_coro).scalars())


def test_ws_resent_client_id_not_stored_twice(client):
    chat = _chat(client)
    script_chat(["First reply"])
    with _connect(client, chat.conversation_id) as ws:
        _auth(ws, chat.token)
        ws.send_json({"type": "message", "text": "hello", "client_id": "c-1"})
        recv_until(ws, "bot_done")
        ws.send_json({"type": "message", "text": "hello", "client_id": "c-1"})
        echo = recv(ws)
        assert echo["type"] == "message" and echo["message"]["client_id"] == "c-1"
        ws.send_json({"type": "ping"})
        assert recv(ws) == {"type": "pong"}  # no second turn was started

    def customer_messages(s):
        return s.execute(
            select(Message).where(
                Message.conversation_id == uuid.UUID(chat.conversation_id), Message.role == "customer"
            )
        )

    assert len(_db(client, lambda s: _scalars(customer_messages(s)))) == 1


def test_ws_invalid_message_error_frame(client):
    chat = _chat(client)
    with _connect(client, chat.conversation_id) as ws:
        _auth(ws, chat.token)
        ws.send_json({"type": "message", "text": "   "})
        assert recv(ws)["code"] == "invalid_message"
        ws.send_json({"type": "message", "text": "x" * 4001})
        assert recv(ws) == {"type": "error", "code": "invalid_message"}
        ws.send_json({"type": "message", "text": 42})
        assert recv(ws)["code"] == "invalid_message"
        ws.send_text("not json")
        assert recv(ws)["code"] == "invalid_frame"
        ws.send_json({"type": "ping"})
        assert recv(ws) == {"type": "pong"}  # the socket stays open


def test_two_sockets_receive_events_once(client):
    chat = _chat(client)
    script_chat(["Hello from the bot"])
    with _connect(client, chat.conversation_id) as first, _connect(client, chat.conversation_id) as second:
        _auth(first, chat.token)
        _auth(second, chat.token)
        first.send_json({"type": "message", "text": "hi there", "client_id": "c-1"})
        for ws in (first, second):
            frames = recv_until(ws, "bot_done")
            messages = [f["message"]["role"] for f in frames if f["type"] == "message"]
            assert messages == ["customer", "bot"]
            assert [f["type"] for f in frames].count("bot_done") == 1
        for ws in (first, second):
            ws.send_json({"type": "ping"})
            assert recv(ws) == {"type": "pong"}  # nothing else was queued


def test_ws_rate_limited_error_frame(client, monkeypatch):
    monkeypatch.setattr(widget_ws, "MESSAGE_PER_CONVERSATION", (1, 60))
    chat = _chat(client)
    script_chat(["Only reply"])
    with _connect(client, chat.conversation_id) as ws:
        _auth(ws, chat.token)
        ws.send_json({"type": "message", "text": "first", "client_id": "c-1"})
        recv_until(ws, "bot_done")
        ws.send_json({"type": "message", "text": "second", "client_id": "c-2"})
        error = recv(ws)
        assert error["type"] == "error" and error["code"] == "rate_limited"
        assert isinstance(error["retry_after"], int) and error["retry_after"] > 0
        ws.send_json({"type": "ping"})
        assert recv(ws) == {"type": "pong"}


def test_ws_org_rate_limit(client, monkeypatch):
    monkeypatch.setattr(widget_ws, "MESSAGE_PER_ORG", (1, 60))
    chat = _chat(client)
    other_token, other_conversation = _customer(client, chat.widget_key)
    script_chat(["Only reply"])
    with _connect(client, chat.conversation_id) as ws:
        _auth(ws, chat.token)
        ws.send_json({"type": "message", "text": "first"})
        recv_until(ws, "bot_done")
    with _connect(client, other_conversation) as ws:
        _auth(ws, other_token)
        ws.send_json({"type": "message", "text": "second"})
        assert recv(ws)["code"] == "rate_limited"


def test_ws_oversize_frame_closes_1009(client):
    chat = _chat(client)
    with _connect(client, chat.conversation_id) as ws:
        _auth(ws, chat.token)
        ws.send_text(json.dumps({"type": "message", "text": "x" * 16400}))
        assert closed_with(ws) == 1009


def test_talk_to_human_escalates(client):
    chat = _chat(client)
    with _connect(client, chat.conversation_id) as ws:
        _auth(ws, chat.token)
        ws.send_json({"type": "talk_to_human"})
        frames = recv_until(ws, "handoff")

    assert frames[0]["type"] == "message" and frames[0]["message"]["role"] == "system"
    assert frames[0]["message"]["text"] == strings_for("en")["no_agent_available"]  # no agent is online
    assert frames[-1] == {"type": "handoff", "status": "open", "agent": None}
    conversation = _db(client, lambda s: s.get(Conversation, uuid.UUID(chat.conversation_id)))
    assert conversation.status == "open"
    assert conversation.escalation["rule_key"] == "customer_request"
    assert conversation.escalation["source"] == "customer"
    assert conversation.escalation["action"] == "escalate"


def test_message_while_open_runs_no_turn(client):
    chat = _chat(client)
    script = script_chat(["Never sent"])
    with _connect(client, chat.conversation_id) as ws:
        _auth(ws, chat.token)
        ws.send_json({"type": "talk_to_human"})
        recv_until(ws, "handoff")
        ws.send_json({"type": "message", "text": "are you there?"})
        assert recv(ws)["message"]["role"] == "customer"
        ws.send_json({"type": "ping"})
        assert recv(ws) == {"type": "pong"}
    assert script.calls == []


def test_message_text_never_logged_over_websocket(client, caplog):
    chat = _chat(client)
    secret = "SECRET-WS-MESSAGE-TEXT-7f3a"
    script_chat(["A plain reply"])
    caplog.set_level(logging.DEBUG)
    with structlog.testing.capture_logs() as logs:
        with _connect(client, chat.conversation_id) as ws:
            _auth(ws, chat.token)
            ws.send_json({"type": "message", "text": f"please help {secret}", "client_id": "c-1"})
            recv_until(ws, "bot_done")
    events = [entry.get("event") for entry in logs]
    assert "message_added" in events and "bot_turn" in events  # the turn really logged
    assert secret not in repr(logs) and secret not in caplog.text
    assert chat.token not in repr(logs) and chat.token not in caplog.text


class _BrokenStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise json.JSONDecodeError("bad payload", "x", 0)


class _BrokenPubSub:
    async def publish(self, channel, message):
        return None

    def subscribe(self, channel):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def cm():
            yield _BrokenStream()

        return cm()


def test_relay_failure_closes_1011_and_app_survives(client):
    chat = _chat(client)
    adapters = client.app.state.container.adapters
    original = adapters.pubsub
    adapters.pubsub = _BrokenPubSub()
    try:
        with _connect(client, chat.conversation_id) as ws:
            _auth(ws, chat.token)
            assert closed_with(ws) == 1011
    finally:
        adapters.pubsub = original
    with _connect(client, chat.conversation_id) as ws:  # the app is fine
        _auth(ws, chat.token)
        ws.send_json({"type": "ping"})
        assert recv(ws) == {"type": "pong"}


# --- idle close ------------------------------------------------------------------------------------


def test_idle_conversation_closed(client):
    chat = _chat(client)
    _other_token, fresh_conversation = _customer(client, chat.widget_key)
    idle_id = uuid.UUID(chat.conversation_id)

    async def age(s):
        await s.execute(
            update(Conversation)
            .where(Conversation.id == idle_id)
            .values(last_message_at=datetime.now(UTC) - timedelta(minutes=31))
        )

    _db(client, age, commit=True)
    with _connect(client, chat.conversation_id) as ws:
        _auth(ws, chat.token)
        closed = client.portal.call(close_idle_conversations, client.app.state.container.job_runtime())
        assert recv(ws) == {"type": "status", "status": "resolved"}

    assert closed == 1
    assert _db(client, lambda s: s.get(Conversation, idle_id)).status == "resolved"
    assert _db(client, lambda s: s.get(Conversation, uuid.UUID(fresh_conversation))).status == "bot"


def test_idle_close_is_a_registered_job():
    from supportcore.knowledge.jobs import JOB_HANDLERS

    assert "close_idle_conversations" in JOB_HANDLERS


# --- fix round 1 -----------------------------------------------------------------------------------


def test_reconnect_redrives_unanswered_message(client):
    """A message left unanswered (e.g. its turn was cancelled at shutdown) is answered when the
    widget connects again."""
    from supportcore.conversations.service import ConversationService

    chat = _chat(client)
    container = client.app.state.container

    async def add_unanswered(s):
        conversation = await s.get(Conversation, uuid.UUID(chat.conversation_id))
        await ConversationService(s, conversation.organisation_id, container.events).add_customer_message(
            conversation, "still waiting", None
        )

    _db(client, add_unanswered, commit=True)
    script_chat(["Sorry for the wait"])
    with _connect(client, chat.conversation_id) as ws:
        _auth(ws, chat.token)
        frames = recv_until(ws, "bot_done")
    assert [f["message"]["text"] for f in frames if f["type"] == "message"] == ["Sorry for the wait"]


def test_talk_to_human_rate_limited(client, monkeypatch):
    monkeypatch.setattr(widget_ws, "MESSAGE_PER_CONVERSATION", (2, 60))
    chat = _chat(client)
    with _connect(client, chat.conversation_id) as ws:
        _auth(ws, chat.token)
        ws.send_json({"type": "talk_to_human"})
        recv_until(ws, "handoff")
        ws.send_json({"type": "talk_to_human"})  # already open: a no-op
        ws.send_json({"type": "talk_to_human"})
        error = recv(ws)
        assert error["type"] == "error" and error["code"] == "rate_limited" and error["retry_after"] > 0
        ws.send_json({"type": "ping"})
        assert recv(ws) == {"type": "pong"}


def test_frame_flood_closes_1008(client, monkeypatch):
    monkeypatch.setattr(widget_ws, "FRAME_BUDGET", (5, 60))
    chat = _chat(client)
    with _connect(client, chat.conversation_id) as ws:
        _auth(ws, chat.token)
        for _ in range(5):
            ws.send_json({"type": "ping"})
            assert recv(ws) == {"type": "pong"}
        ws.send_text("garbage")  # every frame counts, valid or not
        assert closed_with(ws) == 1008


class _HookedPubSub:
    """Delegates to the real pub/sub; runs `before_subscribe` just before subscribing."""

    def __init__(self, inner, before_subscribe):
        self.inner, self.before_subscribe = inner, before_subscribe

    async def publish(self, channel, message):
        await self.inner.publish(channel, message)

    def subscribe(self, channel):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def cm():
            await self.before_subscribe()
            async with self.inner.subscribe(channel) as stream:
                yield stream

        return cm()


def test_status_change_between_auth_and_subscribe_is_seen(client):
    """`ready` reports the status read after subscribing: a handoff that lands after the auth check
    but before the subscription is not missed."""
    chat = _chat(client)
    container = client.app.state.container
    factory = client.app.state.session_factory
    conversation_id = uuid.UUID(chat.conversation_id)

    async def handoff_now():
        async with factory() as s:
            await s.execute(update(Conversation).where(Conversation.id == conversation_id).values(status="open"))
            await s.commit()

    original = container.adapters.pubsub
    container.adapters.pubsub = _HookedPubSub(original, handoff_now)
    try:
        with _connect(client, chat.conversation_id) as ws:
            ws.send_json({"type": "auth", "token": chat.token})
            assert recv(ws) == {"type": "ready", "status": "open"}
    finally:
        container.adapters.pubsub = original


def test_new_conversation_has_last_message_at(client):
    chat = _chat(client)
    conversation = _db(client, lambda s: s.get(Conversation, uuid.UUID(chat.conversation_id)))
    assert conversation.last_message_at is not None  # so idle close applies to empty conversations


def test_each_turn_on_a_socket_is_its_own_trace(client):
    from supportcore.observability.tracing import get_test_span_exporter

    exporter = get_test_span_exporter()
    chat = _chat(client)
    script_chat(["First answer", "Second answer"])
    exporter.clear()
    with _connect(client, chat.conversation_id) as ws:
        _auth(ws, chat.token)
        ws.send_json({"type": "message", "text": "first question", "client_id": "c-1"})
        recv_until(ws, "bot_done")
        ws.send_json({"type": "message", "text": "second question", "client_id": "c-2"})
        recv_until(ws, "bot_done")
    spans = exporter.get_finished_spans()
    turns = [s for s in spans if s.name == "bot.turn"]
    assert len(turns) == 2
    assert len({s.context.trace_id for s in turns}) == 2
    assert all(s.parent is None for s in turns)  # new roots ...
    connection = [s for s in spans if s.kind.name == "SERVER" and "/ws" in s.name]
    assert len(connection) == 1
    for turn in turns:  # ... linked to the connection's span
        assert [link.context.span_id for link in turn.links] == [connection[0].context.span_id]
    assert not [s for s in spans if s.name.endswith((" send", " receive"))]  # no per-frame spans

    async def stored(s):
        rows = await s.execute(select(BotTurn.trace_id).where(BotTurn.conversation_id == uuid.UUID(chat.conversation_id)))
        return sorted(rows.scalars())

    assert _db(client, stored) == sorted(format(s.context.trace_id, "032x") for s in turns)


def test_agent_message_payload_has_author_name_only(client):
    headers, org_id, widget = _owner(client)
    token, conversation_id = _customer(client, widget["public_key"])
    auth = {"Authorization": f"Bearer {token}"}
    with _connect(client, conversation_id) as ws:
        _auth(ws, token)
        ws.send_json({"type": "talk_to_human"})
        recv_until(ws, "handoff")
        _post(client, f"/orgs/{org_id}/inbox/conversations/{conversation_id}/messages", {"text": "On it"}, headers)
        pushed = recv_until(ws, "message")[-1]["message"]
        ws.send_json({"type": "message", "text": "thanks"})
        customer = recv(ws)["message"]

    assert pushed["role"] == "agent" and pushed["author"] == {"name": "Owner"}
    assert customer["role"] == "customer" and customer["author"] is None
    listed = client.get(f"/widget/v1/conversations/{conversation_id}/messages", headers=auth).json()
    by_role = {m["role"]: m for m in listed}
    assert by_role["agent"]["author"] == {"name": "Owner"}
    assert by_role["customer"]["author"] is None and by_role["system"]["author"] is None
    assert "@" not in json.dumps([pushed, listed])  # the display name only, never the email


def test_agent_events_and_conversation_agent_follow_the_assignee(client):
    headers, org_id, widget = _owner(client)
    token, conversation_id = _customer(client, widget["public_key"])
    auth = {"Authorization": f"Bearer {token}"}
    base = f"/orgs/{org_id}/inbox/conversations/{conversation_id}"

    def current():
        return client.get("/widget/v1/conversations", headers=auth).json()[0]["agent"]

    assert current() is None
    with _connect(client, conversation_id) as ws:
        _auth(ws, token)
        _post(client, f"{base}/claim", None, headers)
        frames = recv_until(ws, "handoff")
        assert {"type": "agent", "agent": {"name": "Owner"}} in frames
        assert current() == {"name": "Owner"}

        r = client.put(f"{base}/assignee", json={"membership_id": None}, headers=headers)
        assert r.status_code == 200, r.text
        assert recv_until(ws, "agent")[-1] == {"type": "agent", "agent": None}
        assert current() is None

        _post(client, f"{base}/claim", None, headers)
        assert recv_until(ws, "agent")[-1] == {"type": "agent", "agent": {"name": "Owner"}}
        _post(client, f"{base}/hand-back", None, headers)
        frames = recv_until(ws, "status")
        assert {"type": "agent", "agent": None} in frames and frames[-1] == {"type": "status", "status": "bot"}
        assert current() is None
    assert "@" not in json.dumps(client.get("/widget/v1/conversations", headers=auth).json())


def test_widget_history_returns_the_newest_hundred_in_order(client):
    chat = _chat(client)
    auth = {"Authorization": f"Bearer {chat.token}"}
    now = datetime.now(UTC)

    async def seed(s):
        s.add_all(
            Message(
                conversation_id=uuid.UUID(chat.conversation_id),
                organisation_id=uuid.UUID(chat.org_id),
                role="bot",
                text=f"m{i:03d}",
                citations=[],
                created_at=now + timedelta(seconds=i),
            )
            for i in range(150)
        )

    _db(client, seed, commit=True)
    texts = [m["text"] for m in client.get(f"/widget/v1/conversations/{chat.conversation_id}/messages", headers=auth).json()]
    assert texts == [f"m{i:03d}" for i in range(50, 150)]
    after = (now + timedelta(seconds=147)).isoformat()
    r = client.get(f"/widget/v1/conversations/{chat.conversation_id}/messages", params={"after": after}, headers=auth)
    assert [m["text"] for m in r.json()] == ["m148", "m149"]
