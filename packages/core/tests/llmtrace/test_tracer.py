"""The LLM tracer port: `none` (no callbacks) and the Langfuse adapter.

No Langfuse server is involved: the adapter is given an in-memory span exporter in place of its OTLP
exporter, so the tests read exactly the spans it would send. Each test uses its own public key (the
SDK keeps one client per public key per process).
"""

import asyncio
import json
import logging
import threading
import time
import uuid

import httpx
import pytest
import structlog.testing
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import ValidationError

from supportcore.config import Settings
from supportcore.llmtrace.langfuse_tracer import LangfuseLlmTracer
from supportcore.llmtrace.none_tracer import NoneLlmTracer

ORG = uuid.uuid4()
CONVERSATION = uuid.uuid4()
QUESTION = "Where is my parcel 7781?"
ANSWER = "It ships tomorrow."


def _settings(**kwargs) -> Settings:
    for name in ("langfuse_host", "langfuse_public_key", "langfuse_secret_key", "langfuse_project_id"):
        kwargs.setdefault(name, None)  # not from the environment (.env.example sets them)
    kwargs.setdefault("billing", "stripe")  # prod refuses the dev billing adapter
    kwargs.setdefault("stripe_secret_key", "sk_x")
    kwargs.setdefault("stripe_webhook_secret", "whsec_x")
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@h/db",
        redis_url="redis://h:6379/0",
        jwt_secret="x" * 32,
        smtp_from="noreply@example.com",
        **kwargs,
    )


@pytest.fixture
def exporter():
    return InMemorySpanExporter()


@pytest.fixture
def make_tracer(exporter):
    made: list[LangfuseLlmTracer] = []

    def make(**kwargs) -> LangfuseLlmTracer:
        tracer = LangfuseLlmTracer(
            host="http://langfuse.test",
            public_key=f"pk-lf-test-{uuid.uuid4().hex}",
            secret_key="sk-lf-test",
            span_exporter=exporter,
            **kwargs,
        )
        made.append(tracer)
        return tracer

    yield make
    for tracer in made:
        tracer.shutdown()


def _callbacks(tracer, kind="answer", **kwargs):
    return tracer.callbacks(
        organisation_id=ORG,
        conversation_id=kwargs.pop("conversation_id", CONVERSATION),
        turn_key=kwargs.pop("turn_key", "msg-1"),
        kind=kind,
        provider="openai",
        model="gpt-test",
    )


def _generations(exporter):
    return [s for s in exporter.get_finished_spans() if s.attributes.get("langfuse.observation.type") == "generation"]


def _all_text(spans) -> str:
    return "\n".join(span.to_json() for span in spans)


async def test_none_tracer_no_callbacks():
    tracer = NoneLlmTracer()
    callbacks = tracer.callbacks(
        organisation_id=ORG, conversation_id=CONVERSATION, turn_key="m", kind="answer", provider="openai", model="m"
    )
    assert callbacks == []
    assert tracer.trace_id(callbacks) is None
    assert tracer.trace_url("abc") is None
    await tracer.aclose()


def test_wiring_defaults_to_the_none_tracer():
    from supportcore.wiring import build_adapters

    assert isinstance(build_adapters(_settings(env="test", vector_store="chroma")).llm_tracer, NoneLlmTracer)


async def test_langfuse_callbacks_tagged(make_tracer, exporter):
    tracer = make_tracer()
    callbacks = _callbacks(tracer, kind="answer")
    model = FakeListChatModel(responses=[ANSWER])

    chunks = [chunk async for chunk in model.astream([HumanMessage(QUESTION)], config={"callbacks": callbacks})]
    await tracer.aclose()  # flushes

    assert "".join(c.content for c in chunks) == ANSWER
    [generation] = _generations(exporter)
    attributes = generation.attributes
    assert attributes["session.id"] == str(CONVERSATION)
    assert "user.id" not in attributes  # customers are never Langfuse users
    assert set(attributes["langfuse.trace.tags"]) == {
        "kind:answer",
        f"organisation:{ORG}",
        "provider:openai",
        "model:gpt-test",
    }
    metadata = {
        key.removeprefix("langfuse.trace.metadata."): value
        for key, value in attributes.items()
        if key.startswith("langfuse.trace.metadata.")
    }
    assert metadata == {  # trace level: shared by a turn's calls, so no kind
        "organisation_id": str(ORG),
        "conversation_id": str(CONVERSATION),
        "turn": "msg-1",
        "provider": "openai",
        "model": "gpt-test",
    }
    assert attributes["langfuse.observation.metadata.kind"] == "answer"
    assert attributes["langfuse.trace.name"] == "bot_turn"
    assert QUESTION in attributes["langfuse.observation.input"]  # content capture is on by default
    assert ANSWER in attributes["langfuse.observation.output"]
    assert tracer.trace_id(callbacks) == format(generation.context.trace_id, "032x")
    assert tracer.trace_url("abc123") == "http://langfuse.test/trace/abc123"  # no project id: the redirect
    assert make_tracer(project_id="proj-1").trace_url("abc123") == "http://langfuse.test/project/proj-1/traces/abc123"


