"""RealtimeVoiceSession, the speech-to-speech per-call engine (Phase 7b, Task 9): FakeTransport, the fake Realtime
stream registered under the real provider id (`openai`) and the scripted chat model behind the risk classifier.
Timing constants are patched to tens of milliseconds (on the chained session's module, which owns them); tests wait
on recorded events, never on wall-clock guesses."""

import asyncio
import json
import uuid
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from bot import conftest as bot_fixtures
from prometheus_client import REGISTRY
from sqlalchemy import select, update
from supportcore.bot.prompts import build_realtime_instructions
from supportcore.db.models import BotTurn, Call, CallTransfer, Conversation, IntegrationEvent, Message
from supportcore.db.uow import UnitOfWork
from supportcore.inbox.publisher import InboxPublisher
from supportcore.registry import ProviderRegistry
from supportcore.testing import script_chat
from supportcore.voice import session as voice_session
from supportcore.voice.realtime.fake_realtime import FakeSpeechToSpeech
from supportcore.voice.realtime.ports import (
    AssistantTranscript,
    AudioOut,
    RealtimeAuthError,
    RealtimeConfig,
    RealtimeFactory,
    ResponseDone,
    ResponseStarted,
    ToolCall,
)
from supportcore.voice.realtime.tools import RealtimeToolbox
from supportcore.voice.realtime_session import RealtimeVoiceSession
from supportcore.voice.settings_service import VoiceSetup
from supportcore.voice.strings import strings_for
from supportcore.voice.telephony.ports import TelephonyAccountRef
from supportcore.voice.transport import AudioIn, FakeTransport, MarkPlayed, Stop

# The bot tests' fixtures (database with a pooled engine, scripted chat model, in-memory vector store, runtime).
bot_settings = bot_fixtures.bot_settings
bot_engine = bot_fixtures.bot_engine
bot_factory = bot_fixtures.bot_factory
store = bot_fixtures.store
executor = bot_fixtures.executor
bot_adapters = bot_fixtures.bot_adapters
bus = bot_fixtures.bus
runtime = bot_fixtures.runtime
make_world = bot_fixtures.make_world
_reset_script = bot_fixtures._reset_script

