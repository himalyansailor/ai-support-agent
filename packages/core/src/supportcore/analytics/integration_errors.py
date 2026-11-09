"""The integration error log: how often a model, embedder, reranker, tool or classifier call failed.

One row per (organisation, kind, provider, error_type, UTC hour) with a count (`IntegrationEvent`). Kinds:
`llm`, `embedding`, `rerank`, `tool`, `classifier` and `email` (providers `imap` and `smtp`) and `voice` (providers `twilio`, `deepgram`, `openai` and `openai_realtime`). `provider` is the provider
id, or the tool's name for `tool`; `error_type` is a short code (an exception class name, `timeout`, or a tool
error such as `http_500`). Email error types: auth_failed, connection_failed, timeout, tls_error,
uidvalidity_changed, message_too_large, unparseable, too_complex, isolation_unavailable, ingest_failed,
oversized_response, unreadable_metadata, smtp_4xx, smtp_5xx,
ambiguous, interrupted and bounce. Voice error types: auth_failed, connection_failed, timeout, rate_limited,
stream_error, recording_failed, number_unavailable and transfer_failed. Rows never hold message text, urls, addresses or secrets.

`record_integration_error` runs in its own short UnitOfWork, so it survives the rollback of the turn or job that
failed, and it never raises. Call it after the failed provider call, never while holding a session across one.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from supportcore.db.models import IntegrationEvent
from supportcore.db.uow import UnitOfWork
from supportcore.observability.logging import get_logger

__all__ = ["KINDS", "ErrorRecorder", "error_type_of", "record_integration_error"]

logger = get_logger(__name__)

KINDS = ("llm", "embedding", "rerank", "tool", "classifier", "email", "voice")
MAX_PROVIDER, MAX_ERROR_TYPE = 200, 64

# (kind, provider, error_type); callers inject one where the code has no session factory of its own.
ErrorRecorder = Callable[[str, str, str], Awaitable[None]]


def error_type_of(exc: BaseException) -> str:
    """`timeout` for any timeout, else the exception's class name (never its message)."""
    return "timeout" if isinstance(exc, TimeoutError) else type(exc).__name__


async def record_integration_error(
    session_factory: async_sessionmaker[AsyncSession],
    organisation_id: UUID,
    kind: str,
    provider: str,
    error_type: str,
    *,
    now: datetime | None = None,
) -> None:
    try:
        if kind not in KINDS:
            raise ValueError(f"Unknown integration error kind {kind!r}")
        at = now or datetime.now(UTC)
        hour = at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        stmt = insert(IntegrationEvent).values(
            organisation_id=organisation_id,
            kind=kind,
            provider=provider[:MAX_PROVIDER],
            error_type=error_type[:MAX_ERROR_TYPE],
            hour=hour,
            count=1,
            first_seen_at=at,
            last_seen_at=at,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_integration_events_bucket",
            set_={"count": IntegrationEvent.count + 1, "last_seen_at": at},
        )
        async with UnitOfWork(session_factory) as uow:
            await uow.session.execute(stmt)
            await uow.commit()
    except Exception as exc:  # recording must never break the call that failed
        logger.warning(
            "integration_error_not_recorded",
            organisation_id=str(organisation_id),
            kind=kind,
            error_type=type(exc).__name__,
        )
