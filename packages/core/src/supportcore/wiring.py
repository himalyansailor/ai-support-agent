"""Adapter wiring shared by the API container and the worker.

This module (with apps/api container.py and apps/worker) is the only place that imports adapter
modules; domain services depend on ports. Later Phase 2 tasks add registries and fields here.
"""

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

import redis.asyncio as redis_asyncio

from supportcore.ai.catalog import RERANKER_MODELS, get_embedding_spec
from supportcore.ai.chat.fake_chat import fake_chat_model
from supportcore.ai.chat.langchain_factory import (
    anthropic_chat_model,
    openai_chat_model,
    openai_compatible_chat_model,
)
from supportcore.ai.chat.scripted_chat import scripted_chat_model
from supportcore.ai.credentials import CredentialValidator
from supportcore.ai.embeddings.fake_embedder import FakeEmbedder
from supportcore.ai.embeddings.local_embedder import LocalEmbedder
from supportcore.ai.embeddings.openai_embedder import OpenAIEmbedder
from supportcore.ai.ports import (
    ChatModelFactory,
    Embedder,
    EmbedderFactory,
    Reranker,
    RerankerFactory,
)
from supportcore.ai.rerank.cross_encoder_reranker import (
    DEFAULT_MODEL as DEFAULT_RERANK_MODEL,
)
from supportcore.ai.rerank.cross_encoder_reranker import CrossEncoderReranker
from supportcore.ai.rerank.fake_reranker import FakeReranker
from supportcore.ai.rerank.none_reranker import NoneReranker
from supportcore.ai.validation import ProviderCredentialValidator
from supportcore.assignment.ports import AssignmentStrategy
from supportcore.assignment.round_robin import RoundRobinStrategy
from supportcore.billing.counter_ports import UsageCounter
from supportcore.billing.dev_billing import DevBilling
from supportcore.billing.fake_billing import FakeBilling
from supportcore.billing.memory_counter import MemoryUsageCounter
from supportcore.billing.ports import BillingProvider
from supportcore.billing.redis_counter import RedisUsageCounter
from supportcore.billing.stripe_billing import StripeBilling
from supportcore.bot.checkpointer import CheckpointerProvider, psycopg_conninfo
from supportcore.bot.memory_checkpointer import MemoryCheckpointer
from supportcore.bot.postgres_checkpointer import PostgresCheckpointer
from supportcore.config import Settings
from supportcore.db.session import create_engine
from supportcore.inbox.postgres_search import PostgresConversationSearch
from supportcore.inbox.search_ports import ConversationSearch
from supportcore.jobs.celery_queue import PRODUCER_CONFIG, CeleryJobQueue
from supportcore.jobs.inline_queue import InlineJobQueue
from supportcore.jobs.ports import JobQueue, JobRuntime
from supportcore.knowledge.chunking.langchain_chunkers import (
    FixedChunker,
    MarkdownChunker,
    RecursiveChunker,
)
from supportcore.knowledge.chunking.ports import Chunker
from supportcore.knowledge.parsing.docx import DocxParser
from supportcore.knowledge.parsing.html import HtmlParser
from supportcore.knowledge.parsing.pdf import PdfParser
from supportcore.knowledge.parsing.ports import DocumentParser
from supportcore.knowledge.parsing.text import TextParser
from supportcore.knowledge.vectorstore.chroma_store import ChromaVectorStore
from supportcore.knowledge.vectorstore.pgvector_store import PgVectorStore
from supportcore.knowledge.vectorstore.ports import VectorStore
from supportcore.llmtrace.langfuse_tracer import langfuse_tracer
from supportcore.llmtrace.none_tracer import NoneLlmTracer
from supportcore.llmtrace.ports import LlmTracer
from supportcore.mail.fake_inbound import FakeInboundMailbox
from supportcore.mail.fake_outbound import FakeOutboundMailSender
from supportcore.mail.imap_inbound import ImapToolsInboundMailbox
from supportcore.mail.ports import InboundMailbox, OutboundMailSender
from supportcore.mail.smtp_outbound import AiosmtplibOutboundSender
from supportcore.metricsquery.fake_query import FakeMetricsQuery
from supportcore.metricsquery.ports import MetricsQuery
from supportcore.metricsquery.prometheus_query import PrometheusMetricsQuery
from supportcore.notifications.console_sender import ConsoleEmailSender
from supportcore.notifications.ports import EmailSender
from supportcore.notifications.smtp_sender import SmtpEmailSender
from supportcore.observability.logging import get_logger
from supportcore.presence.memory_presence import MemoryAgentPresence
from supportcore.presence.ports import AgentPresence
from supportcore.presence.redis_presence import RedisAgentPresence
from supportcore.pubsub.memory_pubsub import MemoryPubSub
from supportcore.pubsub.ports import PubSub
from supportcore.pubsub.redis_pubsub import RedisPubSub
from supportcore.ratelimit.memory_limiter import MemoryRateLimiter
from supportcore.ratelimit.ports import RateLimiter
from supportcore.ratelimit.redis_limiter import RedisRateLimiter
from supportcore.registry import ProviderRegistry, UnknownProviderError
from supportcore.risk.ports import RiskAssessor
from supportcore.risk.rules_llm import RulesLlmAssessor
from supportcore.secrets.fernet_cipher import FernetCipher
from supportcore.secrets.ports import SecretCipher
from supportcore.storage.local_storage import LocalFileStorage
from supportcore.storage.ports import FileStorage
from supportcore.storage.s3_storage import S3FileStorage
from supportcore.tools.http_executor import HttpToolExecutor
from supportcore.tools.ports import ToolExecutor
from supportcore.voice.realtime.fake_realtime import FakeSpeechToSpeech
from supportcore.voice.realtime.openai_realtime import OpenAIRealtime
from supportcore.voice.realtime.ports import RealtimeFactory, SpeechToSpeech
from supportcore.voice.stt.deepgram_stt import DeepgramSpeechToText
from supportcore.voice.stt.fake_stt import FakeSpeechToText
from supportcore.voice.stt.ports import SpeechToText, SttFactory
from supportcore.voice.telephony.fake_telephony import FakeTelephony
from supportcore.voice.telephony.ports import Telephony
from supportcore.voice.telephony.twilio_telephony import TwilioTelephony
from supportcore.voice.tts.fake_tts import FakeTextToSpeech
from supportcore.voice.tts.openai_tts import OpenAITextToSpeech
from supportcore.voice.tts.ports import TextToSpeech, TtsFactory
from supportcore.widget.identity.jwt_verifier import JwtHs256CustomerTokenVerifier
from supportcore.widget.identity.ports import CustomerTokenVerifier

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncEngine

logger = get_logger(__name__)

cipher_registry: ProviderRegistry[SecretCipher] = ProviderRegistry("secret_cipher")


@cipher_registry.register("fernet")
def _fernet_cipher(settings: Settings) -> SecretCipher:
    return FernetCipher(settings.effective_secrets_key())


# Embedder factories take (model, api_key, base_url, settings). The "fake" provider is added per
# build_adapters() call, and only when settings.env == "test".
embedder_registry: ProviderRegistry[Embedder] = ProviderRegistry("embedder")


@embedder_registry.register("openai")
def _openai_embedder(*, model: str, api_key: str | None, base_url: str | None, settings: Settings) -> Embedder:
    spec = get_embedding_spec("openai", model)
    return OpenAIEmbedder(model, api_key=api_key, base_url=base_url, settings=settings, dimensions=spec.dimensions)


@embedder_registry.register("openai_compatible")
def _openai_compatible_embedder(
    *, model: str, api_key: str | None, base_url: str | None, settings: Settings
) -> Embedder:
    spec = get_embedding_spec("openai_compatible", model)
    return OpenAIEmbedder(
        model, api_key=api_key, base_url=base_url, settings=settings, dimensions=spec.dimensions, compatible=True
    )


@embedder_registry.register("local")
def _local_embedder(*, model: str, api_key: str | None, base_url: str | None, settings: Settings) -> Embedder:
    if api_key is not None or base_url is not None:
        raise ValueError("The local embedding provider takes no api_key or base_url")
    spec = get_embedding_spec("local", model)
    assert spec.dimensions is not None
    return LocalEmbedder(model, dimensions=spec.dimensions, settings=settings)