async def test_playground_trace_has_no_session(make_tracer, exporter):
    tracer = make_tracer()
    callbacks = _callbacks(tracer, kind="playground", conversation_id=None, turn_key=None)

    await FakeListChatModel(responses=[ANSWER]).ainvoke([HumanMessage(QUESTION)], config={"callbacks": callbacks})
    await tracer.aclose()

    [generation] = _generations(exporter)
    assert "session.id" not in generation.attributes
    assert generation.attributes["langfuse.trace.name"] == "playground"
    assert "langfuse.trace.metadata.conversation_id" not in generation.attributes
    assert "kind:playground" in generation.attributes["langfuse.trace.tags"]


async def test_langfuse_spans_stay_off_the_app_tracer_provider(make_tracer, exporter):
    """The SDK gets its own TracerProvider: our spans (Jaeger) get no prompts or Langfuse attributes, and
    the generation joins the trace of the span that is current when the model is called."""
    app_exporter = InMemorySpanExporter()
    app_provider = TracerProvider()
    app_provider.add_span_processor(SimpleSpanProcessor(app_exporter))
    tracer = make_tracer()

    with app_provider.get_tracer("app").start_as_current_span("bot.generate") as app_span:
        await FakeListChatModel(responses=[ANSWER]).ainvoke(
            [HumanMessage(QUESTION)], config={"callbacks": _callbacks(tracer)}
        )
    await tracer.aclose()

    [app_finished] = app_exporter.get_finished_spans()
    assert not any(key.startswith("langfuse.") or key == "session.id" for key in app_finished.attributes)
    assert QUESTION not in _all_text([app_finished])
    [generation] = _generations(exporter)
    assert generation.context.trace_id == app_span.get_span_context().trace_id
    assert all(span.name != "bot.generate" for span in exporter.get_finished_spans())


async def test_capture_content_false_strips_io(make_tracer, exporter):
    tracer = make_tracer(capture_content=False)
    callbacks = _callbacks(tracer)

    await FakeListChatModel(responses=[ANSWER]).ainvoke(
        [SystemMessage("You are Acme's assistant"), HumanMessage(QUESTION)], config={"callbacks": callbacks}
    )
    await tracer.aclose()

    [generation] = _generations(exporter)
    assert "langfuse.observation.input" not in generation.attributes
    assert "langfuse.observation.output" not in generation.attributes
    text = _all_text(exporter.get_finished_spans())
    assert QUESTION not in text and ANSWER not in text and "Acme's assistant" not in text
    assert generation.attributes["session.id"] == str(CONVERSATION)  # ids, tags and the model stay
    assert "kind:answer" in generation.attributes["langfuse.trace.tags"]


