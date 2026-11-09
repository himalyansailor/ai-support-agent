"""Two concurrent encode/predict calls on one local model segfault Apple MPS: calls are serialised per model."""

import asyncio
import threading
import time
from unittest.mock import patch

from supportcore.ai.embeddings import local_embedder
from supportcore.ai.ports import RerankCandidate
from supportcore.ai.rerank import cross_encoder_reranker
from supportcore.config import Settings


class _OverlapProbe:
    """Counts how many calls are inside the model at once."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self.active = 0
        self.max_active = 0

    def enter_and_leave(self) -> None:
        with self._guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.05)
        with self._guard:
            self.active -= 1


class _StubEncoder:
    def __init__(self, probe: _OverlapProbe) -> None:
        self._probe = probe

    def encode(self, texts, **kw):
        self._probe.enter_and_leave()
        return [[0.0, 1.0] for _ in texts]


class _StubCrossEncoder:
    def __init__(self, probe: _OverlapProbe) -> None:
        self._probe = probe

    def predict(self, pairs, **kw):
        self._probe.enter_and_leave()
        return [0.5 for _ in pairs]


async def test_concurrent_encode_is_serialised():
    probe = _OverlapProbe()
    settings = Settings(
        _env_file=None,
        env="test",
        database_url="postgresql+asyncpg://u:p@h/db",
        redis_url="redis://h:6379/0",
        jwt_secret="x" * 32,
        smtp_from="noreply@example.com",
    )
    embedder = local_embedder.LocalEmbedder("some/model", dimensions=2, settings=settings)
    with patch.object(local_embedder, "_get_model", lambda model, cache_dir: _StubEncoder(probe)):
        await asyncio.gather(*(embedder.embed_documents(["a", "b"]) for _ in range(4)))
    assert probe.max_active == 1


async def test_concurrent_predict_is_serialised():
    probe = _OverlapProbe()
    reranker = cross_encoder_reranker.CrossEncoderReranker("BAAI/bge-reranker-base", cache_dir="/tmp/c")
    from uuid import uuid4

    cands = [RerankCandidate(chunk_id=uuid4(), text="t", score=0.1) for _ in range(2)]
    with patch.object(cross_encoder_reranker, "_get_model", lambda model, cache_dir: _StubCrossEncoder(probe)):
        await asyncio.gather(*(reranker.rerank("q", cands, 2) for _ in range(4)))
    assert probe.max_active == 1
