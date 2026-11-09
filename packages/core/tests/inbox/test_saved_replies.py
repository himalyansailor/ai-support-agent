import pytest

from supportcore.inbox.saved_replies import (
    InvalidSavedReplyError,
    SavedReplyService,
    SavedReplyShortcutTakenError,
    TooManySavedRepliesError,
    render_saved_reply,
)


def test_render_placeholders():
    out = render_saved_reply("Hi {customer_name}, {agent_name} here. {customer_name}!", customer_name="Bo", agent_name="Ann")
    assert out == "Hi Bo, Ann here. Bo!"
    assert render_saved_reply("Hi {customer_name}{agent_name}.", customer_name=None, agent_name=None) == "Hi ."


def test_unknown_placeholder_kept():
    body = "{0} {__class__} {customer} {customer_name.x} {{agent_name}} {}"
    out = render_saved_reply(body, customer_name="Bo", agent_name="Ann")
    assert out == "{0} {__class__} {customer} {customer_name.x} {Ann} {}"
    # Values are inserted literally and never re-expanded.
    assert render_saved_reply("{customer_name}", customer_name="{agent_name}", agent_name="X") == "{agent_name}"


async def test_shortcut_unique_and_format(session, org_a, org_b):
    svc = SavedReplyService(session, org_a.id)
    r = await svc.create("Greeting", "Hello", "hi_1-x")
    assert r.shortcut == "hi_1-x"
    with pytest.raises(SavedReplyShortcutTakenError):
        await svc.create("Other", "Body", "hi_1-x")
    # Same shortcut in another org is fine; replies without a shortcut do not collide.
    await SavedReplyService(session, org_b.id).create("Greeting", "Hello", "hi_1-x")
    a = await svc.create("A", "b", None)
    b = await svc.create("B", "b", "")
    assert a.shortcut is None and b.shortcut is None
    for bad in ("Hi", "has space", "x" * 31, "é", "a/b"):
        with pytest.raises(InvalidSavedReplyError):
            await svc.create("T", "B", bad)
    other_id = (await svc.create("C", "b", "c")).id
    with pytest.raises(SavedReplyShortcutTakenError):
        await svc.update(other_id, shortcut="hi_1-x")
    assert (await svc.update(other_id, shortcut="c")).shortcut == "c"  # unchanged is fine
    assert (await svc.update(other_id, shortcut=None)).shortcut is None
    for title, body in (("", "b"), ("  ", "b"), ("t" * 101, "b"), ("t", ""), ("t", "b" * 5001)):
        with pytest.raises(InvalidSavedReplyError):
            await svc.create(title, body, None)
    assert (await svc.create("t" * 100, "b" * 5000, None)).title == "t" * 100


async def test_list_search_escapes_like(session, org_a):
    svc = SavedReplyService(session, org_a.id)
    await svc.create("Refund policy", "b", "refund")
    await svc.create("100% sure", "b", "sure_1")
    await svc.create("Shipping", "b", None)
    assert [r.title for r in await svc.list("REFUND")] == ["Refund policy"]
    assert [r.title for r in await svc.list("sure_")] == ["100% sure"]
    assert [r.title for r in await svc.list("%")] == ["100% sure"]
    assert [r.title for r in await svc.list("_")] == ["100% sure"]
    assert await svc.list("\\") == []
    assert len(await svc.list(None)) == 3


async def test_limit_500(session, org_a):
    from supportcore.db.models import SavedReply

    session.add_all([SavedReply(organisation_id=org_a.id, title=f"t{i}", body="b") for i in range(500)])
    await session.flush()
    svc = SavedReplyService(session, org_a.id)
    with pytest.raises(TooManySavedRepliesError):
        await svc.create("one more", "b", None)