def _fake_embedder(*, model: str, api_key: str | None, base_url: str | None, settings: Settings) -> Embedder:
    spec = get_embedding_spec("fake", model, include_fake=True)
    assert spec.dimensions is not None
    return FakeEmbedder(dimensions=spec.dimensions, model_id=model)


# Chat model factories take (model, api_key, base_url, temperature, max_tokens, timeout). The
# "openai_compatible" provider is added per build_adapters() call (it needs the settings' SSRF
# policy), and the "fake" and "scripted" ones too, only when settings.env == "test".
chat_registry: "ProviderRegistry[BaseChatModel]" = ProviderRegistry("chat_model")
chat_registry.register("anthropic")(anthropic_chat_model)
chat_registry.register("openai")(openai_chat_model)


# Reranker factories take (params, settings); params are the KB's reranker params. The "fake"
# reranker is added per build_adapters() call, and only when settings.env == "test".
reranker_registry: ProviderRegistry[Reranker] = ProviderRegistry("reranker")


def _check_params(params: dict, allowed: set[str]) -> None:
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise ValueError(f"Unknown reranker params: {', '.join(unknown)}")


@reranker_registry.register("none")
def _none_reranker(*, params: dict, settings: Settings) -> Reranker:
    _check_params(params, set())
    return NoneReranker()


@reranker_registry.register("cross_encoder")
def _cross_encoder_reranker(*, params: dict, settings: Settings) -> Reranker:
    _check_params(params, {"model"})
    model = params.get("model", DEFAULT_RERANK_MODEL)
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Reranker param 'model' must be a non-empty string")
    if model.strip() not in RERANKER_MODELS:  # never download an arbitrary hub model
        raise ValueError(f"Unknown reranker model {model.strip()[:100]!r}; use one of: {', '.join(RERANKER_MODELS)}")
    return CrossEncoderReranker(model.strip(), cache_dir=settings.model_cache_dir)


def _fake_reranker(*, params: dict, settings: Settings) -> Reranker:
    _check_params(params, set())
    return FakeReranker()


storage_registry: ProviderRegistry[FileStorage] = ProviderRegistry("file_storage")


@storage_registry.register("s3")
def _s3_storage(settings: Settings) -> FileStorage:
    return S3FileStorage(
        endpoint_url=settings.s3_endpoint_url,
        bucket=settings.s3_bucket,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key.get_secret_value(),
        region=settings.s3_region,
    )


@storage_registry.register("local")
def _local_storage(settings: Settings) -> FileStorage:
    return LocalFileStorage(settings.local_storage_path)


# Document parsers are keyed by the normalized type from detect_file_type(); factories take no args.
# Third-party parsing libraries are imported lazily inside each parser's parse().
parser_registry: ProviderRegistry[DocumentParser] = ProviderRegistry("document_parser")
parser_registry.register("pdf")(PdfParser)
parser_registry.register("docx")(DocxParser)
parser_registry.register("text")(TextParser)
parser_registry.register("markdown")(TextParser)
parser_registry.register("html")(HtmlParser)


# Chunkers are keyed by chunking strategy; factories take no args.
chunker_registry: ProviderRegistry[Chunker] = ProviderRegistry("chunker")
chunker_registry.register("fixed")(FixedChunker)
chunker_registry.register("recursive")(RecursiveChunker)
chunker_registry.register("markdown")(MarkdownChunker)


# Vector store factories take (settings, engine); engine is the app's AsyncEngine (pgvector reuses its pool).
vector_store_registry: ProviderRegistry[VectorStore] = ProviderRegistry("vector_store")


