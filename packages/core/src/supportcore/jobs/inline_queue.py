"""InlineJobQueue: runs jobs as asyncio tasks on the running loop of this process (dev and tests).

No durability: jobs still pending when the process exits are lost. Tests call `await queue.drain()`.
"""

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable

from supportcore.jobs.ports import JobRuntime
from supportcore.observability.logging import get_logger

logger = get_logger(__name__)

MAX_KEPT_ERRORS = 100

Dispatch = Callable[[JobRuntime, str, dict], Awaitable[None]]


class InlineJobQueue:
    def __init__(self, runtime_factory: Callable[[], JobRuntime], dispatch: Dispatch | None = None) -> None:
        """`runtime_factory` is called when a job runs (not now): the runtime holds the adapters that
        hold this queue, so it can only exist after them."""
        self._runtime_factory = runtime_factory
        self._dispatch = dispatch
        self._tasks: set[asyncio.Task] = set()
        self.delayed: list[tuple[str, dict, float]] = []
        self._errors: deque[BaseException] = deque(maxlen=MAX_KEPT_ERRORS)  # nobody drains in the API

    def enqueue(self, name: str, payload: dict, *, countdown: float | None = None) -> None:
        loop = asyncio.get_running_loop()  # RuntimeError outside a loop: inline jobs need one
        if countdown is not None and countdown > 0:
            # Delayed jobs are kept in `delayed` and started by a timer; drain() does not wait for them.
            self.delayed.append((name, payload, countdown))
            loop.call_later(countdown, self.enqueue, name, payload)
            return
        task = loop.create_task(self._run(name, payload), name=f"job:{name}")
        self._tasks.add(task)
        task.add_done_callback(self._forget)

    def _forget(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)

    async def _run(self, name: str, payload: dict) -> None:
        dispatch = self._dispatch
        if dispatch is None:
            from supportcore.knowledge.jobs import dispatch  # the domain's job table
        try:
            await dispatch(self._runtime_factory(), name, payload)
        except Exception as exc:
            logger.exception("inline_job_failed", job_name=name)
            self._errors.append(exc)

    async def drain(self) -> None:
        """Waits for every pending job, including jobs those jobs enqueue; then re-raises the first
        error a job raised since the last drain (handlers normally record failures instead)."""
        while True:
            # Prune finished tasks here as well as in the done callback, so drain never re-awaits a
            # finished task (a plain `while self._tasks: await gather(...)` was seen to spin).
            self._tasks = {t for t in self._tasks if not t.done()}
            if not self._tasks:
                break
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._errors:
            first = self._errors[0]
            self._errors.clear()
            raise first