async def test_tracer_failure_does_not_break_turn(make_tracer, monkeypatch, capsys, caplog):
    """Every SDK callback raises: the model still answers, and `llm_trace_failed` is logged once, ids only."""
    from langfuse.langchain import CallbackHandler

    def boom(*args, **kwargs):
        raise RuntimeError(f"sdk exploded on {QUESTION}")

    for name in ("on_chat_model_start", "on_llm_start", "on_llm_new_token", "on_llm_end", "on_llm_error"):
        monkeypatch.setattr(CallbackHandler, name, boom)
    tracer = make_tracer()
    model = FakeListChatModel(responses=[ANSWER, ANSWER])
    caplog.set_level(logging.DEBUG)

    with structlog.testing.capture_logs() as logs:
        first = await model.ainvoke([HumanMessage(QUESTION)], config={"callbacks": _callbacks(tracer)})
        streamed = [c async for c in model.astream([HumanMessage(QUESTION)], config={"callbacks": _callbacks(tracer)})]

    assert first.content == ANSWER
    assert "".join(c.content for c in streamed) == ANSWER
    failures = [e for e in logs if e["event"] == "llm_trace_failed"]
    assert failures == [
        {
            "event": "llm_trace_failed",
            "log_level": "warning",
            "organisation_id": str(ORG),
            "conversation_id": str(CONVERSATION),
            "kind": "answer",
            "error_type": "RuntimeError",
        }
    ]  # at most once per minute, ids only
    captured = capsys.readouterr()
    assert QUESTION not in captured.out + captured.err + caplog.text  # never the SDK's message


async def test_sdk_internal_errors_are_reported_not_logged(make_tracer, capsys, caplog):
    """The SDK catches its own errors and logs them (with tracebacks) on the `langfuse` logger: those
    records become one rate-limited `llm_trace_failed` instead."""
    tracer = make_tracer()
    caplog.set_level(logging.DEBUG)

    with structlog.testing.capture_logs() as logs:
        logging.getLogger("langfuse").error("failed to serialise %s", QUESTION)
        logging.getLogger("langfuse").error("again %s", QUESTION)

    assert [e["event"] for e in logs] == ["llm_trace_failed"]
    captured = capsys.readouterr()
    assert QUESTION not in captured.out + captured.err + caplog.text
    tracer.shutdown()
    assert not logging.getLogger("langfuse").filters  # shutdown removes the filter


async def test_tracer_failure_when_building_callbacks(make_tracer, monkeypatch):
    import supportcore.llmtrace.langfuse_tracer as module

    def boom(*args, **kwargs):
        raise RuntimeError("no client")

    monkeypatch.setattr(module, "_TracedHandler", boom)
    tracer = make_tracer()

    with structlog.testing.capture_logs() as logs:
        assert _callbacks(tracer) == []
    assert [e["event"] for e in logs] == ["llm_trace_failed"]


async def test_api_key_never_in_trace_payload(make_tracer, exporter):
    """A real provider client (ChatOpenAI over a mock transport): the key is sent to the provider only."""
    from langchain_openai import ChatOpenAI

    secret = "sk-supersecret-do-not-trace-1234"
    seen_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_keys.append(request.headers.get("authorization", ""))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-test",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": ANSWER}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            },
        )

    model = ChatOpenAI(
        model="gpt-test",
        api_key=secret,
        base_url="https://llm.example.com/v1",
        http_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_retries=0,
    )
    tracer = make_tracer()

    reply = await model.ainvoke([HumanMessage(QUESTION)], config={"callbacks": _callbacks(tracer)})
    await tracer.aclose()

    assert reply.content == ANSWER
    assert seen_keys == [f"Bearer {secret}"]
    spans = exporter.get_finished_spans()
    assert _generations(exporter)
    assert secret not in _all_text(spans)
    assert all(secret not in json.dumps(dict(span.resource.attributes)) for span in spans)


def test_prod_requires_keys():
    prod = {"env": "prod", "secrets_key": "k" * 44, "llm_tracer": "langfuse"}
    with pytest.raises(ValidationError, match="SUPPORT_LANGFUSE"):
        _settings(**prod)
    with pytest.raises(ValidationError, match="SUPPORT_LANGFUSE"):
        _settings(**prod, langfuse_host="https://lf.example.com", langfuse_public_key="pk")  # no secret key
    with pytest.raises(ValidationError, match="SUPPORT_LANGFUSE"):
        _settings(**prod, langfuse_public_key="pk", langfuse_secret_key="sk")  # no host
    with pytest.raises(ValidationError, match="https"):
        _settings(**prod, langfuse_host="http://lf.example.com", langfuse_public_key="pk", langfuse_secret_key="sk")
    assert _settings(langfuse_host="http://localhost:3030").langfuse_host == "http://localhost:3030"  # dev: any
    ok = _settings(**prod, langfuse_host="https://lf.example.com", langfuse_public_key="pk", langfuse_secret_key="sk")
    assert ok.llm_tracer == "langfuse" and ok.langfuse_capture_content is True
    assert "sk" not in repr(ok.langfuse_secret_key)
    assert _settings(env="prod", secrets_key="k" * 44).llm_tracer == "none"  # the default needs nothing
    assert _settings(langfuse_public_key="", langfuse_secret_key="").langfuse_public_key is None


