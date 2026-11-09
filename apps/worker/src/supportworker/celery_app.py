"""Celery app of the worker: `celery -A supportworker.celery_app worker` (see `make dev-worker`).

One task, `supportworker.run_job(name, payload)`, runs a job from supportcore.knowledge.jobs on the
pool process's runtime (supportworker.runtime). Messages are acked after the task finishes
(task_acks_late, task_reject_on_worker_lost) and each process prefetches one, so a crashed worker's job
is redelivered; jobs are idempotent. Stale-job recovery (jobs left `running` by a worker that died
are failed, jobs whose message was lost are sent again) runs in each pool process every few minutes
and, with `make dev-beat` (celery beat), every RECOVERY_INTERVAL_SECONDS on its own schedule. Beat
also closes idle conversations every IDLE_CLOSE_INTERVAL_SECONDS (the `close_idle_conversations` job) and polls the
email mailboxes every MAIL_SYNC_INTERVAL_SECONDS (`sync_mailboxes`, which enqueues one `sync_mailbox` job each), and
finalises stale voice calls every STALE_CALL_SWEEP_INTERVAL_SECONDS (`finalise_stale_calls`), and purges expired call recordings daily at 03:30 UTC (`purge_call_recordings`).
Billing: beat sends overage usage to the provider every REPORT_USAGE_INTERVAL_SECONDS (`report_usage`), resets the usage
counters to the ledger every RECONCILE_INTERVAL_SECONDS (`reconcile_usage_counters`) and emails billing warnings every
BILLING_ALERTS_INTERVAL_SECONDS (`billing_alerts`). Logs are JSON (supportcore logging); traces and metrics are set up per pool process.
"""

import copy
import os
import sys

if sys.platform == "darwin":
    # billiard starts pool processes with spawn on macOS (fork is unsafe there). Celery's prefork pool
    # then needs this flag to set up its task tracer in each child; without it every task fails with
    # "not enough values to unpack". Must be set before celery is imported.
    os.environ.setdefault("FORKED_BY_MULTIPROCESSING", "1")

from celery import Celery, signals  # noqa: E402
from celery.schedules import crontab  # noqa: E402
from celery.exceptions import SoftTimeLimitExceeded  # noqa: E402

from supportcore.analytics.jobs import ROLLUP_INTERVAL_SECONDS  # noqa: E402
from supportcore.billing.alerts import BILLING_ALERTS_INTERVAL_SECONDS  # noqa: E402
from supportcore.billing.reporting import REPORT_USAGE_INTERVAL_SECONDS  # noqa: E402
from supportcore.billing.usage import RECONCILE_INTERVAL_SECONDS  # noqa: E402
from supportcore.config import Settings  # noqa: E402
from supportcore.conversations.jobs import IDLE_CLOSE_INTERVAL_SECONDS  # noqa: E402
from supportcore.inbox.jobs import SLA_CHECK_INTERVAL_SECONDS  # noqa: E402
from supportcore.jobs.celery_queue import PRODUCER_CONFIG, RUN_JOB_TASK  # noqa: E402
from supportcore.jobs.ports import JOB_TIME_LIMIT_SECONDS, register_job_timeout_error  # noqa: E402
from supportcore.mail.sync import MAIL_SYNC_INTERVAL_SECONDS  # noqa: E402
from supportcore.observability.logging import configure_logging  # noqa: E402
from supportcore.observability.tracing import configure_tracing, instrument_celery  # noqa: E402
from supportcore.voice.jobs import STALE_CALL_SWEEP_INTERVAL_SECONDS  # noqa: E402
from supportworker import metrics  # noqa: E402
from supportworker.runtime import (  # noqa: E402
    RECOVERY_INTERVAL_SECONDS,
    run_job_sync,
    run_recovery_sync,
    shutdown_process_state,
    worker_settings,
)

RECOVER_STALE_JOBS_TASK = "supportworker.recover_stale_jobs"
CLOSE_IDLE_CONVERSATIONS_TASK = "supportworker.close_idle_conversations"
CHECK_SLA_TASK = "supportworker.check_sla"
ROLLUP_ANALYTICS_TASK = "supportworker.rollup_analytics"
SYNC_MAILBOXES_TASK = "supportworker.sync_mailboxes"
FINALISE_STALE_CALLS_TASK = "supportworker.finalise_stale_calls"
PURGE_CALL_RECORDINGS_TASK = "supportworker.purge_call_recordings"
REPORT_USAGE_TASK = "supportworker.report_usage"
RECONCILE_USAGE_COUNTERS_TASK = "supportworker.reconcile_usage_counters"
BILLING_ALERTS_TASK = "supportworker.billing_alerts"

TASK_TIME_LIMIT_SECONDS = JOB_TIME_LIMIT_SECONDS
# The soft limit raises SoftTimeLimitExceeded into the job a minute before the hard kill, so the job
# is recorded as "Indexing timed out" instead of being left `running`.
TASK_SOFT_TIME_LIMIT_SECONDS = JOB_TIME_LIMIT_SECONDS - 60

register_job_timeout_error(SoftTimeLimitExceeded)


