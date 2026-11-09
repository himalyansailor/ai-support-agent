import pytest
from supportcore.billing.errors import InvalidWebhookError
from supportcore.billing.fake_billing import FakeBilling
from supportcore.config import Settings
from supportcore.wiring import build_adapters


def _settings(**overrides) -> Settings:
    base = dict(
        env="test",
        database_url="postgresql+asyncpg://u:p@h/db",
        redis_url="redis://localhost:6379/0",
        rate_limiter="memory",
        presence="memory",
        pubsub="memory",
        usage_counter="memory",
        jwt_secret="test-secret-test-secret-test-secret-123",
        smtp_from="noreply@test.local",
        llm_tracer="none",
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


async def test_fake_billing_only_in_test():
    adapters = build_adapters(_settings(billing="fake"))
    try:
        assert isinstance(adapters.billing, FakeBilling)
    finally:
        await adapters.aclose()
    with pytest.raises(ValueError, match="fake"):
        build_adapters(_settings(env="dev", billing="fake"))


async def test_dev_billing_grants_everything():
    adapters = build_adapters(_settings(env="dev", billing="dev"))
    try:
        assert adapters.billing.key == "dev"
        assert adapters.billing.grants_everything is True
    finally:
        await adapters.aclose()


async def test_stripe_billing_is_the_real_adapter_and_builds_without_network():
    adapters = build_adapters(_settings(billing="stripe", stripe_secret_key="sk_x", stripe_webhook_secret="whsec_x"))
    try:
        assert type(adapters.billing).__name__ == "StripeBilling"
        assert adapters.billing.grants_everything is False
        with pytest.raises(InvalidWebhookError):
            adapters.billing.parse_webhook(b"{}", {})
    finally:
        await adapters.aclose()


async def test_usage_counter_redis_client_is_closed_on_aclose():
    adapters = build_adapters(_settings(usage_counter="redis"))
    assert adapters.usage_counter_redis is not None
    await adapters.aclose()
    assert adapters.usage_counter_redis is None


async def test_email_sender_selected_from_settings():
    from supportcore.notifications.console_sender import ConsoleEmailSender
    from supportcore.notifications.smtp_sender import SmtpEmailSender

    for name, kind in (("console", ConsoleEmailSender), ("smtp", SmtpEmailSender)):
        adapters = build_adapters(_settings(email_sender=name))
        try:
            assert isinstance(adapters.email_sender, kind)
        finally:
            await adapters.aclose()
