"""CeleryJobQueue: sends jobs to the supportworker Celery app by task name (no import of the worker).

Trace context travels in the message headers when the Celery instrumentation is active in the
sending process (supportcore.observability.tracing.instrument_celery).
"""

from typing import Any, Protocol

RUN_JOB_TASK = "supportworker.run_job"

# Celery config for every app that sends jobs (the API's producer and the worker): a slow or
# black-holed broker fails the enqueue within seconds instead of stalling the request (Celery's
# defaults retry for minutes). The commit has happened by then; recovery re-sends lost jobs.
PRODUCER_CONFIG = {
    "broker_connection_timeout": 5,
    "broker_transport_options": {"socket_connect_timeout": 5, "socket_timeout": 5},
    "task_publish_retry": True,
    "task_publish_retry_policy": {"max_retries": 2, "interval_start": 0.2, "interval_step": 0.4, "interval_max": 1},
}


class _TaskSender(Protocol):
    def send_task(self, name: str, args: Any = None, **options: Any) -> Any: ...


class CeleryJobQueue:
    def __init__(self, celery_app: _TaskSender) -> None:
        self._app = celery_app

    def enqueue(self, name: str, payload: dict, *, countdown: float | None = None) -> None:
        if countdown is None:
            self._app.send_task(RUN_JOB_TASK, args=[name, payload])
        else:
            self._app.send_task(RUN_JOB_TASK, args=[name, payload], countdown=countdown)
