import asyncio
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
import structlog
from sqlalchemy import update

from supportcore.conversations.service import (
    ConversationService,
    InvalidConversationStateError,
    InvalidCsatError,
    InvalidMessageError,
)
from supportcore.db.models import Conversation, Customer, Message, Organisation
from supportcore.errors import NotFoundError
from supportcore.widget.identity.ports import InvalidCustomerTokenError
from supportcore.widget.session import IdentityRequiredError, OriginNotAllowedError

ORIGIN = "https://shop.example.com"


@pytest.fixture
async def open_widget(widgets, widget, owner):
    return await widgets.update(widget.id, allowed_domains=["shop.example.com"], actor=owner)


async def _signed(widgets, widget, owner, **claims) -> str:
    _, secret = await widgets.rotate_identity_secret(widget.id, owner)
    now = int(time.time())
    return jwt.encode({"sub": "u-1", "iat": now, "exp": now + 600, **claims}, secret, algorithm="HS256")


async def _conversation(conversations, session, widget, org, *, status="bot", csat=None):
    customer = Customer(organisation_id=org.id, visitor_id=f"v-{time.time_ns()}")
    session.add(customer)
    await session.flush()
    conv = await conversations.create(customer.id, widget.id, "en", False)
    if status != "bot":
        conv.status = status
        await session.flush()
    return conv


# --- sessions / customers ---------------------------------------------------------------------


async def test_start_anonymous_creates_customer_with_new_visitor_id(sessions_svc, open_widget, session):
    res = await sessions_svc.start(
        open_widget.public_key, parent_origin=ORIGIN, visitor_id=None, customer_token=None, is_preview=False
    )
    assert res.visitor_id and res.conversation is None
    customer = await session.get(Customer, res.customer_id)
    assert customer.visitor_id == res.visitor_id and customer.external_id is None
    assert customer.organisation_id == open_widget.organisation_id
    claims = sessions_svc.tokens.decode(res.token)
    assert (claims.customer_id, claims.organisation_id, claims.widget_id) == (
        res.customer_id, open_widget.organisation_id, open_widget.id,
    )


async def test_start_same_visitor_reuses_customer(sessions_svc, open_widget):
    kw = dict(parent_origin=ORIGIN, customer_token=None, is_preview=False)
    first = await sessions_svc.start(open_widget.public_key, visitor_id=None, **kw)
    again = await sessions_svc.start(open_widget.public_key, visitor_id=first.visitor_id, **kw)
    assert again.customer_id == first.customer_id and again.visitor_id == first.visitor_id
    unknown = await sessions_svc.start(open_widget.public_key, visitor_id="v-forged", **kw)
    assert unknown.customer_id != first.customer_id and unknown.visitor_id != "v-forged"


async def test_start_resumes_open_conversation(sessions_svc, conversations, open_widget):
    kw = dict(parent_origin=ORIGIN, customer_token=None, is_preview=False)
    first = await sessions_svc.start(open_widget.public_key, visitor_id=None, **kw)
    conv = await conversations.create(first.customer_id, open_widget.id, "en", False)
    again = await sessions_svc.start(open_widget.public_key, visitor_id=first.visitor_id, **kw)
    assert again.conversation.id == conv.id
    await conversations.end(conv)
    assert (await sessions_svc.start(open_widget.public_key, visitor_id=first.visitor_id, **kw)).conversation is None


async def test_start_signed_identifies_by_external_id_across_visitors(
    sessions_svc, widgets, open_widget, owner, session
):
    token = await _signed(widgets, open_widget, owner, email="a@x.com", name="Ann")
    kw = dict(parent_origin=ORIGIN, customer_token=token, is_preview=False)
    a = await sessions_svc.start(open_widget.public_key, visitor_id="v-one", **kw)
    b = await sessions_svc.start(open_widget.public_key, visitor_id="v-two", **kw)
    assert a.customer_id == b.customer_id
    customer = await session.get(Customer, a.customer_id)
    assert (customer.external_id, customer.email, customer.name) == ("u-1", "a@x.com", "Ann")


async def test_start_signed_promotes_the_anonymous_visitor_customer(sessions_svc, widgets, open_widget, owner):
    anon = await sessions_svc.start(
        open_widget.public_key, parent_origin=ORIGIN, visitor_id=None, customer_token=None, is_preview=False
    )
    token = await _signed(widgets, open_widget, owner)
    signed = await sessions_svc.start(
        open_widget.public_key, parent_origin=ORIGIN, visitor_id=anon.visitor_id, customer_token=token, is_preview=False
    )
    assert signed.customer_id == anon.customer_id


