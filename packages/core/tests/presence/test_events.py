import asyncio
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from structlog.testing import capture_logs

from supportcore.db.models import Membership
from supportcore.presence.events import set_agent_status
from supportcore.realtime.spawn import drain_pending, spawn_publish


class RecordingPubSub:
    def __init__(self, delay: float = 0.0, fail: bool = False) -> None:
        self.published: list[tuple[str, dict]] = []
        self.delay, self.fail = delay, fail

    async def publish(self, channel, message):
        await asyncio.sleep(self.delay)
        if self.fail:
            raise ConnectionError("redis down with secret-payload")
        self.published.append((channel, message))


@pytest.fixture
def commit_now(monkeypatch):
    """after_commit runs its callback right away; flush is a no-op."""
    import supportcore.presence.events as events

    monkeypatch.setattr(events, "after_commit", lambda session, cb: cb())
    return SimpleNamespace(flush=lambda: asyncio.sleep(0))


def _membership():
    return SimpleNamespace(id=uuid4(), organisation_id=uuid4(), agent_status="offline")


async def test_event_carries_increasing_at(commit_now):
    pubsub, m = RecordingPubSub(), _membership()
    before = int(time.time() * 1000)
    await set_agent_status(commit_now, m, "online", pubsub)
    await set_agent_status(commit_now, m, "away", pubsub)
    await drain_pending()
    first, second = (msg for _, msg in pubsub.published)
    assert first["at"] >= before and second["at"] > first["at"]
    assert set(second) == {"type", "membership_id", "agent_status", "at"}


async def test_publish_failure_is_logged_without_payload(commit_now):
    pubsub, m = RecordingPubSub(fail=True), _membership()
    with capture_logs() as logs:
        await set_agent_status(commit_now, m, "online", pubsub)
        await drain_pending()
        await asyncio.sleep(0)
    failed = [entry for entry in logs if entry["event"] == "presence_publish_failed"]
    assert len(failed) == 1
    assert failed[0]["channel"] == f"inbox:{m.organisation_id.hex}"
    assert "secret-payload" not in str(failed[0]) and "membership_id" not in failed[0]


async def test_drain_awaits_slow_publish():
    pubsub = RecordingPubSub(delay=0.1)
    spawn_publish(pubsub.publish("c", {"x": 1}), failure_event="e")
    assert pubsub.published == []
    await drain_pending(timeout=2.0)
    assert pubsub.published == [("c", {"x": 1})]


async def test_drain_cancels_after_timeout():
    pubsub = RecordingPubSub(delay=5)
    spawn_publish(pubsub.publish("c", {}), failure_event="e")
    await drain_pending(timeout=0.05)
    assert pubsub.published == []
    await drain_pending(timeout=0.05)  # nothing left pending


async def test_spawn_without_running_loop_closes_coroutine_and_logs():
    pubsub = RecordingPubSub()

    def call_sync():
        with capture_logs() as logs:
            spawn_publish(pubsub.publish("c", {"x": 1}), failure_event="presence_publish_failed", channel="c")
        return logs

    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # a never-awaited coroutine would raise RuntimeWarning
        logs = await asyncio.get_running_loop().run_in_executor(None, call_sync)
        import gc

        gc.collect()
    assert [entry["event"] for entry in logs] == ["presence_publish_failed"]
    assert pubsub.published == []


def test_drain_ignores_tasks_of_another_loop():
    pubsub = RecordingPubSub(delay=0.05)

    async def spawn_in_loop_one():
        spawn_publish(pubsub.publish("one", {}), failure_event="e")

    loop_one = asyncio.new_event_loop()
    loop_one.run_until_complete(spawn_in_loop_one())  # task left pending on loop_one

    async def drain_in_loop_two():
        await drain_pending(timeout=0.5)  # must not touch loop_one's task

    asyncio.run(drain_in_loop_two())
    loop_one.run_until_complete(drain_pending(timeout=1))
    assert pubsub.published == [("one", {})]
    loop_one.close()
