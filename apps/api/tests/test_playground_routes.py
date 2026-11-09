"""Playground (Task 13): retrieval plus the answer the bot would give, over real commits (`kb_client`)."""

import logging
import re
import uuid

from kb_helpers import create_kb, create_org, join_org, signup, upload

from supportcore.ai.ports import ChatModelFactory
from supportcore.observability.tracing import get_test_span_exporter
from supportcore.registry import ProviderRegistry

TIMING_KEYS = {"embed", "search", "rerank", "generate"}
CHUNK_KEYS = {
    "chunk_id", "knowledge_base_id", "document_id", "document_name", "ordinal", "text", "score", "rerank_score", "metadata",
}
FAKE_CHAT = {"chat_provider": "fake", "chat_model": "fake-chat", "chat_credential_id": None, "temperature": 0.0}


async def _indexed_kb(kb_client, kb_app, email="owner@x.com"):
    headers = await signup(kb_client, email)
    org_id = await create_org(kb_client, headers)
    kb_id = (await create_kb(kb_client, org_id, headers))["id"]
    doc = (await upload(kb_client, org_id, kb_id, headers)).json()
    r = await kb_client.post(f"/orgs/{org_id}/kbs/{kb_id}/documents/index", json={"document_ids": [doc["id"]]}, headers=headers)
    assert r.status_code == 202
    await kb_app.state.job_queue.drain()
    return headers, org_id, kb_id


def _url(org_id, kb_id):
    return f"/orgs/{org_id}/kbs/{kb_id}/playground"


async def _use_fake_chat(kb_client, org_id, headers):
    r = await kb_client.put(f"/orgs/{org_id}/ai/settings", json=FAKE_CHAT, headers=headers)
    assert r.status_code == 200 and r.json()["ready"] is True


