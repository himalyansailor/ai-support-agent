import asyncio
from datetime import UTC, datetime
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from starlette.routing import Route

from supportapi.container import build_container
from supportcore.realtime.spawn import drain_pending
from supportapi.errors import register_error_handlers
from supportapi.middleware import (
    DEFAULT_BODY_LIMIT,
    UPLOAD_OVERHEAD,
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
)
from supportapi.observability import MetricsEndpoint, instrument_app
from supportapi.routers import admin, admin_plans, ai, analytics, audit, auth, billing, billing_webhooks, conversations, documents, email_channel, health, inbox, inbox_ws, integration_errors, invites, knowledge, me, notifications, orgs, playground, presence, risk, roles, saved_replies, support_settings, tags, team, testing, tools, voice_agent, voice_channel, widget_public, widget_ws, widgets
from supportcore.analytics.jobs import ROLLUP_INTERVAL_SECONDS, rollup_analytics
from supportcore.config import Settings, get_settings
from supportcore.conversations.jobs import IDLE_CLOSE_INTERVAL_SECONDS, close_idle_conversations
from supportcore.inbox.jobs import SLA_CHECK_INTERVAL_SECONDS, check_sla
from supportcore.db.session import create_engine, session_factory
from supportcore.jobs.ports import JobRuntime
from supportcore.knowledge.jobs import RECOVERY_INTERVAL_SECONDS, recover_stale_jobs
from supportcore.mail.outbound import sweep_outbound
from supportcore.mail.sync import MAIL_SYNC_INTERVAL_SECONDS, sync_mailboxes
from supportcore.observability.logging import configure_logging, get_logger
from supportcore.observability.tracing import configure_tracing, instrument_celery


logger = get_logger(__name__)


async def _recover_periodically(job_runtime: Callable[[], JobRuntime]) -> None:
    """Stale-job recovery at start, then every RECOVERY_INTERVAL_SECONDS (SUPPORT_JOB_QUEUE=inline)."""
    while True:
        try:
            await recover_stale_jobs(job_runtime())
        except Exception as exc:  # e.g. the database is down; try again next time
            logger.warning("api_stale_job_recovery_failed", error_type=type(exc).__name__)
        await asyncio.sleep(RECOVERY_INTERVAL_SECONDS)


async def _close_idle_periodically(job_runtime: Callable[[], JobRuntime]) -> None:
    """Idle conversations are resolved at start, then every IDLE_CLOSE_INTERVAL_SECONDS
    (SUPPORT_JOB_QUEUE=inline; with Celery, beat runs the close_idle_conversations job)."""
    while True:
        try:
            await close_idle_conversations(job_runtime())
        except Exception as exc:  # e.g. the database is down; try again next time
            logger.warning("api_idle_close_failed", error_type=type(exc).__name__)
        await asyncio.sleep(IDLE_CLOSE_INTERVAL_SECONDS)


async def _check_sla_periodically(job_runtime: Callable[[], JobRuntime]) -> None:
    """SLA breaches are flagged at start, then every SLA_CHECK_INTERVAL_SECONDS (SUPPORT_JOB_QUEUE=inline;
    with Celery, beat runs the check_sla job)."""
    while True:
        try:
            await check_sla(job_runtime(), datetime.now(UTC))
        except Exception as exc:  # e.g. the database is down; try again next time
            logger.warning("api_sla_check_failed", error_type=type(exc).__name__)
        await asyncio.sleep(SLA_CHECK_INTERVAL_SECONDS)


async def _rollup_analytics_periodically(job_runtime: Callable[[], JobRuntime]) -> None:
    """Analytics hours are rolled up at start, then every ROLLUP_INTERVAL_SECONDS (SUPPORT_JOB_QUEUE=inline;
    with Celery, beat runs the rollup_analytics job)."""
    while True:
        try:
            await rollup_analytics(job_runtime(), datetime.now(UTC))
        except Exception as exc:  # e.g. the database is down; try again next time
            logger.warning("api_analytics_rollup_failed", error_type=type(exc).__name__)
        await asyncio.sleep(ROLLUP_INTERVAL_SECONDS)


async def _sync_mailboxes_periodically(job_runtime: Callable[[], JobRuntime]) -> None:
    """Email mailboxes are polled at start, then every MAIL_SYNC_INTERVAL_SECONDS (SUPPORT_JOB_QUEUE=inline: the
    sync_mailbox jobs run on the inline queue; with Celery, beat runs the sync_mailboxes job). Each tick also runs
    the outbound sweep (stuck `sending` rows, lost send_email jobs)."""
    while True:
        try:
            await sync_mailboxes(job_runtime(), datetime.now(UTC))
        except Exception as exc:  # e.g. the database is down; try again next time
            logger.warning("api_mail_sync_failed", error_type=type(exc).__name__)
        try:
            await sweep_outbound(job_runtime(), datetime.now(UTC))
        except Exception as exc:
            logger.warning("api_mail_sweep_failed", error_type=type(exc).__name__)
        await asyncio.sleep(MAIL_SYNC_INTERVAL_SECONDS)


