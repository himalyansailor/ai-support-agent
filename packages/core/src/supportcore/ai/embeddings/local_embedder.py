"""Local sentence-transformers embeddings (no API key, runs on CPU)."""

import asyncio
import threading
from typing import Any

from supportcore.ai.ports import EmbeddingError
from supportcore.config import Settings

_models: dict[tuple[str, str], Any] = {}
_lock = threading.Lock()
_run_locks: dict[tuple[str, str], threading.Lock] = {}
_run_locks_guard = threading.Lock()  # not `_lock`, which `_get_model` holds during slow loads
_E5_PREFIX = "intfloat/multilingual-e5-"


def _get_model(model: str, cache_dir: str) -> Any:
    """Loads each model once per process."""
    key = (model, cache_dir)
    with _lock:
        if key not in _models:
            from sentence_transformers import SentenceTransformer  # lazy: imports torch

            _models[key] = SentenceTransformer(model, cache_folder=cache_dir)
        return _models[key]


def _run_lock(model: str, cache_dir: str) -> threading.Lock:
    """One lock per loaded model. Two concurrent `encode` calls on one model segfault the process on
    Apple MPS, so every encode holds this lock."""
    with _run_locks_guard:
        return _run_locks.setdefault((model, cache_dir), threading.Lock())


class LocalEmbedder:
    def __init__(self, model: str, *, dimensions: int, settings: Settings) -> None:
        self.model_id = model
        self.dimensions = dimensions
        self._cache_dir = settings.model_cache_dir
        self._batch_size = max(1, settings.embed_batch_size)
        self._is_e5 = model.startswith(_E5_PREFIX)

    def _encode(self, texts: list[str]) -> list[list[float]]:
        model = _get_model(self.model_id, self._cache_dir)
        with _run_lock(self.model_id, self._cache_dir):
            out = model.encode(texts, batch_size=self._batch_size, normalize_embeddings=True)
        return [list(map(float, row)) for row in out]

    async def _run(self, texts: list[str]) -> list[list[float]]:
        try:
            return await asyncio.to_thread(self._encode, texts)
        except Exception as exc:
            raise EmbeddingError(f"Local embedding with {self.model_id!r} failed: {exc}"[:300]) from None

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        prefix = "passage: " if self._is_e5 else ""
        return await self._run([prefix + t for t in texts])

    async def embed_query(self, text: str) -> list[float]:
        prefix = "query: " if self._is_e5 else ""
        return (await self._run([prefix + text]))[0]
