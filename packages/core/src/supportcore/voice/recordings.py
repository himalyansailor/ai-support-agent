"""Call recordings (jobs `start_call_recording`, `fetch_call_recording`, `purge_call_recordings`).

- start: when a call's media stream connects and the organisation records calls, ask Twilio to record it, with the
  status callback URL the voice process put in the payload; the recording sid is stored on the call. A failure is an integration error (`voice/twilio/recording_failed`), not retried.
- fetch (Twilio's recording-status callback): download the mp3 into our FileStorage, store a `CallRecording` row with
  `delete_after = now + retention` (and tell the inbox after the commit), then delete the recording on Twilio (a failure there is logged, not retried).
  Download failures retry after 60 s, 300 s, 900 s, then record `recording_failed`. A call that already has a
  recording is a no-op, so duplicate callbacks are harmless.
- purge (daily): expired recordings, storage object first, then the row.

No database session or transaction is open across a Twilio or storage call. Ids, sizes and error class names only
in the logs: never a recording URL, token or phone number.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from supportcore.analytics.integration_errors import record_integration_error
from supportcore.db.models import Call, CallRecording, TelephonyAccount, VoiceSettings
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.db.uow import UnitOfWork
from supportcore.errors import NotFoundError
from supportcore.inbox.publisher import InboxPublisher
from supportcore.secrets.ports import SecretDecryptError
from supportcore.voice.telephony.ports import TelephonyAccountRef, TelephonyAuthError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from supportcore.jobs.ports import JobRuntime

__all__ = [
    "FETCH_RETRY_SECONDS",
    "MAX_RECORDING_BYTES",
    "fetch_call_recording",
    "purge_call_recordings",
    "recording_of_conversation",
    "recording_status_url",
    "start_call_recording",
]

logger = structlog.get_logger()

MAX_RECORDING_BYTES = 200 * 1024 * 1024
FETCH_RETRY_SECONDS = {1: 60, 2: 300, 3: 900}  # the delay before attempt n + 1
DEFAULT_RETENTION_DAYS = 90  # VoiceSettings.recording_retention_days' default
CONTENT_TYPE = "audio/mpeg"
PURGE_BATCH = 100


async def recording_of_conversation(
    session: AsyncSession, organisation_id: UUID, conversation_id: UUID, call_id: UUID | None = None
) -> CallRecording:
    """The stored recording of one of the conversation's calls: `call_id`, or the inbound call by default.
    NotFoundError when there is none (a call of another conversation counts as none)."""
    # At most one row: a conversation has one inbound call (partial unique index) and CallRecording.call_id is unique.
    stmt = (
        TenantScopedRepository(session, CallRecording, organisation_id)
        .select()
        .join(
            Call,
            (Call.id == CallRecording.call_id) & (Call.organisation_id == CallRecording.organisation_id),
        )
        .where(Call.conversation_id == conversation_id)
    )
    stmt = stmt.where(Call.id == call_id) if call_id is not None else stmt.where(Call.direction == "inbound")
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFoundError("Recording not found")
    return row


async def _account_ref(runtime: JobRuntime, session, organisation_id: UUID) -> TelephonyAccountRef | None:
    account = (
        await session.execute(TenantScopedRepository(session, TelephonyAccount, organisation_id).select())
    ).scalar_one_or_none()
    if account is None:
        return None
    try:
        return TelephonyAccountRef(account.account_sid, runtime.adapters.cipher.decrypt(account.auth_token_encrypted))
    except SecretDecryptError:
        return None


async def _record_error(runtime: JobRuntime, organisation_id: UUID, error_type: str = "recording_failed") -> None:
    await record_integration_error(runtime.session_factory, organisation_id, "voice", "twilio", error_type)


async def _retry_or_fail(
    runtime: JobRuntime, organisation_id: UUID, call_id: UUID, sid: str, attempt: int, duration: int | None
) -> None:
    """The download retry ladder (also used for a storage failure): re-enqueue with a delay, then record the error."""
    delay = FETCH_RETRY_SECONDS.get(attempt)
    if delay is None:
        await _record_error(runtime, organisation_id)
        return
    payload: dict = {
        "organisation_id": str(organisation_id),
        "call_id": str(call_id),
        "recording_sid": sid,
        "attempt": attempt + 1,
    }
    if duration is not None:
        payload["recording_duration"] = duration
    runtime.adapters.job_queue.enqueue("fetch_call_recording", payload, countdown=delay)


def recording_status_url(voice_public_url: str, call_id: UUID) -> str:
    """Twilio's recording-status callback for a call (built by the voice process, which owns the public URL)."""
    return f"{voice_public_url.rstrip('/')}/twilio/recording/status?call_id={call_id}"


