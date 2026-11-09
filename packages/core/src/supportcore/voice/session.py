"""VoiceSession: one call, end to end, over a MediaTransport.

Greeting -> caller audio to STT -> utterances stored and answered by `TurnRunner.answer_now` -> the reply's
sentences through TTS -> audio frames out. Handles barge-in, silence, the call's time limit, escalation (a live
transfer when someone can take the call now: "please hold", the transfer begun, the stream closed and the `<Connect
action>` joins the conference; otherwise a spoken callback promise) and provider failures (the `<Connect action>` then
speaks the trouble message).

Tasks (all owned by `run()`, none outlives it):
  - transport reader: caller audio to STT, playback marks, the end of the stream;
  - STT reader: transcripts into utterances, barge-in, speech activity;
  - store loop: each utterance stored as a customer message (its own unit of work);
  - turn loop: one `answer_now` at a time, in a shielded inner task so a hangup never interrupts its persistence;
  - speaker: the only writer of outbound media (TTS frames, marks, `clear`), in queue order;
  - silence watch and the max-duration timer.

The lifecycle shared with the speech-to-speech session (task supervision, endings, goodbye, transfer commit,
marks, silence, time limit, error recording) is `CallLifecycle`; the timing constants below are read from this
module at use time by both. No database session is open across an await on STT, TTS, the transport or
`answer_now`: every database step is its own short unit of work (`CallDb`). Never logged: spoken text,
transcripts, phone numbers or audio.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from supportcore.bot.nodes import (
    CHAT_TIMEOUT_SECONDS,
    CLASSIFIER_TIMEOUT_SECONDS,
    MAX_TOOL_ROUNDS,
)
from supportcore.bot.turns import ANSWER_NOW_RETRY_SECONDS, ANSWER_NOW_WAIT_SECONDS
from supportcore.config import Settings
from supportcore.events import EventBus
from supportcore.inbox.publisher import InboxPublisher
from supportcore.voice.call_lifecycle import CallEnd, CallLifecycle, ProviderFailure
from supportcore.voice.metrics import VOICE_RESPONSE_LATENCY
from supportcore.voice.settings_service import VoiceSetup
from supportcore.voice.speech_text import (
    MAX_SPOKEN_CHARS,
    SentenceSplitter,
    cap_spoken,
    to_speech_text,
)
from supportcore.voice.stt.ports import (
    SpeechStarted,
    SttAuthError,
    SttError,
    SttStream,
    Transcript,
    UtteranceEnd,
)
from supportcore.voice.transport import (
    AudioIn,
    MarkPlayed,
    MediaTransport,
    Stop,
)
from supportcore.voice.tts.ports import TextToSpeech, TtsAuthError, TtsError

if TYPE_CHECKING:
    from supportcore.bot.graph import TurnResult
    from supportcore.bot.turns import TurnRunner
    from supportcore.wiring import Adapters

__all__ = [
    "GOODBYE_MARK_TIMEOUT_SECONDS",
    "MAX_SPOKEN_CHARS",
    "SILENCE_HANGUP_SECONDS",
    "SILENCE_PROMPT_SECONDS",
    "TURN_FINISH_TIMEOUT_SECONDS",
    "TURN_LEASE_WAIT_SECONDS",
    "TURN_RETRY_SECONDS",
    "VoiceSession",
]

logger = structlog.get_logger()

# Read at use time (module globals), so tests can patch them.
SILENCE_PROMPT_SECONDS = 10  # after the bot's last words played, with no caller speech: "Are you still there?"
SILENCE_HANGUP_SECONDS = 10  # then this much more: goodbye and hang up
GOODBYE_MARK_TIMEOUT_SECONDS = 15  # longest wait for the closing words to play before hanging up
# The turn lease: answer_now retries a busy lease every TURN_RETRY_SECONDS for up to TURN_LEASE_WAIT_SECONDS and
# then raises TimeoutError. The runner owns these values; they are aliased here so they cannot drift.
TURN_RETRY_SECONDS = ANSWER_NOW_RETRY_SECONDS
TURN_LEASE_WAIT_SECONDS = ANSWER_NOW_WAIT_SECONDS
# How long the end of a call waits for a running turn to persist: the longest turn the runner allows (the lease
# wait, the risk classifier, then an answer call per tool round plus the final answer) and a margin for the
# retrieval and persistence around them. Past it the turn is cancelled (answer_now then publishes its failure
# notice and releases the lease).
TURN_FINISH_TIMEOUT_SECONDS = (
    TURN_LEASE_WAIT_SECONDS + CLASSIFIER_TIMEOUT_SECONDS + CHAT_TIMEOUT_SECONDS * (MAX_TOOL_ROUNDS + 1) + 30
)
STORE_DRAIN_TIMEOUT_SECONDS = 10  # the end of a call waits this long for heard utterances to be stored

@dataclass(frozen=True, eq=False)
class _Say:
    text: str
    owner: _TurnSpeech | None = None  # the turn that produced it; None for the bot's own lines (greeting...)


@dataclass(frozen=True)
class _Mark:
    name: str


@dataclass(frozen=True)
class _Clear: ...


_Out = _Say | _Mark | _Clear


class _TurnSpeech:
    """The current turn's spoken output: splitter state and the running MAX_SPOKEN_CHARS budget."""

    def __init__(self) -> None:
        self.splitter = SentenceSplitter()
        self.chars = 0
        self.capped = False
        self.muted = False  # the caller barged in: the rest of this turn is not spoken
        self.spoke = False
        self.audio_sent = False  # frames of this turn's speech went out (since its last reset)
        self.utterance_ends: list[float] = []  # when the utterances this turn answers ended (latency)


