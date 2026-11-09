"""WS /twilio/media: a live call's Twilio Media Stream, run by a VoiceSession.

The socket waits (bounded) for the `start` event, checks the stream token from its custom parameters, marks the
call's stream connected (a token connects once), takes a slot of `voice_max_calls` and loads the organisation's
voice setup. Any refusal before a session exists closes the socket; once the call row is known, a refusal also opens
a callback ticket and the `<Connect action>` (after-stream) tells the caller.

The session runs as a task the container tracks, not in the socket handler: once the line is closed the slot is
freed and the handler returns, while the session may still be persisting its last turn. When it ends, the stream's
end is recorded on the call (`CallService.stream_ended`); the call is finalised by the later of that and Twilio's
status callback, so `call_ended` always has the final usage and the last turn's reply is stored.

Never logged: the stream token, phone numbers, spoken text.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

import structlog
from fastapi import APIRouter, WebSocket
from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from supportcore.billing.entitlements import EntitlementService
from supportcore.db.models import Call, Conversation
from supportcore.db.session import after_commit
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.db.uow import UnitOfWork
from supportcore.errors import NotFoundError
from supportcore.observability.context import (
    bind_organisation,
    job_context,
    request_context,
)
from supportcore.voice.calls import CallService
from supportcore.voice.errors import InvalidVoiceTokenError
from supportcore.voice.metrics import VOICE_CALLS
from supportcore.voice.realtime_session import RealtimeVoiceSession
from supportcore.voice.recordings import recording_status_url
from supportcore.voice.session import VoiceSession
from supportcore.voice.settings_service import (
    VoiceSettingsService,
    VoiceSetup,
    load_voice_setup,
)
from supportcore.voice.tokens import verify_stream_token
from supportcore.voice.transport import TwilioMediaTransport

from supportvoice.container import ActiveCalls, VoiceContainer

logger = structlog.get_logger()
tracer = trace.get_tracer("supportvoice.media")

router = APIRouter(prefix="/twilio", include_in_schema=False)

START_TIMEOUT_SECONDS = 5.0  # read at use time, so tests can patch it
RECORDING_START_JOB = "start_call_recording"
STREAM_END_RETRY_SECONDS = 0.5  # before the one retry of recording the stream's end


class _Slot:
    """One `voice_max_calls` slot, released exactly once."""

    def __init__(self, calls: ActiveCalls) -> None:
        self._calls = calls
        self._held = True

    def release(self) -> None:
        if self._held:
            self._held = False
            self._calls.release()


@dataclass(frozen=True)
class _Connected:
    call_id: UUID
    conversation_id: UUID
    source: str
    widget_id: UUID | None
    setup: VoiceSetup


@router.websocket("/media")
async def media_stream(websocket: WebSocket) -> None:
    with request_context(uuid.uuid4().hex):  # log context: the organisation is bound once the token checks out
        await _media_stream(websocket)


async def _media_stream(websocket: WebSocket) -> None:
    container: VoiceContainer = websocket.app.state.container
    factory: async_sessionmaker[AsyncSession] = websocket.app.state.session_factory
    await websocket.accept()
    transport = TwilioMediaTransport(websocket)
    try:
        start = await transport.wait_for_start(START_TIMEOUT_SECONDS)
        if start is None:
            logger.info("media_stream_rejected", reason="no_start")
            return
        try:
            call_id, organisation_id = verify_stream_token(container.settings, start.parameters.get("token", ""))
        except InvalidVoiceTokenError:
            logger.info("media_stream_rejected", reason="invalid_token")
            return
        bind_organisation(organisation_id)
        slot = _Slot(container.active_calls) if container.active_calls.acquire() else None
        call: asyncio.Task[None] | None = None
        connected: _Connected | None = None
        try:
            connected = await _connect(container, factory, organisation_id, call_id, has_slot=slot is not None)
            if connected is None or slot is None:
                return
            session: VoiceSession | RealtimeVoiceSession
            if connected.setup.mode == "speech_to_speech":
                session = RealtimeVoiceSession(
                    connected.call_id,
                    organisation_id,
                    connected.conversation_id,
                    connected.source,
                    connected.widget_id,
                    connected.setup,
                    transport,
                    container.adapters,
                    container.bot_runtime,
                    factory,
                    container.events,
                    container.settings,
                    publisher=container.inbox_publisher,
                )
            else:
                session = VoiceSession(
                    connected.call_id,
                    organisation_id,
                    connected.conversation_id,
                    connected.source,
                    connected.setup,
                    transport,
                    container.adapters,
                    container.turn_runner,
                    factory,
                    container.events,
                    container.settings,
                    publisher=container.inbox_publisher,
                    tts_factory=container.tts,
                )
            call = container.start_call(_run_call(container, factory, organisation_id, connected, session, slot))
        finally:
            if call is None:  # no session took over
                if slot is not None:
                    slot.release()
                if connected is not None:  # connected, then failed before the session started: a failed stream
                    await _abandon_stream(container, factory, organisation_id, connected)
        # The handler returns once the line is closed; the call task may still be persisting the last turn.
        line_closed = asyncio.ensure_future(session.line_closed.wait())
        try:
            await asyncio.wait({call, line_closed}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            line_closed.cancel()
    except Exception as exc:  # noqa: BLE001 - never a stack trace towards Twilio: the socket just closes
        logger.error("media_stream_failed", error_type=type(exc).__name__)
    finally:
        await transport.close()


async def _connect(
    container: VoiceContainer,
    factory: async_sessionmaker[AsyncSession],
    organisation_id: UUID,
    call_id: UUID,
    *,
    has_slot: bool,
) -> _Connected | None:
    """One unit of work: the stream connects (single use), then either the voice setup or a callback ticket
    (capacity, the plan's voice minutes used up, voice not ready). None when no session should run. A call that
    connected is never checked against the plan again."""
    settings = container.settings
    async with UnitOfWork(factory) as uow:
        session = uow.session
        service = _call_service(container, session, organisation_id)
        voice = VoiceSettingsService(
            session,
            organisation_id,
            container.events,
            include_fake=settings.env == "test",
            voice_public_url=settings.voice_public_url,
        )
        mode = (await voice.get()).mode  # the engine this call runs on, fixed for its whole life
        try:
            call = await service.mark_stream_connected(call_id, mode)
        except (InvalidVoiceTokenError, NotFoundError):
            logger.info("media_stream_rejected", reason="token_used", call_id=str(call_id))
            return None
        setup: VoiceSetup | None = None
        reason: str | None = None
        if not has_slot:
            reason = "capacity"
        elif (await EntitlementService(session, organisation_id, container.adapters).meter("voice_minutes")).reached:
            reason = "plan_limit"
        else:
            readiness = await voice.readiness()
            ready_for_source = readiness.phone if call.source == "phone" else readiness.browser
            if ready_for_source:
                setup = await load_voice_setup(
                    session, organisation_id, container.adapters, settings, container.events, readiness=readiness
                )
            if setup is None:
                reason = "voice_unavailable"
        if reason is not None:
            await service.request_callback(call.id, reason)
            await service.stream_ended(call.id)  # no session will run: the status callback alone finalises
        elif setup is not None and setup.record_calls:
            payload = {"organisation_id": str(organisation_id), "call_id": str(call.id)}
            if settings.voice_public_url:  # this process owns the URL Twilio reaches; the worker may not know it
                payload["status_callback"] = recording_status_url(settings.voice_public_url, call.id)
            # Only once the call is committed as connected (the job reads it).
            after_commit(session, lambda: container.adapters.job_queue.enqueue(RECORDING_START_JOB, payload))
        connected = None
        if setup is not None:
            conversation = await TenantScopedRepository(session, Conversation, organisation_id).get(call.conversation_id)
            connected = _Connected(
                call.id, call.conversation_id, call.source, conversation.widget_id if conversation else None, setup
            )
        await uow.commit()
    if reason is not None:
        logger.info("call_not_connected", call_id=str(call_id), source=call.source, reason=reason)
        return None
    logger.info("media_stream_connected", call_id=str(call_id), source=call.source)
    return connected


async def _run_call(
    container: VoiceContainer,
    factory: async_sessionmaker[AsyncSession],
    organisation_id: UUID,
    connected: _Connected,
    session: VoiceSession | RealtimeVoiceSession,
    slot: _Slot,
) -> None:
    attributes = {
        "organisation.id": str(organisation_id),
        "conversation.id": str(connected.conversation_id),
        "call.id": str(connected.call_id),
        "call.source": connected.source,
    }
    with (
        job_context(organisation_id, f"call:{connected.call_id}"),
        tracer.start_as_current_span("voice.call", attributes=attributes, record_exception=False),
    ):
        release = asyncio.ensure_future(_release_when_line_closed(session, slot))
        try:
            await session.run()
        except asyncio.CancelledError:  # shutdown: the session is over all the same
            await _stream_ended(container, factory, organisation_id, connected)
            raise
        finally:
            release.cancel()
            slot.release()
        await _stream_ended(container, factory, organisation_id, connected)


async def _release_when_line_closed(session: VoiceSession | RealtimeVoiceSession, slot: _Slot) -> None:
    await session.line_closed.wait()
    slot.release()


async def _stream_ended(
    container: VoiceContainer,
    factory: async_sessionmaker[AsyncSession],
    organisation_id: UUID,
    connected: _Connected,
) -> None:
    """The stream's side of finalisation: the call is finalised now if Twilio's status callback already came,
    else by that callback."""

    async def work(service: CallService, session: AsyncSession) -> tuple[bool, str]:
        finalised = await service.stream_ended(connected.call_id)
        call = await TenantScopedRepository(session, Call, organisation_id).get(connected.call_id)
        return finalised, (call.end_reason if call is not None else None) or "caller_hangup"

    result = await _call_work(container, factory, organisation_id, connected, work)
    if result is not None and result[0]:
        VOICE_CALLS.labels(source=connected.source, mode=connected.setup.mode, end_reason=result[1]).inc()


async def _abandon_stream(
    container: VoiceContainer,
    factory: async_sessionmaker[AsyncSession],
    organisation_id: UUID,
    connected: _Connected,
) -> None:
    """The stream connected but its session never started: the call ends as an error (callback ticket; the
    `<Connect action>` says the trouble message) and the stream end is recorded."""

    async def work(service: CallService, session: AsyncSession) -> tuple[bool, str]:
        await service.request_callback(connected.call_id, "error")
        return await service.stream_ended(connected.call_id), "error"

    result = await _call_work(container, factory, organisation_id, connected, work)
    if result is not None and result[0]:
        VOICE_CALLS.labels(source=connected.source, mode=connected.setup.mode, end_reason="error").inc()


async def _call_work(
    container: VoiceContainer,
    factory: async_sessionmaker[AsyncSession],
    organisation_id: UUID,
    connected: _Connected,
    work: Callable[[CallService, AsyncSession], Awaitable[tuple[bool, str]]],
) -> tuple[bool, str] | None:
    """One unit of work on the call, retried once after STREAM_END_RETRY_SECONDS (a database blip). If both
    attempts fail, the stale-call sweep (supportcore.voice.jobs) finalises the call later."""
    for attempt in (1, 2):
        try:
            async with UnitOfWork(factory) as uow:
                result = await work(_call_service(container, uow.session, organisation_id), uow.session)
                await uow.commit()
                return result
        except Exception as exc:  # noqa: BLE001 - logged; retried once, then left to the sweep
            logger.error(
                "call_stream_end_failed", call_id=str(connected.call_id), attempt=attempt, error_type=type(exc).__name__
            )
            if attempt == 1:
                await asyncio.sleep(STREAM_END_RETRY_SECONDS)
    return None


def _call_service(container: VoiceContainer, session: AsyncSession, organisation_id: UUID) -> CallService:
    return CallService(
        session,
        organisation_id,
        container.events,
        container.inbox_publisher,
        adapters=container.adapters,
        settings=container.settings,
    )
