import pytest

from supportcore.db.session import after_commit


async def test_after_commit_runs_once_after_commit(session_factory):
    calls = []
    async with session_factory() as s:
        after_commit(s, lambda: calls.append(1))
        assert calls == []
        await s.commit()
        assert calls == [1]
        await s.commit()
    assert calls == [1]


async def test_after_commit_not_run_on_rollback(session_factory):
    calls = []
    async with session_factory() as s:
        after_commit(s, lambda: calls.append(1))
        await s.rollback()
        await s.commit()
    assert calls == []


async def test_after_commit_callbacks_do_not_leak_to_next_transaction(session_factory):
    calls = []
    async with session_factory() as s:
        after_commit(s, lambda: calls.append("rolled-back"))
        await s.rollback()
        after_commit(s, lambda: calls.append("committed"))
        await s.commit()
        await s.commit()
    assert calls == ["committed"]


async def test_after_commit_runs_multiple_callbacks_in_order(session_factory):
    calls = []
    async with session_factory() as s:
        after_commit(s, lambda: calls.append(1))
        after_commit(s, lambda: calls.append(2))
        await s.commit()
    assert calls == [1, 2]


async def test_after_commit_callback_error_does_not_break_commit(session_factory):
    calls = []

    def boom():
        raise RuntimeError("x")

    async with session_factory() as s:
        after_commit(s, boom)
        after_commit(s, lambda: calls.append(1))
        await s.commit()
    assert calls == [1]
