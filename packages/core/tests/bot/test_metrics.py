from prometheus_client import REGISTRY

from supportcore.bot.metrics import record_tokens


def _tokens(provider, model, kind="input"):
    return REGISTRY.get_sample_value(
        "support_llm_tokens_total", {"provider": provider, "model": model, "kind": kind}
    ) or 0.0


def test_token_metric_buckets_models_outside_the_catalog():
    before_catalog = _tokens("anthropic", "claude-sonnet-5-5")
    before_other = _tokens("openai_compatible", "other")
    record_tokens("anthropic", "claude-sonnet-5-5", 10, 0)
    record_tokens("openai_compatible", "tenant-chosen-model-0f3a", 7, 0)
    record_tokens("openai", "gpt-anything", 5, 0)  # openai takes any model id too: not a catalog id
    assert _tokens("anthropic", "claude-sonnet-5-5") == before_catalog + 10
    assert _tokens("openai_compatible", "other") == before_other + 7
    assert _tokens("openai_compatible", "tenant-chosen-model-0f3a") == 0.0
    assert _tokens("openai", "gpt-anything") == 0.0
