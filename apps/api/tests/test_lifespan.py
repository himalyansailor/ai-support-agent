"""The API lifespan: periodic stale-job recovery with inline jobs, and resources always released."""

import asyncio

import pytest

import supportapi.main as main
from supportapi.main import create_app


@pytest.fixture
def recorded(monkeypatch):
    calls: list[object] = []

    async def fake_recover(runtime):
        calls.append(runtime)
        return 0

    monkeypatch.setattr(main, "recover_stale_jobs", fake_recover)
    return calls


async def test_inline_mode_runs_recovery_at_start_and_periodically(kb_settings, recorded, monkeypatch):
    monkeypatch.setattr(main, "RECOVERY_INTERVAL_SECONDS", 0.05)
    app = create_app(kb_settings, periodic_jobs=True)
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if len(recorded) >= 2:
                break
            await asyncio.sleep(0.02)
    assert len(recorded) >= 2
    assert recorded[0] is app.state.container.job_runtime()  # the inline jobs' runtime
    count = len(recorded)
    await asyncio.sleep(0.15)
    assert len(recorded) == count  # stopped with the app


async def test_recovery_failure_does_not_stop_the_loop(kb_settings, monkeypatch):
    calls: list[int] = []

    async def flaky(runtime):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database unavailable")
        return 0

    monkeypatch.setattr(main, "recover_stale_jobs", flaky)
    monkeypatch.setattr(main, "RECOVERY_INTERVAL_SECONDS", 0.02)
    app = create_app(kb_settings, periodic_jobs=True)
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.02)
    assert len(calls) >= 2


async def test_test_env_runs_no_periodic_jobs_unless_asked(kb_settings, recorded, monkeypatch):
    monkeypatch.setattr(main, "RECOVERY_INTERVAL_SECONDS", 0.01)
    app = create_app(kb_settings)  # env=test, inline queue
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.05)
    assert recorded == []


async def test_celery_mode_leaves_recovery_to_the_worker(test_settings, recorded):
    app = create_app(test_settings.model_copy(update={"job_queue": "celery"}), periodic_jobs=True)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.05)
    assert recorded == []


async def test_engine_is_disposed_even_if_closing_adapters_fails(test_settings, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncEngine

    app = create_app(test_settings.model_copy(update={"job_queue": "celery"}))
    disposed: list[bool] = []
    original = AsyncEngine.dispose

    async def dispose(engine, *args, **kwargs):
        disposed.append(engine is app.state.engine)
        await original(engine, *args, **kwargs)

    async def broken_close():
        raise RuntimeError("chroma went away")

    monkeypatch.setattr(AsyncEngine, "dispose", dispose)
    monkeypatch.setattr(app.state.container.adapters, "aclose", broken_close)
    with pytest.raises(RuntimeError, match="chroma went away"):
        async with app.router.lifespan_context(app):
            pass
    assert disposed == [True]


async def test_inline_mode_closes_idle_conversations_periodically(kb_settings, recorded, monkeypatch):
    closes: list[object] = []

    async def fake_close(runtime):
        closes.append(runtime)
        return 0

    monkeypatch.setattr(main, "close_idle_conversations", fake_close)
    monkeypatch.setattr(main, "IDLE_CLOSE_INTERVAL_SECONDS", 0.05)
    app = create_app(kb_settings, periodic_jobs=True)
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if len(closes) >= 2:
                break
            await asyncio.sleep(0.02)
    assert len(closes) >= 2 and closes[0] is app.state.container.job_runtime()
    count = len(closes)
    await asyncio.sleep(0.15)
    assert len(closes) == count  # stopped with the app


async def test_inline_mode_checks_sla_periodically(kb_settings, recorded, monkeypatch):
    checks: list[tuple[object, object]] = []

    async def fake_check(runtime, now):
        checks.append((runtime, now))
        return 0

    monkeypatch.setattr(main, "check_sla", fake_check)
    monkeypatch.setattr(main, "SLA_CHECK_INTERVAL_SECONDS", 0.05)
    app = create_app(kb_settings, periodic_jobs=True)
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if len(checks) >= 2:
                break
            await asyncio.sleep(0.02)
    assert len(checks) >= 2 and checks[0][0] is app.state.container.job_runtime()
    assert checks[0][1].tzinfo is not None
    count = len(checks)
    await asyncio.sleep(0.15)
    assert len(checks) == count  # stopped with the app


async def test_inline_mode_rolls_up_analytics_periodically(kb_settings, recorded, monkeypatch):
    runs: list[tuple[object, object]] = []

    async def fake_rollup(runtime, now):
        runs.append((runtime, now))
        return 0

    monkeypatch.setattr(main, "rollup_analytics", fake_rollup)
    monkeypatch.setattr(main, "ROLLUP_INTERVAL_SECONDS", 0.05)
    app = create_app(kb_settings, periodic_jobs=True)
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if len(runs) >= 2:
                break
            await asyncio.sleep(0.02)
    assert len(runs) >= 2 and runs[0][0] is app.state.container.job_runtime()
    assert runs[0][1].tzinfo is not None
    count = len(runs)
    await asyncio.sleep(0.15)
    assert len(runs) == count  # stopped with the app


async def test_shutdown_closes_the_turn_runner_before_the_adapters(test_settings, recorded, monkeypatch):
    """The runner's cancelled turns publish their bot_reset/bot_done and release their leases, so it
    closes while pub/sub and the database are still open."""
    app = create_app(test_settings.model_copy(update={"job_queue": "celery"}))
    container = app.state.container
    order: list[str] = []
    close_adapters = container.adapters.aclose

    async def runner_aclose(timeout=10):
        order.append("runner")

    async def adapters_aclose():
        order.append("adapters")
        await close_adapters()

    monkeypatch.setattr(container.turn_runner, "aclose", runner_aclose)
    monkeypatch.setattr(container.adapters, "aclose", adapters_aclose)
    async with app.router.lifespan_context(app):
        pass
    assert order == ["runner", "adapters"]


async def test_shutdown_drains_publishes_before_closing_adapters(kb_settings, monkeypatch):
    order: list[str] = []

    async def fake_drain(*a, **k):
        order.append("drain")

    monkeypatch.setattr(main, "drain_pending", fake_drain)
    app = create_app(kb_settings)
    adapters = app.state.container.adapters
    original = adapters.aclose

    async def recording_aclose():
        order.append("aclose")
        await original()

    monkeypatch.setattr(adapters, "aclose", recording_aclose)
    async with app.router.lifespan_context(app):
        pass
    assert order == ["drain", "aclose"]
