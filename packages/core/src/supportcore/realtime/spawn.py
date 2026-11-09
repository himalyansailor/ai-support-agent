"""Fire-and-forget publishes started from `after_commit` callbacks (which are sync).

`spawn_publish` keeps a strong reference to the task, logs a failure (`presence_publish_failed`-style, with the
caller's non-sensitive log fields only, never the payload) and `drain_pending` lets shutdown await what is in flight.
"""

import asyncio
import weakref
from collections.abc import Coroutine
from typing import Any

import structlog

_log = structlog.get_logger()
# Pending tasks per event loop: a task belongs to one loop and can only be awaited from it.
_PENDING: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, set[asyncio.Task[Any]]] = weakref.WeakKeyDictionary()


def spawn_publish(coro: Coroutine[Any, Any, Any], *, failure_event: str, **log_fields: Any) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # called outside an event loop: nothing can run it
        coro.close()
        _log.warning(failure_event, error_type="NoRunningLoop", **log_fields)
        return
    task = loop.create_task(coro)
    pending = _PENDING.setdefault(loop, set())
    pending.add(task)

    def _done(t: asyncio.Task[Any]) -> None:
        pending.discard(t)
        if not t.cancelled() and t.exception() is not None:
            _log.warning(failure_event, error_type=type(t.exception()).__name__, **log_fields)

    task.add_done_callback(_done)


async def drain_pending(timeout: float = 2.0) -> None:
    """Awaits outstanding spawned publishes for up to `timeout` seconds; the rest are cancelled."""
    pending = set(_PENDING.get(asyncio.get_running_loop(), ()))
    if not pending:
        return
    _, still = await asyncio.wait(pending, timeout=timeout)
    for task in still:
        task.cancel()
    if still:
        await asyncio.gather(*still, return_exceptions=True)
