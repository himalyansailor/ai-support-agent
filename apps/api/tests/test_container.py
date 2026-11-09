import uuid

import pytest

from supportapi.container import build_container
from supportcore.notifications.console_sender import ConsoleEmailSender
from supportcore.notifications.smtp_sender import SmtpEmailSender


def test_email_sender_selected_from_settings(test_settings):
    assert isinstance(build_container(test_settings).email_sender, ConsoleEmailSender)
    smtp = test_settings.model_copy(update={"email_sender": "smtp"})
    assert isinstance(build_container(smtp).email_sender, SmtpEmailSender)


def test_console_email_sender_refused_in_prod(test_settings):
    prod = test_settings.model_copy(update={"env": "prod", "email_sender": "console"})
    with pytest.raises(ValueError, match="SUPPORT_EMAIL_SENDER=console is not allowed"):
        build_container(prod)
    assert isinstance(
        build_container(prod.model_copy(update={"email_sender": "smtp"})).email_sender, SmtpEmailSender
    )


def test_container_exposes_adapters(test_settings):
    from supportcore.wiring import Adapters

    assert isinstance(build_container(test_settings).adapters, Adapters)


async def test_inline_job_queue_runs_jobs_with_the_container_runtime(db_engine):
    from supportcore.config import Settings
    from supportcore.jobs.inline_queue import InlineJobQueue
    from supportcore.testing import TEST_DATABASE_URL

    from supportapi.container import build_container

    settings = Settings(
        _env_file=None,
        env="test",
        database_url=TEST_DATABASE_URL,
        redis_url="redis://localhost:6379/0",
        jwt_secret="test-secret-test-secret-test-secret-123",
        smtp_from="noreply@test.local",
        job_queue="inline",
    )
    container = build_container(settings, engine=db_engine)
    queue = container.adapters.job_queue
    assert isinstance(queue, InlineJobQueue)
    runtime = queue._runtime_factory()
    assert runtime.adapters is container.adapters
    assert runtime.events is container.events
    assert queue._runtime_factory() is runtime


def test_container_builds_widget_tokens_from_settings(test_settings):
    from uuid import uuid4

    container = build_container(test_settings)
    claims = container.widget_tokens.decode(container.widget_tokens.issue(uuid4(), uuid4(), uuid4()))
    assert claims.preview is False
    assert container.adapters.customer_token_verifier is not None


async def test_routes_and_bot_turns_share_one_inbox_publisher(test_settings, db_engine):
    from supportapi.container import build_container
    from supportapi.deps import build_assignment_service

    container = build_container(test_settings, engine=db_engine)
    try:
        runtime = container.turn_runner._rt
        assert container.inbox_publisher is not None
        assert runtime.publisher() is container.inbox_publisher
        async with runtime.session_factory() as session:
            org_id = uuid.uuid4()
            assert runtime.assignment(session, org_id)._publisher is container.inbox_publisher
            assert build_assignment_service(session, org_id, container)._publisher is container.inbox_publisher
    finally:
        await container.adapters.aclose()


def test_note_service_needs_the_inbox_publisher(test_settings):
    """T8: without an engine there is no inbox publisher; notes must fail loudly, not publish nothing."""
    from supportapi.deps import build_note_service

    from dataclasses import replace

    container = replace(build_container(test_settings), inbox_publisher=None)  # as without an engine
    with pytest.raises(RuntimeError, match="inbox publisher"):
        build_note_service(None, uuid.uuid4(), container)