async def test_identity_required_without_token_401(sessions_svc, widgets, open_widget, owner):
    await widgets.rotate_identity_secret(open_widget.id, owner)
    await widgets.update(open_widget.id, identity_required=True, actor=owner)
    with pytest.raises(IdentityRequiredError):
        await sessions_svc.start(
            open_widget.public_key, parent_origin=ORIGIN, visitor_id=None, customer_token=None, is_preview=False
        )
    token = await _signed(widgets, open_widget, owner)
    res = await sessions_svc.start(
        open_widget.public_key, parent_origin=ORIGIN, visitor_id=None, customer_token=token, is_preview=False
    )
    assert res.token


async def test_invalid_customer_token_rejected(sessions_svc, widgets, open_widget, owner):
    kw = dict(parent_origin=ORIGIN, visitor_id=None, is_preview=False)
    with pytest.raises(InvalidCustomerTokenError):  # the widget has no secret yet
        await sessions_svc.start(open_widget.public_key, customer_token="a.b.c", **kw)
    await widgets.rotate_identity_secret(open_widget.id, owner)
    with pytest.raises(InvalidCustomerTokenError):
        await sessions_svc.start(open_widget.public_key, customer_token="a.b.c", **kw)


async def test_origin_checks_and_preview(sessions_svc, open_widget, settings):
    kw = dict(visitor_id=None, customer_token=None)
    ticket = sessions_svc.tokens.issue_preview_ticket(open_widget.organisation_id, open_widget.id)
    with pytest.raises(OriginNotAllowedError):
        await sessions_svc.start(open_widget.public_key, parent_origin="https://evil.example", is_preview=False, **kw)
    with pytest.raises(OriginNotAllowedError):
        await sessions_svc.start(
            open_widget.public_key, parent_origin="https://evil.example", is_preview=True, preview_ticket=ticket, **kw
        )
    # The dashboard origin is not an embedding site.
    with pytest.raises(OriginNotAllowedError):
        await sessions_svc.start(open_widget.public_key, parent_origin=settings.web_origin, is_preview=False, **kw)
    # Preview needs the ticket, on the dashboard origin.
    with pytest.raises(OriginNotAllowedError):
        await sessions_svc.start(open_widget.public_key, parent_origin=settings.web_origin, is_preview=True, **kw)
    with pytest.raises(OriginNotAllowedError):
        await sessions_svc.start(open_widget.public_key, parent_origin=ORIGIN, is_preview=True, preview_ticket=ticket, **kw)
    res = await sessions_svc.start(
        open_widget.public_key, parent_origin=settings.web_origin, is_preview=True, preview_ticket=ticket, **kw
    )
    assert sessions_svc.tokens.decode(res.token).preview is True
    res = await sessions_svc.start(open_widget.public_key, parent_origin=ORIGIN, is_preview=False, **kw)
    assert sessions_svc.tokens.decode(res.token).preview is False


async def test_empty_allowed_domains_means_only_preview_works(sessions_svc, widgets, open_widget, owner, settings):
    await widgets.update(open_widget.id, allowed_domains=[], actor=owner)
    kw = dict(visitor_id=None, customer_token=None)
    for origin in (settings.web_origin, ORIGIN):
        with pytest.raises(OriginNotAllowedError):
            await sessions_svc.start(open_widget.public_key, parent_origin=origin, is_preview=False, **kw)
    ticket = sessions_svc.tokens.issue_preview_ticket(open_widget.organisation_id, open_widget.id)
    await sessions_svc.start(
        open_widget.public_key, parent_origin=settings.web_origin, is_preview=True, preview_ticket=ticket, **kw
    )