async def test_playground_without_chat_settings_warns(kb_client, kb_app):
    headers, org_id, kb_id = await _indexed_kb(kb_client, kb_app)
    # Default AI settings: the recommended anthropic model without an API key -> not ready.
    r = await kb_client.post(_url(org_id, kb_id), json={"question": "How do refunds work?"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] is None and body["warnings"] == ["no_chat_model_configured"]
    assert body["chunks"] and set(body["chunks"][0]) == CHUNK_KEYS
    assert body["chunks"][0]["document_name"] == "refunds.md" and "refund" in body["chunks"][0]["text"].lower()
    assert set(body["timings_ms"]) == TIMING_KEYS and body["timings_ms"]["generate"] == 0


async def test_playground_retrieval_only_and_validation(kb_client, kb_app):
    headers, org_id, kb_id = await _indexed_kb(kb_client, kb_app)
    r = await kb_client.post(
        _url(org_id, kb_id), json={"question": "refund", "top_k": 1, "with_answer": False}, headers=headers
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["chunks"]) == 1 and body["answer"] is None and body["warnings"] == []
    for bad in ({"question": "   "}, {"question": "x" * 2001}, {"question": "q", "top_k": 0}, {"question": "q", "top_k": 21}):
        assert (await kb_client.post(_url(org_id, kb_id), json=bad, headers=headers)).status_code == 422, bad


async def test_playground_answer_with_citations(kb_client, kb_app):
    headers, org_id, kb_id = await _indexed_kb(kb_client, kb_app)
    await _use_fake_chat(kb_client, org_id, headers)
    r = await kb_client.post(_url(org_id, kb_id), json={"question": "How do refunds work?"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["warnings"] == []
    assert body["answer"] == {
        "text": "Fake answer [1]",
        "citations": [body["chunks"][0]["chunk_id"]],
        "provider": "fake",
        "model": "fake-chat",
    }
    assert re.fullmatch(r"[0-9a-f]{32}", body["trace_id"])
    assert set(body["timings_ms"]) == TIMING_KEYS and all(isinstance(v, int) for v in body["timings_ms"].values())


class _FailingModel:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def ainvoke(self, messages):
        raise self.error


def _failing_chat_factory(error: BaseException) -> ChatModelFactory:
    registry: ProviderRegistry = ProviderRegistry("chat_model")
    registry.register("fake")(lambda **_kwargs: _FailingModel(error))
    return ChatModelFactory(registry)


async def test_playground_provider_error_is_sanitised_warning(kb_client, kb_app, caplog):
    headers, org_id, kb_id = await _indexed_kb(kb_client, kb_app)
    await _use_fake_chat(kb_client, org_id, headers)
    adapters = kb_app.state.container.adapters
    original = adapters.chat_models
    question = "Private question walrus-banjo-77 about refunds"
    caplog.set_level(logging.DEBUG)
    adapters.chat_models = _failing_chat_factory(
        RuntimeError("upstream exploded token=abcdefghijklmnopqrstuvwxyz0123 answer-fragment-xyz")
    )
    try:
        r = await kb_client.post(_url(org_id, kb_id), json={"question": question}, headers=headers)
        adapters.chat_models = _failing_chat_factory(TimeoutError())
        timed_out = await kb_client.post(_url(org_id, kb_id), json={"question": question}, headers=headers)
    finally:
        adapters.chat_models = original
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] is None and body["chunks"]
    assert body["warnings"] == ["chat_model_error: upstream exploded token=*** answer-fragment-xyz"]
    assert timed_out.json()["warnings"] == ["chat_model_error: timed out"]

    # Logs carry the error type only: never the question, the context or the provider's text.
    logged = [caplog.text, *(str(rec.msg) for rec in caplog.records)]
    assert any("playground_chat_error" in line for line in logged)
    for line in logged:
        assert "walrus-banjo" not in line
        assert "answer-fragment-xyz" not in line and "abcdefghijklmnopqrstuvwxyz0123" not in line
        assert not any(c["text"] in line for c in body["chunks"])


async def test_playground_cross_tenant_404(kb_client, kb_app, session_factory):
    headers, org_id, kb_id = await _indexed_kb(kb_client, kb_app)
    other = await signup(kb_client, "other@x.com")
    other_org = await create_org(kb_client, other, "Other")
    body = {"question": "refunds?"}
    # Their own org, someone else's KB.
    r = await kb_client.post(_url(other_org, kb_id), json=body, headers=other)
    assert r.status_code == 404
    # Someone else's org.
    assert (await kb_client.post(_url(org_id, kb_id), json=body, headers=other)).status_code == 404
    assert (await kb_client.post(_url(org_id, uuid.uuid4()), json=body, headers=headers)).status_code == 404
    # kb.manage is required: an Agent of the org gets 403.
    agent = await join_org(kb_client, session_factory, org_id, "agent@x.com", role_name="Agent")
    assert (await kb_client.post(_url(org_id, kb_id), json=body, headers=agent)).status_code == 403


async def test_playground_span_has_no_question_text(kb_client, kb_app):
    headers, org_id, kb_id = await _indexed_kb(kb_client, kb_app)
    await _use_fake_chat(kb_client, org_id, headers)
    question = "Secret question about refunds zebra-quokka-42"
    exporter = get_test_span_exporter()
    exporter.clear()
    r = await kb_client.post(_url(org_id, kb_id), json={"question": question, "top_k": 3}, headers=headers)
    assert r.status_code == 200
    body = r.json()

    spans = exporter.get_finished_spans()
    (run,) = [s for s in spans if s.name == "playground.run"]
    attrs = dict(run.attributes or {})
    assert attrs["kb.id"] == kb_id and attrs["retrieval.top_k"] == 3
    assert attrs["retrieval.hits"] == len(body["chunks"])
    assert (attrs["llm.provider"], attrs["llm.model"]) == ("fake", "fake-chat")
    assert format(run.context.trace_id, "032x") == body["trace_id"]
    chunk_texts = [c["text"] for c in body["chunks"]]
    for span in spans:
        dumped = span.name + " " + " ".join(str(v) for v in (span.attributes or {}).values())
        assert "zebra-quokka" not in dumped
        assert not any(t in dumped for t in chunk_texts)
        assert "Fake answer" not in dumped


class _StubValidator:
    async def validate(self, provider, api_key, base_url) -> None:
        return None


async def test_playground_free_text_compatible_chat_model(kb_client, kb_app):
    from langchain_core.language_models import FakeListChatModel

    headers, org_id, kb_id = await _indexed_kb(kb_client, kb_app)
    container = kb_app.state.container
    container.adapters.credential_validator = _StubValidator()
    key = "sk-compat-SECRET-5678"
    r = await kb_client.post(
        f"/orgs/{org_id}/ai/credentials",
        json={"provider": "openai_compatible", "label": "Local", "api_key": key, "base_url": "http://10.0.0.5/v1"},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    settings = {"chat_provider": "openai_compatible", "chat_model": "my-model", "chat_credential_id": r.json()["id"], "temperature": 0.3}
    r = await kb_client.put(f"/orgs/{org_id}/ai/settings", json=settings, headers=headers)
    assert r.status_code == 200 and r.json()["ready"] is True  # free-text ids match the "*" spec

    created: list[dict] = []

    def compatible(**kwargs):
        created.append(kwargs)
        return FakeListChatModel(responses=["Compatible answer [1][1]"])

    registry: ProviderRegistry = ProviderRegistry("chat_model")
    registry.register("openai_compatible")(compatible)
    original = container.adapters.chat_models
    container.adapters.chat_models = ChatModelFactory(registry)
    try:
        r = await kb_client.post(_url(org_id, kb_id), json={"question": "refunds?"}, headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["warnings"] == []
        assert body["answer"] == {
            "text": "Compatible answer [1][1]",
            "citations": [body["chunks"][0]["chunk_id"]],
            "provider": "openai_compatible",
            "model": "my-model",
        }
        assert created == [
            {"model": "my-model", "api_key": key, "base_url": "http://10.0.0.5/v1", "temperature": 0.3, "max_tokens": None, "timeout": 60.0}
        ]

        # The SSRF guard runs before the provider call, and its error is a real error, not a warning.
        container.settings.allow_private_base_urls = False
        r = await kb_client.post(_url(org_id, kb_id), json={"question": "refunds?"}, headers=headers)
        assert r.status_code == 422 and r.json()["code"] == "credential_invalid"
        assert len(created) == 1
    finally:
        container.adapters.chat_models = original
        container.settings.allow_private_base_urls = None


async def test_playground_holds_no_transaction_during_provider_calls(kb_client, kb_app):
    """The query embedding and the chat call run with the request session's transaction ended."""
    from dataclasses import replace

    from supportcore.ai.embeddings.fake_embedder import FakeEmbedder
    from supportcore.ai.ports import EmbedderFactory
    from supportcore.db.session import get_current_session

    headers, org_id, kb_id = await _indexed_kb(kb_client, kb_app)
    await _use_fake_chat(kb_client, org_id, headers)
    observed: list[tuple[str, bool]] = []

    class _Embedder(FakeEmbedder):
        async def embed_query(self, text):
            observed.append(("embed", get_current_session().in_transaction()))
            return await super().embed_query(text)

    class _Chat:
        async def ainvoke(self, messages):
            observed.append(("chat", get_current_session().in_transaction()))
            from langchain_core.messages import AIMessage

            return AIMessage(content="Answer [1]")

    embedders: ProviderRegistry = ProviderRegistry("embedder")
    embedders.register("fake")(lambda **_kw: _Embedder(dimensions=8))
    chats: ProviderRegistry = ProviderRegistry("chat_model")
    chats.register("fake")(lambda **_kw: _Chat())
    container = kb_app.state.container
    original = container.adapters
    container.adapters = replace(original, embedders=EmbedderFactory(embedders), chat_models=ChatModelFactory(chats))
    try:
        r = await kb_client.post(_url(org_id, kb_id), json={"question": "How do refunds work?"}, headers=headers)
    finally:
        container.adapters = original
    assert r.status_code == 200, r.text
    assert r.json()["answer"]["text"] == "Answer [1]" and r.json()["chunks"]
    assert observed == [("embed", False), ("chat", False)]


async def test_playground_compatible_chat_error_hides_the_response_body(kb_client, kb_app):
    """A real openai_compatible chat model against a loopback endpoint answering 502 with a body."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    secret = b"INTERNAL-METADATA-SECRET-VALUE-ABC"

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = b'{"error": {"message": "' + secret + b'"}}'
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        headers, org_id, kb_id = await _indexed_kb(kb_client, kb_app)
        kb_app.state.container.adapters.credential_validator = _StubValidator()
        r = await kb_client.post(
            f"/orgs/{org_id}/ai/credentials",
            json={"provider": "openai_compatible", "label": "Local", "api_key": "sk-compat-123456",
                  "base_url": f"http://127.0.0.1:{server.server_address[1]}/v1"},
            headers=headers,
        )
        assert r.status_code == 201, r.text
        settings = {"chat_provider": "openai_compatible", "chat_model": "m", "chat_credential_id": r.json()["id"], "temperature": 0.0}
        assert (await kb_client.put(f"/orgs/{org_id}/ai/settings", json=settings, headers=headers)).status_code == 200
        r = await kb_client.post(_url(org_id, kb_id), json={"question": "refunds?"}, headers=headers)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert r.status_code == 200, r.text
    assert r.json()["warnings"] == ["chat_model_error: Provider returned HTTP 502"]
    assert "INTERNAL" not in r.text


async def test_playground_connect_time_credential_error_is_warning(kb_client, kb_app):
    """The hardened client refuses a connection with CredentialInvalidError during ainvoke: a warning."""
    from supportcore.ai.errors import CredentialInvalidError

    headers, org_id, kb_id = await _indexed_kb(kb_client, kb_app)
    await _use_fake_chat(kb_client, org_id, headers)
    adapters = kb_app.state.container.adapters
    original = adapters.chat_models
    adapters.chat_models = _failing_chat_factory(CredentialInvalidError("Base URL resolves to a private address"))
    try:
        r = await kb_client.post(_url(org_id, kb_id), json={"question": "refunds?"}, headers=headers)
    finally:
        adapters.chat_models = original
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] is None and len(body["warnings"]) == 1
    assert body["warnings"][0].startswith("chat_model_error: ")
