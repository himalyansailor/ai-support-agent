"""Per-process job runtime for the Celery worker.

Each pool process owns one event loop and one JobRuntime (engine, adapters), created lazily on its
first task, so nothing loop-bound (asyncpg connections) is shared across a fork or across loops.
Every task runs to completion on that loop (`run_until_complete`); with the prefork pool a process
runs one task at a time. The loop and runtime are closed at worker_process_shutdown.
"""

import asyncio
import os
import threading
import time
from dataclasses import dataclass

from supportcore.audit.subscribers import register_audit_subscribers
from supportcore.config import Settings, get_settings
from supportcore.db.session import create_engine, get_current_session, session_factory
from supportcore.events import EventBus
from supportcore.jobs.ports import JobRuntime
from supportcore.jobs.ports import is_job_timeout
from supportcore.knowledge.indexing import TIMED_OUT
# RECOVERY_INTERVAL_SECONDS: celery beat's schedule, and per pool process (on its next job after it).
from supportcore.knowledge.jobs import RECOVERY_INTERVAL_SECONDS, dispatch, fail_job, recover_stale_jobs
from supportcore.observability.logging import get_logger
from supportcore.observability.tracing import configure_tracing
from supportcore.realtime.spawn import drain_pending
from supportcore.wiring import build_adapters

SERVICE_NAME = "support-worker"

logger = get_logger(__name__)


def worker_settings(settings: Settings | None = None) -> Settings:
    """The environment's settings, with the worker's own service name (logs, traces). Jobs that jobs
    enqueue always go through Celery: SUPPORT_JOB_QUEUE=inline is for the API only."""
    return (settings or get_settings()).model_copy(update={"service_name": SERVICE_NAME, "job_queue": "celery"})


def build_runtime(settings: Settings | None = None) -> JobRuntime:
    """Engine, session factory, adapters and event bus for jobs in this process. The adapters own the
    engine, so `runtime.adapters.aclose()` disposes it."""
    settings = worker_settings(settings)
    engine = create_engine(settings)
    configure_tracing(settings, engine)
    events = EventBus()
    register_audit_subscribers(events, get_current_session)
    holder: dict[str, JobRuntime] = {}
    adapters = build_adapters(settings, engine=engine, runtime_factory=lambda: holder["runtime"])
    adapters.owned_engine = engine
    holder["runtime"] = JobRuntime(
        settings=settings, session_factory=session_factory(engine), adapters=adapters, events=events
    )
    return holder["runtime"]


@dataclass
class _ProcessState:
    pid: int
    loop: asyncio.AbstractEventLoop
    runtime: JobRuntime | None = None
    last_recovery: float | None = None  # time.monotonic() of the last stale-job recovery


_state: _ProcessState | None = None
_lock = threading.Lock()  # a thread pool would otherwise drive one loop from two threads


def _current_state() -> _ProcessState:
    global _state
    if _state is None or _state.pid != os.getpid():  # first task, or a forked child of a used parent
        _state = _ProcessState(pid=os.getpid(), loop=asyncio.new_event_loop())
    return _state


def run_job_sync(name: str, payload: dict) -> None:
    """Runs one job on this process's loop and runtime (the Celery task body).

    Before a job, at most every RECOVERY_INTERVAL_SECONDS per process, jobs a dead worker left
    `running` are failed (recover_stale_jobs). If the time limit
    interrupts the job outside its own error handling (the exception surfaces from the loop), the job
    is cancelled, recorded as timed out, and the exception re-raised for Celery."""
    with _lock:
        state = _ready_state()
        if state.last_recovery is None or time.monotonic() - state.last_recovery >= RECOVERY_INTERVAL_SECONDS:
            _recover(state)
        task = state.loop.create_task(_run_and_drain(state.runtime, name, payload))
        try:
            state.loop.run_until_complete(task)
        except BaseException as exc:
            if not is_job_timeout(exc):
                raise
            task.cancel()
            state.loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
            state.loop.run_until_complete(fail_job(state.runtime, name, payload, TIMED_OUT))
            raise


async def _run_and_drain(runtime: JobRuntime, name: str, payload: dict) -> None:
    """The job, then the publishes its commits spawned: the loop stops between jobs, so they would wait for the
    next job (or be lost at exit). A job that raised drains too."""
    try:
        await dispatch(runtime, name, payload)
    finally:
        await drain_pending(timeout=2.0)


def _ready_state() -> _ProcessState:
    state = _current_state()
    if state.runtime is None:
        state.runtime = build_runtime()
    return state


def _recover(state: _ProcessState) -> None:
    state.last_recovery = time.monotonic()
    try:
        state.loop.run_until_complete(recover_stale_jobs(state.runtime))
    except Exception as exc:
        logger.warning("worker_stale_job_recovery_failed", error_type=type(exc).__name__)


def run_recovery_sync() -> None:
    """Stale-job recovery on its own (the `supportworker.recover_stale_jobs` task, e.g. for a beat
    schedule)."""
    with _lock:
        _recover(_ready_state())


def shutdown_process_state() -> None:
    """Disposes the runtime (engine, clients) on its own loop, then closes the loop."""
    global _state
    with _lock:
        state, _state = _state, None
        if state is None or state.pid != os.getpid():
            return
        try:
            if state.runtime is not None:
                state.loop.run_until_complete(drain_pending(timeout=2.0))
                state.loop.run_until_complete(state.runtime.adapters.aclose())
        except Exception as exc:
            logger.warning("worker_runtime_close_failed", error_type=type(exc).__name__)
        finally:
            state.loop.close()


def reset_process_state() -> None:
    """Forgets the loop and runtime without disposing anything (tests)."""
    global _state
    with _lock:
        if _state is not None and not _state.loop.is_closed():
            _state.loop.close()
        _state = None
