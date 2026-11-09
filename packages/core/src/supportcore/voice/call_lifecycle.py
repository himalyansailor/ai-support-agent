"""The call lifecycle the voice sessions share: the chained VoiceSession and the speech-to-speech
RealtimeVoiceSession.

The lifecycle covers:
  - task supervision (every task spawned through `_spawn` and owned by the session's run());
  - the first ending wins, and each ending is acted on: hangup, error, live transfer, callback promise, time limit,
    silence;
  - the closing words, and the bounded wait for them to play;
  - the transfer row committed before the line closes;
  - playback marks, the silence watch and the time limit;
  - provider-error recording;
  - the callback ticket for a caller who hangs up while the call is being handed to people.

Each session supplies its media through the hooks below: how its output is stopped, how a fixed line is spoken and
when it is idle.

The timing constants are module globals of `supportcore.voice.session`, read at use time (tests patch them there).
No database session is held across an await: every step is a `CallDb` unit of work. Never logged: spoken text,
transcripts, phone numbers or audio.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass
from types import ModuleType
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from supportcore.analytics.integration_errors import record_integration_error
from supportcore.config import Settings
from supportcore.db.models import Conversation
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.events import EventBus
from supportcore.inbox.publisher import InboxPublisher
from supportcore.voice.call_db import CallDb
from supportcore.voice.metrics import VOICE_PROVIDER_ERRORS
from supportcore.voice.settings_service import VoiceSetup
from supportcore.voice.strings import strings_for
from supportcore.voice.transport import MediaTransport, TransportClosed

if TYPE_CHECKING:
    from supportcore.wiring import Adapters

__all__ = ["CallEnd", "CallLifecycle", "ProviderFailure"]

logger = structlog.get_logger()


@dataclass(frozen=True)
class CallEnd:
    # A Call.end_reason, or `transfer` (internal: a live transfer begins, and TransferService owns how the call ends;
    # never written to Call.end_reason).
    reason: str
    provider: str | None = None  # error: the failing provider id
    error_type: str | None = None


class ProviderFailure(Exception):
    def __init__(self, provider: str, error_type: str) -> None:
        super().__init__(error_type)
        self.provider = provider
        self.error_type = error_type


def _timing() -> ModuleType:
    """The module holding the timing constants (SILENCE_*, GOODBYE_MARK_TIMEOUT_SECONDS, ...)."""
    from supportcore.voice import session

    return session


class CallLifecycle:
    def __init__(
        self,
        call_id: UUID,
        organisation_id: UUID,
        conversation_id: UUID,
        source: str,
        setup: VoiceSetup,
        transport: MediaTransport,
        adapters: Adapters,
        session_factory: async_sessionmaker[AsyncSession],
        events: EventBus,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.monotonic,
        publisher: InboxPublisher | None = None,
    ) -> None:
        self._call_id = call_id
        self._org_id = organisation_id
        self._conversation_id = conversation_id
        self._source = source
        self._setup = setup
        self._transport = transport
        self._adapters = adapters
        self._session_factory = session_factory
        self._events = events
        self._settings = settings
        self._clock = clock
        self._publisher = publisher or InboxPublisher(adapters.pubsub, session_factory)
        self._db = CallDb(organisation_id, session_factory, events, self._publisher, adapters, settings, self._ids())
        self._strings = strings_for(setup.language)

        self._tasks: set[asyncio.Task[None]] = set()
        self._end: asyncio.Future[CallEnd] | None = None
        self._end_reason: str | None = None  # what _end_call acted on
        self._ending = False
        self._handing_off = False  # this session began handing the call to people (transfer or callback)
        self._transport_ended = asyncio.Event()
        # Set once the call's media line is closed, possibly well before run() returns (it may still wait for a
        # turn to persist). The caller can free the call's capacity slot then.
        self.line_closed = asyncio.Event()
        self._goodbye_played = asyncio.Event()

        # playback marks
        self._playing = False  # frames sent since the last clear, and not all played back yet
        self._last_mark: str | None = None
        self._frames_after_mark = 0
        self._marks = 0  # turn-{n} counter; turn-0 is the greeting

        # silence
        self._silence_armed = False
        self._silence_stage = 0
        self._activity = asyncio.Event()

    # --- hooks (each session's media) ------------------------------------------------------------------

    def _stop_output(self, *, clear: bool) -> None:
        """Drops what is queued to be said (and with `clear`, the audio the transport buffered). Synchronous."""
        raise NotImplementedError

    async def _interrupt_output(self) -> None:
        """The call's time is up: stop what the bot is saying now."""
        raise NotImplementedError

    async def _handoff_starting(self) -> None:
        """The transfer or callback-promise ending is about to speak its closing words."""

    async def _speak_closing(self, text: str) -> None:
        """Queues the closing words, followed by the `goodbye` mark. May raise ProviderFailure."""
        raise NotImplementedError

    async def _prompt_still_there(self) -> None:
        """The first silence stage: "Are you still there?", ending with the next turn mark."""
        raise NotImplementedError

    def _idle(self) -> bool:
        """Nothing is being said, queued or worked on, and the call is not ending: the silence timer may run."""
        raise NotImplementedError

    async def _exchange_escalated(self) -> bool:
        """After the line closed: whether the exchange in progress at a hangup handed the call to people."""
        return await self._left_bot()

    # --- tasks and the ending --------------------------------------------------------------------------

    def _spawn(self, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(self._guarded(work))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _guarded(self, work: Coroutine[Any, Any, None]) -> None:
        try:
            await work
        except asyncio.CancelledError:
            raise
        except ProviderFailure as exc:
            await self._provider_failed(exc.provider, exc.error_type)
        except TransportClosed:
            self._transport_ended.set()
            self._finish(CallEnd("caller_hangup"))
        except Exception as exc:  # noqa: BLE001 - a failed task ends the call as an error
            logger.error("voice_session_task_failed", **self._ids(), error_type=type(exc).__name__)
            self._finish(CallEnd("error"))

    def _finish(self, end: CallEnd) -> None:
        """The first ending wins; the coordinator in run() acts on it."""
        if self._end is not None and not self._end.done():
            self._end.set_result(end)

    async def _end_call(self, end: CallEnd) -> None:
        self._ending = True
        self._end_reason = end.reason
        self._activity.set()
        logger.info("voice_session_ending", **self._ids(), end_reason=end.reason, error_type=end.error_type)
        # A turn still running is not interrupted: _shutdown() waits for it to persist, after the line is closed.
        if end.reason == "caller_hangup":
            self._stop_output(clear=False)
            await self._close_transport()
            await self._db.calls(lambda calls: calls.set_end_reason(self._call_id, "caller_hangup"))
        elif end.reason == "error":
            self._stop_output(clear=False)
            if end.provider is not None and end.error_type is not None:
                await self._record_provider_error(end.provider, end.error_type)
            await self._db.calls(lambda calls: calls.request_callback(self._call_id, "error"))
            await self._close_transport()  # at once: the <Connect action> speaks the trouble message
        elif end.reason == "transfer":
            # The stream's close makes Twilio run the <Connect action>, which joins the call to the transfer's
            # conference: the transfer is committed before the transport is closed (in _shutdown).
            await self._handoff_starting()
            await self._goodbye(self._strings["voice_transferring"], self._begin_transfer)
        elif end.reason == "handoff_callback":
            await self._handoff_starting()
            key = "voice_callback_phone" if self._source == "phone" else "voice_callback_browser"
            await self._goodbye(
                self._strings[key],
                lambda: self._db.calls(lambda calls: calls.request_callback(self._call_id, "handoff_callback")),
            )
        elif end.reason == "max_duration":
            await self._interrupt_output()
            await self._goodbye(
                self._strings["voice_max_duration"],
                lambda: self._db.calls(lambda calls: calls.request_callback(self._call_id, "max_duration")),
            )
        elif end.reason == "silence":
            await self._goodbye(
                self._strings["voice_goodbye"],
                lambda: self._db.calls(lambda calls: calls.set_end_reason(self._call_id, "silence")),
            )

    async def _goodbye(self, text: str, record: Callable[[], Awaitable[Any]]) -> None:
        """Speaks the closing words, records the outcome (its own committed unit of work) while they play, and
        waits (bounded) for them to finish playing, or for the caller to hang up first."""
        try:
            await self._speak_closing(text)
        except ProviderFailure as exc:  # nothing will be spoken: record it and do not wait
            await self._record_provider_error(exc.provider, exc.error_type)
            self._goodbye_played.set()
        await record()
        waits = [asyncio.create_task(self._goodbye_played.wait()), asyncio.create_task(self._transport_ended.wait())]
        try:
            await asyncio.wait(
                waits, timeout=_timing().GOODBYE_MARK_TIMEOUT_SECONDS, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for waiter in waits:
                waiter.cancel()
            await asyncio.gather(*waits, return_exceptions=True)

    async def _begin_transfer(self) -> None:
        """The goodbye's record step for a transfer: the `ringing` transfer row, committed. Should that fail, the
        <Connect action> would find no transfer and just hang up, so the conversation becomes a callback ticket."""
        begun = await self._db.transfers(lambda transfers: transfers.begin(self._call_id))
        if begun is None:
            await self._db.calls(lambda calls: calls.request_callback(self._call_id, "handoff_callback"))

    async def _callback_if_hung_up_in_handoff(self, exchange_in_progress: bool) -> None:
        """One rule for both sessions, run after the line closed (and any running turn persisted). A caller who
        hung up while the call was being handed to people still becomes a callback ticket; the end reason stays
        caller_hangup, since the first one wins. "Being handed to people" means one of two things:
          - this session had begun the handoff;
          - the exchange in progress at the hangup (a chained turn, a model reply) left the conversation no longer
            the bot's."""
        if self._end_reason != "caller_hangup":
            return
        if self._handing_off or (exchange_in_progress and await self._exchange_escalated()):
            await self._db.calls(lambda calls: calls.request_callback(self._call_id, "handoff_callback"))

    async def _left_bot(self) -> bool:
        """Whether the conversation is no longer the bot's (someone took over, or it was routed)."""

        async def status(session: AsyncSession) -> str | None:
            conversation = await TenantScopedRepository(session, Conversation, self._org_id).get(
                self._conversation_id
            )
            return conversation.status if conversation is not None else None

        current = await self._db.session(status)
        return current is not None and current != "bot"

    async def _close_transport(self) -> None:
        with suppress(Exception):
            await self._transport.close()

    # --- playback marks, silence and the time limit ------------------------------------------------------

    def _mark_played(self, name: str) -> None:
        if name == "goodbye":
            self._goodbye_played.set()
        if name == self._last_mark and self._frames_after_mark == 0:
            self._playing = False
            self._arm_silence()

    def _arm_silence(self) -> None:
        if self._idle():
            self._silence_armed = True
            self._activity.set()

    def _disarm_silence(self) -> None:
        if self._silence_armed:
            self._silence_armed = False
            self._activity.set()

    async def _silence_watch(self) -> None:
        while not self._ending:
            self._activity.clear()
            if not self._silence_armed:
                await self._activity.wait()
                continue
            timing = _timing()
            timeout = timing.SILENCE_PROMPT_SECONDS if self._silence_stage == 0 else timing.SILENCE_HANGUP_SECONDS
            try:
                await asyncio.wait_for(self._activity.wait(), timeout)
                continue  # something happened: re-evaluate (caller speech restarts the timer)
            except TimeoutError:
                pass
            if self._ending or not self._silence_armed:
                continue
            if self._silence_stage == 0:
                self._silence_stage = 1
                await self._prompt_still_there()
            else:
                self._finish(CallEnd("silence"))
                return

    async def _time_limit(self, started: float) -> None:
        await asyncio.sleep(max(0.0, self._setup.max_call_seconds - (self._clock() - started)))
        self._finish(CallEnd("max_duration"))

    # --- errors -----------------------------------------------------------------------------------------

    async def _provider_failed(self, provider: str, error_type: str) -> None:
        if not self._ending:
            self._finish(CallEnd("error", provider, error_type))
            return
        # Already ending (e.g. the provider failed on the goodbye): record it and stop waiting for the goodbye.
        await self._record_provider_error(provider, error_type)
        self._goodbye_played.set()

    async def _record_provider_error(self, provider: str, error_type: str) -> None:
        logger.warning("voice_provider_failed", **self._ids(), provider=provider, error_type=error_type)
        VOICE_PROVIDER_ERRORS.labels(provider=provider, error_type=error_type).inc()
        await record_integration_error(self._session_factory, self._org_id, "voice", provider, error_type)

    # --- ids -------------------------------------------------------------------------------------------

    def _ids(self) -> dict[str, str]:
        return {
            "organisation_id": str(self._org_id),
            "call_id": str(self._call_id),
            "conversation_id": str(self._conversation_id),
        }
