"""Deterministic offline embedder for tests (registered only when settings.env == "test")."""

import hashlib
import math
import re

from supportcore.ai.ports import EmbeddingError

_TOKEN = re.compile(r"\w+")


class FakeEmbedder:
    def __init__(self, dimensions: int = 8, model_id: str = "fake-embedding", fail_on: str | None = None) -> None:
        self.model_id = model_id
        self.dimensions = dimensions
        self.fail_on = fail_on
        self.calls: list[list[str]] = []

    def _vector(self, text: str) -> list[float]:
        if self.fail_on is not None and self.fail_on in text:
            raise EmbeddingError(f"Fake embedder configured to fail on {self.fail_on!r}")
        vec = [0.0] * self.dimensions
        tokens = _TOKEN.findall(text.lower()) or [""]
        for token in tokens:
            digest = hashlib.sha256(token.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            vec[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0.0:  # opposing tokens cancelled out: fall back to a hash-derived direction
            bag = "\x00".join(sorted(tokens)).encode()
            vec[int.from_bytes(hashlib.sha256(bag).digest()[:4], "big") % self.dimensions] = 1.0
            return vec
        return [x / norm for x in vec]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        self.calls.append([text])
        return self._vector(text)