@vector_store_registry.register("pgvector")
def _pgvector_store(settings: Settings, engine: "AsyncEngine | None") -> VectorStore:
    if engine is None:
        raise ValueError("The pgvector store needs an AsyncEngine")
    return PgVectorStore(engine)


@vector_store_registry.register("chroma")
def _chroma_store(settings: Settings, engine: "AsyncEngine | None") -> VectorStore:
    return ChromaVectorStore(settings.chroma_url)


# Job queue factories take (settings, runtime_factory). runtime_factory builds the JobRuntime inline
# jobs run with; it is only called when a job runs, since the runtime holds these adapters.
job_queue_registry: ProviderRegistry[JobQueue] = ProviderRegistry("job_queue")


@job_queue_registry.register("inline")
def _inline_job_queue(settings: Settings, runtime_factory: "Callable[[], JobRuntime] | None") -> JobQueue:
    if runtime_factory is None:
        raise ValueError(
            "SUPPORT_JOB_QUEUE=inline needs a job runtime factory: build_adapters(..., runtime_factory=...)"
        )
    return InlineJobQueue(runtime_factory)


@job_queue_registry.register("celery")
def _celery_job_queue(settings: Settings, runtime_factory: "Callable[[], JobRuntime] | None") -> JobQueue:
    from celery import Celery  # imported lazily: only processes that send jobs need it

    producer = Celery("supportcore-producer", broker=settings.effective_celery_broker_url(), set_as_current=False)
    producer.conf.update(
        task_serializer="json", accept_content=["json"], task_ignore_result=True, **copy.deepcopy(PRODUCER_CONFIG)
    )
    return CeleryJobQueue(producer)


# Checkpointer factories take (settings); they build lazily, so creating one opens no connection.
checkpointer_registry: ProviderRegistry[CheckpointerProvider] = ProviderRegistry("checkpointer")


@checkpointer_registry.register("postgres")
def _postgres_checkpointer(settings: Settings) -> CheckpointerProvider:
    return PostgresCheckpointer(psycopg_conninfo(settings.database_url))


@checkpointer_registry.register("memory")
def _memory_checkpointer(settings: Settings) -> CheckpointerProvider:
    return MemoryCheckpointer()


rate_limiter_registry: ProviderRegistry[tuple[RateLimiter, "Redis | None"]] = ProviderRegistry("rate_limiter")


@rate_limiter_registry.register("redis")
def _redis_rate_limiter(settings: Settings) -> tuple[RateLimiter, "Redis | None"]:
    # from_url connects lazily, so building adapters never needs Redis to be up.
    client = redis_asyncio.from_url(settings.redis_url)
    return RedisRateLimiter(client), client


@rate_limiter_registry.register("memory")
def _memory_rate_limiter(settings: Settings) -> tuple[RateLimiter, "Redis | None"]:
    return MemoryRateLimiter(), None


presence_registry: ProviderRegistry[tuple[AgentPresence, "Redis | None"]] = ProviderRegistry("presence")


@presence_registry.register("redis")
def _redis_presence(settings: Settings) -> tuple[AgentPresence, "Redis | None"]:
    client = redis_asyncio.from_url(settings.redis_url)
    return RedisAgentPresence(client), client


@presence_registry.register("memory")
def _memory_presence(settings: Settings) -> tuple[AgentPresence, "Redis | None"]:
    return MemoryAgentPresence(), None


# Picks the agent an escalated conversation is assigned to (settings.assignment_strategy).
assignment_registry: ProviderRegistry[AssignmentStrategy] = ProviderRegistry("assignment_strategy")


@assignment_registry.register("round_robin")
def _round_robin(settings: Settings) -> AssignmentStrategy:
    return RoundRobinStrategy()


pubsub_registry: ProviderRegistry[tuple[PubSub, "Redis | None"]] = ProviderRegistry("pubsub")


@pubsub_registry.register("redis")
def _redis_pubsub(settings: Settings) -> tuple[PubSub, "Redis | None"]:
    client = redis_asyncio.from_url(settings.redis_url)  # lazy: building adapters never needs Redis up
    return RedisPubSub(client), client


