"""Composition root of the voice process: settings, adapters, the bot's TurnRunner, the call counter and the
running calls."""

import asyncio
from collections.abc import Coroutine, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from supportcore.audit.subscribers import register_audit_subscribers
from supportcore.bot.runtime import BotRuntime
from supportcore.bot.turns import TurnRunner
from supportcore.config import Settings
from supportcore.db.session import get_current_session, session_factory
from supportcore.db.uow import UnitOfWork
from supportcore.events import EventBus
from supportcore.inbox.publisher import InboxPublisher
from supportcore.jobs.ports import JobRuntime
from supportcore.observability.logging import get_logger
from supportcore.realtime.spawn import drain_pending
from supportcore.voice.metrics import VOICE_ACTIVE_CALLS
from supportcore.voice.telephony.ports import TelephonyAccountRef
from supportcore.voice.transfer_actions import load_account_ref, run_transfer_actions
from supportcore.voice.transfers import (
    TRANSFER_WATCHDOG_GRACE_SECONDS,
    TransferAction,
    TransferService,
)
from supportcore.voice.tts.ports import TextToSpeech
from supportcore.wiring import Adapters, build_adapters

logger = get_logger(__name__)

# Shutdown: how long cancelled calls get to finish their cleanup (close the line, store what was heard).
CALL_SHUTDOWN_SECONDS = 5.0


class ActiveCalls:
    """Voice sessions running in this process, against `voice_max_calls`. The inbound and browser webhooks check
    `full`; the media stream `acquire()`s when it connects and `release()`s once the call's line is closed."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._count = 0

    @property
    def count(self) -> int:
        return self._count

    @property
    def full(self) -> bool:
        return self._count >= self._limit

    def acquire(self) -> bool:
        if self.full:
            return False
        self._count += 1
        VOICE_ACTIVE_CALLS.set(self._count)
        return True

    def release(self) -> None:
        self._count = max(0, self._count - 1)
        VOICE_ACTIVE_CALLS.set(self._count)


@dataclass
class VoiceContainer:
    settings: Settings
    events: EventBus
    adapters: Adapters
    inbox_publisher: InboxPublisher
    turn_runner: TurnRunner
    bot_runtime: BotRuntime  # the runtime the turn_runner uses; speech-to-speech sessions share it
    active_calls: ActiveCalls
    session_factory: async_sessionmaker[AsyncSession]
    _tts: dict[str, TextToSpeech] = field(default_factory=dict)
    _calls: set[asyncio.Task[None]] = field(default_factory=set)
    _transfer_work: set[asyncio.Task[None]] = field(default_factory=set)  # live-transfer Twilio actions
    _watchdogs: set[asyncio.Task[None]] = field(default_factory=set)  # one per started transfer
    _closing: bool = False  # aclose() has begun: no new transfer tasks; a second aclose() does nothing

    def tts(self, provider: str) -> TextToSpeech:
        """One adapter per provider for the whole process (its HTTP clients are shared and closed in aclose())."""
        if provider not in self._tts:
            self._tts[provider] = self.adapters.text_to_speech.create(provider)
        return self._tts[provider]

    def start_call(self, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Runs one call's session as a task the process tracks: it can outlive its WebSocket (a hung-up call may
        still be persisting its last turn), and shutdown cancels it."""
        task = asyncio.get_running_loop().create_task(work)
        self._calls.add(task)
        task.add_done_callback(self._calls.discard)
        task.add_done_callback(_log_failure)
        return task

    def run_actions(
        self, organisation_id: UUID, account: TelephonyAccountRef, actions: Iterable[TransferAction]
    ) -> asyncio.Task[None] | None:
        """Runs a transfer's Twilio work as a tracked task. Call it once the unit of work that produced the actions
        has committed (`after_commit`); no session is open while Twilio is called."""
        actions = list(actions)
        if not actions:
            return None
        return self._track(
            self._transfer_work,
            run_transfer_actions(
                adapters=self.adapters,
                session_factory=self.session_factory,
                events=self.events,
                publisher=self.inbox_publisher,
                settings=self.settings,
                organisation_id=organisation_id,
                account=account,
                actions=actions,
            ),
        )

    def watch_transfer(
        self, transfer_id: UUID, organisation_id: UUID, deadline: datetime
    ) -> asyncio.Task[None] | None:
        """Gives the transfer up if it is still ringing a little after its deadline (`deadline_passed`)."""
        return self._track(self._watchdogs, self._watch(transfer_id, organisation_id, deadline))

    async def _watch(self, transfer_id: UUID, organisation_id: UUID, deadline: datetime) -> None:
        delay = (deadline - datetime.now(UTC)).total_seconds() + TRANSFER_WATCHDOG_GRACE_SECONDS
        await asyncio.sleep(max(0.0, delay))
        try:
            async with UnitOfWork(self.session_factory) as uow:
                service = TransferService(
                    uow.session,
                    organisation_id,
                    self.events,
                    self.inbox_publisher,
                    self.adapters,
                    self.settings,
                    session_factory=self.session_factory,
                )
                actions = await service.deadline_passed(transfer_id)
                account = await load_account_ref(uow.session, self.adapters, organisation_id) if actions else None
                await uow.commit()
        except Exception as exc:  # noqa: BLE001  (logged by class name; the stale sweep is the backstop)
            logger.warning("transfer_watchdog_failed", transfer_id=str(transfer_id), error_type=type(exc).__name__)
            return
        if actions and account is not None:
            self.run_actions(organisation_id, account, actions)

    def _track(self, tasks: set[asyncio.Task[None]], work: Coroutine[Any, Any, None]) -> asyncio.Task[None] | None:
        """Starts `work` as a task in `tasks`; refused once shutdown has begun (it would outlive the adapters; the
        stale-call sweep then picks the transfer up)."""
        if self._closing:
            work.close()
            logger.warning("transfer_task_refused_at_shutdown")
            return None
        task = asyncio.get_running_loop().create_task(work)
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        task.add_done_callback(_log_failure)
        return task

    async def aclose(self) -> None:
        """Shutdown: transfer watchdogs (cancelled), running transfer actions (a moment to finish, then cancelled),
        running calls (cancelled, not waited for: a call's last turn may take minutes), bot turns, the TTS clients,
        in-flight publishes, then the adapters. The engine is the caller's to dispose. Idempotent."""
        if self._closing:
            return
        self._closing = True
        for task in list(self._watchdogs):
            task.cancel()
        await _settle(list(self._watchdogs) + list(self._transfer_work), CALL_SHUTDOWN_SECONDS)
        calls = list(self._calls)
        for task in calls:
            task.cancel()
        await self.turn_runner.aclose()  # cancels the calls' running turns, so their cleanup does not wait on them
        if calls:
            _done, pending = await asyncio.wait(calls, timeout=CALL_SHUTDOWN_SECONDS)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=CALL_SHUTDOWN_SECONDS)
        for tts in self._tts.values():
            close = getattr(tts, "aclose", None)
            if close is not None:
                await close()
        self._tts.clear()
        await drain_pending()  # in-flight inbox publishes need the Redis client
        await self.adapters.aclose()


