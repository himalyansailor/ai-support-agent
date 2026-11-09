import base64
from functools import lru_cache
from pathlib import Path
from typing import Literal

import structlog
from pydantic import AliasChoices, Field, PrivateAttr, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _repo_root() -> Path:
    """The uv workspace root (holds uv.lock), found from this file; falls back to the CWD."""
    here = Path(__file__).resolve()
    return next((p for p in here.parents if (p / "uv.lock").is_file()), Path.cwd())


# Fixed Fernet key (urlsafe base64 of 32 bytes) used ONLY when SUPPORT_SECRETS_KEY is unset outside prod.
_DEV_SECRETS_KEY = base64.urlsafe_b64encode(b"support-dev-only-insecure-key!!!").decode()

# `cp .env.example .env` at the repo root is enough for local runs; real environment variables
# always take precedence over the file, and a missing file is ignored.
ENV_FILE = _repo_root() / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SUPPORT_", extra="ignore", env_file=ENV_FILE, env_file_encoding="utf-8", populate_by_name=True
    )

    env: Literal["dev", "test", "prod"] = "dev"
    database_url: str
    redis_url: str
    jwt_secret: SecretStr
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 2592000
    password_hasher: str = "argon2"
    token_service: str = "jwt"
    web_origin: str = "http://localhost:3000"
    email_sender: str = "console"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_from: str
    otlp_endpoint: str = "http://localhost:4317"
    service_name: str = "support-api"
    invite_ttl_seconds: int = 604800
    # Bearer token Prometheus must send to scrape /metrics. Unset: open in dev/test, 404 in prod.
    metrics_token: SecretStr | None = None
    # Shared secret the web server's proxy sends (X-Support-Internal-Token) so the frame-policy lookup is
    # rate limited per embedding visitor's IP, not per web server. Unset: the peer IP is always used.
    internal_api_token: SecretStr | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    # When set, JSON logs also go to this rotating file (10 MB x 5); Alloy tails it into Loki (make dev-*).
    log_file: str | None = None

    # Fernet key for encrypting org-provided API keys at rest. Required in prod (make gen-secrets-key).
    secrets_key: SecretStr | None = None

    # File storage
    file_storage: Literal["s3", "local"] = "s3"
    s3_endpoint_url: str | None = "http://localhost:9000"
    s3_bucket: str = "support-documents"
    s3_access_key: str = "support"
    s3_secret_key: SecretStr = SecretStr("supportsecret")
    s3_region: str = "us-east-1"
    local_storage_path: str = ".data/uploads"

    # Vector store
    vector_store: Literal["pgvector", "chroma"] = "pgvector"
    chroma_url: str = "http://localhost:8001"

    # Background jobs
    job_queue: Literal["inline", "celery"] = "celery"
    celery_broker_url: str | None = None  # falls back to redis_url
    worker_metrics_port: int = 9101

    # Phase 3: bot, widget
    checkpointer: Literal["postgres", "memory"] = "postgres"
    pubsub: Literal["redis", "memory"] = "redis"
    rate_limiter: Literal["redis", "memory"] = "redis"
    presence: Literal["redis", "memory"] = "redis"
    assignment_strategy: Literal["round_robin"] = "round_robin"
    trusted_proxy_hops: int = 0
    widget_session_ttl_seconds: int = 86400

    # Uploads and models
    max_upload_bytes: int = 26214400
    model_cache_dir: str = ".data/models"
    embed_batch_size: int = 64

    # Whether an openai_compatible base URL may point at loopback/private/link-local addresses
    # (e.g. a local Ollama). Unset: allowed in dev/test, refused in prod (SSRF guard).
    allow_private_base_urls: bool | None = None

    # LLM tracing (Phase 5): none, or langfuse (self-hosted: `docker compose --profile langfuse`). The keys
    # are a Langfuse project's API keys; langfuse is refused without them (and the host) when env=prod.
    llm_tracer: Literal["none", "langfuse"] = "none"
    langfuse_host: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    # The Langfuse project the keys belong to: trace links use its UI route. Also read from
    # LANGFUSE_INIT_PROJECT_ID (the compose profile's headless-init project).
    langfuse_project_id: str | None = Field(
        default=None, validation_alias=AliasChoices("SUPPORT_LANGFUSE_PROJECT_ID", "LANGFUSE_INIT_PROJECT_ID")
    )
    # Prompts and completions go to Langfuse (the platform's own infrastructure); False sends ids,
    # tags, the model, usage and timings only.
    langfuse_capture_content: bool = True

    # Platform admin overview (Phase 5): the metrics store it queries and the tools it links to. `metrics_query`
    # is "prometheus"; "fake" exists only when env=test. The Langfuse link reuses langfuse_host/_project_id.
    metrics_query: str = "prometheus"
    prometheus_url: str = "http://localhost:9090"
    grafana_url: str = "http://localhost:3001"
    jaeger_url: str = "http://localhost:16686"

    # Email channel (Phase 6): "imap"/"smtp" are the real adapters; "fake" exists only when env=test.
    mail_inbound: str = "imap"
    mail_outbound: str = "smtp"

    # Voice channel (Phase 7a): "twilio" is the real adapter; "fake" exists only when env=test. The Twilio pair is
    # the platform's master account (unset hides "Buy a number"). voice_public_url is the https:// origin Twilio
    # reaches apps/voice at; prod refuses http://.
    telephony: str = "twilio"
    twilio_account_sid: str | None = None
    twilio_auth_token: SecretStr | None = None
    voice_public_url: str | None = None
    voice_max_calls: int = Field(default=50, ge=1)
    voice_stream_token_ttl_seconds: int = Field(default=60, ge=1)
    # The voice process's own DB pool (pool_size; overflow VOICE_DB_MAX_OVERFLOW): every live call does short units
    # of work per utterance, turn lease and renewal. Other processes keep SQLAlchemy's default pool.
    voice_db_pool_size: int = Field(default=20, ge=1)

    # Billing (Phase 8): "stripe" is the real adapter, "dev" a local stand-in refused when env=prod, "fake" exists
    # only when env=test. The usage counter holds the month's totals ("redis"; "memory" is per process).
    billing: str = "dev"
    stripe_secret_key: SecretStr | None = None
    stripe_webhook_secret: SecretStr | None = None
    billing_currency: str = "usd"
    usage_counter: Literal["redis", "memory"] = "redis"

    _warned_dev_key: bool = PrivateAttr(default=False)

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, v: object) -> object:
        return v.upper() if isinstance(v, str) else v

    @field_validator(
        "metrics_token", "internal_api_token", "secrets_key", "celery_broker_url", "allow_private_base_urls",
        "langfuse_host", "langfuse_public_key", "langfuse_secret_key", "langfuse_project_id",
        "twilio_account_sid", "twilio_auth_token", "voice_public_url", "stripe_secret_key", "stripe_webhook_secret",
        mode="before",
    )
    @classmethod
    def _empty_token_is_unset(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @model_validator(mode="after")
    def _require_strong_secret_outside_dev(self) -> "Settings":
        if self.env != "dev" and len(self.jwt_secret.get_secret_value()) < 32:
            raise ValueError("SUPPORT_JWT_SECRET must be at least 32 characters when SUPPORT_ENV != dev")
        return self

    @model_validator(mode="after")
    def _require_secrets_key_in_prod(self) -> "Settings":
        if self.env == "prod" and self.secrets_key is None:
            raise ValueError("SUPPORT_SECRETS_KEY is required when SUPPORT_ENV=prod (make gen-secrets-key)")
        return self

    @model_validator(mode="after")
    def _refuse_in_memory_adapters_in_prod(self) -> "Settings":
        # Per-process state: with more than one instance, fan-out, limits and resumable turns break.
        if self.env == "prod":
            for name, value in (
                ("SUPPORT_PUBSUB", self.pubsub),
                ("SUPPORT_RATE_LIMITER", self.rate_limiter),
                ("SUPPORT_PRESENCE", self.presence),
                ("SUPPORT_CHECKPOINTER", self.checkpointer),
                ("SUPPORT_USAGE_COUNTER", self.usage_counter),
            ):
                if value == "memory":
                    raise ValueError(f"{name}=memory is not allowed when SUPPORT_ENV=prod")
        return self

    @model_validator(mode="after")
    def _require_langfuse_keys_in_prod(self) -> "Settings":
        if self.env == "prod" and self.llm_tracer == "langfuse" and not self.langfuse_configured():
            raise ValueError(
                "SUPPORT_LLM_TRACER=langfuse needs SUPPORT_LANGFUSE_HOST, SUPPORT_LANGFUSE_PUBLIC_KEY and "
                "SUPPORT_LANGFUSE_SECRET_KEY when SUPPORT_ENV=prod"
            )
        if self.env == "prod" and self.llm_tracer == "langfuse" and not str(self.langfuse_host).startswith("https://"):
            # The secret key (Basic auth) and the prompts must not travel in clear text.
            raise ValueError("SUPPORT_LANGFUSE_HOST must be an https:// URL when SUPPORT_ENV=prod")
        return self

    @field_validator("voice_public_url", mode="after")
    @classmethod
    def _normalise_voice_public_url(cls, v: str | None) -> str | None:
        # Twilio signs the exact URL, so the origin is kept without whitespace or a trailing slash.
        return (v.strip().rstrip("/") or None) if v is not None else None

    @model_validator(mode="after")
    def _require_https_voice_url_in_prod(self) -> "Settings":
        if self.env == "prod" and self.voice_public_url is not None and not self.voice_public_url.lower().startswith("https://"):
            # Twilio webhooks are signed against this URL and carry caller data.
            raise ValueError("SUPPORT_VOICE_PUBLIC_URL must be an https:// URL when SUPPORT_ENV=prod")
        return self

    @field_validator("billing_currency", mode="after")
    @classmethod
    def _normalise_billing_currency(cls, v: str) -> str:
        v = v.strip().lower()
        if len(v) != 3 or not v.isascii() or not v.isalpha():
            raise ValueError("SUPPORT_BILLING_CURRENCY must be a 3-letter currency code")
        return v

    @model_validator(mode="after")
    def _check_billing_in_prod(self) -> "Settings":
        if self.env == "prod":
            if self.billing == "dev":
                raise ValueError("SUPPORT_BILLING=dev is not allowed when SUPPORT_ENV=prod")
            if self.billing == "stripe" and (self.stripe_secret_key is None or self.stripe_webhook_secret is None):
                raise ValueError(
                    "SUPPORT_BILLING=stripe needs SUPPORT_STRIPE_SECRET_KEY and SUPPORT_STRIPE_WEBHOOK_SECRET "
                    "when SUPPORT_ENV=prod"
                )
        return self

    def langfuse_configured(self) -> bool:
        return bool(self.langfuse_host and self.langfuse_public_key and self.langfuse_secret_key)

    def effective_secrets_key(self) -> str:
        """The Fernet key to use; outside prod falls back to a fixed insecure dev key (warns once)."""
        if self.secrets_key is not None:
            return self.secrets_key.get_secret_value()
        if not self._warned_dev_key:
            self._warned_dev_key = True
            structlog.get_logger().warning(
                "secrets_key_dev_fallback", detail="SUPPORT_SECRETS_KEY unset; using insecure dev key"
            )
        return _DEV_SECRETS_KEY

    def effective_allow_private_base_urls(self) -> bool:
        if self.allow_private_base_urls is not None:
            return self.allow_private_base_urls
        return self.env in ("dev", "test")

    def effective_celery_broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
