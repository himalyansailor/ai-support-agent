"""MetricsQuery: the Prometheus adapter (bounded, never raises) and the system metrics step (constant PromQL)."""

import asyncio
import re
from uuid import uuid4

import httpx
import pytest

from supportcore.metricsquery import system
from supportcore.metricsquery.fake_query import FakeMetricsQuery
from supportcore.metricsquery.ports import Sample
from supportcore.metricsquery.prometheus_query import PrometheusMetricsQuery
from supportcore.observability.links import grafana_org_url, jaeger_url, langfuse_org_url


def _adapter(handler, timeout=3.0) -> PrometheusMetricsQuery:
    return PrometheusMetricsQuery("http://prom:9090/", timeout=timeout, transport=httpx.MockTransport(handler))


async def test_parses_vector_and_sends_query():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [
            {"metric": {"provider": "openai"}, "value": [1.0, "0.25"]}, {"metric": {}, "value": [1.0, "NaN"]}]}})

    result = await _adapter(handler).query("up")
    assert not result.unavailable
    assert result[0] == Sample({"provider": "openai"}, 0.25)
    assert result[1].value != result[1].value  # NaN is kept; the caller decides
    assert seen["url"].startswith("http://prom:9090/api/v1/query?") and "query=up" in seen["url"]


@pytest.mark.parametrize("response", [
    httpx.Response(500, text="boom"),
    httpx.Response(200, json={"status": "error", "error": "bad"}),
    httpx.Response(200, text="not json"),
    httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": []}}),
])
async def test_bad_responses_are_unavailable_not_raised(response):
    result = await _adapter(lambda r: response).query("up")
    assert list(result) == [] and result.unavailable


async def test_connection_error_and_timeout_are_unavailable():
    def refuse(request):
        raise httpx.ConnectError("down")

    assert (await _adapter(refuse).query("up")).unavailable

    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={})

    result = await _adapter(slow, timeout=0.05).query("up")
    assert result.unavailable


async def test_promql_built_safely():
    """Every query is a module constant: nothing in the step takes input, so no org id or request value can
    reach PromQL; the constants contain no template placeholders."""
    fake = FakeMetricsQuery({})
    await system.collect_system_metrics(fake)
    assert fake.queries, "queries were issued"
    constants = {v for k, v in vars(system).items() if k.startswith("Q_") and isinstance(v, str)}
    assert set(fake.queries) == constants
    for q in constants:
        assert not re.search(r"[{][^}]*(\$|%s|\{)", q) and "organisation" not in q and "{org" not in q
    import inspect
    assert list(inspect.signature(system.collect_system_metrics).parameters) == ["metrics", "timeout"]


async def test_promql_semantics_of_the_constants():
    # queue depth: every worker exports the same list length, so max per queue, then sum over queues
    assert system.Q_QUEUE_DEPTH == "sum(max by (queue) (support_job_queue_depth))"
    # a healthy API has no 5xx series: the numerator falls back to 0, the denominator stays bare
    assert system.Q_ERROR_RATE_5M.startswith("(sum(rate(") and "or vector(0))" in system.Q_ERROR_RATE_5M
    # a healthy provider has no failed series: it is kept at 0 by `all * 0`
    assert "* 0))" in system.Q_PROVIDER_FAILURE_RATE_1H and system.Q_PROVIDER_FAILURE_RATE_1H.count("sum by (provider)") == 3


async def test_error_rate_healthy_is_zero_and_no_traffic_is_null():
    healthy = await system.collect_system_metrics(FakeMetricsQuery({system.Q_ERROR_RATE_5M: [Sample({}, 0.0)]}))
    assert healthy.error_rate_5m == 0.0 and not healthy.metrics_unavailable
    for empty in ([], [Sample({}, float("nan"))]):  # empty denominator / 0 / 0
        m = await system.collect_system_metrics(FakeMetricsQuery({system.Q_ERROR_RATE_5M: empty}))
        assert m.error_rate_5m is None and not m.metrics_unavailable


async def test_healthy_provider_is_listed_at_zero():
    m = await system.collect_system_metrics(FakeMetricsQuery({system.Q_PROVIDER_FAILURE_RATE_1H: [
        Sample({"provider": "openai"}, 0.0), Sample({"provider": "anthropic"}, 0.2)]}))
    assert m.provider_failure_rate_1h == {"openai": 0.0, "anthropic": 0.2}


async def test_system_metrics_values_and_partial_failure():
    fake = FakeMetricsQuery({
        system.Q_ERROR_RATE_5M: [Sample({}, 0.02)],
        system.Q_P50_MS: [Sample({}, float("nan"))],  # no traffic: unknown, not an outage
        system.Q_P95_MS: [Sample({}, 480.0)],
        system.Q_QUEUE_DEPTH: [Sample({}, 7.0)],
        system.Q_PROVIDER_FAILURE_RATE_1H: [Sample({"provider": "openai"}, 0.1)],
    })
    m = await system.collect_system_metrics(fake)
    assert (m.error_rate_5m, m.p50_ms, m.p95_ms, m.queue_depth) == (0.02, None, 480.0, 7.0)
    assert m.provider_failure_rate_1h == {"openai": 0.1} and not m.metrics_unavailable

    fake.unavailable = {system.Q_P95_MS, system.Q_PROVIDER_FAILURE_RATE_1H}
    m = await system.collect_system_metrics(fake)
    assert m.metrics_unavailable and m.p95_ms is None and m.provider_failure_rate_1h is None
    assert m.error_rate_5m == 0.02 and m.queue_depth == 7.0  # the rest survives


async def test_whole_step_is_time_bounded():
    fake = FakeMetricsQuery({}, delay=5.0)
    started = asyncio.get_running_loop().time()
    m = await system.collect_system_metrics(fake, timeout=0.1)
    assert asyncio.get_running_loop().time() - started < 1.0
    assert m.metrics_unavailable and m.error_rate_5m is None and m.provider_failure_rate_1h is None


def test_org_links_encoded():
    org = uuid4()
    url = grafana_org_url("http://g:3001/", org)
    assert url == f"http://g:3001/d/support-platform?var-org={org}"
    lf = langfuse_org_url("http://lf:3030/", "proj/1 &x", org)
    assert lf.startswith("http://lf:3030/project/proj%2F1%20%26x/traces?filter=")
    assert "tags%3BarrayOptions%3B%3Bany+of%3Borganisation%3A" + str(org) in lf
    assert langfuse_org_url("http://lf", None, org) is None and langfuse_org_url(None, "p", org) is None
    assert jaeger_url("http://j:16686", "support api") == "http://j:16686/search?service=support+api"
