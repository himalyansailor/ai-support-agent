import threading

from opentelemetry import trace
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy.ext.asyncio import AsyncEngine

from supportcore.config import Settings

_lock = threading.Lock()
_provider: TracerProvider | None = None
_test_exporter: InMemorySpanExporter | None = None


def get_test_span_exporter() -> InMemorySpanExporter:
    if _test_exporter is None:
        raise RuntimeError("configure_tracing() has not been called with env='test'")
    return _test_exporter


def _ensure_provider(settings: Settings) -> TracerProvider:
    """Create and install the global tracer provider once per process."""
    global _provider, _test_exporter
    with _lock:
        if _provider is not None:
            return _provider
        provider = TracerProvider(resource=Resource.create({"service.name": settings.service_name}))
        if settings.env == "test":
            _test_exporter = InMemorySpanExporter()
            provider.add_span_processor(SimpleSpanProcessor(_test_exporter))
        else:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

            exporter = OTLPSpanExporter(endpoint=settings.otlp_endpoint, insecure=True)
            provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _provider = provider
        return provider


def configure_tracing(settings: Settings, engine: AsyncEngine | None = None) -> TracerProvider:
    """Install the tracer provider (once) and instrument the engine. Web-framework
    instrumentation is the app's job (see supportapi.observability)."""
    provider = _ensure_provider(settings)
    if engine is not None:
        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine, tracer_provider=provider)
    return provider


def instrument_celery(provider: TracerProvider) -> None:
    """Celery spans and trace-context propagation through task headers. The worker calls it in each
    pool process; a process that sends jobs to Celery calls it so the worker's spans join its trace."""
    from opentelemetry.instrumentation.celery import CeleryInstrumentor  # only worker/producer processes

    instrumentor = CeleryInstrumentor()
    if not instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.instrument(tracer_provider=provider)
