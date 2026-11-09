from pathlib import Path

import pytest
from pydantic import ValidationError

from supportcore.config import ENV_FILE, Settings


def _base_env(monkeypatch):
    monkeypatch.setenv("SUPPORT_DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("SUPPORT_REDIS_URL", "redis://h:6379/0")
    monkeypatch.setenv("SUPPORT_JWT_SECRET", "x" * 32)
    monkeypatch.setenv("SUPPORT_SMTP_FROM", "noreply@example.com")
    # prod refuses the dev billing adapter and Stripe without its secrets
    monkeypatch.setenv("SUPPORT_BILLING", "stripe")
    monkeypatch.setenv("SUPPORT_STRIPE_SECRET_KEY", "sk_live_x")
    monkeypatch.setenv("SUPPORT_STRIPE_WEBHOOK_SECRET", "whsec_x")


def test_settings_reads_prefixed_env(monkeypatch):
    _base_env(monkeypatch)
    s = Settings(_env_file=None)
    assert s.database_url.endswith("/db")
    assert s.access_token_ttl_seconds == 900
    assert s.env == "dev"


def test_settings_rejects_short_jwt_secret_outside_dev(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("SUPPORT_ENV", "prod")
    monkeypatch.setenv("SUPPORT_JWT_SECRET", "short")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_allows_short_jwt_secret_in_dev(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("SUPPORT_JWT_SECRET", "short")
    assert Settings(_env_file=None).jwt_secret.get_secret_value() == "short"


def test_empty_metrics_token_means_unset(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("SUPPORT_METRICS_TOKEN", "")
    assert Settings(_env_file=None).metrics_token is None
    monkeypatch.setenv("SUPPORT_METRICS_TOKEN", "abc")
    assert Settings(_env_file=None).metrics_token.get_secret_value() == "abc"


def test_log_level_is_case_insensitive(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("SUPPORT_LOG_LEVEL", "debug")
    assert Settings(_env_file=None).log_level == "DEBUG"
    monkeypatch.setenv("SUPPORT_LOG_LEVEL", "loud")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_env_file_is_the_repo_root_dotenv():
    root = Path(__file__).resolve().parents[3]
    assert (root / "uv.lock").exists()
    assert ENV_FILE == root / ".env"
    assert Settings.model_config["env_file"] == ENV_FILE


def test_dotenv_values_are_read_and_env_vars_win(monkeypatch, tmp_path):
    for key in ("DATABASE_URL", "REDIS_URL", "JWT_SECRET", "SMTP_FROM", "SERVICE_NAME"):
        monkeypatch.delenv(f"SUPPORT_{key}", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SUPPORT_DATABASE_URL=postgresql+asyncpg://u:p@h/fromfile\n"
        "SUPPORT_REDIS_URL=redis://h:6379/0\n"
        "SUPPORT_JWT_SECRET=" + "y" * 32 + "\n"
        "SUPPORT_SMTP_FROM=file@example.com\n"
        "SUPPORT_SERVICE_NAME=fromfile\n"
    )
    s = Settings(_env_file=env_file)
    assert s.database_url.endswith("/fromfile") and s.service_name == "fromfile"
    monkeypatch.setenv("SUPPORT_SERVICE_NAME", "fromenv")
    assert Settings(_env_file=env_file).service_name == "fromenv"


def test_secrets_key_required_in_prod(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("SUPPORT_ENV", "prod")
    monkeypatch.delenv("SUPPORT_SECRETS_KEY", raising=False)
    with pytest.raises(ValidationError, match="SECRETS_KEY"):
        Settings(_env_file=None)


def test_prod_accepts_explicit_secrets_key(monkeypatch):
    from cryptography.fernet import Fernet

    _base_env(monkeypatch)
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("SUPPORT_ENV", "prod")
    monkeypatch.setenv("SUPPORT_SECRETS_KEY", key)
    assert Settings(_env_file=None).effective_secrets_key() == key


def test_dev_secrets_key_fallback_is_valid_fernet(monkeypatch, caplog):
    from cryptography.fernet import Fernet

    _base_env(monkeypatch)
    monkeypatch.delenv("SUPPORT_SECRETS_KEY", raising=False)
    s = Settings(_env_file=None)
    token = Fernet(s.effective_secrets_key()).encrypt(b"hello")
    assert Fernet(s.effective_secrets_key()).decrypt(token) == b"hello"


def test_dev_secrets_key_fallback_warns_once(monkeypatch):
    import structlog

    _base_env(monkeypatch)
    monkeypatch.delenv("SUPPORT_SECRETS_KEY", raising=False)
    s = Settings(_env_file=None)
    with structlog.testing.capture_logs() as logs:
        s.effective_secrets_key()
        s.effective_secrets_key()
    assert len([e for e in logs if e["log_level"] == "warning"]) == 1


def test_phase2_defaults(monkeypatch):
    _base_env(monkeypatch)
    s = Settings(_env_file=None)
    assert s.max_upload_bytes == 26214400
    assert s.vector_store == "pgvector"
    assert s.job_queue == "celery"
    assert s.file_storage == "s3"
    assert s.s3_bucket == "support-documents"
    assert s.s3_secret_key.get_secret_value() == "supportsecret"
    assert s.chroma_url == "http://localhost:8001"
    assert s.worker_metrics_port == 9101
    assert s.embed_batch_size == 64
    assert s.celery_broker_url is None
    assert s.effective_celery_broker_url() == s.redis_url


def test_phase3_defaults(monkeypatch):
    _base_env(monkeypatch)
    s = Settings(_env_file=None)
    assert s.checkpointer == "postgres"
    assert s.pubsub == "redis"
    assert s.rate_limiter == "redis"
    assert s.presence == "redis"
    assert s.trusted_proxy_hops == 0
    assert s.widget_session_ttl_seconds == 86400


def test_empty_internal_api_token_means_unset(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("SUPPORT_INTERNAL_API_TOKEN", "")
    assert Settings(_env_file=None).internal_api_token is None
    monkeypatch.setenv("SUPPORT_INTERNAL_API_TOKEN", "abc")
    assert Settings(_env_file=None).internal_api_token.get_secret_value() == "abc"


@pytest.mark.parametrize(
    ("variable", "value"),
    [("SUPPORT_PUBSUB", "memory"), ("SUPPORT_RATE_LIMITER", "memory"), ("SUPPORT_CHECKPOINTER", "memory"), ("SUPPORT_PRESENCE", "memory")],
)
def test_prod_refuses_in_memory_adapters(monkeypatch, variable, value):
    from cryptography.fernet import Fernet

    _base_env(monkeypatch)
    monkeypatch.setenv("SUPPORT_ENV", "prod")
    monkeypatch.setenv("SUPPORT_SECRETS_KEY", Fernet.generate_key().decode())
    assert Settings(_env_file=None).pubsub == "redis"  # the defaults are fine
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValidationError, match=variable):
        Settings(_env_file=None)
    monkeypatch.setenv("SUPPORT_ENV", "dev")
    assert Settings(_env_file=None).env == "dev"  # allowed outside prod


def _prod_env(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("SUPPORT_ENV", "prod")
    monkeypatch.setenv("SUPPORT_SECRETS_KEY", "k" * 44)


def test_prod_refuses_dev_billing(monkeypatch):
    _prod_env(monkeypatch)
    monkeypatch.setenv("SUPPORT_BILLING", "dev")
    with pytest.raises(ValidationError, match="SUPPORT_BILLING"):
        Settings(_env_file=None)


def test_prod_stripe_requires_secrets(monkeypatch):
    _prod_env(monkeypatch)
    monkeypatch.delenv("SUPPORT_STRIPE_SECRET_KEY")
    monkeypatch.delenv("SUPPORT_STRIPE_WEBHOOK_SECRET")
    with pytest.raises(ValidationError, match="SUPPORT_STRIPE"):
        Settings(_env_file=None)
    monkeypatch.setenv("SUPPORT_STRIPE_SECRET_KEY", "sk_live_x")
    with pytest.raises(ValidationError, match="SUPPORT_STRIPE"):
        Settings(_env_file=None)
    monkeypatch.setenv("SUPPORT_STRIPE_WEBHOOK_SECRET", "whsec_x")
    assert Settings(_env_file=None).billing == "stripe"


def test_prod_refuses_memory_usage_counter(monkeypatch):
    _prod_env(monkeypatch)
    monkeypatch.setenv("SUPPORT_USAGE_COUNTER", "memory")
    with pytest.raises(ValidationError, match="SUPPORT_USAGE_COUNTER"):
        Settings(_env_file=None)


def test_billing_currency_lowercased(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("SUPPORT_BILLING_CURRENCY", "USD")
    assert Settings(_env_file=None).billing_currency == "usd"
    monkeypatch.setenv("SUPPORT_BILLING_CURRENCY", "dollars")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_empty_stripe_secrets_mean_unset(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("SUPPORT_STRIPE_SECRET_KEY", "")
    monkeypatch.setenv("SUPPORT_STRIPE_WEBHOOK_SECRET", "")
    s = Settings(_env_file=None)
    assert s.stripe_secret_key is None and s.stripe_webhook_secret is None
