from prometheus_client import Counter, Gauge, Histogram, make_asgi_app

HTTP_REQUESTS = Counter(
    "support_http_requests_total",
    "HTTP requests handled",
    ["method", "route", "status", "organisation_id"],
)
HTTP_LATENCY = Histogram(
    "support_http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "route"],
)

# Background indexing (supportcore.knowledge.indexing). kind: document | rebuild.
INDEXING_JOBS = Counter(
    "support_indexing_jobs_total",
    "Indexing jobs finished, by outcome",
    ["kind", "status", "organisation_id"],
)
INDEXING_JOB_DURATION = Histogram(
    "support_indexing_job_duration_seconds",
    "Indexing job duration in seconds",
    ["kind"],
    buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1800),
)
INDEXED_CHUNKS = Counter(
    "support_indexed_chunks_total",
    "Chunks written to the vector store",
    ["organisation_id"],
)
EMBEDDING_REQUESTS = Counter(
    "support_embedding_requests_total",
    "Embedding provider calls (one per batch)",
    ["provider", "status"],
)

# The bot (supportcore.bot). outcome: answered | offered_human | escalated | error.
BOT_TURNS = Counter(
    "support_bot_turns_total",
    "Bot turns finished, by outcome",
    ["outcome", "organisation_id"],
)
BOT_TURN_DURATION = Histogram(
    "support_bot_turn_duration_seconds",
    "Bot turn duration in seconds",
    buckets=(0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120),
)
# rule_kind: topic, a built-in or system rule key (low_confidence, write_action, bot_error, ...) or none.
RISK_DECISIONS = Counter(
    "support_risk_decisions_total",
    "Final risk decision of each bot turn",
    ["action", "rule_kind", "source"],
)
# kind: input | output. model: a catalog model id, or "other" (see supportcore.bot.metrics.model_label).
LLM_TOKENS = Counter(
    "support_llm_tokens_total",
    "Chat model tokens used by the bot (answers and the risk classifier)",
    ["provider", "model", "kind"],
)
# status: ok | failed | rejected (unknown tool, invalid arguments, over the limit) | escalated (write tool).
TOOL_CALLS = Counter(
    "support_tool_calls_total",
    "Business tool calls requested by the bot",
    ["status"],
)
# kind: first_response | resolution.
SLA_BREACHES = Counter(
    "support_sla_breaches_total",
    "Conversations that missed an SLA target",
    ["kind"],
)

# One per chat-model call. provider: a catalog provider or "other"; kind: answer | classifier | playground;
# status: ok | failed (see supportcore.bot.metrics.record_llm_request, which bounds the values).
LLM_REQUESTS = Counter(
    "support_llm_requests_total",
    "Chat model calls, by provider, kind and outcome",
    ["provider", "kind", "status"],
)
# kind: widget | inbox. Open WebSockets of this API process.
WS_CONNECTIONS = Gauge(
    "support_ws_connections",
    "Open WebSocket connections",
    ["kind"],
)
# support_job_queue_depth{queue} is exported by the worker (supportworker.metrics.QueueDepthCollector),
# which reads the Redis list length at scrape time.


def metrics_app():
    return make_asgi_app()
