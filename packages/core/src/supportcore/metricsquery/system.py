"""The platform overview's system numbers, read from Prometheus with constant PromQL.

Every query is a module constant (Q_*): the function takes no request input, so nothing a caller sends can
reach PromQL. The gauges (queue depth) are exported per process, so they are summed. The five queries run
concurrently under ONE time bound; one that fails or does not finish in time nulls only its own field and
sets `metrics_unavailable`. A query that answers with no samples (no traffic) is not a failure: its field is None.
"""

import asyncio
import math
from dataclasses import dataclass

from supportcore.metricsquery.ports import MetricsQuery, QueryResult

TIMEOUT_SECONDS = 3.0

# 5xx rate `or vector(0)`: no 5xx series means 0, not unknown. No traffic at all (empty or zero denominator) is null.
Q_ERROR_RATE_5M = (
    '(sum(rate(support_http_requests_total{status=~"5.."}[5m])) or vector(0))'
    " / sum(rate(support_http_requests_total[5m]))"
)
Q_P50_MS = "1000 * histogram_quantile(0.5, sum by (le) (rate(support_http_request_duration_seconds_bucket[5m])))"
Q_P95_MS = "1000 * histogram_quantile(0.95, sum by (le) (rate(support_http_request_duration_seconds_bucket[5m])))"
# Every scraped worker exports the same Redis list length per queue: max over processes, then sum over queues.
Q_QUEUE_DEPTH = "sum(max by (queue) (support_job_queue_depth))"
# Numerator `or (all * 0)`: a provider with traffic and no failed series is 0.0, not missing.
Q_PROVIDER_FAILURE_RATE_1H = (
    '(sum by (provider) (increase(support_llm_requests_total{status="failed"}[1h])) '
    "or (sum by (provider) (increase(support_llm_requests_total[1h])) * 0)) "
    "/ sum by (provider) (increase(support_llm_requests_total[1h]))"
)


@dataclass
class SystemMetrics:
    error_rate_5m: float | None = None
    p50_ms: float | None = None
    p95_ms: float | None = None
    queue_depth: float | None = None
    provider_failure_rate_1h: dict[str, float] | None = None
    metrics_unavailable: bool = False


def _scalar(result: QueryResult | None) -> float | None:
    if not result or math.isnan(result[0].value) or math.isinf(result[0].value):
        return None
    return result[0].value


async def collect_system_metrics(metrics: MetricsQuery, timeout: float = TIMEOUT_SECONDS) -> SystemMetrics:
    queries = {
        "error_rate_5m": Q_ERROR_RATE_5M,
        "p50_ms": Q_P50_MS,
        "p95_ms": Q_P95_MS,
        "queue_depth": Q_QUEUE_DEPTH,
        "providers": Q_PROVIDER_FAILURE_RATE_1H,
    }
    tasks = {name: asyncio.ensure_future(metrics.query(q)) for name, q in queries.items()}
    try:
        await asyncio.wait(tasks.values(), timeout=timeout)
    finally:  # also when the request itself is cancelled: no task outlives this call
        pending = [t for t in tasks.values() if not t.done()]
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    results: dict[str, QueryResult | None] = {}
    for name, task in tasks.items():
        ok = task.done() and not task.cancelled() and task.exception() is None and not task.result().unavailable
        results[name] = task.result() if ok else None
    out = SystemMetrics(metrics_unavailable=any(r is None for r in results.values()))
    out.error_rate_5m = _scalar(results["error_rate_5m"])
    out.p50_ms = _scalar(results["p50_ms"])
    out.p95_ms = _scalar(results["p95_ms"])
    out.queue_depth = _scalar(results["queue_depth"])
    providers = results["providers"]
    if providers is not None:
        out.provider_failure_rate_1h = {
            s.labels["provider"]: s.value
            for s in providers
            if "provider" in s.labels and not math.isnan(s.value) and not math.isinf(s.value)
        }
    return out
