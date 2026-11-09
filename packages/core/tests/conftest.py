from supportcore.testing import db_engine, org_a, org_b, session, session_factory  # noqa: F401


def pytest_collection_modifyitems(config, items):
    import os

    import pytest

    if os.environ.get("SUPPORT_RUN_MODEL_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="model tests need SUPPORT_RUN_MODEL_TESTS=1")
    for item in items:
        if "models" in item.keywords:
            item.add_marker(skip)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_price_cache():
    """The model-price cache is per process; tests start (and end) with it empty."""
    from supportcore.analytics.pricing import clear_cache

    clear_cache()
    yield
    clear_cache()


@pytest.fixture(autouse=True)
def _no_llm_tracer(monkeypatch):
    """Core tests never trace to Langfuse, whatever the developer's environment says (SUPPORT_LLM_TRACER):
    a test that wants a tracer passes llm_tracer explicitly or builds one."""
    monkeypatch.setenv("SUPPORT_LLM_TRACER", "none")
