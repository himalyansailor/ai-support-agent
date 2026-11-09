"""Pass-through reranker: keeps the retrieval order and just truncates."""

from supportcore.ai.ports import RerankCandidate, RerankResult


class NoneReranker:
    async def rerank(self, query: str, candidates: list[RerankCandidate], top_n: int) -> list[RerankResult]:
        if top_n <= 0:
            return []
        return [RerankResult(c.chunk_id, c.score, None) for c in candidates[:top_n]]
