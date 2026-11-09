"""LangfuseLlmTracer (settings.llm_tracer="langfuse"): the Langfuse SDK's LangChain CallbackHandler, one per
model call.

The SDK (v4, OpenTelemetry-based; v5 needs Langfuse server >= 4.59, the compose profile runs v3) gets its own TracerProvider, never the global one: prompts and
completions never reach the app's span exporter (Jaeger), and the app's spans never reach Langfuse. A
generation still joins the trace of the app span that is current when the model is called (the bot turn,
the playground run), so a turn's classifier and answer calls land in one Langfuse trace whose id is the
turn's OpenTelemetry trace id.

The trace attributes (session = conversation, tags, metadata, name; never a user id) are set on each
call's generation with the SDK's `propagate_attributes`, under a non-recording stand-in for the current app
span so they are not also written on the app span. A turn's calls share one trace, so the call's `kind`
(classifier | answer | playground) is observation metadata; the trace's tags are the union of its calls'
`kind:*` tags.

Each handler method is async, so LangChain awaits it on the event loop (no thread hop per streamed token);
it runs in a copy of the context (the SDK's context changes never leak into the caller) and catches every
exception. The SDK's own logs (logger `langfuse`, which may quote content) are dropped; its errors count
as failures too: `llm_trace_failed`, ids only, at most once a minute. Spans are sent by the SDK's batch
processor thread, never on the request path, each export bounded by EXPORT_TIMEOUT_SECONDS.

`aclose()` is bounded at process level: the final flush and the SDK's shutdown run on a daemon thread that
is awaited for at most `flush_timeout_seconds` (neither the loop's executor nor interpreter exit waits for
it); the SDK's shutdown unregisters its own atexit flush, and the provider registers none.

`langfuse_capture_content=False` deletes the input and output attributes (prompts, tool definitions,
completions) from every span before export: ids, model, usage and timings are still sent.

One tracer per public key per process (the SDK keeps a single client per public key, even after its
shutdown): building a second one raises.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

from langfuse import Langfuse, propagate_attributes
from langfuse.langchain import CallbackHandler
from langfuse.types import MaskOtelSpansParams, MaskOtelSpansResult, OtelSpanPatch
from opentelemetry import context as otel_context
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter

from supportcore.llmtrace.ports import TraceKind
from supportcore.observability.logging import get_logger

logger = get_logger(__name__)

FAILURE_LOG_INTERVAL_SECONDS = 60.0
FLUSH_TIMEOUT_SECONDS = 2.0
EXPORT_TIMEOUT_SECONDS = 5  # per OTLP request (the SDK default); background only, aclose() does not wait for it
_used_public_keys: set[str] = set()
_used_lock = threading.Lock()
# The tracer langfuse_tracer() built per public key (the SDK keeps one client per public key per process).
_tracers: dict[str, "LangfuseLlmTracer"] = {}
_tracers_lock = threading.Lock()
_CONTENT_ATTRIBUTES = (
    "langfuse.observation.input",
    "langfuse.observation.output",
    "langfuse.trace.input",
    "langfuse.trace.output",
)


class _FailureLog:
    """Logs `llm_trace_failed` at most once per interval (thread-safe: the SDK logs from its own threads)."""

    def __init__(self, interval: float = FAILURE_LOG_INTERVAL_SECONDS) -> None:
        self._interval = interval
        self._last: float | None = None
        self._lock = threading.Lock()

    def report(self, error_type: str, ids: dict[str, str] | None = None) -> None:
        now = time.monotonic()
        with self._lock:
            if self._last is not None and now - self._last < self._interval:
                return
            self._last = now
        logger.warning("llm_trace_failed", **(ids or {}), error_type=error_type)


class _SdkLogFilter(logging.Filter):
    """Drops the SDK's log records; errors count as tracing failures (warnings, e.g. an unparsed model
    name, do not)."""

    def __init__(self, failures: _FailureLog) -> None:
        super().__init__()
        self._failures = failures

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.ERROR:
            exc_type = record.exc_info[0] if record.exc_info else None
            self._failures.report(exc_type.__name__ if exc_type else f"langfuse_{record.levelname.lower()}")
        return False


def _drop_content(*, params: MaskOtelSpansParams) -> MaskOtelSpansResult:
    patches = {}
    for identifier, span in params.spans.items():
        present = tuple(key for key in _CONTENT_ATTRIBUTES if key in span.attributes)
        if present:
            patches[identifier] = OtelSpanPatch(delete_attributes=present)
    return MaskOtelSpansResult(span_patches=patches)


class _TracedHandler(CallbackHandler):
    """The SDK's handler for one model call (the call is the root run: its config carries only these
    callbacks), with the trace attributes applied and every failure contained."""

    def __init__(
        self,
        *,
        public_key: str,
        attributes: dict[str, Any],
        kind: str,
        ids: dict[str, str],
        failures: _FailureLog,
    ) -> None:
        super().__init__(public_key=public_key)
        self._trace_attributes = attributes
        self._kind = kind
        self._ids = ids
        self._failures = failures

    def _contained(self, method: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        try:
            contextvars.copy_context().run(method, *args, **kwargs)
        except Exception as exc:
            self._failures.report(type(exc).__name__, self._ids)

    def _start_root(self, start: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """Runs in the copied context: the stand-in span is never detached, the copy is dropped."""
        if kwargs.get("parent_run_id") is None:
            kwargs["metadata"] = {**(kwargs.get("metadata") or {}), "kind": self._kind}  # observation level
            parent = otel_trace.get_current_span().get_span_context()
            otel_context.attach(otel_trace.set_span_in_context(otel_trace.NonRecordingSpan(parent)))
            with propagate_attributes(**self._trace_attributes):
                start(*args, **kwargs)
        else:
            start(*args, **kwargs)

    async def on_chat_model_start(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        self._contained(self._start_root, super().on_chat_model_start, *args, **kwargs)

    async def on_llm_start(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        self._contained(self._start_root, super().on_llm_start, *args, **kwargs)

    async def on_llm_new_token(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        self._contained(super().on_llm_new_token, *args, **kwargs)

    async def on_llm_end(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        self._contained(super().on_llm_end, *args, **kwargs)

    async def on_llm_error(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        self._contained(super().on_llm_error, *args, **kwargs)


class LangfuseLlmTracer:
    def __init__(
        self,
        *,
        host: str,
        public_key: str,
        secret_key: str,
        capture_content: bool = True,
        project_id: str | None = None,
        environment: str | None = None,
        flush_timeout_seconds: float = FLUSH_TIMEOUT_SECONDS,
        span_exporter: SpanExporter | None = None,
    ) -> None:
        """`span_exporter` replaces the SDK's OTLP exporter (tests). `project_id` makes `trace_url` the
        project-scoped UI route. Raises ValueError for a public key a tracer of this process already used."""
        with _used_lock:
            if public_key in _used_public_keys:
                raise ValueError("A Langfuse tracer for this public key already exists in this process")
            _used_public_keys.add(public_key)
        self._host = host.rstrip("/")
        self._project_id = project_id
        self._public_key = public_key
        self._flush_timeout = flush_timeout_seconds
        self._failures = _FailureLog()
        self._log_filter = _SdkLogFilter(self._failures)
        logging.getLogger("langfuse").addFilter(self._log_filter)
        self._closed = False
        # Its own provider: never the global (app/Jaeger) one. No atexit hook: aclose() bounds the last flush.
        self._provider = TracerProvider(shutdown_on_exit=False)
        self._client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            base_url=self._host,
            environment=environment,
            tracer_provider=self._provider,
            timeout=EXPORT_TIMEOUT_SECONDS,
            mask_otel_spans=None if capture_content else _drop_content,
            span_exporter=span_exporter,
        )

    def callbacks(
        self,
        *,
        organisation_id: UUID,
        conversation_id: UUID | None = None,
        turn_key: str | None = None,
        kind: TraceKind,
        provider: str,
        model: str,
    ) -> list:
        ids = {"organisation_id": str(organisation_id), "kind": kind}
        if conversation_id is not None:
            ids["conversation_id"] = str(conversation_id)
        try:
            # Trace level: shared by a turn's calls, so no `kind` (it is on each generation).
            metadata = {key: value for key, value in ids.items() if key != "kind"}
            metadata.update(provider=provider, model=model)
            if turn_key is not None:
                metadata["turn"] = turn_key
            attributes: dict[str, Any] = {
                "trace_name": "playground" if kind == "playground" else "bot_turn",
                "tags": [f"kind:{kind}", f"organisation:{organisation_id}", f"provider:{provider}", f"model:{model}"],
                "metadata": metadata,
            }
            if conversation_id is not None:
                attributes["session_id"] = str(conversation_id)
            return [
                _TracedHandler(
                    public_key=self._public_key, attributes=attributes, kind=kind, ids=ids, failures=self._failures
                )
            ]
        except Exception as exc:  # never break the call: it runs untraced
            self._failures.report(type(exc).__name__, ids)
            return []

    def trace_id(self, callbacks: list) -> str | None:
        return next(
            (h.last_trace_id for h in callbacks if isinstance(h, CallbackHandler) and h.last_trace_id), None
        )

    def trace_url(self, trace_id: str) -> str | None:
        if self._project_id:
            return f"{self._host}/project/{self._project_id}/traces/{trace_id}"
        return f"{self._host}/trace/{trace_id}"  # the server redirects to the project's trace

    async def aclose(self) -> None:
        """Flushes and shuts the SDK down on a daemon thread, waiting at most flush_timeout_seconds: what is
        not sent by then is dropped, and nothing (the loop's executor, interpreter exit) waits for it."""
        if self._closed:
            return
        self._closed = True
        loop = asyncio.get_running_loop()
        done = loop.create_future()

        def settle(error: BaseException | None) -> None:
            if not done.done():
                done.set_result(error)

        def run() -> None:
            error: BaseException | None = None
            try:
                self._client.shutdown()  # flush, stop its threads, unregister its atexit flush
            except BaseException as exc:
                error = exc
            try:
                loop.call_soon_threadsafe(settle, error)
            except RuntimeError:  # the loop is gone: nobody is waiting
                pass

        threading.Thread(target=run, name="langfuse-shutdown", daemon=True).start()
        try:
            error = await asyncio.wait_for(asyncio.shield(done), self._flush_timeout)
        except TimeoutError:
            error = TimeoutError()
        if error is not None:
            self._failures.report(type(error).__name__)
        # The log filter stays: the shutdown thread may still log (content) after the wait.

    def shutdown(self) -> None:
        """Synchronous, unbounded shutdown (tests): stops the SDK's threads and removes the log filter."""
        self._closed = True
        logging.getLogger("langfuse").removeFilter(self._log_filter)
        self._client.shutdown()

    @property
    def closed(self) -> bool:
        return self._closed


def langfuse_tracer(*, public_key: str, **kwargs: Any) -> LangfuseLlmTracer:
    """The process's tracer for `public_key`: the open one when this process already built it (e.g. a worker
    retrying build_adapters after a later adapter failed to build), else a new LangfuseLlmTracer(**kwargs).
    Raises ValueError only when that key's tracer was closed: the SDK client cannot be rebuilt in-process."""
    with _tracers_lock:
        tracer = _tracers.get(public_key)
        if tracer is not None:
            if tracer.closed:
                raise ValueError("The Langfuse tracer for this public key was closed in this process")
            return tracer
        tracer = LangfuseLlmTracer(public_key=public_key, **kwargs)
        _tracers[public_key] = tracer
        return tracer
