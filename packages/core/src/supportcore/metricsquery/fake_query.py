"""FakeMetricsQuery (settings.metrics_query="fake", env=test only): answers from a dict of PromQL -> samples."""

import asyncio

from supportcore.metricsquery.ports import QueryResult, Sample


class FakeMetricsQuery:
    def __init__(
        self, results: dict[str, list[Sample]] | None = None, *, unavailable: set[str] | None = None, delay: float = 0.0
    ) -> None:
        self.results = results or {}
        self.unavailable = unavailable or set()  # queries that fail
        self.delay = delay
        self.queries: list[str] = []

    async def query(self, promql: str) -> QueryResult:
        self.queries.append(promql)
        if self.delay:
            await asyncio.sleep(self.delay)
        if promql in self.unavailable:
            return QueryResult(unavailable=True)
        return QueryResult(list(self.results.get(promql, [])))

    async def aclose(self) -> None:
        return None