@pubsub_registry.register("memory")
def _memory_pubsub(settings: Settings) -> tuple[PubSub, "Redis | None"]:
    return MemoryPubSub(), None


# Verifies the signed identity token a business's backend hands its logged-in user for the widget.
customer_token_registry: ProviderRegistry[CustomerTokenVerifier] = ProviderRegistry("customer_token_verifier")


@customer_token_registry.register("jwt_hs256")
def _jwt_hs256_customer_tokens(settings: Settings) -> CustomerTokenVerifier:
    return JwtHs256CustomerTokenVerifier()


# Decides per customer message whether the bot answers, offers a person or escalates.
risk_assessor_registry: ProviderRegistry[RiskAssessor] = ProviderRegistry("risk_assessor")


@risk_assessor_registry.register("rules_llm")
def _rules_llm_risk_assessor(settings: Settings) -> RiskAssessor:
    return RulesLlmAssessor()


# Calls a business tool's endpoint for the bot (and the dashboard's test button).
tool_executor_registry: ProviderRegistry[ToolExecutor] = ProviderRegistry("tool_executor")


@tool_executor_registry.register("http")
def _http_tool_executor(settings: Settings) -> ToolExecutor:
    return HttpToolExecutor(settings)


# Traces chat model calls (settings.llm_tracer); see supportcore.llmtrace.ports.
llm_tracer_registry: ProviderRegistry[LlmTracer] = ProviderRegistry("llm_tracer")


@llm_tracer_registry.register("none")
def _none_llm_tracer(settings: Settings) -> LlmTracer:
    return NoneLlmTracer()


@llm_tracer_registry.register("langfuse")
def _langfuse_llm_tracer(settings: Settings) -> LlmTracer:
    if not settings.langfuse_configured():  # refused in prod by Settings; elsewhere tracing stays off
        logger.warning(
            "llm_tracer_unconfigured",
            detail="SUPPORT_LLM_TRACER=langfuse needs SUPPORT_LANGFUSE_HOST, _PUBLIC_KEY and _SECRET_KEY; not tracing",
        )
        return NoneLlmTracer()
    assert settings.langfuse_host and settings.langfuse_public_key and settings.langfuse_secret_key
    # The open tracer for this key when one was built already (a retried build after a later adapter failed).
    return langfuse_tracer(
        host=settings.langfuse_host,
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key.get_secret_value(),
        capture_content=settings.langfuse_capture_content,
        project_id=settings.langfuse_project_id,
        environment=settings.env,
    )


# Instant PromQL queries for the platform admin overview (settings.metrics_query). The "fake" one is added per
# build_adapters() call, and only when settings.env == "test".
metrics_query_registry: ProviderRegistry[MetricsQuery] = ProviderRegistry("metrics_query")


@metrics_query_registry.register("prometheus")
def _prometheus_metrics_query(settings: Settings) -> MetricsQuery:
    return PrometheusMetricsQuery(settings.prometheus_url)


def _fake_metrics_query(settings: Settings) -> MetricsQuery:
    return FakeMetricsQuery()


# Email channel (settings.mail_inbound / mail_outbound). The "fake" ones are added per build_adapters() call, and
# only when settings.env == "test".
inbound_mail_registry: ProviderRegistry[InboundMailbox] = ProviderRegistry("inbound_mailbox")
outbound_mail_registry: ProviderRegistry[OutboundMailSender] = ProviderRegistry("outbound_mail")


@inbound_mail_registry.register("imap")
def _imap_inbound(settings: Settings) -> InboundMailbox:
    return ImapToolsInboundMailbox(settings)


@outbound_mail_registry.register("smtp")
def _smtp_outbound(settings: Settings) -> OutboundMailSender:
    return AiosmtplibOutboundSender(settings)


def _fake_inbound_mail(settings: Settings) -> InboundMailbox:
    return FakeInboundMailbox()


def _fake_outbound_mail(settings: Settings) -> OutboundMailSender:
    return FakeOutboundMailSender()