class _SpeakingSink:
    """The TurnSink handed to answer_now. It only enqueues work and returns: TTS and playback run in the
    speaker task (a sink call that blocks would be dropped by the runner)."""

    def __init__(self, session: VoiceSession, speech: _TurnSpeech) -> None:
        self._session = session
        self._speech = speech

    async def token(self, delta: str) -> None:
        self._session._speak_delta(self._speech, delta)

    async def reset(self) -> None:
        self._session._reset_turn(self._speech)


class VoiceSession(CallLifecycle):
    def __init__(
        self,
        call_id: UUID,
        organisation_id: UUID,
        conversation_id: UUID,
        source: str,
        setup: VoiceSetup,
        transport: MediaTransport,
        adapters: Adapters,
        runner: TurnRunner,
        session_factory: async_sessionmaker[AsyncSession],
        events: EventBus,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.monotonic,
        publisher: InboxPublisher | None = None,
        tts_factory: Callable[[str], TextToSpeech] | None = None,
    ) -> None:
        super().__init__(
            call_id,
            organisation_id,
            conversation_id,
            source,
            setup,
            transport,
            adapters,
            session_factory,
            events,
            settings,
            clock=clock,
            publisher=publisher,
        )
        self._runner = runner
        # The TTS adapter is created in run(), where a failure ends the call properly. The voice process passes
        # its shared per-provider adapters (closed at shutdown); the default builds one for this call.
        self._tts_factory = tts_factory or adapters.text_to_speech.create
        self._tts: TextToSpeech | None = None

        self._stt: SttStream | None = None
        # line_closed (CallLifecycle) is set once STT is closed, TTS stopped and the transport closed.

        # outbound media (speaker)
        self._out: deque[_Out] = deque()
        self._out_ready = asyncio.Event()
        self._tts_task: asyncio.Task[None] | None = None
        self._tts_characters = 0
        self._speaking: _TurnSpeech | None = None  # owner of the text being synthesized now

        # caller speech and turns
        self._finals: list[str] = []
        self._utterances: asyncio.Queue[tuple[str, float]] = asyncio.Queue()  # (text, when it ended)
        self._stored_ends: list[float] = []  # stored utterances not yet taken by a turn
        self._answer_needed = asyncio.Event()
        self._turn_task: asyncio.Task[TurnResult | None] | None = None
        self._turn_speech: _TurnSpeech | None = None
        self._in_turn = False
        self._storing = False
        self._store_task: asyncio.Task[None] | None = None

    # --- lifecycle -------------------------------------------------------------------------------------

    async def run(self) -> None:
        """Runs the call to its end. Never raises (cancellation still propagates after cleanup)."""
        self._end = asyncio.get_running_loop().create_future()
        started = self._clock()
        try:
            self._tts = self._tts_factory(self._setup.tts.provider)
            try:
                self._stt = await self._adapters.speech_to_text.create(self._setup.stt.provider).open(self._setup.stt)
            except (SttAuthError, SttError) as exc:
                await self._end_call(CallEnd("error", self._setup.stt.provider, _error_type(exc)))
                return
            self._store_task = self._spawn(self._store_loop())
            for work in (
                self._read_transport(),
                self._read_stt(),
                self._turn_loop(),
                self._speaker(),
                self._silence_watch(),
                self._time_limit(started),
            ):
                self._spawn(work)
            self._greet()
            await self._end_call(await self._end)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - run() never raises: any bug ends the call as an error
            logger.error("voice_session_failed", **self._ids(), error_type=type(exc).__name__)
            await self._db.calls(lambda calls: calls.request_callback(self._call_id, "error"))
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        self._ending = True
        if self._store_task is not None and not self._store_task.done():
            # let the store loop finish the utterances it has (cancelling it mid-store would lose one)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._utterances.join(), STORE_DRAIN_TIMEOUT_SECONDS)
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._stop_tts()
        if self._stt is not None:
            with suppress(Exception):
                await self._stt.aclose()
        await self._close_transport()
        self.line_closed.set()
        # STT is closed and TTS stopped: the usage is final, so the call row gets it before any turn wait.
        stt_seconds = self._stt.audio_seconds if self._stt is not None else 0.0
        await self._db.calls(lambda calls: calls.add_usage(self._call_id, stt_seconds, self._tts_characters))
        turn_running = self._in_turn  # a turn was still running when the call ended
        await self._finish_turn()
        # The caller asked for a person and hung up before the turn finished: it still becomes a callback ticket.
        await self._callback_if_hung_up_in_handoff(turn_running)
        await self._store_remaining()

    async def _exchange_escalated(self) -> bool:
        return await self._turn_escalated()

    async def _turn_escalated(self) -> bool:
        """Whether the last turn routed the conversation, or it is otherwise no longer the bot's."""
        task = self._turn_task
        if task is not None and task.done() and not task.cancelled() and task.exception() is None:
            result = task.result()
            if result is not None and result.routed:
                return True
        return await self._left_bot()

    async def _finish_turn(self) -> None:
        """Waits for a running answer_now to finish (it persists the turn); cancels it past the bound."""
        task = self._turn_task
        if task is None or task.done():
            return
        done, _pending = await asyncio.wait({task}, timeout=TURN_FINISH_TIMEOUT_SECONDS)
        if not done:
            task.cancel()
            logger.warning("voice_turn_abandoned", **self._ids())
            await asyncio.wait({task})
        if not task.cancelled():
            task.exception()  # retrieved (the turn loop logs a failed turn, unless it was cancelled first)

    # --- inbound: transport and STT ---------------------------------------------------------------------

    async def _read_transport(self) -> None:
        try:
            async for event in self._transport.events():
                if isinstance(event, AudioIn):
                    if not self._ending and self._stt is not None:
                        await self._stt_send(event.frame)
                elif isinstance(event, MarkPlayed):
                    self._mark_played(event.name)
                elif isinstance(event, Stop):
                    break
        finally:
            self._transport_ended.set()
        self._finish(CallEnd("caller_hangup"))

    async def _stt_send(self, frame: bytes) -> None:
        assert self._stt is not None
        try:
            await self._stt.send(frame)
        except (SttAuthError, SttError) as exc:
            raise ProviderFailure(self._setup.stt.provider, _error_type(exc)) from None

    async def _read_stt(self) -> None:
        assert self._stt is not None
        try:
            async for event in self._stt.events():
                if self._ending:
                    continue
                if isinstance(event, SpeechStarted):
                    continue  # voice activity alone (often noise) is neither speech nor a barge-in
                if isinstance(event, Transcript):
                    self._transcript(event)
                elif isinstance(event, UtteranceEnd):
                    self._utterance_done()
        except (SttAuthError, SttError) as exc:
            raise ProviderFailure(self._setup.stt.provider, _error_type(exc)) from None
        if not self._ending:  # the provider closed the stream mid-call: the caller can no longer be heard
            raise ProviderFailure(self._setup.stt.provider, "stream_error")

    def _transcript(self, event: Transcript) -> None:
        text = event.text.strip()
        if text:
            self._caller_activity()
            if self._playing:
                self._barge_in()
        if event.is_final and text:
            self._finals.append(text)
        if event.speech_final:
            self._utterance_done()

    def _barge_in(self) -> None:
        logger.info("voice_barge_in", **self._ids())
        if self._turn_speech is not None:
            self._turn_speech.muted = True
        self._cancel_speech(clear=True)

    def _utterance_done(self) -> None:
        text = " ".join(self._finals).strip()
        self._finals.clear()
        if not text:
            return
        self._disarm_silence()
        self._utterances.put_nowait((text, self._clock()))

    def _caller_activity(self) -> None:
        """Words from the caller (a transcript with text): back to the first silence stage, timer restarted."""
        self._silence_stage = 0
        self._activity.set()

    # --- utterances and turns ---------------------------------------------------------------------------

    async def _store_loop(self) -> None:
        while True:
            text, ended_at = await self._utterances.get()
            self._storing = True
            try:
                await self._store(text)
                self._stored_ends.append(ended_at)
            finally:
                self._storing = False
                self._utterances.task_done()
            if not self._ending:
                self._answer_needed.set()

    async def _store(self, text: str) -> None:
        await self._db.calls(lambda calls: calls.add_caller_utterance(self._call_id, text))

    async def _store_remaining(self) -> None:
        """At the end of the call: utterances heard but not yet stored still belong in the transcript."""
        while not self._utterances.empty():
            text, _ended_at = self._utterances.get_nowait()
            await self._store(text)

    async def _turn_loop(self) -> None:
        while True:
            await self._answer_needed.wait()
            self._answer_needed.clear()
            if self._ending:
                return
            self._in_turn = True
            self._disarm_silence()
            speech = self._turn_speech = _TurnSpeech()
            speech.utterance_ends, self._stored_ends = self._stored_ends, []
            self._turn_task = asyncio.create_task(
                self._runner.answer_now(self._org_id, self._conversation_id, _SpeakingSink(self, speech))
            )
            result: TurnResult | None = None
            failed = False
            try:
                result = await asyncio.shield(self._turn_task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - TimeoutError (lease busy) or any failure: a failed turn
                failed = True
                logger.warning("voice_turn_failed", **self._ids(), error_type=type(exc).__name__)
            if not speech.muted and not speech.capped:  # the tail first: it never waits on the database
                rest = speech.splitter.flush()
                if rest:
                    self._speak_sentence(speech, rest)
            routed = result is not None and result.routed
            escalated = routed or (not failed and not self._ending and await self._left_bot())
            self._turn_speech = None
            if self._ending:
                return
            if escalated:
                # A live transfer when someone can take the call now, else the callback promise.
                self._handing_off = True
                transfer = await self._db.transfers(lambda transfers: transfers.can_transfer(self._call_id))
                self._finish(CallEnd("transfer" if transfer else "handoff_callback"))
                return
            answer_failed = failed or (result is not None and result.outcome == "error" and not speech.spoke)
            if answer_failed and not speech.muted:  # after a barge-in the caller is already talking
                self._enqueue(_Say(self._strings["voice_turn_failed"], speech))
            self._in_turn = False
            self._mark_turn()

    # --- speaking ---------------------------------------------------------------------------------------

    def _greet(self) -> None:
        self._enqueue(_Say(self._strings["voice_greeting"].format(organisation=self._setup.organisation_name)))
        if self._setup.record_calls:
            self._enqueue(_Say(self._strings["voice_recording_notice"]))
        self._enqueue(_Mark("turn-0"))

    def _mark_turn(self) -> None:
        self._marks += 1
        self._enqueue(_Mark(f"turn-{self._marks}"))

    def _speak_delta(self, speech: _TurnSpeech, delta: str) -> None:
        if self._ending or speech.muted or speech.capped:
            return
        for sentence in speech.splitter.feed(delta):
            self._speak_sentence(speech, sentence)

    def _speak_sentence(self, speech: _TurnSpeech, sentence: str) -> None:
        if self._ending or speech.muted or speech.capped:
            return
        text = to_speech_text(sentence)
        if not text:
            return
        remaining = MAX_SPOKEN_CHARS - speech.chars
        if len(text) > remaining:
            speech.capped = True
            text = cap_spoken(text, remaining) if remaining > 0 else ""
            # Past the first sentence, only a cut at a sentence end is spoken.
            if not text or (speech.chars > 0 and text[-1] not in ".!?"):
                return
        speech.chars += len(text)
        speech.spoke = True
        self._enqueue(_Say(text, speech))

    def _reset_turn(self, speech: _TurnSpeech) -> None:
        """The streamed text is not the reply after all: drop this turn's speech, unspoken or playing. Earlier
        turns' speech is left alone; `clear` (which empties the whole playback buffer) is sent only when this
        turn's own audio already went out."""
        speech.splitter = SentenceSplitter()
        speech.chars = 0
        speech.capped = False
        speech.spoke = False
        if self._ending or speech.muted:
            return
        kept = [item for item in self._out if not (isinstance(item, _Say) and item.owner is speech)]
        self._out.clear()
        self._out.extend(kept)
        if self._speaking is speech and self._tts_task is not None and not self._tts_task.done():
            self._tts_task.cancel()
        if speech.audio_sent:
            speech.audio_sent = False
            self._playing = False
            self._out.appendleft(_Clear())
            self._out_ready.set()

    def _enqueue(self, item: _Out) -> None:
        if isinstance(item, _Say):
            self._disarm_silence()
        self._out.append(item)
        self._out_ready.set()

    def _cancel_speech(self, *, clear: bool) -> None:
        """Drops queued speech, stops the TTS in progress and (optionally) tells the transport to drop the audio
        it buffered. Synchronous: callers never wait on TTS or the transport."""
        kept = [item for item in self._out if not isinstance(item, _Say)]
        self._out.clear()
        self._out.extend(kept)
        if self._tts_task is not None and not self._tts_task.done():
            self._tts_task.cancel()
        if clear:
            self._playing = False
            self._out.appendleft(_Clear())
            self._out_ready.set()

    async def _speaker(self) -> None:
        try:
            while True:
                while not self._out:
                    self._out_ready.clear()
                    await self._out_ready.wait()
                item = self._out.popleft()
                if isinstance(item, _Say):
                    await self._play(item.text, item.owner)
                elif isinstance(item, _Mark):
                    self._last_mark = item.name
                    self._frames_after_mark = 0
                    await self._transport.send_mark(item.name)
                else:
                    self._playing = False
                    await self._transport.clear()
        finally:
            await self._stop_tts()

    async def _play(self, text: str, owner: _TurnSpeech | None) -> None:
        self._speaking = owner
        self._tts_task = asyncio.create_task(self._synthesize(text, owner))
        try:
            await self._tts_task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():  # type: ignore[union-attr]
                raise
            # only the TTS was cancelled (barge-in or reset): go on with the queue
        finally:
            self._tts_task = None
            self._speaking = None

    async def _synthesize(self, text: str, owner: _TurnSpeech | None) -> None:
        self._tts_characters += len(text)
        assert self._tts is not None
        frames = self._tts.synthesize(text, self._setup.tts)
        try:
            async for frame in frames:
                self._frame_out(owner)
                await self._transport.send_audio(frame)
        except (TtsAuthError, TtsError) as exc:
            raise ProviderFailure(self._setup.tts.provider, _error_type(exc)) from None
        finally:
            close = getattr(frames, "aclose", None)
            if close is not None:
                with suppress(Exception):
                    await close()

    def _frame_out(self, owner: _TurnSpeech | None) -> None:
        self._playing = True
        self._frames_after_mark += 1
        if owner is not None:
            owner.audio_sent = True
            now = self._clock()
            for ended_at in owner.utterance_ends:  # once per utterance, at its answer's first frame
                VOICE_RESPONSE_LATENCY.observe(max(0.0, now - ended_at))
            owner.utterance_ends = []

    async def _stop_tts(self) -> None:
        task = self._tts_task
        if task is not None and not task.done():
            task.cancel()
            with suppress(BaseException):
                await task

    # --- CallLifecycle hooks -----------------------------------------------------------------------------

    def _stop_output(self, *, clear: bool) -> None:
        self._cancel_speech(clear=clear)

    async def _interrupt_output(self) -> None:
        self._cancel_speech(clear=True)

    async def _speak_closing(self, text: str) -> None:
        self._enqueue(_Say(text))
        self._enqueue(_Mark("goodbye"))

    async def _prompt_still_there(self) -> None:
        self._enqueue(_Say(self._strings["voice_still_there"]))
        self._mark_turn()

    def _idle(self) -> bool:
        return not (
            self._ending
            or self._in_turn
            or self._out
            or self._answer_needed.is_set()
            or self._storing
            or not self._utterances.empty()
        )


def _error_type(exc: BaseException) -> str:
    if isinstance(exc, SttAuthError | TtsAuthError):
        return "auth_failed"
    if isinstance(exc, SttError | TtsError):
        return exc.error_type
    return "stream_error"