def create_app(settings: Settings | None = None, *, periodic_jobs: bool | None = None) -> FastAPI:
    """`periodic_jobs`: run the inline periodic jobs (recovery, idle close, SLA, analytics rollup, mail sync) in the
    lifespan when SUPPORT_JOB_QUEUE=inline. Defaults to on, except with env=test, where background jobs would
    race the tests' own database work and truncation: a test opts in with True."""
    settings = settings or get_settings()
    run_periodic = settings.job_queue == "inline" and (
        periodic_jobs if periodic_jobs is not None else settings.env != "test"
    )
    configure_logging(settings)
    if settings.env == "prod" and settings.internal_api_token is None:
        logger.warning(
            "internal_api_token_unset",
            detail="SUPPORT_INTERNAL_API_TOKEN is unset: set the same value on the API and the web server, "
            "or every widget frame-policy lookup shares the web server's rate limit",
        )
    engine = create_engine(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # With inline jobs no worker exists to recover stale or lost jobs: the API does it.
        container = app.state.container
        periodic = (
            [
                asyncio.create_task(_recover_periodically(container.job_runtime)),
                asyncio.create_task(_close_idle_periodically(container.job_runtime)),
                asyncio.create_task(_check_sla_periodically(container.job_runtime)),
                asyncio.create_task(_rollup_analytics_periodically(container.job_runtime)),
                asyncio.create_task(_sync_mailboxes_periodically(container.job_runtime)),
            ]
            if run_periodic
            else []
        )
        try:
            yield
        finally:
            try:
                for task in periodic:
                    task.cancel()
                await asyncio.gather(*periodic, return_exceptions=True)
                if container.turn_runner is not None:
                    await container.turn_runner.aclose()
                await drain_pending()  # in-flight presence/inbox publishes need the Redis client
                await container.adapters.aclose()
            finally:
                await engine.dispose()

    app = FastAPI(title="Support API", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory(engine)
    app.state.container = build_container(settings, engine=engine)
    # The queue jobs are enqueued on; with SUPPORT_JOB_QUEUE=inline tests `await app.state.job_queue.drain()`.
    app.state.job_queue = app.state.container.adapters.job_queue
    register_error_handlers(app)

    # add_middleware wraps outward: BodySizeLimit is innermost of these, so its 413 passes through CORS.
    app.add_middleware(
        BodySizeLimitMiddleware,
        default_limit=DEFAULT_BODY_LIMIT,
        upload_limit=settings.max_upload_bytes + UPLOAD_OVERHEAD,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.web_origin],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestContextMiddleware)

    # Route (not Mount) so /metrics is served without a trailing-slash redirect.
    app.router.routes.append(Route("/metrics", endpoint=MetricsEndpoint(settings), include_in_schema=False))
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(me.router)
    app.include_router(orgs.router)
    app.include_router(roles.router)
    app.include_router(team.router)
    app.include_router(invites.router)
    app.include_router(audit.router)
    app.include_router(integration_errors.router)
    app.include_router(analytics.router)
    app.include_router(admin.router)
    app.include_router(admin_plans.router)
    app.include_router(billing.router)
    app.include_router(billing_webhooks.router)
    app.include_router(ai.router)
    app.include_router(knowledge.router)
    app.include_router(widgets.router)
    app.include_router(risk.router)
    app.include_router(support_settings.router)
    app.include_router(presence.router)
    app.include_router(notifications.router)
    app.include_router(conversations.router)
    app.include_router(inbox.router)
    app.include_router(inbox_ws.router)
    app.include_router(tags.router)
    app.include_router(saved_replies.router)
    app.include_router(tools.router)
    app.include_router(email_channel.router)
    app.include_router(voice_channel.router)
    app.include_router(voice_agent.router)
    app.include_router(widget_public.router)
    app.include_router(widget_ws.router)
    app.include_router(documents.router)
    app.include_router(playground.router)
    testing.include_test_routes(app, settings)

    provider = configure_tracing(settings, engine)
    instrument_app(app, provider)
    if settings.job_queue == "celery":
        instrument_celery(provider)  # trace context goes to the worker in the task headers
    return app