def create_celery_app(settings: Settings) -> Celery:
    app = Celery("supportworker", broker=settings.effective_celery_broker_url())
    app.conf.update(
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        task_time_limit=TASK_TIME_LIMIT_SECONDS,
        task_soft_time_limit=TASK_SOFT_TIME_LIMIT_SECONDS,
        # A pool process that dies mid-job (OOM, kill) puts the message back: jobs are idempotent.
        task_reject_on_worker_lost=True,
        task_serializer="json",
        accept_content=["json"],
        task_ignore_result=True,
        broker_connection_retry_on_startup=True,
        worker_hijack_root_logger=False,  # keep supportcore's JSON handler...
        worker_redirect_stdouts=False,  # ...which writes to the real stdout
        beat_schedule={
            "recover-stale-jobs": {"task": RECOVER_STALE_JOBS_TASK, "schedule": RECOVERY_INTERVAL_SECONDS},
            "close-idle-conversations": {
                "task": CLOSE_IDLE_CONVERSATIONS_TASK,
                "schedule": IDLE_CLOSE_INTERVAL_SECONDS,
            },
            "check-sla": {"task": CHECK_SLA_TASK, "schedule": SLA_CHECK_INTERVAL_SECONDS},
            "rollup-analytics": {"task": ROLLUP_ANALYTICS_TASK, "schedule": ROLLUP_INTERVAL_SECONDS},
            "sync-mailboxes": {"task": SYNC_MAILBOXES_TASK, "schedule": MAIL_SYNC_INTERVAL_SECONDS},
            "finalise-stale-calls": {
                "task": FINALISE_STALE_CALLS_TASK,
                "schedule": STALE_CALL_SWEEP_INTERVAL_SECONDS,
            },
            "purge-call-recordings": {
                "task": PURGE_CALL_RECORDINGS_TASK,
                "schedule": crontab(hour=3, minute=30),  # daily, UTC (celery's default timezone)
            },
            "report-usage": {"task": REPORT_USAGE_TASK, "schedule": REPORT_USAGE_INTERVAL_SECONDS},
            "reconcile-usage-counters": {
                "task": RECONCILE_USAGE_COUNTERS_TASK,
                "schedule": RECONCILE_INTERVAL_SECONDS,
            },
            "billing-alerts": {"task": BILLING_ALERTS_TASK, "schedule": BILLING_ALERTS_INTERVAL_SECONDS},
        },
        **copy.deepcopy(PRODUCER_CONFIG),
    )

    @app.task(name=RUN_JOB_TASK)
    def run_job(name: str, payload: dict) -> None:
        run_job_sync(name, payload)

    @app.task(name=RECOVER_STALE_JOBS_TASK)
    def recover_stale_jobs() -> None:
        """Scheduled by celery beat (beat_schedule); also runs every few minutes inside run_job."""
        run_recovery_sync()

    @app.task(name=CLOSE_IDLE_CONVERSATIONS_TASK)
    def close_idle_conversations() -> None:
        """Scheduled by celery beat: resolves conversations idle for 30 minutes."""
        run_job_sync("close_idle_conversations", {})

    @app.task(name=CHECK_SLA_TASK)
    def check_sla() -> None:
        """Scheduled by celery beat every minute: flags conversations that missed an SLA target."""
        run_job_sync("check_sla", {})

    @app.task(name=ROLLUP_ANALYTICS_TASK)
    def rollup_analytics() -> None:
        """Scheduled by celery beat every 5 minutes: recomputes the analytics rollups of recent and dirty hours."""
        run_job_sync("rollup_analytics", {})

    @app.task(name=SYNC_MAILBOXES_TASK)
    def sync_mailboxes() -> None:
        """Scheduled by celery beat every minute: enqueues a sync_mailbox job per due email mailbox."""
        run_job_sync("sync_mailboxes", {})

    @app.task(name=FINALISE_STALE_CALLS_TASK)
    def finalise_stale_calls() -> None:
        """Scheduled by celery beat every 5 minutes: finalises voice calls neither the stream nor Twilio's status
        callback finished (supportcore.voice.jobs)."""
        run_job_sync("finalise_stale_calls", {})

    @app.task(name=PURGE_CALL_RECORDINGS_TASK)
    def purge_call_recordings() -> None:
        """Scheduled by celery beat daily at 03:30 UTC: deletes call recordings past their retention."""
        run_job_sync("purge_call_recordings", {})

    @app.task(name=REPORT_USAGE_TASK)
    def report_usage() -> None:
        """Scheduled by celery beat every 10 minutes: sends overage usage to the billing provider as meter events."""
        run_job_sync("report_usage", {})

    @app.task(name=RECONCILE_USAGE_COUNTERS_TASK)
    def reconcile_usage_counters() -> None:
        """Scheduled by celery beat hourly: sets the usage counters to the ledger's sums."""
        run_job_sync("reconcile_usage_counters", {})

    @app.task(name=BILLING_ALERTS_TASK)
    def billing_alerts() -> None:
        """Scheduled by celery beat every 10 minutes: emails usage, trial and payment warnings to billing managers."""
        run_job_sync("billing_alerts", {})

    return app


def _pool_index() -> int | str | None:
    """None in the main process; the pool slot (0-based, stable across respawns) in a prefork child."""
    from billiard import current_process

    proc = current_process()
    if proc.name == "MainProcess":
        return None
    index = getattr(proc, "index", None)
    return index if index is not None else "x"


settings = worker_settings()
configure_logging(settings, _pool_index())
app = create_celery_app(settings)


@signals.worker_init.connect
def _on_worker_init(**_kwargs) -> None:  # main process, before the pool forks
    metrics.wipe_multiprocess_dir()


@signals.worker_process_init.connect
def _on_process_init(**_kwargs) -> None:  # each pool process
    configure_logging(settings, _pool_index())  # its own log file: never share a rotating file
    instrument_celery(configure_tracing(settings))
    metrics.start_metrics_server(settings.worker_metrics_port, settings.effective_celery_broker_url())


@signals.worker_process_shutdown.connect
def _on_process_shutdown(**_kwargs) -> None:
    shutdown_process_state()
    metrics.mark_process_dead()