async def start_call_recording(
    runtime: JobRuntime, organisation_id: UUID, call_id: UUID, status_callback: str | None = None
) -> None:
    """`status_callback` comes from the voice process (the job payload). Payloads enqueued before it was added fall
    back to this process's `voice_public_url`; with neither, nothing is asked of Twilio (a relative callback URL
    would lose the recording) and `recording_failed` is recorded."""
    async with UnitOfWork(runtime.session_factory) as uow:
        call = await TenantScopedRepository(uow.session, Call, organisation_id).get(call_id)
        ref = await _account_ref(runtime, uow.session, organisation_id) if call is not None else None
        if call is None or ref is None or call.recording_sid:
            logger.info("call_recording_start_skipped", call_id=str(call_id), found=call is not None)
            return
        call_sid = call.provider_call_sid
        await uow.commit()
    if status_callback is None and runtime.settings.voice_public_url:
        status_callback = recording_status_url(runtime.settings.voice_public_url, call_id)
    if not status_callback:
        logger.warning("call_recording_start_failed", call_id=str(call_id), error="voice_public_url_not_set")
        await _record_error(runtime, organisation_id)
        return
    try:
        sid = await runtime.adapters.telephony.start_recording(ref, call_sid, status_callback)
    except Exception as exc:  # noqa: BLE001
        logger.warning("call_recording_start_failed", call_id=str(call_id), error=type(exc).__name__)
        await _record_error(runtime, organisation_id)
        return
    async with UnitOfWork(runtime.session_factory) as uow:
        call = await TenantScopedRepository(uow.session, Call, organisation_id).get(call_id, for_update=True)
        if call is not None:
            call.recording_sid = sid
        await uow.commit()
    logger.info("call_recording_started", call_id=str(call_id), recording_sid=sid)


