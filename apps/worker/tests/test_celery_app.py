"""The Celery worker app: task registration, settings, and the per-process runtime loop."""

import importlib
import sys

import pytest

from supportcore.config import Settings
from supportcore.testing import TEST_DATABASE_URL

ENV = {
    "SUPPORT_ENV": "test",
    "SUPPORT_DATABASE_URL": TEST_DATABASE_URL,
    "SUPPORT_REDIS_URL": "redis://localhost:6380/0",
    "SUPPORT_JWT_SECRET": "test-secret-test-secret-test-secret-123",
    "SUPPORT_SMTP_FROM": "noreply@test.local",
    "SUPPORT_SERVICE_NAME": "support-api",  # the worker overrides it
}


def _settings(**kw) -> Settings:
    base = {k.removeprefix("SUPPORT_").lower(): v for k, v in ENV.items()}
    return Settings(_env_file=None, **{**base, **kw})


@pytest.fixture
def celery_module(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    from supportcore.config import get_settings

    get_settings.cache_clear()
    sys.modules.pop("supportworker.celery_app", None)
    module = importlib.import_module("supportworker.celery_app")
    yield module
    sys.modules.pop("supportworker.celery_app", None)
    get_settings.cache_clear()


def test_worker_registers_run_job_task(celery_module):
    app = celery_module.app
    assert "supportworker.run_job" in app.tasks
    assert app.main == "supportworker"
    assert app.conf.task_acks_late is True
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.task_time_limit == 1800
    assert app.conf.task_soft_time_limit == 1740
    assert app.conf.task_reject_on_worker_lost is True
    assert app.conf.broker_url == "redis://localhost:6380/0"


def test_broker_url_prefers_celery_broker_url(celery_module):
    app = celery_module.create_celery_app(_settings(celery_broker_url="redis://broker:6379/5"))
    assert app.conf.broker_url == "redis://broker:6379/5"


def test_worker_settings_use_worker_service_name():
    from supportworker.runtime import worker_settings

    assert worker_settings(_settings()).service_name == "support-worker"


def test_worker_always_sends_jobs_through_celery():
    """Jobs a job enqueues (a re-queued document, a rescheduled rebuild) go to the broker, never
    inline inside the pool process, whatever SUPPORT_JOB_QUEUE says."""
    from supportworker.runtime import worker_settings

    assert worker_settings(_settings(job_queue="inline")).job_queue == "celery"


def test_run_job_dispatches_on_one_loop_per_process(monkeypatch):
    """run_job reuses the process's event loop and runtime across tasks."""
    from supportworker import runtime as rt

    calls = []

    async def fake_dispatch(runtime, name, payload):
        import asyncio

        calls.append((runtime, name, payload, asyncio.get_running_loop()))

    sentinel = object()

    def fake_build(settings=None):
        return sentinel

    recovered = []

    async def fake_recover(runtime):
        recovered.append(runtime)
        return 0

    monkeypatch.setattr(rt, "dispatch", fake_dispatch)
    monkeypatch.setattr(rt, "build_runtime", fake_build)
    monkeypatch.setattr(rt, "recover_stale_jobs", fake_recover)
    rt.reset_process_state()
    try:
        rt.run_job_sync("index_document", {"job_id": "1"})
        rt.run_job_sync("purge_kb", {"kb_id": "2"})
    finally:
        rt.reset_process_state()
    assert [c[1] for c in calls] == ["index_document", "purge_kb"]
    assert calls[0][0] is sentinel and calls[1][0] is sentinel
    assert calls[0][3] is calls[1][3]
    assert not calls[0][3].is_running()
    assert recovered == [sentinel]  # stale jobs recovered once, on the process's first job


def test_run_job_delivers_publishes_spawned_at_commit(monkeypatch):
    """A publish spawned by a job (after_commit) is delivered before run_job_sync returns: the loop stops
    between jobs, so nothing may be left pending on it."""
    import asyncio

    from supportcore.realtime.spawn import spawn_publish
    from supportworker import runtime as rt

    delivered = []

    async def publish():
        await asyncio.sleep(0.05)  # still in flight when the job body returns
        delivered.append("event")

    async def fake_dispatch(runtime, name, payload):
        spawn_publish(publish(), failure_event="test_publish_failed")

    async def no_recover(runtime):
        return 0

    monkeypatch.setattr(rt, "dispatch", fake_dispatch)
    monkeypatch.setattr(rt, "build_runtime", lambda settings=None: object())
    monkeypatch.setattr(rt, "recover_stale_jobs", no_recover)
    rt.reset_process_state()
    try:
        rt.run_job_sync("check_sla", {})
        assert delivered == ["event"]
    finally:
        rt.reset_process_state()


def test_celery_queue_and_worker_agree_on_task_name(celery_module):
    from supportcore.jobs.celery_queue import RUN_JOB_TASK

    assert RUN_JOB_TASK in celery_module.app.tasks


def test_time_limit_outside_the_job_records_timeout(monkeypatch):
    """Celery's soft limit raises from a signal handler; if it surfaces from the loop instead of inside
    the job, the job is cancelled and recorded as timed out, and the exception goes back to Celery."""
    import asyncio
    import signal

    from celery.exceptions import SoftTimeLimitExceeded

    from supportcore.jobs.ports import register_job_timeout_error
    from supportcore.knowledge.indexing import TIMED_OUT
    from supportworker import runtime as rt

    register_job_timeout_error(SoftTimeLimitExceeded)
    cancelled, failed = [], []

    async def slow_dispatch(runtime, name, payload):
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.append(name)
            raise

    async def fake_fail(runtime, name, payload, message):
        failed.append((name, payload, message))

    async def no_recover(runtime):
        return 0

    def raise_soft_limit(signum, frame):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(rt, "dispatch", slow_dispatch)
    monkeypatch.setattr(rt, "fail_job", fake_fail)
    monkeypatch.setattr(rt, "recover_stale_jobs", no_recover)
    monkeypatch.setattr(rt, "build_runtime", lambda settings=None: object())
    previous = signal.signal(signal.SIGALRM, raise_soft_limit)
    rt.reset_process_state()
    try:
        signal.setitimer(signal.ITIMER_REAL, 0.2)
        with pytest.raises(SoftTimeLimitExceeded):
            rt.run_job_sync("index_document", {"job_id": "j", "organisation_id": "o"})
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        rt.reset_process_state()
    assert failed == [("index_document", {"job_id": "j", "organisation_id": "o"}, TIMED_OUT)]
    assert cancelled == ["index_document"] or cancelled == []  # [] if it surfaced inside the job itself


def test_stale_job_recovery_runs_periodically(monkeypatch):
    """Recovery runs before a process's first job and again once RECOVERY_INTERVAL_SECONDS passed,
    so a rebuild left `running` by a dead worker is failed even on a long-lived process."""
    from supportworker import runtime as rt

    clock = [1000.0]
    recovered, ran = [], []

    async def fake_recover(runtime):
        recovered.append(clock[0])
        return 0

    async def fake_dispatch(runtime, name, payload):
        ran.append(name)

    monkeypatch.setattr(rt.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(rt, "recover_stale_jobs", fake_recover)
    monkeypatch.setattr(rt, "dispatch", fake_dispatch)
    monkeypatch.setattr(rt, "build_runtime", lambda settings=None: object())
    rt.reset_process_state()
    try:
        rt.run_job_sync("index_document", {})
        clock[0] += rt.RECOVERY_INTERVAL_SECONDS - 1
        rt.run_job_sync("index_document", {})
        clock[0] += 2
        rt.run_job_sync("index_document", {})
        rt.run_recovery_sync()  # the beat-schedulable task
    finally:
        rt.reset_process_state()
    assert ran == ["index_document"] * 3
    assert recovered == [1000.0, 1000.0 + rt.RECOVERY_INTERVAL_SECONDS + 1, 1000.0 + rt.RECOVERY_INTERVAL_SECONDS + 1]


def test_recover_stale_jobs_task_registered(celery_module):
    assert "supportworker.recover_stale_jobs" in celery_module.app.tasks


def test_beat_schedules_stale_job_recovery_every_five_minutes(celery_module):
    entry = celery_module.app.conf.beat_schedule["recover-stale-jobs"]
    assert entry["task"] == "supportworker.recover_stale_jobs" == celery_module.RECOVER_STALE_JOBS_TASK
    assert entry["schedule"] == 300
    assert celery_module.RECOVER_STALE_JOBS_TASK in celery_module.app.tasks


def test_beat_checks_sla_every_minute(celery_module, monkeypatch):
    entry = celery_module.app.conf.beat_schedule["check-sla"]
    assert entry["task"] == "supportworker.check_sla" == celery_module.CHECK_SLA_TASK
    assert entry["schedule"] == 60
    calls = []
    task = celery_module.app.tasks[celery_module.CHECK_SLA_TASK]
    monkeypatch.setitem(task.run.__globals__, "run_job_sync", lambda name, payload: calls.append((name, payload)))
    task.run()
    assert calls == [("check_sla", {})]


def test_beat_rolls_up_analytics_every_five_minutes(celery_module, monkeypatch):
    entry = celery_module.app.conf.beat_schedule["rollup-analytics"]
    assert entry["task"] == "supportworker.rollup_analytics" == celery_module.ROLLUP_ANALYTICS_TASK
    assert entry["schedule"] == 300
    calls = []
    task = celery_module.app.tasks[celery_module.ROLLUP_ANALYTICS_TASK]
    monkeypatch.setitem(task.run.__globals__, "run_job_sync", lambda name, payload: calls.append((name, payload)))
    task.run()
    assert calls == [("rollup_analytics", {})]


def test_beat_closes_idle_conversations_every_five_minutes(celery_module, monkeypatch):
    entry = celery_module.app.conf.beat_schedule["close-idle-conversations"]
    assert entry["task"] == "supportworker.close_idle_conversations" == celery_module.CLOSE_IDLE_CONVERSATIONS_TASK
    assert entry["schedule"] == 300
    calls = []
    task = celery_module.app.tasks[celery_module.CLOSE_IDLE_CONVERSATIONS_TASK]
    monkeypatch.setitem(task.run.__globals__, "run_job_sync", lambda name, payload: calls.append((name, payload)))
    task.run()
    assert calls == [("close_idle_conversations", {})]


def test_beat_finalises_stale_calls_every_five_minutes(celery_module, monkeypatch):
    entry = celery_module.app.conf.beat_schedule["finalise-stale-calls"]
    assert entry["task"] == "supportworker.finalise_stale_calls" == celery_module.FINALISE_STALE_CALLS_TASK
    assert entry["schedule"] == 300
    calls = []
    task = celery_module.app.tasks[celery_module.FINALISE_STALE_CALLS_TASK]
    monkeypatch.setitem(task.run.__globals__, "run_job_sync", lambda name, payload: calls.append((name, payload)))
    task.run()
    assert calls == [("finalise_stale_calls", {})]


@pytest.mark.parametrize(
    ("entry_name", "task_name", "job_name", "seconds"),
    [
        ("report-usage", "REPORT_USAGE_TASK", "report_usage", 600),
        ("reconcile-usage-counters", "RECONCILE_USAGE_COUNTERS_TASK", "reconcile_usage_counters", 3600),
        ("billing-alerts", "BILLING_ALERTS_TASK", "billing_alerts", 600),
    ],
)
def test_beat_runs_billing_jobs(celery_module, monkeypatch, entry_name, task_name, job_name, seconds):
    entry = celery_module.app.conf.beat_schedule[entry_name]
    assert entry["task"] == f"supportworker.{job_name}" == getattr(celery_module, task_name)
    assert entry["schedule"] == seconds
    calls = []
    task = celery_module.app.tasks[entry["task"]]
    monkeypatch.setitem(task.run.__globals__, "run_job_sync", lambda name, payload: calls.append((name, payload)))
    task.run()
    assert calls == [(job_name, {})]