async def test_preview_ticket_checks(sessions_svc, open_widget, settings):
    from uuid import uuid4

    from supportcore.widget.tokens import WidgetTokenService

    secret = settings.jwt_secret.get_secret_value()
    tokens = sessions_svc.tokens
    org, wid = open_widget.organisation_id, open_widget.id
    assert tokens.preview_ticket_valid(tokens.issue_preview_ticket(org, wid), org, wid)
    assert not tokens.preview_ticket_valid(tokens.issue_preview_ticket(org, uuid4()), org, wid)
    assert not tokens.preview_ticket_valid(tokens.issue_preview_ticket(uuid4(), wid), org, wid)
    assert not tokens.preview_ticket_valid(None, org, wid) and not tokens.preview_ticket_valid("junk", org, wid)
    # Widget session tokens and access tokens are not tickets, and tickets are not session tokens.
    assert not tokens.preview_ticket_valid(tokens.issue(uuid4(), org, wid), org, wid)
    from supportcore.identity.jwt_tokens import JwtAccessTokenService

    assert not tokens.preview_ticket_valid(JwtAccessTokenService(secret, 900).issue(uuid4()), org, wid)
    from supportcore.widget.tokens import InvalidWidgetTokenError

    with pytest.raises(InvalidWidgetTokenError):
        tokens.decode(tokens.issue_preview_ticket(org, wid))
    expired = WidgetTokenService(secret, timedelta(seconds=60))
    import jwt as pyjwt

    now = datetime.now(UTC)
    old = pyjwt.encode(
        {"type": "widget_preview", "org": str(org), "wid": str(wid), "iat": now - timedelta(seconds=400), "exp": now - timedelta(seconds=100)},
        secret,
        algorithm="HS256",
    )
    assert not expired.preview_ticket_valid(old, org, wid)


async def test_concurrent_first_sessions_for_the_same_external_id(session_factory, adapters, settings, bus):
    from supportcore.db.models import User
    from supportcore.widget.service import WidgetService
    from supportcore.widget.session import WidgetSessionService

    async with session_factory() as s:
        from supportcore.orgs.service import OrganisationService

        owner = User(email="o@x.com", password_hash="x", name="O")
        s.add(owner)
        await s.flush()
        org = await OrganisationService(s, bus).create(owner, "Race")
        svc = WidgetService(s, org.id, bus, adapters.cipher)
        w = await svc.create("W", owner)
        await svc.update(w.id, allowed_domains=["shop.example.com"], actor=owner)
        _, secret = await svc.rotate_identity_secret(w.id, owner)
        key = w.public_key
        await s.commit()
    now = int(time.time())
    token = jwt.encode({"sub": "same-user", "iat": now, "exp": now + 600}, secret, algorithm="HS256")

    async def attempt():
        async with session_factory() as s:
            res = await WidgetSessionService(s, adapters, settings, bus).start(
                key, parent_origin=ORIGIN, visitor_id=None, customer_token=token, is_preview=False
            )
            await s.commit()
            return res.customer_id

    ids = await asyncio.gather(*(attempt() for _ in range(4)))
    assert len(set(ids)) == 1


async def test_unknown_and_disabled_widget_not_found(sessions_svc, widgets, open_widget, owner):
    with pytest.raises(NotFoundError):
        await sessions_svc.public_config("wk_nope")
    await widgets.update(open_widget.id, enabled=False, actor=owner)
    with pytest.raises(NotFoundError):
        await sessions_svc.public_config(open_widget.public_key)
    with pytest.raises(NotFoundError):
        await sessions_svc.start(
            open_widget.public_key, parent_origin=ORIGIN, visitor_id=None, customer_token=None, is_preview=False
        )
    assert await sessions_svc.frame_policy(open_widget.public_key) == "'none'"


async def test_public_config_and_frame_policy(sessions_svc, widgets, open_widget, owner, settings):
    await widgets.update(open_widget.id, appearance={"greeting": "Welcome!"}, actor=owner)
    cfg = await sessions_svc.public_config(open_widget.public_key)
    assert cfg.organisation_name == "Acme" and cfg.language == "en" and cfg.csat_enabled is True
    assert cfg.strings["greeting"] == "Welcome!" and cfg.strings["send"] == "Send"
    assert cfg.appearance["primary_color"] == "#4f46e5"
    assert await sessions_svc.frame_policy(open_widget.public_key) == f"{settings.web_origin} https://shop.example.com"


# --- conversations / messages -----------------------------------------------------------------


async def test_create_and_scoping(conversations, session, widget, org, bus, owner):
    conv = await _conversation(conversations, session, widget, org)
    assert (conv.status, conv.channel, conv.language, conv.is_test) == ("bot", "chat", "en", False)
    assert [c.id for c in await conversations.list_for_customer(conv.customer_id)] == [conv.id]
    assert (await conversations.get_for_customer(conv.id, conv.customer_id)).id == conv.id
    other = Customer(organisation_id=org.id, visitor_id="v-other")
    session.add(other)
    await session.flush()
    with pytest.raises(NotFoundError):
        await conversations.get_for_customer(conv.id, other.id)
    assert await conversations.list_for_customer(other.id) == []
    other_org = Organisation(name="Other", slug="other")
    session.add(other_org)
    await session.flush()
    with pytest.raises(NotFoundError):
        await ConversationService(session, other_org.id, bus).get_for_customer(conv.id, conv.customer_id)


