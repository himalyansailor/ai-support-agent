import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from supportcore.events import DomainEvent, EventBus


@dataclass(frozen=True)
class ThingHappened(DomainEvent):
    thing: str = ""


@dataclass(frozen=True)
class OtherHappened(DomainEvent):
    pass


def test_domain_event_defaults():
    ev = DomainEvent()
    assert ev.organisation_id is None and ev.actor_user_id is None
    assert ev.occurred_at.tzinfo is UTC
    assert abs((datetime.now(UTC) - ev.occurred_at).total_seconds()) < 5


async def test_event_bus_dispatches_to_subscribers_in_order():
    bus = EventBus()
    calls = []

    async def first(e):
        calls.append(("first", e.thing))

    async def second(e):
        calls.append(("second", e.thing))

    async def unrelated(e):
        calls.append(("unrelated", None))

    bus.subscribe(ThingHappened, first)
    bus.subscribe(ThingHappened, second)
    bus.subscribe(OtherHappened, unrelated)
    await bus.publish(ThingHappened(organisation_id=uuid4(), actor_user_id=None, thing="t"))
    assert calls == [("first", "t"), ("second", "t")]


async def test_event_bus_handler_error_does_not_block_others(caplog):
    bus = EventBus()
    calls = []

    async def boom(e):
        raise RuntimeError("nope")

    async def ok(e):
        calls.append("ok")

    bus.subscribe(ThingHappened, boom)
    bus.subscribe(ThingHappened, ok)
    with caplog.at_level(logging.ERROR):
        await bus.publish(ThingHappened())
    assert calls == ["ok"]
    assert any("ThingHappened" in r.getMessage() for r in caplog.records)


async def test_event_bus_base_class_subscriber_receives_subclass():
    bus = EventBus()
    seen = []

    async def on_any(e):
        seen.append(type(e).__name__)

    async def on_specific(e):
        seen.append("specific")

    bus.subscribe(DomainEvent, on_any)
    bus.subscribe(ThingHappened, on_specific)
    await bus.publish(ThingHappened())
    await bus.publish(OtherHappened())
    assert seen == ["ThingHappened", "specific", "OtherHappened"]


async def test_propagating_handler_error_is_raised_after_all_handlers_run():
    import pytest

    bus = EventBus()
    calls = []

    async def audit(e):
        calls.append("audit")
        raise RuntimeError("audit failed")

    async def later(e):
        calls.append("later")

    bus.subscribe(ThingHappened, audit, propagate=True)
    bus.subscribe(ThingHappened, later)
    with pytest.raises(RuntimeError, match="audit failed"):
        await bus.publish(ThingHappened())
    assert calls == ["audit", "later"]


async def test_non_propagating_error_still_swallowed_when_mixed():
    bus = EventBus()
    calls = []

    async def flaky(e):
        raise RuntimeError("ignored")

    async def audit(e):
        calls.append("audit")

    bus.subscribe(ThingHappened, flaky)
    bus.subscribe(ThingHappened, audit, propagate=True)
    await bus.publish(ThingHappened())
    assert calls == ["audit"]