# Platform emails (invites, billing alerts): settings.email_sender. The API refuses "console" when env == "prod".
email_sender_registry: ProviderRegistry[EmailSender] = ProviderRegistry("email_sender")


@email_sender_registry.register("console")
def _console_email_sender(settings: Settings) -> EmailSender:
    return ConsoleEmailSender()


@email_sender_registry.register("smtp")
def _smtp_email_sender(settings: Settings) -> EmailSender:
    return SmtpEmailSender(host=settings.smtp_host, port=settings.smtp_port, from_addr=settings.smtp_from)


# Voice channel (settings.telephony; STT/TTS are chosen per organisation by provider id). The twilio/deepgram/openai
# adapters need the settings, so they are added per build_adapters() call, like the "fake" ones (the latter
# only when settings.env == "test").
telephony_registry: ProviderRegistry[Telephony] = ProviderRegistry("telephony")
stt_registry: ProviderRegistry[SpeechToText] = ProviderRegistry("speech_to_text")
tts_registry: ProviderRegistry[TextToSpeech] = ProviderRegistry("text_to_speech")
# Speech-to-speech models (chosen per organisation by provider id). The "openai" Realtime adapter (needs the settings)
# is added per build_adapters() call; "fake" only when env == "test".
speech_to_speech_registry: ProviderRegistry[SpeechToSpeech] = ProviderRegistry("speech_to_speech")


def _twilio_telephony(settings: Settings) -> Telephony:
    return TwilioTelephony(settings)


def _fake_telephony(settings: Settings) -> Telephony:
    return FakeTelephony()


# Billing (settings.billing). "dev" needs nothing; "stripe" needs the settings, so it is added per build_adapters()
# call; "fake" only when env == "test", one instance per build.
billing_registry: ProviderRegistry[BillingProvider] = ProviderRegistry("billing")


@billing_registry.register("dev")
def _dev_billing() -> BillingProvider:
    return DevBilling()


usage_counter_registry: ProviderRegistry[tuple[UsageCounter, "Redis | None"]] = ProviderRegistry("usage_counter")


@usage_counter_registry.register("redis")
def _redis_usage_counter(settings: Settings) -> tuple[UsageCounter, "Redis | None"]:
    client = redis_asyncio.from_url(settings.redis_url)  # lazy: building adapters never needs Redis to be up
    return RedisUsageCounter(client), client


@usage_counter_registry.register("memory")
def _memory_usage_counter(settings: Settings) -> tuple[UsageCounter, "Redis | None"]:
    return MemoryUsageCounter(), None