def test_langfuse_without_keys_outside_prod_falls_back_to_none():
    from supportcore.wiring import build_adapters

    settings = _settings(env="test", vector_store="chroma", llm_tracer="langfuse")
    with structlog.testing.capture_logs() as logs:
        assert isinstance(build_adapters(settings).llm_tracer, NoneLlmTracer)
    assert "llm_tracer_unconfigured" in [e["event"] for e in logs]


def test_a_public_key_gets_one_tracer_per_process(make_tracer):
    tracer = make_tracer()
    with pytest.raises(ValueError, match="already exists"):
        LangfuseLlmTracer(host="http://langfuse.test", public_key=tracer._public_key, secret_key="sk")


def test_the_wired_tracer_is_reused_per_public_key_and_refused_once_closed():
    """build_adapters run again in the same process (a worker task retried after a later adapter failed to build)
    gets the open tracer back instead of "already exists"; only a closed one is refused."""
    from supportcore.llmtrace.langfuse_tracer import langfuse_tracer
    from supportcore.wiring import build_adapters

    key = f"pk-lf-test-{uuid.uuid4().hex}"
    settings = _settings(
        env="test", vector_store="chroma", llm_tracer="langfuse", langfuse_host="http://langfuse.test",
        langfuse_public_key=key, langfuse_secret_key="sk-lf-test",
    )
    first = build_adapters(settings).llm_tracer
    try:
        assert isinstance(first, LangfuseLlmTracer)
        assert build_adapters(settings).llm_tracer is first
        assert langfuse_tracer(public_key=key, host="http://langfuse.test", secret_key="sk") is first
    finally:
        first.shutdown()
    with pytest.raises(ValueError, match="was closed"):
        build_adapters(settings)


class _SlowExporter(InMemorySpanExporter):
    """An exporter that hangs (Langfuse black-holed)."""

    def __init__(self, seconds: float) -> None:
        super().__init__()
        self.seconds = seconds
        self.started = threading.Event()

    def export(self, spans):
        self.started.set()
        time.sleep(self.seconds)
        return super().export(spans)


def test_aclose_is_bounded_at_process_level():
    """The flush runs on a daemon thread: neither aclose(), nor asyncio.run()'s executor shutdown, waits for a
    hung export."""
    exporter = _SlowExporter(seconds=6.0)
    tracer = LangfuseLlmTracer(
        host="http://langfuse.test",
        public_key=f"pk-lf-test-{uuid.uuid4().hex}",
        secret_key="sk",
        span_exporter=exporter,
        flush_timeout_seconds=0.5,
    )

    async def main() -> None:
        await FakeListChatModel(responses=[ANSWER]).ainvoke(
            [HumanMessage(QUESTION)], config={"callbacks": _callbacks(tracer)}
        )
        with structlog.testing.capture_logs() as logs:
            await tracer.aclose()
        assert [e["error_type"] for e in logs if e["event"] == "llm_trace_failed"] == ["TimeoutError"]

    started = time.monotonic()
    asyncio.run(main())
    elapsed = time.monotonic() - started
    assert elapsed < 0.5 + 1.0, elapsed
    assert exporter.started.wait(5)  # the export did start (and is still hanging on its daemon thread)


def test_tool_definitions_sent_to_the_model_hold_only_the_schema():
    """The SDK copies bound tool definitions into the generation input: they carry no URL or auth value."""
    from supportcore.tools.ports import ToolSnapshot
    from supportcore.tools.schema import tool_definition

    tool = ToolSnapshot(
        id=uuid.uuid4(),
        name="get_order",
        description="Looks up an order",
        method="GET",
        url_template="https://api.example.com/orders/{order_id}",
        input_schema={"type": "object", "properties": {"order_id": {"type": "string"}}},
        read_only=True,
        auth_header_name="X-Api-Key",
        auth_value="secret-tool-key",
    )
    definition = tool_definition(tool)
    assert set(definition) == {"name", "description", "parameters"}
    assert "secret-tool-key" not in json.dumps(definition) and "api.example.com" not in json.dumps(definition)
