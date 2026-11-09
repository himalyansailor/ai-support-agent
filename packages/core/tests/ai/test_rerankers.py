from unittest.mock import patch
from uuid import uuid4

import pytest

from supportcore.ai.ports import RerankCandidate
from supportcore.config import Settings
from supportcore.registry import UnknownProviderError
from supportcore.wiring import build_adapters


def _settings(env="test"):
    return Settings(
        _env_file=None,
        env=env,
        database_url="postgresql+asyncpg://u:p@h/db",
        redis_url="redis://h:6379/0",
        jwt_secret="x" * 32,
        smtp_from="noreply@example.com",
    )


def _cands(*pairs):
    return [RerankCandidate(chunk_id=uuid4(), text=t, score=s) for t, s in pairs]


def _factory(env="test"):
    return build_adapters(_settings(env)).rerankers


async def test_none_reranker_truncates():
    c = _cands(("a", 0.9), ("b", 0.8), ("c", 0.7))
    out = await _factory().create("none", {}).rerank("q", c, 2)
    assert [r.chunk_id for r in out] == [c[0].chunk_id, c[1].chunk_id]
    assert all(r.rerank_score is None for r in out)
    assert out[0].score == 0.9


async def test_none_and_fake_edge_cases():
    for name in ("none", "fake"):
        r = _factory().create(name, {})
        assert await r.rerank("q", [], 3) == []
        assert await r.rerank("q", _cands(("a", 0.5)), 0) == []
        assert await r.rerank("q", _cands(("a", 0.5)), -1) == []


async def test_fake_reranker_reorders():
    c = _cands(("a", 0.9), ("b", 0.8), ("c", 0.7))
    out = await _factory().create("fake", {}).rerank("q", c, 2)
    assert [r.chunk_id for r in out] == [c[2].chunk_id, c[1].chunk_id]
    assert out[0].rerank_score > out[1].rerank_score
    assert out[0].score == 0.7


async def test_cross_encoder_sorts_by_score():
    from supportcore.ai.rerank import cross_encoder_reranker as mod

    seen = {}

    class FakeModel:
        def predict(self, pairs, **kw):
            seen["pairs"] = pairs
            return [0.1, 0.9, 0.9, 0.5]

    c = _cands(("a", 0.5), ("b", 0.4), ("c", 0.6), ("x" * 5000, 0.3))
    with patch.object(mod, "_get_model", lambda model, cache_dir: FakeModel()):
        r = _factory().create("cross_encoder", {})
        out = await r.rerank("q", c, 3)
    # tie at 0.9 broken by original score desc: c (0.6) before b (0.4)
    assert [x.chunk_id for x in out] == [c[2].chunk_id, c[1].chunk_id, c[3].chunk_id]
    assert [x.rerank_score for x in out] == [0.9, 0.9, 0.5]
    assert all(isinstance(x.rerank_score, float) for x in out)
    assert seen["pairs"][0] == ("q", "a")
    assert len(seen["pairs"][3][1]) == 2000


def test_cross_encoder_default_and_allow_listed_models():
    from supportcore.ai.catalog import RERANKER_MODELS

    f = _factory()
    assert RERANKER_MODELS == ("BAAI/bge-reranker-base", "BAAI/bge-reranker-v2-m3")
    assert f.create("cross_encoder", {}).model_id == "BAAI/bge-reranker-base"
    assert f.create("cross_encoder", {"model": "BAAI/bge-reranker-v2-m3"}).model_id == "BAAI/bge-reranker-v2-m3"
    # Anything else would be downloaded from the Hugging Face hub on first use: refused.
    for model in ("m/x", "attacker/huge-model", "BAAI/bge-reranker-base/../x"):
        with pytest.raises(ValueError, match="Unknown reranker model"):
            f.create("cross_encoder", {"model": model})


def test_cross_encoder_loads_each_model_under_its_own_lock(monkeypatch):
    """Loading one model (slow, may download) does not block using another that is loaded."""
    import threading
    import types

    from supportcore.ai.rerank import cross_encoder_reranker as mod

    started, release = threading.Event(), threading.Event()

    class SlowCrossEncoder:
        def __init__(self, model, cache_folder):
            if model == "slow":
                started.set()
                release.wait(5)
            self.model = model

    monkeypatch.setitem(__import__("sys").modules, "sentence_transformers", types.SimpleNamespace(CrossEncoder=SlowCrossEncoder))
    monkeypatch.setattr(mod, "_models", {})
    slow = threading.Thread(target=mod._get_model, args=("slow", "/tmp/c"))
    slow.start()
    assert started.wait(5)
    import time

    t0 = time.monotonic()
    fast = mod._get_model("fast", "/tmp/c")  # with one global lock this waits for "slow"
    waited = time.monotonic() - t0
    release.set()
    slow.join(5)
    assert waited < 1
    assert fast.model == "fast" and mod._get_model("slow", "/tmp/c").model == "slow"


def test_factory_errors():
    f = _factory()
    with pytest.raises(UnknownProviderError):
        f.create("nope", {})
    with pytest.raises(ValueError):
        f.create("cross_encoder", {"bogus": 1})
    with pytest.raises(ValueError):
        f.create("none", {"model": "x"})
    with pytest.raises(ValueError):
        f.create("cross_encoder", {"model": 5})


def test_fake_only_in_test_env():
    assert _factory("test").providers() == ["none", "cross_encoder", "fake"]
    assert "fake" not in _factory("dev").providers()


@pytest.mark.models
async def test_cross_encoder_real_model_prefers_relevant():
    r = _factory().create("cross_encoder", {})
    c = _cands(("Parcel delivery takes 3 days", 0.9), ("To reset your password, click 'Forgot password'.", 0.5))
    out = await r.rerank("How do I reset my password?", c, 2)
    assert out[0].chunk_id == c[1].chunk_id
    assert out[0].rerank_score > out[1].rerank_score
