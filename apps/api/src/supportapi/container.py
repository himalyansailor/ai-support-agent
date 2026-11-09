"""Composition root: the only place adapters are wired to ports."""

from collections.abc import Callable
from dataclasses import dataclass

from supportcore.audit.subscribers import register_audit_subscribers
from sqlalchemy.ext.asyncio import AsyncEngine

from supportcore.bot.runtime import BotRuntime
from supportcore.bot.turns import TurnRunner
from supportcore.inbox.publisher import InboxPublisher
from supportcore.config import Settings
from supportcore.db.session import get_current_session, session_factory
from supportcore.events import EventBus
from supportcore.jobs.ports import JobRuntime
from supportcore.identity.argon2_hasher import Argon2PasswordHasher
from supportcore.identity.jwt_tokens import JwtAccessTokenService
from supportcore.identity.ports import AccessTokenService, PasswordHasher
from supportcore.notifications.ports import EmailSender
from supportcore.registry import ProviderRegistry
from supportcore.widget.tokens import WidgetTokenService, widget_tokens_from_settings
from supportcore.wiring import Adapters, build_adapters, email_sender_registry

hasher_registry: ProviderRegistry[PasswordHasher] = ProviderRegistry("password_hasher")
token_registry: ProviderRegistry[AccessTokenService] = ProviderRegistry("token_service")


@hasher_registry.register("argon2")
def _argon2_hasher(settings: Settings) -> PasswordHasher:
    return Argon2PasswordHasher()


@token_registry.register("jwt")
def _jwt_tokens(settings: Settings) -> AccessTokenService:
    return JwtAccessTokenService(
        secret=settings.jwt_secret.get_secret_value(), ttl_seconds=settings.access_token_ttl_seconds
    )


@dataclass
class Container:
    settings: Settings
    hasher_registry: ProviderRegistry[PasswordHasher]
    token_registry: ProviderRegistry[AccessTokenService]
    email_sender_registry: ProviderRegistry[EmailSender]
    hasher: PasswordHasher
    tokens: AccessTokenService
    widget_tokens: WidgetTokenService
    email_sender: EmailSender  # adapters.email_sender (the same object the jobs use)
    events: EventBus
    adapters: Adapters
    # The runtime inline jobs (and the API's periodic stale-job recovery) run with; built lazily.
    job_runtime: Callable[[], JobRuntime]
    # Bot turns for the widget WebSocket (the graph compiles on the first turn); None without an engine.
    turn_runner: TurnRunner | None = None
    # Inbox, notification and widget events after a commit (summaries load on its own sessions); None without an engine.
    inbox_publisher: InboxPublisher | None = None


def build_container(settings: Settings, engine: AsyncEngine | None = None) -> Container:
    if settings.env == "prod" and settings.email_sender == "console":
        raise ValueError("SUPPORT_EMAIL_SENDER=console is not allowed when SUPPORT_ENV=prod; use smtp")
    events = EventBus()
    register_audit_subscribers(events, get_current_session)
    runtime: JobRuntime | None = None

    def job_runtime() -> JobRuntime:
        """For SUPPORT_JOB_QUEUE=inline: jobs run in this process with the API's adapters and engine
        (each job opens its own sessions). Built on the first job, once the adapters exist."""
        nonlocal runtime
        if runtime is None:
            job_engine = engine if engine is not None else adapters.owned_engine
            if job_engine is None:
                raise RuntimeError("Inline jobs need a database engine: pass one to build_container()")
            runtime = JobRuntime(
                settings=settings, session_factory=session_factory(job_engine), adapters=adapters, events=events
            )
        return runtime

    adapters = build_adapters(settings, engine=engine, runtime_factory=job_runtime)
    bot_engine = engine if engine is not None else adapters.owned_engine
    bot_sessions = session_factory(bot_engine) if bot_engine is not None else None
    # One publisher for routes and bot turns alike (one inbox summary loader).
    inbox_publisher = InboxPublisher(adapters.pubsub, bot_sessions) if bot_sessions is not None else None
    turn_runner = (
        TurnRunner(
            BotRuntime(
                settings=settings,
                session_factory=bot_sessions,
                adapters=adapters,
                events=events,
                inbox_publisher=inbox_publisher,
            )
        )
        if bot_sessions is not None
        else None
    )
    return Container(
        settings=settings,
        hasher_registry=hasher_registry,
        token_registry=token_registry,
        email_sender_registry=email_sender_registry,
        hasher=hasher_registry.create(settings.password_hasher, settings=settings),
        tokens=token_registry.create(settings.token_service, settings=settings),
        widget_tokens=widget_tokens_from_settings(settings),
        email_sender=adapters.email_sender,
        events=events,
        adapters=adapters,
        job_runtime=job_runtime,
        turn_runner=turn_runner,
        inbox_publisher=inbox_publisher,
    )