async def test_add_customer_message_idempotent_by_client_id(conversations, session, widget, org):
    conv = await _conversation(conversations, session, widget, org)
    m1, created1 = await conversations.add_customer_message(conv, "  hello  ", "c-1")
    m2, created2 = await conversations.add_customer_message(conv, "hello again", "c-1")
    assert (created1, created2) == (True, False) and m1.id == m2.id and m1.text == "hello" and m1.role == "customer"
    m3, created3 = await conversations.add_customer_message(conv, "no id", None)
    m4, created4 = await conversations.add_customer_message(conv, "no id", None)
    assert created3 and created4 and m3.id != m4.id
    assert [m.id for m in await conversations.messages(conv.id)] == [m1.id, m3.id, m4.id]


async def test_add_customer_message_validates_text(conversations, session, widget, org):
    conv = await _conversation(conversations, session, widget, org)
    for bad in ("", "   ", "x" * 4001):
        with pytest.raises(InvalidMessageError):
            await conversations.add_customer_message(conv, bad, None)
    await conversations.add_customer_message(conv, "x" * 4000, None)


async def test_messages_after_and_limit(conversations, session, widget, org):
    conv = await _conversation(conversations, session, widget, org)
    first, _ = await conversations.add_customer_message(conv, "one", None)
    await asyncio.sleep(0.01)
    bot = await conversations.add_message(
        conv, "bot", "two", citations=[{"chunk_id": "c", "document_id": "d", "document_name": "Doc"}]
    )
    await asyncio.sleep(0.01)
    await conversations.add_message(conv, "agent", "three")
    after = await conversations.messages(conv.id, after=first.created_at)
    assert [m.text for m in after] == ["two", "three"] and after[0].citations[0]["document_name"] == "Doc"
    assert [m.text for m in await conversations.messages(conv.id, limit=2)] == ["two", "three"]  # the newest two
    assert [m.text for m in await conversations.messages(conv.id, after=first.created_at, limit=1)] == ["two"]
    assert conv.last_message_at >= bot.created_at


async def test_messages_history_is_the_newest_hundred_in_order(conversations, session, widget, org):
    conv = await _conversation(conversations, session, widget, org)
    for i in range(150):
        await conversations.add_message(conv, "bot", f"m{i:03d}")
    newest = await conversations.messages(conv.id)
    assert [m.text for m in newest] == [f"m{i:03d}" for i in range(50, 150)]
    after = await conversations.messages(conv.id, after=newest[0].created_at, limit=5)
    assert [m.text for m in after] == [f"m{i:03d}" for i in range(51, 56)]  # gap fill: what follows, oldest first


async def test_end_resolves_and_csat_only_after_resolved(conversations, session, widget, org):
    conv = await _conversation(conversations, session, widget, org)
    with pytest.raises(InvalidConversationStateError):
        await conversations.rate(conv, 5, "great")
    await conversations.end(conv)
    assert conv.status == "resolved"
    await conversations.rate(conv, 4, "ok")
    assert (conv.csat_score, conv.csat_comment) == (4, "ok")
    with pytest.raises(InvalidConversationStateError):  # only once
        await conversations.rate(conv, 5, None)


async def test_csat_validation_and_disabled(conversations, session, widget, org, widgets, owner):
    conv = await _conversation(conversations, session, widget, org, status="resolved")
    for score in (0, 6):
        with pytest.raises(InvalidCsatError):
            await conversations.rate(conv, score, None)
    with pytest.raises(InvalidCsatError):
        await conversations.rate(conv, 3, "x" * 1001)
    await widgets.update(widget.id, csat_enabled=False, actor=owner)
    with pytest.raises(InvalidConversationStateError):
        await conversations.rate(conv, 3, None)


async def test_contact_email_only_when_open(conversations, session, widget, org):
    conv = await _conversation(conversations, session, widget, org)
    with pytest.raises(InvalidConversationStateError):
        await conversations.set_contact_email(conv, "a@x.com")
    decision = {
        "action": "escalate", "rule_key": "asks_for_human", "rule_name": "Asks for a human", "reason": "r",
        "source": "builtin", "ignored": 1,
    }
    await conversations.escalate(conv, decision)
    assert conv.status == "open"
    assert conv.escalation == {k: v for k, v in decision.items() if k != "ignored"}
    await conversations.set_contact_email(conv, " a@x.com ")
    assert conv.contact_email == "a@x.com"
    from supportcore.conversations.service import InvalidContactEmailError

    with pytest.raises(InvalidContactEmailError):
        await conversations.set_contact_email(conv, "nope")


