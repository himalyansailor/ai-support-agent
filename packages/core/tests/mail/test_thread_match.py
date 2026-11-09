from datetime import UTC, datetime, timedelta

from supportcore.db.models import Conversation, Customer, EmailMessage, Message
from supportcore.mail.thread_match import candidate_ids, find_thread, normalize_subject
from .test_classify import parsed

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


async def _customer(session, org) -> Customer:
    customer = Customer(organisation_id=org.id, email="c@example.com")
    session.add(customer)
    await session.flush()
    return customer


async def _conv(session, org, customer, *, channel="email", status="open", subject="Order late", last=NOW):
    conv = Conversation(
        organisation_id=org.id, customer_id=customer.id, channel=channel, status=status, subject=subject,
        last_message_at=last,
    )
    session.add(conv)
    await session.flush()
    return conv


async def _mail(session, org, conv, rfc, direction="inbound"):
    msg = Message(organisation_id=org.id, conversation_id=conv.id, role="customer", text="x")
    session.add(msg)
    await session.flush()
    session.add(
        EmailMessage(
            organisation_id=org.id, message_id=msg.id, conversation_id=conv.id, direction=direction,
            rfc_message_id=rfc, from_address="c@example.com",
            delivery_status="received" if direction == "inbound" else "sent",
        )
    )
    await session.flush()


def test_normalize_subject_strips_prefixes_and_tags():
    assert normalize_subject("RE: Fwd: [Ticket] Order  late") == "order late"
    assert normalize_subject("  AW: SV: WG: [a][b b] Hello\tWorld ") == "hello world"
    assert normalize_subject("Re:") == ""
    assert normalize_subject(None) == ""
    assert normalize_subject("Reply about order") == "reply about order"


def test_candidate_ids_order_and_dedupe():
    p = parsed(in_reply_to="c", references=("a", "b", "c"))
    assert candidate_ids(p) == ["c", "b", "a"]
    assert candidate_ids(parsed()) == []


async def test_thread_by_in_reply_to(session, org_a):
    cust = await _customer(session, org_a)
    one, two = await _conv(session, org_a, cust), await _conv(session, org_a, cust)
    await _mail(session, org_a, one, "a@x")
    await _mail(session, org_a, two, "b@x")
    found = await find_thread(session, org_a.id, parsed(in_reply_to="b@x", references=("a@x",), subject=""), cust.id, NOW)
    assert found.id == two.id


async def test_thread_by_references_newest_first(session, org_a):
    cust = await _customer(session, org_a)
    one, two = await _conv(session, org_a, cust), await _conv(session, org_a, cust)
    await _mail(session, org_a, one, "a@x")
    await _mail(session, org_a, two, "b@x")
    found = await find_thread(session, org_a.id, parsed(in_reply_to="zzz@x", references=("a@x", "b@x")), cust.id, NOW)
    assert found.id == two.id


async def test_thread_by_our_outbound_id(session, org_a):
    cust = await _customer(session, org_a)
    chat = await _conv(session, org_a, cust, channel="chat", subject=None)
    await _mail(session, org_a, chat, "out@acme.example", "outbound")
    found = await find_thread(session, org_a.id, parsed(in_reply_to="out@acme.example", subject="Whatever"), cust.id, NOW)
    assert found.id == chat.id


async def test_header_match_returns_closed_conversation(session, org_a):
    cust = await _customer(session, org_a)
    closed = await _conv(session, org_a, cust, status="closed")
    await _mail(session, org_a, closed, "a@x")
    assert (await find_thread(session, org_a.id, parsed(in_reply_to="a@x"), cust.id, NOW)).id == closed.id


async def test_subject_fallback_same_customer_only(session, org_a):
    cust = await _customer(session, org_a)
    other = Customer(organisation_id=org_a.id, email="d@example.com")
    session.add(other)
    await session.flush()
    conv = await _conv(session, org_a, cust)
    p = parsed(subject="RE: order  LATE")
    assert (await find_thread(session, org_a.id, p, cust.id, NOW)).id == conv.id
    assert await find_thread(session, org_a.id, p, other.id, NOW) is None
    assert await find_thread(session, org_a.id, parsed(subject="Re: [x]"), cust.id, NOW) is None


async def test_subject_fallback_ignores_closed_and_older_than_30_days(session, org_a):
    cust = await _customer(session, org_a)
    await _conv(session, org_a, cust, status="closed")
    await _conv(session, org_a, cust, last=NOW - timedelta(days=31))
    assert await find_thread(session, org_a.id, parsed(subject="Order late"), cust.id, NOW) is None
    fresh = await _conv(session, org_a, cust, status="resolved", last=NOW - timedelta(days=29))
    assert (await find_thread(session, org_a.id, parsed(subject="Order late"), cust.id, NOW)).id == fresh.id


async def test_thread_lookup_tenant_scoped(session, org_a, org_b):
    cust_a, cust_b = await _customer(session, org_a), await _customer(session, org_b)
    conv_b = await _conv(session, org_b, cust_b)
    await _mail(session, org_b, conv_b, "same@x")
    p = parsed(in_reply_to="same@x", references=("same@x",), subject="Order late")
    assert await find_thread(session, org_a.id, p, cust_a.id, NOW) is None
    # nor via the subject fallback, even with org B's customer id
    assert await find_thread(session, org_a.id, p, cust_b.id, NOW) is None


async def test_message_id_match_is_case_sensitive(session, org_a):
    cust = await _customer(session, org_a)
    conv = await _conv(session, org_a, cust, subject="Unrelated")
    await _mail(session, org_a, conv, "AbC@x")
    assert await find_thread(session, org_a.id, parsed(in_reply_to="abc@x", subject="Nope"), cust.id, NOW) is None
    assert (await find_thread(session, org_a.id, parsed(in_reply_to="AbC@x"), cust.id, NOW)).id == conv.id


async def test_header_candidates_capped_at_21(session, org_a):
    cust = await _customer(session, org_a)
    conv = await _conv(session, org_a, cust, subject="Unrelated")
    await _mail(session, org_a, conv, "old@x")
    refs = ("old@x",) + tuple(f"r{i}@x" for i in range(21))  # newest first puts old@x at position 23
    assert await find_thread(session, org_a.id, parsed(references=refs, subject="Nope"), cust.id, NOW) is None
