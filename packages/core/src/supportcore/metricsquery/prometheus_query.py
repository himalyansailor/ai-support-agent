"""PrometheusMetricsQuery (settings.metrics_query="prometheus"): GET /api/v1/query, bounded and non-raising."""

import httpx

from supportcore.metricsquery.ports import QueryResult, Sample
from supportcore.observability.logging import get_logger

logger = get_logger(__name__)

TIMEOUT_SECONDS = 3.0


class PrometheusMetricsQuery:
    def __init__(
        self, base_url: str, *, timeout: float = TIMEOUT_SECONDS, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)

    async def query(self, promql: str) -> QueryResult:
        try:
            response = await self._client.get("/api/v1/query", params={"query": promql})
            response.raise_for_status()
            body = response.json()
            if body.get("status") != "success" or body["data"]["resultType"] != "vector":
                raise ValueError("unexpected response")
            return QueryResult(
                [Sample(dict(item["metric"]), float(item["value"][1])) for item in body["data"]["result"]]
            )
        except Exception as exc:  # contained by contract: callers show "unavailable"
            logger.warning("metrics_query_failed", error_type=type(exc).__name__)  # never the query or response
            return QueryResult(unavailable=True)

    async def aclose(self) -> None:
        await self._client.aclose()
