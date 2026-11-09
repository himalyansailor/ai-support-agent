from supportvoice.container import ActiveCalls


def test_active_calls_enforces_the_limit_and_the_gauge():
    from supportcore.voice.metrics import VOICE_ACTIVE_CALLS

    active = ActiveCalls(2)
    assert active.acquire() and active.acquire()
    assert active.full and not active.acquire()
    assert VOICE_ACTIVE_CALLS._value.get() == 2
    active.release()
    assert not active.full and active.count == 1
    active.release()
    active.release()  # never below zero
    assert active.count == 0


async def test_aclose_closes_tts_adapters_and_the_turn_runner(app):
    container = app.state.container
    closed = []

    class Tts:
        async def aclose(self):
            closed.append("tts")

    class NoClose:
        pass

    container._tts.update({"openai": Tts(), "plain": NoClose()})
    original = container.turn_runner.aclose

    async def runner_close(*args, **kwargs):
        closed.append("runner")
        await original(*args, **kwargs)

    container.turn_runner.aclose = runner_close
    await container.aclose()
    assert closed == ["runner", "tts"]


def test_tts_is_one_adapter_per_provider(app):
    container = app.state.container
    assert container.tts("fake") is container.tts("fake")


async def test_a_failed_call_task_is_logged_by_class_name(app, capsys):
    import asyncio

    async def boom():
        raise ValueError("secret detail")

    task = app.state.container.start_call(boom())
    await asyncio.wait({task})
    await asyncio.sleep(0)
    out = capsys.readouterr().out
    assert "voice_call_task_failed" in out and "ValueError" in out
    assert "secret detail" not in out


async def test_voice_engine_pool_is_sized_by_voice_db_pool_size(voice_settings, db_engine):
    """Every utterance, turn lease and renewal of up to `voice_max_calls` calls shares this process's pool."""
    from supportcore.config import Settings
    from supportvoice.main import create_app

    assert Settings(_env_file=None).voice_db_pool_size == 20
    app = create_app(voice_settings.model_copy(update={"voice_db_pool_size": 7}))
    try:
        pool = app.state.engine.pool
        assert (pool.size(), pool._max_overflow) == (7, 10)
    finally:
        await app.state.container.aclose()
        await app.state.engine.dispose()


def test_other_processes_keep_the_default_pool(voice_settings):
    from supportcore.db.session import create_engine

    engine = create_engine(voice_settings.model_copy(update={"voice_db_pool_size": 7}))
    assert (engine.pool.size(), engine.pool._max_overflow) == (5, 10)  # SQLAlchemy's default
