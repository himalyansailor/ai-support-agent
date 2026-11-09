from prometheus_client import REGISTRY

from supportcore.ai.metrics import record_llm_request
from supportcore.observability.metrics import LLM_REQUESTS, WS_CONNECTIONS


def _value(**labels) -> float:
    return REGISTRY.get_sample_value("support_llm_requests_total", labels) or 0.0


def test_llm_request_metric_labels_bounded():
    assert LLM_REQUESTS._labelnames == ("provider", "kind", "status")
    record_llm_request("openai", "answer", "ok")
    assert _value(provider="openai", kind="answer", status="ok") >= 1
    # Free text from a tenant (provider, kind, status) never becomes a label value.
    before = _value(provider="other", kind="other", status="failed")
    record_llm_request("my-custom-provider-123", "weird kind", "boom: secret text")
    assert _value(provider="my-custom-provider-123", kind="weird kind", status="boom: secret text") == 0
    assert _value(provider="other", kind="other", status="failed") == before + 1


def test_ws_connections_gauge_has_only_kind_label():
    assert WS_CONNECTIONS._labelnames == ("kind",)