@dataclass
class Adapters:
    """Phase 2 adapters (storage, vector store, queue, ...) are added here."""

    cipher: SecretCipher
    embedders: EmbedderFactory
    chat_models: ChatModelFactory
    credential_validator: CredentialValidator
    storage: FileStorage
    # file type (pdf, docx, text, markdown, html) -> parser; look up with detect_file_type()'s result
    parsers: Mapping[str, DocumentParser]
    # chunking strategy (fixed, recursive, markdown) -> chunker
    chunkers: Mapping[str, Chunker]
    vector_store: VectorStore
    rerankers: RerankerFactory
    checkpointer: CheckpointerProvider
    rate_limiter: RateLimiter
    pubsub: PubSub
    presence: AgentPresence
    assignment: AssignmentStrategy
    customer_token_verifier: CustomerTokenVerifier
    risk_assessor: RiskAssessor
    tool_executor: ToolExecutor
    conversation_search: ConversationSearch
    metrics_query: MetricsQuery
    inbound_mailbox: InboundMailbox
    outbound_mail: OutboundMailSender
    telephony: Telephony
    speech_to_text: SttFactory
    text_to_speech: TtsFactory
    speech_to_speech: RealtimeFactory
    # Agents' registered browser softphones: the same backend as `presence`, in its own namespace.
    softphones: AgentPresence
    billing: BillingProvider
    usage_counter: UsageCounter
    # Platform emails to users (invites, billing alerts), settings.email_sender.
    email_sender: EmailSender
    # Chat model call tracing (settings.llm_tracer); aclose() flushes it.
    llm_tracer: LlmTracer = field(default_factory=NoneLlmTracer)
    # Background jobs (settings.job_queue); services enqueue only via after_commit().
    job_queue: JobQueue | None = None
    # The engine build_adapters() created because the caller passed none; aclose() disposes it.
    owned_engine: "AsyncEngine | None" = None
    # The Redis client behind the rate limiter, when it has one; aclose() closes it.
    rate_limiter_redis: "Redis | None" = None
    # The Redis client behind pubsub, when it has one; aclose() closes it.
    pubsub_redis: "Redis | None" = None
    # The Redis client behind presence, when it has one; aclose() closes it.
    presence_redis: "Redis | None" = None
    # The Redis client behind the usage counter, when it has one; aclose() closes it.
    usage_counter_redis: "Redis | None" = None

    async def aclose(self) -> None:
        """Releases resources these adapters own: the self-created engine and the Chroma HTTP client.
        A caller-provided engine is left alone (its owner disposes it). The LLM tracer is flushed first
        (bounded by a short timeout)."""
        await self.llm_tracer.aclose()
        await self.metrics_query.aclose()
        close = getattr(self.vector_store, "close", None)
        if close is not None:
            await close()
        await self.checkpointer.aclose()
        if self.rate_limiter_redis is not None:
            await self.rate_limiter_redis.aclose()
            self.rate_limiter_redis = None
        if self.pubsub_redis is not None:
            await self.pubsub_redis.aclose()
            self.pubsub_redis = None
        if self.presence_redis is not None:
            await self.presence_redis.aclose()
            self.presence_redis = None
        if self.usage_counter_redis is not None:
            await self.usage_counter_redis.aclose()
            self.usage_counter_redis = None
        if self.owned_engine is not None:
            await self.owned_engine.dispose()
            self.owned_engine = None


