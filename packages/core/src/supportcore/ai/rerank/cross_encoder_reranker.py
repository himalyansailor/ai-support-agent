"""Local sentence-transformers cross-encoder reranker (no API key, runs on CPU)."""

import asyncio
import threading
from typing import Any

from supportcore.ai.catalog import DEFAULT_RERANKER_MODEL
from supportcore.ai.ports import RerankCandidate, RerankResult

DEFAULT_MODEL = DEFAULT_RERANKER_MODEL
MAX_TEXT_CHARS = 2000

_models: dict[tuple[str, str], Any] = {}
_load_locks: dict[tuple[str, str], threading.Lock] = {}
_run_locks: dict[tuple[str, str], threading.Lock] = {}
_locks_guard = threading.Lock()


def _get_model(model: str, cache_dir: str) -> Any:
    """Loads each model once per process. Each model has its own load lock, so a slow load (or
    download) of one does not hold up reranking with another."""
    key = (model, cache_dir)
    loaded = _models.get(key)
    if loaded is not None:
        return loaded
    with _locks_guard:
        lock = _load_locks.setdefault(key, threading.Lock())
    with lock:
        if key not in _models:
            from sentence_transformers import CrossEncoder  # lazy: imports torch

            _models[key] = CrossEncoder(model, cache_folder=cache_dir)
        return _models[key]


def _run_lock(model: str, cache_dir: str) -> threading.Lock:
    """One lock per loaded model: two concurrent `predict` calls on one model segfault Apple MPS."""
    with _locks_guard:
        return _run_locks.setdefault((model, cache_dir), threading.Lock())


class CrossEncoderReranker:
    def __init__(self, model: str = DEFAULT_MODEL, *, cache_dir: str) -> None:
        self.model_id = model
        self._cache_dir = cache_dir

    def _score(self, query: str, texts: list[str]) -> list[float]:
        model = _get_model(self.model_id, self._cache_dir)
        pairs = [(query, t[:MAX_TEXT_CHARS]) for t in texts]
        with _run_lock(self.model_id, self._cache_dir):
            return [float(s) for s in model.predict(pairs)]

    async def rerank(self, query: str, candidates: list[RerankCandidate], top_n: int) -> list[RerankResult]:
        if top_n <= 0 or not candidates:
            return []
        scores = await asyncio.to_thread(self._score, query, [c.text for c in candidates])
        ranked = sorted(zip(candidates, scores, strict=True), key=lambda p: (p[1], p[0].score), reverse=True)
        return [RerankResult(c.chunk_id, c.score, s) for c, s in ranked[:top_n]]