CALLER = "+14155550123"
EN = strings_for("en")
GREETING = EN["voice_greeting"].format(organisation="Acme") + " " + EN["voice_recording_notice"]
SILENCE = b"\x7f" * 160  # an ordinary audio frame: 20 ms for the fake stream
ALL_DAY = {day: [["00:00", "24:00"]] for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


# --- test doubles -----------------------------------------------------------------------------------


class WatchedStream:
    """Wraps the fake stream: `checks(where)` at every await on it, `seen` records the events handed out, and
    `gate` (when set) holds back the next event the predicate matches until the gate opens."""

    def __init__(self, inner, checks) -> None:
        self.inner = inner
        self._checks = checks
        self.seen: list = []
        self.gate: asyncio.Event | None = None
        self.gate_on = lambda event: False
        self.tool_gate: asyncio.Event | None = None  # holds send_tool_result until set

    async def send_audio(self, frame):
        self._checks("realtime")
        await self.inner.send_audio(frame)

    async def send_tool_result(self, call_id, output, *, respond=True):
        self._checks("realtime")
        if self.tool_gate is not None:
            await self.tool_gate.wait()
        await self.inner.send_tool_result(call_id, output, respond=respond)

    async def say(self, text, say_id):
        self._checks("realtime")
        await self.inner.say(text, say_id)

    async def cancel_response(self):
        self._checks("realtime")
        await self.inner.cancel_response()

    async def truncate(self, item_id, audio_end_ms):
        self._checks("realtime")
        await self.inner.truncate(item_id, audio_end_ms)

    async def events(self):
        async for event in self.inner.events():
            self._checks("realtime")
            if self.gate is not None and self.gate_on(event):
                await self.gate.wait()
            self.seen.append(event)
            yield event

    async def aclose(self):
        await self.inner.aclose()

    @property
    def audio_seconds(self):
        return self.inner.audio_seconds


class WatchedRealtime:
    def __init__(self, checks) -> None:
        self.fake = FakeSpeechToSpeech()
        self.checks = checks
        self.streams: list[WatchedStream] = []
        self.opened: list[RealtimeConfig] = []
        self.fail_open = False

    async def open(self, config):
        self.opened.append(config)
        if self.fail_open:
            raise RealtimeAuthError("rejected")
        stream = WatchedStream(await self.fake.open(config), self.checks)
        self.streams.append(stream)
        return stream


class Checks:
    def __init__(self) -> None:
        self.factory = None
        self.seen: dict[str, list[bool]] = {}

    def __call__(self, where: str) -> None:
        held = self.factory.held() if self.factory is not None else False
        self.seen.setdefault(where, []).append(held)


class TrackingFactory:
    """A session factory that remembers its sessions and the task that opened each; `held()` is whether the
    current task has one inside a transaction."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.sessions: list = []

    def __call__(self):
        session = self._inner()
        self.sessions.append((asyncio.current_task(), session))
        return session

    def held(self) -> bool:
        task = asyncio.current_task()
        return any(owner is task and s.in_transaction() for owner, s in self.sessions)


def marks(transport: FakeTransport) -> list[str]:
    return [entry[1] for entry in transport.sent if entry[0] == "mark"]


def has_mark(name: str):
    return lambda t: name in marks(t)


def audio_frames(transport: FakeTransport) -> list[bytes]:
    return [entry[1] for entry in transport.sent if entry[0] == "audio"]


def sample(name: str, labels: dict | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


# --- fixtures ---------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _timing(monkeypatch):
    monkeypatch.setattr(voice_session, "SILENCE_PROMPT_SECONDS", 30)
    monkeypatch.setattr(voice_session, "SILENCE_HANGUP_SECONDS", 30)
    monkeypatch.setattr(voice_session, "GOODBYE_MARK_TIMEOUT_SECONDS", 2)


class Env:
    def __init__(self, world, call_id, factory, adapters, runtime, bus, settings, checks, realtime) -> None:
        self.world = world
        self.call_id = call_id
        self.factory = factory
        self.adapters = adapters
        self.runtime = runtime
        self.bus = bus
        self.settings = settings
        self.checks = checks
        self.realtime = realtime
        self.transport = FakeTransport()
        self.setup = VoiceSetup(
            stt=None,
            tts=None,
            record_calls=True,
            max_call_seconds=1800,
            organisation_name="Acme",
            language="en",
            telephony=TelephonyAccountRef("AC" + "a" * 32, "t" * 32),
            mode="speech_to_speech",
            realtime=RealtimeConfig("openai", "gpt-realtime", "marin", "en", instructions="", api_key="sk-test"),
        )
        self.clock = None
        self.task: asyncio.Task | None = None

    def start(self, source: str = "phone", session_factory=None, publisher=None) -> asyncio.Task:
        kwargs = {"publisher": publisher}
        if self.clock is not None:
            kwargs["clock"] = self.clock
        self.voice = RealtimeVoiceSession(
            self.call_id,
            self.world.org_id,
            self.world.conversation_id,
            source,
            self.world.widget_id,
            self.setup,
            self.transport,
            self.adapters,
            self.runtime,
            session_factory or self.factory,
            self.bus,
            self.settings,
            **kwargs,
        )
        self.task = asyncio.create_task(self.voice.run())
        return self.task

    @property
    def stream(self) -> WatchedStream:
        return self.realtime.streams[0]

    async def done(self, timeout: float = 10.0) -> None:
        await asyncio.wait_for(asyncio.shield(self.task), timeout)

    async def hang_up(self) -> None:
        self.transport.push(Stop())
        await self.done()

    def say(self, text: str) -> None:
        self.transport.push(AudioIn(b"FAKE-RT:" + text.encode()))

    async def call(self) -> Call:
        async with self.factory() as s:
            return await s.get(Call, self.call_id)

    async def conversation(self) -> Conversation:
        async with self.factory() as s:
            return await s.get(Conversation, self.world.conversation_id)

    async def messages(self) -> list[Message]:
        async with self.factory() as s:
            stmt = select(Message).where(Message.conversation_id == self.world.conversation_id).order_by(
                Message.created_at
            )
            return list((await s.execute(stmt)).scalars())

    async def turns(self) -> list[BotTurn]:
        async with self.factory() as s:
            stmt = select(BotTurn).where(BotTurn.conversation_id == self.world.conversation_id)
            return list((await s.execute(stmt)).scalars())

    async def transfer(self) -> CallTransfer | None:
        async with self.factory() as s:
            stmt = select(CallTransfer).where(CallTransfer.call_id == self.call_id)
            return (await s.execute(stmt)).scalar_one_or_none()

    async def integration_rows(self) -> list[tuple[str, str, str, int]]:
        async with self.factory() as s:
            stmt = select(IntegrationEvent).where(IntegrationEvent.organisation_id == self.world.org_id)
            rows = await s.execute(stmt)
            return [(r.kind, r.provider, r.error_type, r.count) for r in rows.scalars()]


@pytest.fixture
async def env(make_world, bot_factory, bot_adapters, bot_settings, runtime, bus):
    world = await make_world()
    call_id = uuid.uuid4()
    async with UnitOfWork(bot_factory) as uow:
        await uow.session.execute(
            update(Conversation).where(Conversation.id == world.conversation_id).values(channel="voice")
        )
        uow.session.add(
            Call(
                id=call_id,
                organisation_id=world.org_id,
                conversation_id=world.conversation_id,
                source="phone",
                provider_call_sid="CA" + uuid.uuid4().hex,
                caller_e164=CALLER,
                status="in_progress",
                started_at=datetime.now(UTC),
            )
        )
        await uow.commit()
    checks = Checks()
    realtime = WatchedRealtime(checks)
    registry = ProviderRegistry("speech_to_speech")
    registry.register("openai")(lambda: realtime)
    adapters = replace(bot_adapters, speech_to_speech=RealtimeFactory(registry))
    e = Env(world, call_id, bot_factory, adapters, runtime, bus, bot_settings, checks, realtime)
    yield e
    if e.task is not None and not e.task.done():
        e.task.cancel()
        with suppress(BaseException):
            await e.task


async def _memory_presence(env) -> None:
    from supportcore.presence.memory_presence import MemoryAgentPresence

    env.adapters = replace(
        env.adapters, presence=MemoryAgentPresence(), softphones=MemoryAgentPresence(namespace="softphone")
    )


async def _agent_available(env) -> None:
    """The owner is online, present, within business hours and has a softphone registered: a transfer target."""
    from supportcore.db.models import Membership
    from supportcore.events import EventBus
    from supportcore.support.settings_service import SupportSettingsService

    await _memory_presence(env)
    org_id = env.world.org_id
    async with UnitOfWork(env.factory) as uow:
        s = uow.session
        membership = (
            await s.execute(select(Membership).where(Membership.user_id == env.world.owner_id))
        ).scalar_one()
        membership.agent_status = "online"
        (await SupportSettingsService(s, org_id, EventBus()).get()).weekly_hours = ALL_DAY
        await uow.commit()
    await env.adapters.presence.beat(org_id, membership.id)
    await env.adapters.softphones.beat(org_id, membership.id)


async def _greeted(env) -> None:
    env.start()
    await env.transport.wait_until(has_mark("turn-0"))


# --- tests ------------------------------------------------------------------------------------------


async def test_greeting_said_first(env):
    await _greeted(env)

    config = env.realtime.opened[0]
    assert config.instructions == build_realtime_instructions("Acme", "en")
    assert {t.name for t in config.tools} == {"search_knowledge_base", "transfer_to_human"}
    assert (config.model, config.voice, config.api_key) == ("gpt-realtime", "marin", "sk-test")
    assert env.stream.inner.said == [GREETING]
    first_mark = env.transport.sent.index(("mark", "turn-0"))
    assert first_mark > 0 and all(e[0] == "audio" for e in env.transport.sent[:first_mark])
    assert env.transport.sent[0][1].startswith(b"FAKE-RT-SAY:")
    assert all(len(frame) == 160 for frame in audio_frames(env.transport))  # the remainder was padded
    await env.hang_up()
    assert env.transport.closed
    assert (await env.call()).end_reason == "caller_hangup"
    assert await env.messages() == []  # the greeting is a fixed line, not a stored reply


async def test_caller_and_bot_turns_stored_with_usage(env):
    latency_before = sample("support_voice_response_latency_seconds_count")
    await _greeted(env)
    for _ in range(3):
        env.transport.push(AudioIn(SILENCE))
    env.say("What are your opening hours?")
    await env.transport.wait_until(has_mark("turn-1"))
    reply = b"".join(audio_frames(env.transport)).split(b"FAKE-RT-REPLY:")[1].rstrip(b"\xff")
    assert reply == b"What are your opening hours?"
    await env.hang_up()

    messages = await env.messages()
    assert [(m.role, m.text) for m in messages] == [
        ("customer", "What are your opening hours?"),
        ("bot", "Reply: What are your opening hours?"),
    ]
    (turn,) = await env.turns()
    assert turn.reply_message_id == messages[1].id and turn.message_id == messages[0].id
    assert (turn.model["provider"], turn.model["model"]) == ("openai", "gpt-realtime")
    assert (turn.model["input_tokens"], turn.model["output_tokens"]) == (10, 20)
    assert turn.latency_ms is not None and turn.latency_ms >= 0
    assert sample("support_voice_response_latency_seconds_count") == latency_before + 1
    call = await env.call()
    assert call.stt_seconds == pytest.approx(0.06)
    assert call.tts_characters == 0


async def test_knowledge_tool_round_trip(env):
    await _greeted(env)
    env.say('[tool:search_knowledge_base {"query":"hours"}]')
    await env.transport.wait_until(has_mark("turn-2"))  # turn-1 ends the response that called the tool

    ((call_id, output, respond),) = env.stream.inner.tool_results
    assert call_id.startswith("call") and respond is True
    assert output["results"] and {r["document"] for r in output["results"]} == {"faq.md"}
    await env.hang_up()
    bot = [m.text for m in await env.messages() if m.role == "bot"]
    assert len(bot) == 1 and bot[0].startswith("Tool said: ")
    (turn,) = await env.turns()
    assert turn.retrieval["kb_ids"] == [str(env.world.kb_id)]
    assert len(turn.retrieval["chunks"]) == len(output["results"])
    # the tool-calling response's usage (5 + 5) is carried onto the reply's (10 + 20)
    assert (turn.model["input_tokens"], turn.model["output_tokens"]) == (15, 25)


async def test_unknown_tool_gets_an_error(env):
    await _greeted(env)
    env.say("[tool:open_the_pod_bay_doors {}]")
    await env.transport.wait_until(has_mark("turn-2"))

    assert [(o, r) for _c, o, r in env.stream.inner.tool_results] == [({"error": "unknown_tool"}, True)]
    await env.hang_up()


async def test_transfer_tool_routes_and_transfers_when_agent_available(env):
    await _agent_available(env)
    at_close = []  # the committed transfer row, as seen from a fresh session when the transport is first closed
    real_close = env.transport.close

    async def close() -> None:
        if not env.transport.closed:
            at_close.append(await env.transfer())
        await real_close()

    env.transport.close = close
    await _greeted(env)
    env.transport.auto_marks = False  # the goodbye mark plays only when the test says so
    env.say("[tool:transfer_to_human {}]")
    await env.transport.wait_until(has_mark("goodbye"))
    await asyncio.sleep(0.2)
    assert not env.transport.closed  # waiting for the closing words to play
    env.transport.push(MarkPlayed("goodbye"))
    await env.done()

    assert [(o, r) for _c, o, r in env.stream.inner.tool_results] == [({"ok": True}, False)]
    assert env.stream.inner.said[-1] == EN["voice_transferring"]
    assert env.transport.sent.index(("mark", "goodbye")) < env.transport.sent.index(("close",))
    assert len(at_close) == 1 and at_close[0] is not None and at_close[0].status == "ringing"
    call, conversation = await env.call(), await env.conversation()
    assert call.end_reason is None
    assert conversation.status == "open" and conversation.callback_status is None
    escalation = conversation.escalation
    assert (escalation["rule_key"], escalation["source"]) == ("voice_requested_human", "voice")


async def test_transfer_tool_without_agent_speaks_callback(env):
    await _memory_presence(env)  # nobody present: nobody can take the call
    await _greeted(env)
    env.say("[tool:transfer_to_human {\"reason\": \"wants a person\"}]")
    await env.done()

    assert env.stream.inner.said[-1] == EN["voice_callback_phone"]
    assert marks(env.transport)[-1] == "goodbye"
    assert env.transport.closed
    assert await env.transfer() is None
    call, conversation = await env.call(), await env.conversation()
    assert call.end_reason == "handoff_callback"
    assert (conversation.status, conversation.callback_status, conversation.callback_phone) == (
        "open",
        "requested",
        CALLER,
    )
    assert conversation.escalation["rule_key"] == "voice_requested_human"


async def test_risk_escalation_cancels_response_and_hands_off(env):
    await _memory_presence(env)
    await _greeted(env)
    env.say("I want a refund for my order")
    await env.done()

    assert env.stream.inner.cancelled == 1
    assert env.stream.inner.said[-1] == EN["voice_callback_phone"]
    assert env.transport.sent.index(("clear",)) < _closing_line_at(env)
    call, conversation = await env.call(), await env.conversation()
    assert call.end_reason == "handoff_callback"
    assert (conversation.status, conversation.callback_status) == ("open", "requested")
    assert conversation.escalation["action"] == "escalate"
    assert conversation.escalation["rule_key"] not in (None, "voice_callback")  # the risk rule routed it
    customer = [m.text for m in await env.messages() if m.role == "customer"]
    assert customer == ["I want a refund for my order"]


async def test_offer_human_becomes_escalate_on_voice(env):
    await _memory_presence(env)
    frustrated = json.dumps({"topic_ids": [], "asks_for_human": False, "frustrated": True, "reason": "angry"})
    script_chat([], classifier=[frustrated])
    await _greeted(env)
    env.say("this is useless")
    await env.done()

    assert env.stream.inner.cancelled == 1
    assert env.stream.inner.said[-1] == EN["voice_callback_phone"]
    conversation = await env.conversation()
    assert (conversation.escalation["action"], conversation.escalation["rule_key"]) == ("escalate", "frustration")
    assert (await env.call()).end_reason == "handoff_callback"


async def test_reply_during_own_handoff_is_stored(env):
    """The reply of the response in progress when the session routes the conversation is stored (routed), not
    dropped as superseded."""
    await _memory_presence(env)
    await _greeted(env)
    stream = env.stream
    stream.tool_gate = asyncio.Event()  # the handoff waits until the reply below is under way
    env.say("[tool:transfer_to_human {}]")
    await eventually(lambda: _seen(stream, ToolCall))
    stream.inner._reply("One moment please.", b"FAKE-RT-REPLY:", "One moment please.", out_of_band=False)
    reply_id = f"resp{stream.inner._counter - 1}"
    stream.gate = asyncio.Event()
    stream.gate_on = lambda e: isinstance(e, ResponseDone) and e.response_id == reply_id
    await eventually(lambda: _seen(stream, AssistantTranscript))
    stream.tool_gate.set()
    await eventually(lambda: _status_is(env, "open"))
    stream.gate.set()
    await env.done()

    bot = [m.text for m in await env.messages() if m.role == "bot"]
    assert bot == ["One moment please."]
    assert (await env.call()).end_reason == "handoff_callback"
    assert env.stream.inner.cancelled == 1  # the reply in progress was told to stop for the closing line


async def test_search_result_after_escalation_does_not_speak(env, monkeypatch):
    """The model asked for a search, and the risk check escalates before it returns: the result goes back without
    asking for a response, no model audio follows, nothing more is stored, and the closing line follows the clear."""
    await _memory_presence(env)
    released = asyncio.Event()
    search = RealtimeToolbox.search

    async def held_search(self, query):
        await released.wait()
        return await search(self, query)

    monkeypatch.setattr(RealtimeToolbox, "search", held_search)
    await _greeted(env)
    env.transport.auto_marks = False  # the closing line "plays" only once the search has returned
    env.say('I want a refund for my order [tool:search_knowledge_base {"query":"refund"}]')
    await eventually(lambda: _status_is(env, "open"))  # the risk check escalated and routed it
    released.set()
    await eventually(lambda: _tool_results(env))
    await env.transport.wait_until(has_mark("goodbye"))
    env.transport.push(MarkPlayed("goodbye"))
    await env.done()

    ((_call_id, output, respond),) = env.stream.inner.tool_results
    assert respond is False and "results" in output
    assert not any(f.startswith(b"FAKE-RT-REPLY:") for f in audio_frames(env.transport))
    assert [m.role for m in await env.messages()] == ["customer"]
    assert env.stream.inner.cancelled == 1
    assert env.transport.sent.index(("clear",)) < _closing_line_at(env)
    assert (await env.call()).end_reason == "handoff_callback"


async def test_hangup_during_handoff_leaves_callback(env, monkeypatch):
    from supportcore.voice.transfers import TransferService

    await _memory_presence(env)
    checking = asyncio.Event()

    async def stuck_can_transfer(self, call_id):
        checking.set()
        await asyncio.Event().wait()  # the caller hangs up first

    monkeypatch.setattr(TransferService, "can_transfer", stuck_can_transfer)
    await _greeted(env)
    env.say("[tool:transfer_to_human {}]")
    await asyncio.wait_for(checking.wait(), 5)
    env.say("hello? are you there")  # handing off: not forwarded, so the model has nothing new to answer
    await asyncio.sleep(0.05)
    await env.hang_up()

    assert [m.text for m in await env.messages()] == ["[tool:transfer_to_human {}]"]
    call, conversation = await env.call(), await env.conversation()
    assert call.end_reason == "caller_hangup"
    assert (conversation.status, conversation.callback_status, conversation.callback_phone) == (
        "open",
        "requested",
        CALLER,
    )


async def test_refused_fixed_lines_do_not_wedge_the_call(env, monkeypatch):
    monkeypatch.setattr(voice_session, "SILENCE_PROMPT_SECONDS", 0.05)
    monkeypatch.setattr(voice_session, "SILENCE_HANGUP_SECONDS", 0.05)
    await _greeted(env)
    env.stream.inner.reject_says = True  # "are you still there?" and the goodbye are refused
    await env.done(timeout=3)

    assert env.stream.inner.said == [GREETING, EN["voice_still_there"], EN["voice_goodbye"]]
    assert marks(env.transport) == ["turn-0"]
    assert (await env.call()).end_reason == "silence"


async def test_remainder_carried_across_deltas_and_padded_per_response(env):
    await _greeted(env)
    queue = env.stream.inner._queue
    greeting_frames = len(audio_frames(env.transport))
    for event in (
        ResponseStarted("rx", False),
        AudioOut("rx", "ix", b"A" * 100),
        AudioOut("rx", "ix", b"B" * 100),
        ResponseStarted("ry", False),
        AudioOut("ry", "iy", b"C" * 50),
        ResponseDone("rx", "completed", 1, 1, False),  # pads rx only: ry's partial frame waits for more audio
        AudioOut("ry", "iy", b"D" * 110),
        ResponseDone("ry", "completed", 1, 1, False),
    ):
        queue.put_nowait(event)
    await env.transport.wait_until(has_mark("turn-2"))

    assert audio_frames(env.transport)[greeting_frames:] == [
        b"A" * 100 + b"B" * 60,
        b"B" * 40 + b"\xff" * 120,
        b"C" * 50 + b"D" * 110,
    ]
    assert marks(env.transport) == ["turn-0", "turn-1", "turn-2"]
    await env.hang_up()


async def test_barge_in_clears_and_truncates(env):
    now = [100.0]
    env.clock = lambda: now[0]
    env.transport.auto_marks = False
    await _greeted(env)
    env.transport.push(MarkPlayed("turn-0"))
    env.transport.hold_audio()
    env.say("tell me a story")
    await env.transport.wait_until(lambda t: any(f.startswith(b"FAKE-RT-REPLY:") for f in audio_frames(t)))
    reply_item = next(
        e.item_id for e in env.stream.seen if isinstance(e, AudioOut) and e.audio.startswith(b"FAKE-RT-REPLY")
    )
    now[0] += 1.0  # a second since the reply's first frame: more than the 20 ms of audio sent
    env.say("wait stop")
    await env.transport.wait_until(lambda t: ("clear",) in t.sent)
    env.transport.release_audio()
    await env.transport.wait_until(has_mark("turn-2"))

    assert env.stream.inner.truncations == [(reply_item, 20)]
    clear_at = env.transport.sent.index(("clear",))
    after = [e[1] for e in env.transport.sent[clear_at:] if e[0] == "audio"]
    assert after and all(not f.startswith(b"FAKE-RT-REPLY:tell") for f in after)
    assert b"".join(after).startswith(b"FAKE-RT-REPLY:wait stop")
    await env.hang_up()
    customer = [m.text for m in await env.messages() if m.role == "customer"]
    assert customer == ["tell me a story", "wait stop"]


async def test_silence_prompts_then_hangs_up(env, monkeypatch):
    monkeypatch.setattr(voice_session, "SILENCE_PROMPT_SECONDS", 0.05)
    monkeypatch.setattr(voice_session, "SILENCE_HANGUP_SECONDS", 0.05)
    env.start()
    await env.done()

    assert env.stream.inner.said == [GREETING, EN["voice_still_there"], EN["voice_goodbye"]]
    assert marks(env.transport) == ["turn-0", "turn-1", "goodbye"]
    assert env.transport.closed
    call, conversation = await env.call(), await env.conversation()
    assert call.end_reason == "silence"
    assert conversation.callback_status is None


async def test_max_duration_requests_callback(env):
    env.setup = replace(env.setup, max_call_seconds=0.2)
    env.start()
    await env.done()

    assert env.stream.inner.said[-1] == EN["voice_max_duration"]
    assert marks(env.transport)[-1] == "goodbye"
    call, conversation = await env.call(), await env.conversation()
    assert call.end_reason == "max_duration"
    assert (conversation.callback_status, conversation.callback_phone) == ("requested", CALLER)


async def test_realtime_auth_failure_ends_with_error_and_callback(env):
    before = sample("support_voice_provider_errors_total", {"provider": "openai_realtime", "error_type": "auth_failed"})
    await _greeted(env)
    env.transport.push(AudioIn(b"FAKE-RT-AUTHFAIL"))
    await env.done()

    assert await env.integration_rows() == [("voice", "openai_realtime", "auth_failed", 1)]
    after = sample("support_voice_provider_errors_total", {"provider": "openai_realtime", "error_type": "auth_failed"})
    assert after == before + 1
    assert env.stream.inner.said == [GREETING]  # the <Connect action> says the trouble message
    assert env.transport.closed
    call, conversation = await env.call(), await env.conversation()
    assert call.end_reason == "error"
    assert (conversation.status, conversation.callback_status) == ("open", "requested")


async def test_realtime_auth_failure_at_connect(env):
    env.realtime.fail_open = True
    env.start()
    await env.done()

    assert await env.integration_rows() == [("voice", "openai_realtime", "auth_failed", 1)]
    assert env.transport.sent == [("close",)]
    assert env.voice.line_closed.is_set()
    assert (await env.call()).end_reason == "error"


async def test_realtime_socket_drop_ends_with_error(env):
    await _greeted(env)
    env.transport.push(AudioIn(b"FAKE-RT-DROP"))
    await env.done()

    assert await env.integration_rows() == [("voice", "openai_realtime", "stream_error", 1)]
    assert env.transport.closed
    call, conversation = await env.call(), await env.conversation()
    assert call.end_reason == "error"
    assert conversation.callback_status == "requested"


async def test_hangup_mid_response_stores_partial_turn_once(env):
    await _greeted(env)
    stream = env.stream
    stream.gate = asyncio.Event()
    stream.gate_on = lambda e: isinstance(e, ResponseDone) and not e.out_of_band
    env.say("where is my parcel")
    await eventually(lambda: _seen(stream, AssistantTranscript))
    env.transport.push(Stop())  # the caller hangs up before the response is done
    await env.done()
    stream.gate.set()  # too late: the session is over
    await asyncio.sleep(0.05)

    messages = await env.messages()
    assert [(m.role, m.text) for m in messages] == [
        ("customer", "where is my parcel"),
        ("bot", "Reply: where is my parcel"),
    ]
    (turn,) = await env.turns()
    assert turn.reply_message_id == messages[1].id
    assert turn.model["input_tokens"] is None  # the response never finished: its usage is unknown
    assert (await env.call()).end_reason == "caller_hangup"


async def test_no_session_open_while_awaiting_realtime(env, monkeypatch):
    tracking = TrackingFactory(env.factory)
    env.checks.factory = tracking
    env.transport.on_send = lambda entry: env.checks("transport")
    search, assess = RealtimeToolbox.search, RealtimeToolbox.assess

    async def checked_search(self, query):
        env.checks("toolbox")
        return await search(self, query)

    async def checked_assess(self, text, recent):
        env.checks("toolbox")
        return await assess(self, text, recent)

    monkeypatch.setattr(RealtimeToolbox, "search", checked_search)
    monkeypatch.setattr(RealtimeToolbox, "assess", checked_assess)
    async with tracking() as probe:  # the checks can fail: an open session of this task is seen
        await probe.execute(select(1))
        assert tracking.held()
    tracking.sessions.clear()
    env.start(session_factory=tracking, publisher=InboxPublisher(env.adapters.pubsub, env.factory))
    await env.transport.wait_until(has_mark("turn-0"))
    env.transport.push(AudioIn(SILENCE))
    env.say("hello")
    await env.transport.wait_until(has_mark("turn-1"))
    env.say('[tool:search_knowledge_base {"query":"shipping"}]')
    await env.transport.wait_until(has_mark("turn-3"))
    await env.hang_up()

    assert tracking.sessions  # the session did use the database...
    assert set(env.checks.seen) == {"realtime", "transport", "toolbox"}
    assert {where: any(states) for where, states in env.checks.seen.items()} == {
        "realtime": False,
        "transport": False,
        "toolbox": False,
    }


# --- helpers ----------------------------------------------------------------------------------------


async def eventually(check, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not await check():
            await asyncio.sleep(0.01)


async def _status_is(env, status: str) -> bool:
    return (await env.conversation()).status == status


def _closing_line_at(env) -> int:
    """Where the closing line's first frame was sent."""
    return next(i for i, e in enumerate(env.transport.sent) if e[0] == "audio" and e[1].startswith(b"FAKE-RT-SAY:I'm"))


async def _tool_results(env) -> bool:
    return bool(env.stream.inner.tool_results)


async def _seen(stream, kind) -> bool:
    return any(isinstance(e, kind) for e in stream.seen)