def build_adapters(
    settings: Settings,
    engine: "AsyncEngine | None" = None,
    *,
    runtime_factory: "Callable[[], JobRuntime] | None" = None,
) -> Adapters:
    """`engine` is the caller's AsyncEngine, shared with the pgvector store (the caller disposes it).
    Without one (and with pgvector selected), build_adapters creates a lazy engine owned by the
    Adapters; call `aclose()` to dispose it.

    `runtime_factory` is required for SUPPORT_JOB_QUEUE=inline: a zero-argument callable returning
    the JobRuntime (settings, session factory, these adapters, event bus) that inline jobs run with.
    It is called lazily, once a job runs (see supportapi.container.build_container)."""
    owned_engine = create_engine(settings) if engine is None and settings.vector_store == "pgvector" else None
    engine = engine if engine is not None else owned_engine
    embedders = embedder_registry.copy()
    chats = chat_registry.copy()
    chats.register("openai_compatible")(
        partial(openai_compatible_chat_model, allow_private_base_urls=settings.effective_allow_private_base_urls())
    )
    if settings.env == "test":
        embedders.register("fake")(_fake_embedder)
        chats.register("fake")(fake_chat_model)
        chats.register("scripted")(scripted_chat_model)
    rerankers = reranker_registry.copy()
    if settings.env == "test":
        rerankers.register("fake")(_fake_reranker)
    metrics_queries = metrics_query_registry.copy()
    if settings.env == "test":
        metrics_queries.register("fake")(_fake_metrics_query)
    inbound_mails, outbound_mails = inbound_mail_registry.copy(), outbound_mail_registry.copy()
    if settings.env == "test":
        inbound_mails.register("fake")(_fake_inbound_mail)
        outbound_mails.register("fake")(_fake_outbound_mail)
    telephonies, stts, ttss = telephony_registry.copy(), stt_registry.copy(), tts_registry.copy()
    telephonies.register("twilio")(_twilio_telephony)
    stts.register("deepgram")(partial(DeepgramSpeechToText, settings))
    ttss.register("openai")(partial(OpenAITextToSpeech, settings))
    if settings.env == "test":
        telephonies.register("fake")(_fake_telephony)
        fake_stt, fake_tts = FakeSpeechToText(), FakeTextToSpeech()  # one per build, so tests can inspect them
        stts.register("fake")(lambda: fake_stt)
        ttss.register("fake")(lambda: fake_tts)
    speech_to_speechs = speech_to_speech_registry.copy()
    speech_to_speechs.register("openai")(partial(OpenAIRealtime, settings))
    if settings.env == "test":
        fake_realtime = FakeSpeechToSpeech()  # one per build, so tests can inspect it
        speech_to_speechs.register("fake")(lambda: fake_realtime)
    billings = billing_registry.copy()
    billings.register("stripe")(partial(StripeBilling, settings))
    if settings.env == "test":
        fake_billing = FakeBilling(web_origin=settings.web_origin)  # one per build, so tests can inspect it
        billings.register("fake")(lambda: fake_billing)
    try:
        billing = billings.create(settings.billing)
    except UnknownProviderError as exc:
        raise ValueError(str(exc)) from None
    usage_counter, usage_counter_redis = usage_counter_registry.create(settings.usage_counter, settings=settings)
    try:
        telephony = telephonies.create(settings.telephony, settings=settings)
    except UnknownProviderError as exc:
        raise ValueError(str(exc)) from None
    rate_limiter, rate_limiter_redis = rate_limiter_registry.create(settings.rate_limiter, settings=settings)
    pubsub, pubsub_redis = pubsub_registry.create(settings.pubsub, settings=settings)
    presence, presence_redis = presence_registry.create(settings.presence, settings=settings)
    softphones: AgentPresence = (
        RedisAgentPresence(presence_redis, namespace="softphone")
        if presence_redis is not None
        else MemoryAgentPresence(namespace="softphone")
    )
    embedder_factory, chat_factory = EmbedderFactory(embedders), ChatModelFactory(chats)
    return Adapters(
        cipher=cipher_registry.create("fernet", settings=settings),
        embedders=embedder_factory,
        chat_models=chat_factory,
        credential_validator=ProviderCredentialValidator(embedder_factory, chat_factory, settings=settings),
        storage=storage_registry.create(settings.file_storage, settings=settings),
        parsers={key: parser_registry.create(key) for key in parser_registry.keys()},
        chunkers={key: chunker_registry.create(key) for key in chunker_registry.keys()},
        vector_store=vector_store_registry.create(settings.vector_store, settings=settings, engine=engine),
        rerankers=RerankerFactory(rerankers, settings),
        checkpointer=checkpointer_registry.create(settings.checkpointer, settings=settings),
        rate_limiter=rate_limiter,
        rate_limiter_redis=rate_limiter_redis,
        pubsub=pubsub,
        pubsub_redis=pubsub_redis,
        presence=presence,
        presence_redis=presence_redis,
        assignment=assignment_registry.create(settings.assignment_strategy, settings=settings),
        customer_token_verifier=customer_token_registry.create("jwt_hs256", settings=settings),
        risk_assessor=risk_assessor_registry.create("rules_llm", settings=settings),
        tool_executor=tool_executor_registry.create("http", settings=settings),
        conversation_search=PostgresConversationSearch(),
        metrics_query=metrics_queries.create(settings.metrics_query, settings=settings),
        inbound_mailbox=inbound_mails.create(settings.mail_inbound, settings=settings),
        outbound_mail=outbound_mails.create(settings.mail_outbound, settings=settings),
        telephony=telephony,
        speech_to_text=SttFactory(stts),
        text_to_speech=TtsFactory(ttss),
        speech_to_speech=RealtimeFactory(speech_to_speechs),
        softphones=softphones,
        billing=billing,
        usage_counter=usage_counter,
        usage_counter_redis=usage_counter_redis,
        email_sender=email_sender_registry.create(settings.email_sender, settings=settings),
        llm_tracer=llm_tracer_registry.create(settings.llm_tracer, settings=settings),
        job_queue=job_queue_registry.create(settings.job_queue, settings=settings, runtime_factory=runtime_factory),
        owned_engine=owned_engine,
    )