async def test_turn_lease_exclusive_until_expiry(conversations, session, widget, org):
    conv = await _conversation(conversations, session, widget, org)
    lease = await conversations.acquire_turn_lease(conv.id)
    assert isinstance(lease, datetime)
    assert await conversations.acquire_turn_lease(conv.id) is None
    await conversations.release_turn_lease(conv.id, lease)
    lease = await conversations.acquire_turn_lease(conv.id)
    assert lease is not None
    # An expired lease can be taken over.
    await session.execute(
        update(Conversation).where(Conversation.id == conv.id).values(turn_lease_until=datetime.now(UTC) - timedelta(seconds=1))
    )
    assert await conversations.acquire_turn_lease(conv.id) is not None
    assert await conversations.acquire_turn_lease(conv.id) is None


async def _lease_value(session, conv_id):
    from sqlalchemy import select

    return (await session.execute(select(Conversation.turn_lease_until).where(Conversation.id == conv_id))).scalar_one()


async def test_late_release_does_not_clear_a_taken_over_lease(conversations, session, widget, org):
    conv = await _conversation(conversations, session, widget, org)
    lease_a = await conversations.acquire_turn_lease(conv.id)
    await session.execute(  # A's lease expires while A is still running
        update(Conversation).where(Conversation.id == conv.id).values(turn_lease_until=datetime.now(UTC) - timedelta(seconds=1))
    )
    lease_b = await conversations.acquire_turn_lease(conv.id)
    assert lease_b is not None and lease_b != lease_a
    await conversations.release_turn_lease(conv.id, lease_a)  # A finishes late
    assert await _lease_value(session, conv.id) == lease_b
    assert await conversations.acquire_turn_lease(conv.id) is None
    await conversations.release_turn_lease(conv.id, lease_b)
    assert await _lease_value(session, conv.id) is None


async def test_turn_lease_atomic_across_sessions(session_factory, bus):
    async with session_factory() as s:
        org = Organisation(name="Lease", slug="lease")
        s.add(org)
        await s.flush()
        customer = Customer(organisation_id=org.id, visitor_id="v")
        s.add(customer)
        await s.flush()
        conv = await ConversationService(s, org.id, bus).create(customer.id, None, "en", False)
        org_id, conv_id = org.id, conv.id
        await s.commit()

    async def attempt() -> datetime | None:
        async with session_factory() as s:
            got = await ConversationService(s, org_id, bus).acquire_turn_lease(conv_id)
            await s.commit()
            return got

    results = await asyncio.gather(*(attempt() for _ in range(6)))
    assert sum(r is not None for r in results) == 1


async def test_events_logged_with_ids_only_never_message_text(conversations, session, widget, org):
    secret_text = "my card number is 4242-SECRET-TEXT"
    with structlog.testing.capture_logs() as logs:
        conv = await _conversation(conversations, session, widget, org)
        await conversations.add_customer_message(conv, secret_text, "c-9")
        await conversations.add_message(conv, "bot", secret_text)
        await conversations.messages(conv.id)
        await conversations.end(conv)
    assert logs, "conversation events are logged"
    assert "SECRET-TEXT" not in repr(logs)
    assert any(entry["event"] == "conversation_started" for entry in logs)


@pytest.mark.parametrize(
    ("rule_key", "source", "expected"),
    [
        ("topic:6f1c", "classifier", "high"),
        ("topic:refunds", "keyword", "high"),
        ("write_action", "system", "high"),
        ("asks_for_human", "keyword", "normal"),
        ("asks_for_human", "classifier", "normal"),
        ("frustration", "classifier", "normal"),
        ("low_confidence", "system", "normal"),
        ("repeated_failure", "system", "normal"),
        ("customer_request", "customer", "normal"),
        ("bot_unavailable", "system", "normal"),
    ],
)
async def test_escalate_sets_open_and_priority_by_rule(conversations, session, widget, org, rule_key, source, expected):
    conv = await _conversation(conversations, session, widget, org)
    await conversations.escalate(
        conv, {"action": "escalate", "rule_key": rule_key, "rule_name": "n", "reason": "r", "source": source}
    )
    assert conv.status == "open" and conv.priority == expected


async def test_escalate_never_lowers_urgent(conversations, session, widget, org):
    conv = await _conversation(conversations, session, widget, org)
    conv.priority = "urgent"
    await conversations.escalate(
        conv, {"action": "escalate", "rule_key": "topic:x", "rule_name": "n", "reason": "r", "source": "classifier"}
    )
    assert conv.priority == "urgent"