async def _settle(tasks: list[asyncio.Task[None]], timeout: float) -> None:
    """Waits up to `timeout` for `tasks`, then cancels what is left and gives it the same time to unwind."""
    if not tasks:
        return
    _done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.wait(pending, timeout=timeout)


def _log_failure(task: asyncio.Task[None]) -> None:
    """A call task is written not to raise; if one ever does, it is logged (class name only) instead of asyncio's
    "exception was never retrieved"."""
    if not task.cancelled() and (exc := task.exception()) is not None:
        logger.error("voice_call_task_failed", error_type=type(exc).__name__)


def build_voice_container(settings: Settings, engine: AsyncEngine) -> VoiceContainer:
    events = EventBus()
    register_audit_subscribers(events, get_current_session)
    sessions = session_factory(engine)
    runtime: JobRuntime | None = None

    def job_runtime() -> JobRuntime:
        """For SUPPORT_JOB_QUEUE=inline (dev): jobs run in this process, built once the adapters exist."""
        nonlocal runtime
        if runtime is None:
            runtime = JobRuntime(settings=settings, session_factory=sessions, adapters=adapters, events=events)
        return runtime

    adapters = build_adapters(settings, engine=engine, runtime_factory=job_runtime)
    publisher = InboxPublisher(adapters.pubsub, sessions)
    bot_runtime = BotRuntime(
        settings=settings, session_factory=sessions, adapters=adapters, events=events, inbox_publisher=publisher
    )
    turn_runner = TurnRunner(bot_runtime)
    return VoiceContainer(
        settings=settings,
        events=events,
        adapters=adapters,
        inbox_publisher=publisher,
        turn_runner=turn_runner,
        bot_runtime=bot_runtime,
        active_calls=ActiveCalls(settings.voice_max_calls),
        session_factory=sessions,
    )
