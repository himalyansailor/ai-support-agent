"""RealtimeVoiceSession: one speech-to-speech call, end to end, over a MediaTransport.

Caller audio goes to the Realtime stream, and the model's audio comes back in 160-byte frames.

Caller transcripts are stored as customer messages and checked by the bot's risk check. An `escalate` cancels the
reply in progress and hands the call to people. The model's transcripts are stored as the bot's replies, with their
usage.

The model's tools are the knowledge base search and `transfer_to_human`. Fixed lines (the greeting, "are you still
there?", goodbyes) are spoken by the model as out-of-band responses, paired with their marks by say id.

The rest is the lifecycle shared with the chained session (`CallLifecycle`): barge-in, silence, the call's time
limit, the handoff (a live transfer when someone can take the call now, else the callback promise) and provider
failures (the `<Connect action>` then speaks the trouble message).

Once a handoff has begun, the model cannot talk over it:
  - caller audio is no longer forwarded;
  - tool results no longer ask for a response;
  - the reply in progress is cancelled;
  - any response that starts later is neither played nor stored.

The fixed lines are the exception.

Tasks (all owned by `run()`, none outlives it):
  - transport reader: caller audio to the stream, playback marks, the end of the stream;
  - Realtime reader: model audio into frames, transcripts, tool calls, barge-in, response ends (marks, replies);
  - store loop: caller utterances and bot replies, in order, each its own unit of work;
  - speaker: the only writer of outbound media (frames, marks, `clear`) and of truncations, in queue order;
  - tool and risk-check tasks, the silence watch and the max-duration timer.

No database session is open across an await on the Realtime stream, the transport or the toolbox: every database
step is its own short unit of work (`CallDb`). Never logged: spoken text, transcripts, tool arguments or results,
phone numbers or audio.

A stored reply is the model's whole transcript for the response. For a response cut short (a barge-in, a risk
cancel), that can include words the caller never heard, since the model generates faster than real time.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from supportcore.bot.prompts import build_realtime_instructions
from supportcore.config import Settings
from supportcore.events import EventBus
from supportcore.inbox.publisher import InboxPublisher
from supportcore.voice import session as chained
from supportcore.voice.audio import FRAME_BYTES
from supportcore.voice.call_lifecycle import CallEnd, CallLifecycle, ProviderFailure
from supportcore.voice.metrics import VOICE_RESPONSE_LATENCY
from supportcore.voice.realtime.ports import (
    AssistantTranscript,
    AudioOut,
    CallerSpeechStarted,
    CallerTranscript,
    RealtimeAuthError,
    RealtimeError,
    RealtimeEvent,
    RealtimeStream,
    ResponseDone,
    ResponseStarted,
    SayFailed,
    ToolCall,
)
from supportcore.voice.realtime.tools import SEARCH_TOOL, TRANSFER_TOOL, RealtimeToolbox
from supportcore.voice.settings_service import VoiceSetup
from supportcore.voice.transport import AudioIn, MarkPlayed, MediaTransport, Stop

if TYPE_CHECKING:
    from supportcore.bot.runtime import BotRuntime
    from supportcore.wiring import Adapters

__all__ = ["RISK_RECENT_MESSAGES", "VOICE_REQUESTED_HUMAN", "RealtimeVoiceSession"]

logger = structlog.get_logger()

_T = TypeVar("_T")

RISK_RECENT_MESSAGES = 6  # the stored (role, text) messages the risk check gets as history
# The decision a `transfer_to_human` tool call routes the conversation with.
VOICE_REQUESTED_HUMAN = {
    "action": "escalate",
    "rule_key": "voice_requested_human",
    "rule_name": "Caller asked for a person",
    "reason": None,
    "source": "voice",
}
_MULAW_SILENCE = b"\xff"
_MULAW_BYTES_PER_MS = 8
# The integration-error / metric provider id of a Realtime provider (the chained `openai` id is the TTS).
_ERROR_PROVIDER = {"openai": "openai_realtime"}


@dataclass(frozen=True)
class _Frame:
    response_id: str
    item_id: str
    audio: bytes = field(repr=False)


@dataclass(frozen=True)
class _Mark:
    name: str


@dataclass(frozen=True)
class _Clear: ...


@dataclass(frozen=True)
class _Truncate:
    item_id: str
    audio_end_ms: int


_Out = _Frame | _Mark | _Clear | _Truncate


@dataclass(frozen=True)
class _Utterance:
    text: str = field(repr=False)


@dataclass(frozen=True)
class _Reply:
    text: str = field(repr=False)
    decision: Mapping[str, Any]
    retrieval: Mapping[str, Any]
    model: Mapping[str, Any]
    latency_ms: int | None


@dataclass
class _Response:
    """A model reply heard so far: its transcripts and their items."""

    texts: list[str] = field(default_factory=list, repr=False)
    item_ids: list[str] = field(default_factory=list)


class RealtimeVoiceSession(CallLifecycle):
    def __init__(
        self,
        call_id: UUID,
        organisation_id: UUID,
        conversation_id: UUID,
        source: str,
        widget_id: UUID | None,
        setup: VoiceSetup,
        transport: MediaTransport,
        adapters: Adapters,
        runtime: BotRuntime,
        session_factory: async_sessionmaker[AsyncSession],
        events: EventBus,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.monotonic,
        publisher: InboxPublisher | None = None,
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
        self._toolbox = RealtimeToolbox(organisation_id, conversation_id, widget_id, setup.language, runtime)
        self._provider = setup.realtime.provider if setup.realtime is not None else "openai"
        self._error_provider = _ERROR_PROVIDER.get(self._provider, self._provider)
        self._stream: RealtimeStream | None = None
        self._reply_cancelled = False  # the handoff already cancelled the reply in progress (risk check)

        # responses: id -> out of band; and what is done with their audio and transcripts
        self._open: dict[str, bool] = {}
        self._dropped_responses: set[str] = set()  # their (late) audio is not played
        self._unstored_responses: set[str] = set()  # started after the handoff began: not a reply
        self._says = 0
        self._pending_says: dict[str, str] = {}  # say id -> the mark its line ends with

        # outbound media (speaker)
        self._out: deque[_Out] = deque()
        self._out_ready = asyncio.Event()
        self._send_task: asyncio.Task[None] | None = None
        self._remainders: dict[str, tuple[str, bytes]] = {}  # item -> (response, audio short of a whole frame)
        self._current_item: str | None = None  # the item and response of the last frame sent
        self._current_response: str | None = None
        self._item_bytes: dict[str, int] = {}
        self._item_first_sent: dict[str, float] = {}
        self._heard_at: float | None = None  # a caller transcript not yet answered by audio (latency)
        self._item_latency_ms: dict[str, int] = {}

        # replies and storing
        self._responses: dict[str, _Response] = {}
        self._pending_tokens = (0, 0)  # usage of model responses that had nothing to store (tool calls)
        self._recent: deque[tuple[str, str]] = deque(maxlen=RISK_RECENT_MESSAGES)
        self._stores: asyncio.Queue[_Utterance | _Reply] = asyncio.Queue()
        self._store_task: asyncio.Task[None] | None = None
        self._tools_running = 0
        self._caller_speaking = False

    # --- lifecycle -------------------------------------------------------------------------------------

    async def run(self) -> None:
        """Runs the call to its end. Never raises (cancellation still propagates after cleanup)."""
        self._end = asyncio.get_running_loop().create_future()
        started = self._clock()
        try:
            if self._setup.realtime is None:
                raise RuntimeError("speech-to-speech call without a Realtime setup")
            config = replace(
                self._setup.realtime,
                instructions=build_realtime_instructions(self._setup.organisation_name, self._setup.language),
                tools=self._toolbox.definitions(),
            )
            try:
                self._stream = await self._adapters.speech_to_speech.create(config.provider).open(config)
                await self._say(self._greeting(), "turn-0")
            except (RealtimeAuthError, RealtimeError) as exc:
                await self._end_call(CallEnd("error", self._error_provider, _error_type(exc)))
                return
            except ProviderFailure as exc:
                await self._end_call(CallEnd("error", exc.provider, exc.error_type))
                return
            self._store_task = self._spawn(self._store_loop())
            for work in (
                self._read_transport(),
                self._read_realtime(),
                self._speaker(),
                self._silence_watch(),
                self._time_limit(started),
            ):
                self._spawn(work)
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
        reply_open = any(not out_of_band for out_of_band in self._open.values())  # a reply was being heard
        if self._store_task is not None and not self._store_task.done():
            # let the store loop finish what it has (cancelling it mid-store would lose a message)
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stores.join(), chained.STORE_DRAIN_TIMEOUT_SECONDS)
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._stream is not None:
            with suppress(Exception):
                await self._stream.aclose()
        await self._close_transport()
        self.line_closed.set()
        audio_seconds = self._stream.audio_seconds if self._stream is not None else 0.0
        await self._db.calls(lambda calls: calls.add_usage(self._call_id, audio_seconds, 0))
        await self._store_remaining()
        await self._callback_if_hung_up_in_handoff(reply_open)

    # --- CallLifecycle hooks -----------------------------------------------------------------------------

    def _stop_output(self, *, clear: bool) -> None:
        self._drop_output(clear=clear)

    async def _interrupt_output(self) -> None:
        with suppress(ProviderFailure):  # a dead stream fails the goodbye too, which records it
            await self._realtime(self._live_stream().cancel_response())
        self._drop_output(clear=True)

    async def _handoff_starting(self) -> None:
        if not self._reply_cancelled:  # the model must not go on talking over the closing line
            self._reply_cancelled = True
            with suppress(ProviderFailure):
                await self._realtime(self._live_stream().cancel_response())

    async def _speak_closing(self, text: str) -> None:
        await self._say(text, "goodbye")

    async def _prompt_still_there(self) -> None:
        self._marks += 1
        await self._say(self._strings["voice_still_there"], f"turn-{self._marks}")

    def _idle(self) -> bool:
        return not (
            self._ending
            or self._handing_off
            or self._playing
            or self._out
            or self._remainders
            or self._pending_says
            or self._tools_running
            or self._caller_speaking
        )

    # --- the Realtime stream ---------------------------------------------------------------------------

    async def _realtime(self, operation: Awaitable[_T]) -> _T:
        try:
            return await operation
        except RealtimeAuthError:
            raise ProviderFailure(self._error_provider, "auth_failed") from None
        except RealtimeError as exc:
            raise ProviderFailure(self._error_provider, exc.error_type) from None

    def _live_stream(self) -> RealtimeStream:
        assert self._stream is not None
        return self._stream

    async def _say(self, text: str, mark: str) -> None:
        """A fixed line, spoken by the model out of band; `mark` is sent once its audio is out."""
        self._says += 1
        say_id = f"say-{self._says}"
        self._pending_says[say_id] = mark
        self._disarm_silence()
        await self._realtime(self._live_stream().say(text, say_id))

    async def _interrupt_reply(self) -> None:
        """Stops the model's reply in progress and drops its audio, sent or not."""
        await self._realtime(self._live_stream().cancel_response())
        self._drop_output(clear=True)

    # --- inbound: transport and Realtime -----------------------------------------------------------------

    async def _read_transport(self) -> None:
        try:
            async for event in self._transport.events():
                if isinstance(event, AudioIn):
                    if not (self._ending or self._handing_off):  # nothing new for the model to answer
                        await self._realtime(self._live_stream().send_audio(event.frame))
                elif isinstance(event, MarkPlayed):
                    self._mark_played(event.name)
                elif isinstance(event, Stop):
                    break
        finally:
            self._transport_ended.set()
        self._finish(CallEnd("caller_hangup"))

    async def _read_realtime(self) -> None:
        try:
            async for event in self._live_stream().events():
                self._on_event(event)
        except (RealtimeAuthError, RealtimeError) as exc:
            raise ProviderFailure(self._error_provider, _error_type(exc)) from None
        if not self._ending:  # the provider closed the stream mid-call
            raise ProviderFailure(self._error_provider, "stream_error")

    def _on_event(self, event: RealtimeEvent) -> None:
        if isinstance(event, ResponseStarted):
            self._response_started(event.response_id, event.out_of_band)
        elif isinstance(event, AudioOut):
            self._audio_out(event)
        elif isinstance(event, CallerSpeechStarted):
            if not (self._ending or self._handing_off):
                self._caller_speaking = True
                self._disarm_silence()
                if self._playing:
                    self._barge_in()
        elif isinstance(event, CallerTranscript):
            self._caller_transcript(event)
        elif isinstance(event, AssistantTranscript):
            if not self._open.get(event.response_id) and event.response_id not in self._unstored_responses:
                response = self._responses.setdefault(event.response_id, _Response())
                response.texts.append(event.text)
                response.item_ids.append(event.item_id)
        elif isinstance(event, ToolCall):
            self._tool_call(event)
        elif isinstance(event, ResponseDone):
            self._response_done(event)
        elif isinstance(event, SayFailed):
            self._say_failed(event.say_id)

    def _response_started(self, response_id: str, out_of_band: bool) -> None:
        if response_id in self._open:
            return
        self._open[response_id] = out_of_band
        if not out_of_band and (self._handing_off or self._ending):
            # A model response begun after the handoff: neither played nor stored (the fixed lines still are).
            self._dropped_responses.add(response_id)
            self._unstored_responses.add(response_id)

    def _caller_transcript(self, event: CallerTranscript) -> None:
        text = event.text.strip()
        if not text:
            return
        recent = list(self._recent)
        self._recent.append(("customer", text))
        self._stores.put_nowait(_Utterance(text))  # stored even while the call ends: it was said
        if self._ending or self._handing_off:
            return
        if self._heard_at is None:
            self._heard_at = self._clock()
        self._caller_speaking = False
        self._silence_stage = 0  # words from the caller: back to the first silence stage, timer restarted
        self._activity.set()
        self._arm_silence()
        self._spawn(self._assess(text, recent))

    def _barge_in(self) -> None:
        """The caller talks over the bot: the playback buffer is dropped, and the model is told how much of its
        item was heard (OpenAI cancels the response itself)."""
        item = self._current_item
        if item is None:
            return
        sent_ms = self._item_bytes.get(item, 0) / _MULAW_BYTES_PER_MS
        since_first_ms = (self._clock() - self._item_first_sent.get(item, self._clock())) * 1000
        played_ms = int(max(0.0, min(sent_ms, since_first_ms)))
        logger.info("voice_barge_in", **self._ids())
        self._drop_output(clear=True)
        self._out.insert(1, _Truncate(item, played_ms))  # right after the clear

    def _response_done(self, event: ResponseDone) -> None:
        rid = event.response_id
        self._flush_remainders(rid)
        out_of_band = self._open.pop(rid, event.out_of_band) or event.out_of_band
        response = self._responses.pop(rid, None)
        if out_of_band:  # a fixed line: not a reply; ends with its own mark, whatever its status
            mark = self._pending_says.pop(event.say_id, None) if event.say_id is not None else None
            if mark is None and event.say_id is None and self._pending_says:  # no id came back: the oldest
                mark = self._pending_says.pop(next(iter(self._pending_says)))
            if mark is not None:
                self._enqueue(_Mark(mark))
            return
        self._caller_speaking = False
        if rid in self._unstored_responses:
            return
        text = " ".join(response.texts).strip() if response is not None else ""
        if text and response is not None:
            self._stores.put_nowait(self._reply(text, response, event.input_tokens, event.output_tokens))
        else:  # e.g. the response that called a tool: its usage goes on the next reply
            pending_in, pending_out = self._pending_tokens
            self._pending_tokens = (pending_in + event.input_tokens, pending_out + event.output_tokens)
        self._marks += 1
        self._enqueue(_Mark(f"turn-{self._marks}"))

    def _say_failed(self, say_id: str) -> None:
        """The provider refused a fixed line: nothing will play, and no mark will follow it."""
        mark = self._pending_says.pop(say_id, None)
        logger.warning("voice_say_refused", **self._ids())
        if mark == "goodbye":
            self._goodbye_played.set()  # nothing to wait for
        elif mark is not None:
            self._arm_silence()

    def _reply(self, text: str, response: _Response, input_tokens: int | None, output_tokens: int | None) -> _Reply:
        """A bot reply to store: the toolbox's record for this response, its usage (None when the response never
        finished) and its latency."""
        decision, retrieval = self._toolbox.turn_record()
        if input_tokens is not None and output_tokens is not None:
            pending_in, pending_out = self._pending_tokens
            input_tokens, output_tokens = input_tokens + pending_in, output_tokens + pending_out
            self._pending_tokens = (0, 0)
        model = self._setup.realtime.model if self._setup.realtime is not None else None
        latencies = [self._item_latency_ms.pop(i) for i in response.item_ids if i in self._item_latency_ms]
        self._recent.append(("bot", text))
        return _Reply(
            text,
            decision,
            retrieval,
            {"provider": self._provider, "model": model, "input_tokens": input_tokens, "output_tokens": output_tokens},
            min(latencies) if latencies else None,
        )

    # --- tools and the risk check --------------------------------------------------------------------

    def _tool_call(self, event: ToolCall) -> None:
        if self._ending:
            return
        self._tools_running += 1
        self._disarm_silence()
        self._spawn(self._run_tool(event))

    async def _run_tool(self, event: ToolCall) -> None:
        stream = self._live_stream()
        try:
            if event.name == SEARCH_TOOL:
                query = event.arguments.get("query")
                output = await self._toolbox.search(query if isinstance(query, str) else "")
                await self._realtime(stream.send_tool_result(event.call_id, output, respond=self._may_respond()))
            elif event.name == TRANSFER_TOOL:
                logger.info("voice_transfer_requested", **self._ids())
                await self._realtime(stream.send_tool_result(event.call_id, {"ok": True}, respond=False))
                await self._hand_off(VOICE_REQUESTED_HUMAN)
            else:
                await self._realtime(
                    stream.send_tool_result(event.call_id, {"error": "unknown_tool"}, respond=self._may_respond())
                )
        finally:
            self._tools_running -= 1

    def _may_respond(self) -> bool:
        """Whether a tool result may ask the model for a response: not once the call is being handed over."""
        return not (self._handing_off or self._ending)

    async def _assess(self, text: str, recent: list[tuple[str, str]]) -> None:
        try:
            decision = await self._toolbox.assess(text, recent)
        except Exception as exc:  # noqa: BLE001 - a failed check leaves the reply alone; the call goes on
            logger.warning("voice_risk_check_failed", **self._ids(), error_type=type(exc).__name__)
            return
        if decision.get("action") != "escalate" or self._ending or self._handing_off:
            return
        self._handing_off = True
        self._reply_cancelled = True
        logger.info("voice_risk_escalation", **self._ids(), rule_key=decision.get("rule_key"))
        await self._interrupt_reply()
        await self._route_and_end(decision)

    async def _hand_off(self, decision: Mapping[str, Any]) -> None:
        if self._ending or self._handing_off:
            return
        self._handing_off = True
        await self._route_and_end(decision)

    async def _route_and_end(self, decision: Mapping[str, Any]) -> None:
        """Routes the conversation to the team, then ends the call into a live transfer when someone can take it
        now, else the callback promise."""
        await self._db.calls(lambda calls: calls.route_to_team(self._call_id, decision))
        transfer = await self._db.transfers(lambda transfers: transfers.can_transfer(self._call_id))
        self._finish(CallEnd("transfer" if transfer else "handoff_callback"))

    # --- storing ---------------------------------------------------------------------------------------

    async def _store_loop(self) -> None:
        while True:
            item = await self._stores.get()
            try:
                await self._store(item)
            finally:
                self._stores.task_done()

    async def _store(self, item: _Utterance | _Reply) -> None:
        if isinstance(item, _Utterance):
            await self._db.calls(lambda calls: calls.add_caller_utterance(self._call_id, item.text))
            return
        await self._db.calls(
            lambda calls: calls.add_bot_reply(
                self._call_id,
                item.text,
                decision=item.decision,
                retrieval=item.retrieval,
                model=item.model,
                latency_ms=item.latency_ms,
                routed=self._handing_off,  # this session's own handoff does not supersede the reply
            )
        )

    async def _store_remaining(self) -> None:
        """At the end of the call: what was heard but not yet stored still belongs in the transcript, including
        the transcript of a reply that never finished (its usage unknown). Each is stored once."""
        while not self._stores.empty():
            await self._store(self._stores.get_nowait())
        for response_id in list(self._responses):
            response = self._responses.pop(response_id)
            text = " ".join(response.texts).strip()
            if text:
                await self._store(self._reply(text, response, None, None))

    # --- outbound media ----------------------------------------------------------------------------------

    def _greeting(self) -> str:
        text = self._strings["voice_greeting"].format(organisation=self._setup.organisation_name)
        if self._setup.record_calls:
            text += " " + self._strings["voice_recording_notice"]
        return text

    def _audio_out(self, event: AudioOut) -> None:
        rid = event.response_id
        if rid not in self._open:  # its start was not seen: treat it as starting now
            self._response_started(rid, False)
        if rid in self._dropped_responses:
            return
        _rid, rest = self._remainders.pop(event.item_id, (rid, b""))
        data = rest + event.audio
        whole = len(data) - len(data) % FRAME_BYTES
        for start in range(0, whole, FRAME_BYTES):
            self._enqueue_frame(rid, event.item_id, data[start : start + FRAME_BYTES])
        if whole < len(data):
            self._remainders[event.item_id] = (rid, data[whole:])

    def _flush_remainders(self, response_id: str) -> None:
        """Pads and queues the last partial frame of each item of the response that finished."""
        for item_id, (rid, rest) in list(self._remainders.items()):
            if rid == response_id:
                del self._remainders[item_id]
                self._enqueue_frame(rid, item_id, rest.ljust(FRAME_BYTES, _MULAW_SILENCE))

    def _enqueue_frame(self, response_id: str, item_id: str, frame: bytes) -> None:
        if self._heard_at is not None:  # the first audio after the caller was heard: the response latency
            elapsed = max(0.0, self._clock() - self._heard_at)
            self._heard_at = None
            VOICE_RESPONSE_LATENCY.observe(elapsed)
            self._item_latency_ms.setdefault(item_id, round(elapsed * 1000))
        self._enqueue(_Frame(response_id, item_id, frame))

    def _enqueue(self, item: _Out) -> None:
        if isinstance(item, _Frame):
            self._disarm_silence()
        self._out.append(item)
        self._out_ready.set()

    def _drop_output(self, *, clear: bool) -> None:
        """Drops the audio of every response in progress, sent or not (late deltas included), stops the frame
        being sent and (optionally) tells the transport to drop the audio it buffered. Synchronous: callers never
        wait on the transport."""
        responses = set(self._open) | {item.response_id for item in self._out if isinstance(item, _Frame)}
        responses |= {rid for rid, _rest in self._remainders.values()}
        if self._playing and self._current_response is not None:
            responses.add(self._current_response)
        self._dropped_responses |= responses
        self._remainders.clear()
        kept = [item for item in self._out if not isinstance(item, _Frame)]
        self._out.clear()
        self._out.extend(kept)
        if self._send_task is not None and not self._send_task.done():
            self._send_task.cancel()
        if clear:
            self._playing = False
            self._out.appendleft(_Clear())
            self._out_ready.set()

    async def _speaker(self) -> None:
        while True:
            while not self._out:
                self._out_ready.clear()
                await self._out_ready.wait()
            item = self._out.popleft()
            if isinstance(item, _Frame):
                await self._send_frame(item)
            elif isinstance(item, _Mark):
                self._last_mark = item.name
                self._frames_after_mark = 0
                await self._transport.send_mark(item.name)
            elif isinstance(item, _Clear):
                self._playing = False
                await self._transport.clear()
            else:
                await self._realtime(self._live_stream().truncate(item.item_id, item.audio_end_ms))

    async def _send_frame(self, frame: _Frame) -> None:
        self._playing = True
        self._frames_after_mark += 1
        self._current_item, self._current_response = frame.item_id, frame.response_id
        self._item_first_sent.setdefault(frame.item_id, self._clock())
        self._item_bytes[frame.item_id] = self._item_bytes.get(frame.item_id, 0) + len(frame.audio)
        self._send_task = asyncio.create_task(self._transport.send_audio(frame.audio))
        try:
            await self._send_task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():  # type: ignore[union-attr]
                raise
            # only this frame's send was cancelled (barge-in): go on with the queue
        finally:
            self._send_task = None


def _error_type(exc: BaseException) -> str:
    if isinstance(exc, RealtimeAuthError):
        return "auth_failed"
    if isinstance(exc, RealtimeError):
        return exc.error_type
    return "stream_error"