async def fetch_call_recording(
    runtime: JobRuntime,
    organisation_id: UUID,
    call_id: UUID,
    recording_sid: str,
    attempt: int = 1,
    recording_duration: int | None = None,
) -> None:
    async with UnitOfWork(runtime.session_factory) as uow:
        session = uow.session
        existing = (
            await session.execute(
                TenantScopedRepository(session, CallRecording, organisation_id)
                .select()
                .where(CallRecording.call_id == call_id)
            )
        ).scalar_one_or_none()
        call = await TenantScopedRepository(session, Call, organisation_id).get(call_id)
        ref = await _account_ref(runtime, session, organisation_id) if call is not None else None
        voice = (
            await session.execute(TenantScopedRepository(session, VoiceSettings, organisation_id).select())
        ).scalar_one_or_none()
        if existing is not None or call is None:
            logger.info("call_recording_fetch_skipped", call_id=str(call_id), stored=existing is not None)
            return
        retention_days = voice.recording_retention_days if voice is not None else DEFAULT_RETENTION_DAYS
        # Twilio's own duration when the callback carried it, else the call's (0 until its status callback lands).
        duration = recording_duration if recording_duration is not None else (call.duration_seconds or 0)
        conversation_id = call.conversation_id
        await uow.commit()
    if ref is None:  # the account is gone or its token unreadable: nothing to download with
        logger.warning("call_recording_account_unusable", call_id=str(call_id))
        await _record_error(runtime, organisation_id)
        return

    telephony = runtime.adapters.telephony
    try:
        data = await telephony.download_recording(ref, recording_sid, MAX_RECORDING_BYTES)
    except TelephonyAuthError:  # the credentials were rejected: retrying cannot help
        logger.warning("call_recording_download_failed", call_id=str(call_id), attempt=attempt, error="TelephonyAuthError")
        await _record_error(runtime, organisation_id, "auth_failed")
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("call_recording_download_failed", call_id=str(call_id), attempt=attempt, error=type(exc).__name__)
        await _retry_or_fail(runtime, organisation_id, call_id, recording_sid, attempt, recording_duration)
        return

    key = f"orgs/{organisation_id}/calls/{call_id}/{uuid4()}.mp3"
    try:
        await runtime.adapters.storage.put(key, data, CONTENT_TYPE)
    except Exception as exc:  # noqa: BLE001
        logger.warning("call_recording_store_failed", call_id=str(call_id), attempt=attempt, error=type(exc).__name__)
        await _retry_or_fail(runtime, organisation_id, call_id, recording_sid, attempt, recording_duration)
        return
    try:
        async with UnitOfWork(runtime.session_factory) as uow:
            await TenantScopedRepository(uow.session, CallRecording, organisation_id).add(
                CallRecording(
                    organisation_id=organisation_id,
                    call_id=call_id,
                    storage_key=key,
                    content_type=CONTENT_TYPE,
                    size_bytes=len(data),
                    duration_seconds=duration,
                    delete_after=datetime.now(UTC) + timedelta(days=retention_days),
                )
            )
            # After the commit: an agent with the conversation open gets the player without reloading.
            InboxPublisher(runtime.adapters.pubsub, runtime.session_factory).conversation_changed(
                uow.session, organisation_id, conversation_id
            )
            await uow.commit()
    except IntegrityError:  # a concurrent duplicate callback stored it first
        await runtime.adapters.storage.delete(key)
        return
    logger.info("call_recording_stored", call_id=str(call_id), size_bytes=len(data))
    try:
        await telephony.delete_recording(ref, recording_sid)
    except Exception as exc:  # noqa: BLE001  # the copy is safe with us; Twilio's own retention will remove the original
        logger.warning("call_recording_twilio_delete_failed", call_id=str(call_id), error=type(exc).__name__)


async def purge_call_recordings(runtime: JobRuntime) -> int:
    """Deletes the expired recordings (storage object first, then the row). Returns how many rows were deleted."""
    deleted = 0
    skipped: set[UUID] = set()  # storage delete failed: the row stays for tomorrow's run
    while True:
        async with runtime.session_factory() as s:
            # Deliberately across organisations (a beat sweep, like the other periodic jobs); each row is then
            # deleted tenant-scoped.
            stmt = (
                select(CallRecording.id, CallRecording.organisation_id, CallRecording.storage_key)
                .where(CallRecording.delete_after < datetime.now(UTC), CallRecording.id.not_in(skipped))
                .order_by(CallRecording.delete_after)
                .limit(PURGE_BATCH)
            )
            batch = list((await s.execute(stmt)).all())
        for row_id, org, key in batch:
            try:
                await runtime.adapters.storage.delete(key)
            except Exception as exc:  # noqa: BLE001
                skipped.add(row_id)
                logger.warning("call_recording_purge_failed", organisation_id=str(org), error=type(exc).__name__)
                continue
            async with UnitOfWork(runtime.session_factory) as uow:
                repo = TenantScopedRepository(uow.session, CallRecording, org)
                row = await repo.get(row_id)
                if row is not None:
                    await repo.delete(row)
                await uow.commit()
            deleted += 1
        if len(batch) < PURGE_BATCH:
            break
    if deleted:
        logger.info("call_recordings_purged", recordings=deleted)
    return deleted
