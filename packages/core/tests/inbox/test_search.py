"""Inbox search: message text plus customer name/email, parameterised, org-scoped, never logged (Task 5)."""

import logging

import pytest
from structlog.testing import capture_logs

from supportcore.inbox.service import InvalidSearchQueryError


async def _found(service, q, **kw):
    return [s.id for s in (await service.list("all", q=q, **kw)).items]


async def test_search_message_text_and_customer_email(service, build):
    refund = await build.conversation(None, [("customer", "I want a refund for my order")], status="open")
    await build.conversation(None, [("customer", "How do I reset my password?")], status="open")
    cara = await build.customer(name="Cara Jones", email="cara.jones@example.com")
    by_email = await build.conversation(cara, [("customer", "hi")], status="open")

    assert await _found(service, "refund") == [refund.id]
    assert await _found(service, "refunds ORDER") == []  # 'simple' config: no stemming, no partial words
    assert await _found(service, '"refund for my"') == [refund.id]
    assert await _found(service, "cara.jones@example.com") == [by_email.id]
    assert await _found(service, "CARA.JONES@EXAMPLE.COM") == [by_email.id]
    assert await _found(service, "cara") == [by_email.id]  # substring of the email and the name
    assert await _found(service, "cara jones") == [by_email.id]  # trigram match on the name
    assert await _found(service, "zebra giraffe") == []
    # Search combines with the other filters and keeps newest-first order.
    both = await build.conversation(cara, [("customer", "refund again")], status="open", priority="high")
    assert await _found(service, "refund") == [both.id, refund.id]
    assert await _found(service, "refund", priority="high") == [both.id]


async def test_search_literal_special_characters(service, build):
    plain = await build.conversation(None, [("customer", "just a normal question")], status="open")
    injected = await build.conversation(None, [("customer", "refund' OR 1=1")], status="open")
    tag = await build.conversation(None, [("customer", "<script>alert(1)</script> hello")], status="open")
    percent = await build.conversation(None, [("customer", "100%_done")], status="open")

    for q in ("refund' OR 1=1", "<script>", "%_", "'; DROP TABLE messages; --", "\\\\", "zq:*&|!()", '"unbalanced'):
        found = await _found(service, q)  # no error
        assert plain.id not in found, q
    assert await _found(service, "refund' OR 1=1") == [injected.id]
    assert await _found(service, "hello") == [tag.id]  # the parser treats <script>..</script> as markup and drops it: no error
    assert await _found(service, "%_") == []
    assert percent.id not in await _found(service, "%_")
    literal = await build.conversation(await build.customer(name="50%_off fan"), [("customer", "hi")], status="open")
    await build.conversation(await build.customer(name="500 offers"), [("customer", "hi")], status="open")
    assert await _found(service, "%_") == [literal.id]  # wildcards match only the literal characters


async def test_search_org_scoped(service, build, make_service, org_b, session):
    mine = await build.conversation(None, [("customer", "refund please")], status="open")
    other = type(build)(session, org_b)
    theirs = await other.conversation(
        await other.customer(email="refund@x.com"), [("customer", "refund please")], status="open"
    )
    assert await _found(service, "refund") == [mine.id]
    assert [s.id for s in (await make_service(org_b).list("all", q="refund")).items] == [theirs.id]


async def test_search_excludes_test_by_default(service, build):
    real = await build.conversation(None, [("customer", "refund")], status="open")
    test = await build.conversation(None, [("customer", "refund")], status="open", is_test=True)
    assert await _found(service, "refund") == [real.id]
    assert await _found(service, "refund", include_test=True) == [test.id, real.id]


async def test_search_query_length_limits(service):
    for q in ("", " ", "a", " a "):
        with pytest.raises(InvalidSearchQueryError):
            await service.list("all", q=q)
    with pytest.raises(InvalidSearchQueryError):
        await service.list("all", q="x" * 201)
    await service.list("all", q="ab")
    await service.list("all", q="x" * 200)


async def test_search_query_not_logged(service, build, caplog, capsys):
    await build.conversation(await build.customer(email="secret.person@example.com"), [("customer", "hi")], status="open")
    query = "secret.person@example.com topsecret-needle"
    with caplog.at_level(logging.DEBUG), capture_logs() as structured:
        await service.list("all", q=query)
        await service.list("all", q="x" * 1 + "topsecret-needle")
    out = capsys.readouterr()
    haystack = repr(structured) + caplog.text + out.out + out.err
    assert "topsecret-needle" not in haystack and "secret.person" not in haystack
