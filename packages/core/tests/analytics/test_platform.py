"""The cross-tenant platform numbers (grouped rollup aggregate + per-org live hours) equal what each
organisation's own dashboard query (`queries.overview`) gives, including cost semantics."""

from datetime import timedelta

from supportcore.analytics import platform, queries
from .test_queries import NOW, _org, at, turn


async def _expected(factory, org_id):
    today = NOW.date()
    rng7 = queries.resolve_range("UTC", today - timedelta(days=6), today, "day", now=NOW)
    rng30 = queries.resolve_range("UTC", today - timedelta(days=29), today, "day", now=NOW)
    async with factory() as s:
        t7 = (await queries.overview(s, org_id, rng7, now=NOW))["totals"]
        t30 = (await queries.overview(s, org_id, rng30, now=NOW))["totals"]
    return t7, t30


async def test_org_windows_equal_per_org_overview(session_factory):
    a = await _org(session_factory, "Alpha")
    b = await _org(session_factory, "Beta")
    quiet = await _org(session_factory, "Quiet")  # no events: absent from the result, zeros

    # Alpha: a priced turn 15 days ago (stored), a stored hour today, the open hour live (priced + unpriced);
    # watermark through the current hour.
    for when, cost, stored in ((at(9, 5, day=5), 100, True), (at(10, 5), 250, True), (at(12, 5), 40, False)):
        c = await a.conversation()
        await a.events(c, ("conversation_created", when, {}),
                       ("bot_turn", when, turn(provider="p", model="m", input_tokens=10, output_tokens=5, cost_micros=cost)))
        if stored:
            await a.roll(when)
    c = await a.conversation()
    await a.events(c, ("conversation_created", at(12, 7), {}),
                   ("bot_turn", at(12, 7), turn(provider="p", model="m", input_tokens=1, output_tokens=1)))
    await a.watermark(at(12))

    # Beta: only unpriced turns; its watermark lags (hour 10 and 11 were never stored: computed live).
    for when in (at(8, 5), at(10, 20), at(11, 5)):
        c = await b.conversation()
        await b.events(c, ("conversation_created", when, {}),
                       ("bot_turn", when, turn(provider="p", model="m", input_tokens=7, output_tokens=3)))
    await b.roll(at(8, 5))
    await b.watermark(at(10))

    async with session_factory() as s:
        activity = await platform.last_activity(s)
        got = await platform.org_windows(s, activity, now=NOW)

    assert quiet.org_id not in got
    for log in (a, b):
        t7, t30 = await _expected(session_factory, log.org_id)
        w = got[log.org_id]
        assert (w.conversations_7d, w.conversations_30d) == (t7["conversations_started"], t30["conversations_started"])
        assert w.tokens_30d == t30["input_tokens"] + t30["output_tokens"]
        assert w.cost_micros_30d == t30["cost_micros"]
        assert w.unpriced_turns_30d == t30["cost_unpriced_turns"]
    # the fixture is not trivial
    assert got[a.org_id].cost_micros_30d == 390 and got[a.org_id].unpriced_turns_30d == 1
    assert got[a.org_id].conversations_30d == 4 and got[a.org_id].conversations_7d == 3
    assert got[b.org_id].cost_micros_30d is None and got[b.org_id].unpriced_turns_30d == 3
    assert got[b.org_id].conversations_30d == 3


async def test_last_activity_leaves_out_test_conversations(session_factory):
    only_test = await _org(session_factory, "Preview")
    mixed = await _org(session_factory, "Mixed")
    c = await only_test.conversation(is_test=True)
    await only_test.events(c, ("conversation_created", at(11, 5), {}))
    real = await mixed.conversation()
    await mixed.events(real, ("conversation_created", at(9, 5), {}))
    preview = await mixed.conversation(is_test=True)
    await mixed.events(preview, ("conversation_created", at(11, 30), {}))

    async with session_factory() as s:
        activity = await platform.last_activity(s)

    assert only_test.org_id not in activity  # a widget preview is not activity
    assert activity[mixed.org_id] == at(9, 5)  # the latest non-test event


async def test_rollup_states_match_the_dashboard_rule(session_factory):
    behind = await _org(session_factory, "Behind")
    await behind.watermark(at(1))
    current = await _org(session_factory, "Current")
    await current.watermark(at(12))

    async with session_factory() as s:
        states = await platform.rollup_states(s, now=NOW)
        for log in (behind, current):
            assert states[log.org_id] == await queries.read_rollup_state(s, log.org_id, NOW)

    assert states[behind.org_id].lagging and not states[current.org_id].lagging
