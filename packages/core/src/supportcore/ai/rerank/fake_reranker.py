"""Test-only reranker: reverses the candidate order so tests can see it ran."""

from supportcore.ai.ports import RerankCandidate, RerankResult


class FakeReranker:
    async def rerank(self, query: str, candidates: list[RerankCandidate], top_n: int) -> list[RerankResult]:
        if top_n <= 0:
            return []
        n = len(candidates)
        reversed_ = reversed(candidates)
        return [RerankResult(c.chunk_id, c.score, float(n - i)) for i, c in enumerate(reversed_)][:top_n]
