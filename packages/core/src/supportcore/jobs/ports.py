"""Background job queue port and the runtime jobs run with.

Adapters: InlineJobQueue (runs jobs as asyncio tasks in this process) and CeleryJobQueue (sends
them to the supportworker app), wired in supportcore.wiring by `settings.job_queue`. Job names and
payloads are listed in supportcore.knowledge.jobs.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from supportcore.config import Settings
from supportcore.events import EventBus

if TYPE_CHECKING:
    from supportcore.wiring import Adapters


# A job may run at most this long (the Celery worker's hard time limit). A job still `running` after
# it was started longer ago than this was interrupted; recover_stale_jobs() fails it.
JOB_TIME_LIMIT_SECONDS = 1800

# Exception types a queue raises into a job that ran out of time (the worker registers Celery's
# SoftTimeLimitExceeded), so job code can report "timed out" without importing the queue library.
_TIMEOUT_ERRORS: list[type[BaseException]] = []


def register_job_timeout_error(error: type[BaseException]) -> None:
    if error not in _TIMEOUT_ERRORS:
        _TIMEOUT_ERRORS.append(error)


def is_job_timeout(exc: BaseException) -> bool:
    return isinstance(exc, tuple(_TIMEOUT_ERRORS))


class JobQueue(Protocol):
    def enqueue(self, name: str, payload: dict, *, countdown: float | None = None) -> None:
        """Schedules job `name`, to start no sooner than `countdown` seconds from now if given. Sync;
        call it from `after_commit(session, ...)` or with no transaction open, never inside one.
        `payload` must be JSON-serialisable (UUIDs as str)."""
        ...


class UnknownJobError(KeyError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(name)

    def __str__(self) -> str:
        return f"Unknown job {self.name!r}"


@dataclass
class JobRuntime:
    """What a job handler needs. Each job opens its own sessions (UnitOfWork) from `session_factory`.

    Built by supportworker.runtime.build_runtime() in the worker, and by the API container for
    SUPPORT_JOB_QUEUE=inline."""

    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    adapters: "Adapters"
    events: EventBus
