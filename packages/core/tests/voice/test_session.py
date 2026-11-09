"""VoiceSession, the per-call engine (Phase 7a, Task 8): FakeTransport, fake STT/TTS registered under the real
provider ids (`deepgram`, `openai`) and the scripted chat model behind a real TurnRunner. Timing constants are
patched to tens of milliseconds; tests wait on recorded events, never on wall-clock guesses."""

import asyncio
import uuid
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from bot import conftest as bot_fixtures
from prometheus_client import REGISTRY
from sqlalchemy import select, update
from supportcore.bot.turns import TurnRunner
from supportcore.db.models import BotTurn, Call, Conversation, IntegrationEvent, Message
from supportcore.db.uow import UnitOfWork
from supportcore.inbox.publisher import InboxPublisher
from supportcore.registry import ProviderRegistry
from supportcore.testing import script_chat
from supportcore.voice import session as voice_session
from supportcore.voice.session import VoiceSession
from supportcore.voice.settings_service import VoiceSetup
from supportcore.voice.strings import strings_for
from supportcore.voice.stt.fake_stt import FakeSpeechToText
from supportcore.voice.stt.ports import SpeechStarted, SttConfig, SttFactory, Transcript
from supportcore.voice.telephony.ports import TelephonyAccountRef
from supportcore.voice.transport import AudioIn, FakeTransport, MarkPlayed, Stop
from supportcore.voice.tts.fake_tts import FakeTextToSpeech
from supportcore.voice.tts.ports import TtsAuthError, TtsConfig, TtsFactory

# The bot tests' fixtures (database with a pooled engine, scripted chat model, memory checkpointer, runtime).
bot_settings = bot_fixtures.bot_settings
bot_engine = bot_fixtures.bot_engine
bot_factory = bot_fixtures.bot_factory
store = bot_fixtures.store
executor = bot_fixtures.executor
bot_adapters = bot_fixtures.bot_adapters
bus = bot_fixtures.bus
runtime = bot_fixtures.runtime
graph = bot_fixtures.graph
make_world = bot_fixtures.make_world
_reset_script = bot_fixtures._reset_script

CALLER = "+14155550123"
EN = strings_for("en")
GREETING = EN["voice_greeting"].format(organisation="Acme")
NOTICE = EN["voice_recording_notice"]
SILENCE = b"\x7f" * 160  # an ordinary audio frame: 20 ms for the fake STT
REAL_TURN_FINISH_TIMEOUT = voice_session.TURN_FINISH_TIMEOUT_SECONDS  # before the timing fixture patches it


# --- test doubles -----------------------------------------------------------------------------------


class WatchedSttStream:
    """Wraps the fake stream; `handled` counts events the session has finished processing."""

    def __init__(self, inner, checks) -> None:
        self._inner = inner
        self._checks = checks
        self.handled: list = []

    async def send(self, frame: bytes) -> None:
        self._checks("stt")
        await self._inner.send(frame)

    async def events(self):
        async for event in self._inner.events():
            self._checks("stt")
            yield event
            self.handled.append(event)  # runs once the consumer asks for the next event

    async def aclose(self) -> None:
        await self._inner.aclose()

    def inject(self, event) -> None:
        """Delivers an STT event directly (e.g. an interim transcript the fake cannot produce from audio)."""
        self._inner._queue.put_nowait(event)

    @property
    def audio_seconds(self) -> float:
        return self._inner.audio_seconds


class WatchedStt:
    def __init__(self, checks) -> None:
        self.fake = FakeSpeechToText()
        self.checks = checks
        self.streams: list[WatchedSttStream] = []

    async def open(self, config):
        stream = WatchedSttStream(await self.fake.open(config), self.checks)
        self.streams.append(stream)
        return stream


class WatchedTts:
    """The fake TTS; records which synthesize iterators were closed before they finished."""

    def __init__(self, checks) -> None:
        self.fake = FakeTextToSpeech()
        self.checks = checks
        self.interrupted: list[str] = []
        self.fail_on: str | None = None  # this exact text raises TtsAuthError

    async def synthesize(self, text, config):
        finished = False
        try:
            if text == self.fail_on:
                self.fake.requests.append((text, config))
                raise TtsAuthError("fake tts auth failure")
            async for frame in self.fake.synthesize(text, config):
                self.checks("tts")
                yield frame
            finished = True
        finally:
            if not finished:
                self.interrupted.append(text)

    @property
    def texts(self) -> list[str]:
        return [text for text, _config in self.fake.requests]


class Checks:
    """Records, at each provider await, whether a database session from the voice session's factory is open.
    `answer_now` and `tts` run in fresh inner tasks, so there any open session of the call counts (the test
    serialises the call's database work around them); elsewhere, sessions of the awaiting task."""

    ACROSS_TASKS = frozenset({"answer_now", "tts"})

    def __init__(self) -> None:
        self.factory = None
        self.seen: dict[str, list[bool]] = {}

    def __call__(self, where: str) -> None:
        held = self.factory.held(any_task=where in self.ACROSS_TASKS) if self.factory is not None else False
        self.seen.setdefault(where, []).append(held)


class TrackingFactory:
    """A session factory that remembers its sessions and the task that opened each; `held()` is whether the
    current task (or with `any_task`, any task) has one inside a transaction."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.sessions: list = []

    def __call__(self):
        session = self._inner()
        self.sessions.append((asyncio.current_task(), session))
        return session

    def held(self, *, any_task: bool = False) -> bool:
        task = asyncio.current_task()
        return any((any_task or owner is task) and s.in_transaction() for owner, s in self.sessions)


def spoken(transport: FakeTransport) -> list[str]:
    """The texts the fake TTS turned into the frames sent, in order."""
    audio = b"".join(entry[1] for entry in transport.sent if entry[0] == "audio")
    return [part.rstrip(b"\xff").decode() for part in audio.split(b"FAKE-TTS:") if part]


def marks(transport: FakeTransport) -> list[str]:
    return [entry[1] for entry in transport.sent if entry[0] == "mark"]


def has_mark(name: str):
    return lambda t: name in marks(t)


def sample(name: str, labels: dict | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


# --- fixtures ---------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _timing(monkeypatch):
    monkeypatch.setattr(voice_session, "SILENCE_PROMPT_SECONDS", 30)
    monkeypatch.setattr(voice_session, "SILENCE_HANGUP_SECONDS", 30)
    monkeypatch.setattr(voice_session, "GOODBYE_MARK_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(voice_session, "TURN_FINISH_TIMEOUT_SECONDS", 10)


class Env:
    def __init__(self, world, call_id, factory, adapters, runner, bus, settings, checks, stt, tts) -> None:
        self.world = world
        self.call_id = call_id
        self.factory = factory
        self.adapters = adapters
        self.runner = runner
        self.bus = bus
        self.settings = settings
        self.checks = checks
        self.stt = stt
        self.tts = tts
        self.transport = FakeTransport()
        self.setup = VoiceSetup(
            stt=SttConfig("deepgram", "nova-3", "en"),
            tts=TtsConfig("openai", "gpt-4o-mini-tts", "coral", "en"),
            record_calls=True,
            max_call_seconds=1800,
            organisation_name="Acme",
            language="en",
            telephony=TelephonyAccountRef("AC" + "a" * 32, "t" * 32),
        )
        self.task: asyncio.Task | None = None

    def session(self, source: str = "phone", session_factory=None, publisher=None) -> VoiceSession:
        return VoiceSession(
            self.call_id,
            self.world.org_id,
            self.world.conversation_id,
            source,
            self.setup,
            self.transport,
            self.adapters,
            self.runner,
            session_factory or self.factory,
            self.bus,
            self.settings,
            publisher=publisher,
        )

    def start(self, source: str = "phone", session_factory=None, publisher=None) -> asyncio.Task:
        self.voice = self.session(source, session_factory, publisher)
        self.task = asyncio.create_task(self.voice.run())
        return self.task

    async def done(self, timeout: float = 10.0) -> None:
        await asyncio.wait_for(asyncio.shield(self.task), timeout)

    async def hang_up(self) -> None:
        self.transport.push(Stop())
        await self.done()

    def say(self, text: str) -> None:
        self.transport.push(AudioIn(b"FAKE-STT:" + text.encode()))

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


async def _make_env(make_world, bot_factory, bot_adapters, bot_settings, runtime, graph, bus, source: str) -> Env:
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
                source=source,
                provider_call_sid="CA" + uuid.uuid4().hex,
                caller_e164=CALLER if source == "phone" else None,
                status="in_progress",
                started_at=datetime.now(UTC),
            )
        )
        await uow.commit()
    checks = Checks()
    stt, tts = WatchedStt(checks), WatchedTts(checks)
    stts, ttss = ProviderRegistry("speech_to_text"), ProviderRegistry("text_to_speech")
    stts.register("deepgram")(lambda: stt)
    ttss.register("openai")(lambda: tts)
    adapters = replace(bot_adapters, speech_to_text=SttFactory(stts), text_to_speech=TtsFactory(ttss))
    runner = TurnRunner(runtime, graph)
    return Env(world, call_id, bot_factory, adapters, runner, bus, bot_settings, checks, stt, tts)


@pytest.fixture
async def env(make_world, bot_factory, bot_adapters, bot_settings, runtime, graph, bus):
    e = await _make_env(make_world, bot_factory, bot_adapters, bot_settings, runtime, graph, bus, "phone")
    yield e
    await _teardown(e)


async def _teardown(e: Env) -> None:
    """A test that failed early may leave its session running: cancel it before the next test."""
    if e.task is not None and not e.task.done():
        e.task.cancel()
        with suppress(BaseException):
            await e.task
    await e.runner.aclose()


@pytest.fixture
async def browser_env(make_world, bot_factory, bot_adapters, bot_settings, runtime, graph, bus):
    e = await _make_env(make_world, bot_factory, bot_adapters, bot_settings, runtime, graph, bus, "browser")
    yield e
    await _teardown(e)


# --- tests ------------------------------------------------------------------------------------------


async def test_greeting_spoken_first_with_recording_notice(env):
    env.start()
    await env.transport.wait_until(has_mark("turn-0"))

    assert spoken(env.transport) == [GREETING, NOTICE]
    assert env.transport.sent[0][0] == "audio"
    first_mark = next(i for i, e in enumerate(env.transport.sent) if e[0] == "mark")
    assert all(e[0] == "audio" for e in env.transport.sent[:first_mark])

    await env.hang_up()
    assert env.transport.closed
    assert (await env.call()).end_reason == "caller_hangup"


async def test_greeting_without_recording_notice(env):
    env.setup = replace(env.setup, record_calls=False)
    env.start()
    await env.transport.wait_until(has_mark("turn-0"))
    assert spoken(env.transport) == [GREETING]
    await env.hang_up()


async def test_utterance_answered_and_spoken_in_sentences(env):
    script_chat(["Shipping takes three days. Returns are free! Anything else?"])
    latency_before = sample("support_voice_response_latency_seconds_count")
    env.start()
    await env.transport.wait_until(has_mark("turn-0"))
    env.say("how long is shipping")
    await env.transport.wait_until(has_mark("turn-1"))

    assert spoken(env.transport)[2:] == ["Shipping takes three days.", "Returns are free!", "Anything else?"]
    assert sample("support_voice_response_latency_seconds_count") == latency_before + 1
    await env.hang_up()
    messages = await env.messages()
    assert [(m.role, m.text) for m in messages] == [
        ("customer", "how long is shipping"),
        ("bot", "Shipping takes three days. Returns are free! Anything else?"),
    ]


async def _reply_playing(env, reply_first: str) -> None:
    """Greeting played, then the caller asks and the bot's first reply frame is out but not played."""
    env.transport.auto_marks = False
    env.start()
    await env.transport.wait_until(has_mark("turn-0"))
    env.transport.push(MarkPlayed("turn-0"))
    env.transport.hold_audio()
    env.say("tell me about shipping")
    await env.transport.wait_until(lambda t: reply_first in env.tts.texts and len(spoken(t)) == 3)


async def test_barge_in_clears_playback_and_cancels_tts(env):
    script_chat(["Sentence one is here. Sentence two is here. Sentence three is here.", "Okay, stopping."])
    await _reply_playing(env, "Sentence one is here.")
    assert ("clear",) not in env.transport.sent

    env.say("wait stop")
    await env.transport.wait_until(lambda t: ("clear",) in t.sent)
    env.transport.release_audio()
    await env.transport.wait_until(has_mark("turn-2"))

    assert env.tts.interrupted == ["Sentence one is here."]
    assert "Sentence two is here." not in env.tts.texts
    clear_at = env.transport.sent.index(("clear",))
    after = FakeTransport()
    after.sent = env.transport.sent[clear_at:]
    assert spoken(after) == ["Okay, stopping."]
    await env.hang_up()
    customer = [m.text for m in await env.messages() if m.role == "customer"]
    assert customer == ["tell me about shipping", "wait stop"]


async def test_vad_event_without_words_does_not_barge_in(env):
    script_chat(["Sentence one is here. Sentence two is here."])
    await _reply_playing(env, "Sentence one is here.")
    stream = env.stt.streams[0]
    handled = len(stream.handled)

    env.transport.push(AudioIn(b"FAKE-NOISE"))
    await env.transport.wait_until(
        lambda t: any(isinstance(e, SpeechStarted) for e in stream.handled[handled:])
    )
    assert ("clear",) not in env.transport.sent

    env.transport.release_audio()
    await env.transport.wait_until(has_mark("turn-1"))
    assert spoken(env.transport)[2:] == ["Sentence one is here.", "Sentence two is here."]
    assert env.tts.interrupted == []
    await env.hang_up()


async def test_utterances_during_turn_answered_next(env):
    pushed = False

    async def hook(messages):
        nonlocal pushed
        if pushed:
            return
        pushed = True
        env.say("and another thing")
        env.say("one more")
        async with asyncio.timeout(5):  # both stored while this turn still runs
            while len([m for m in await env.messages() if m.role == "customer"]) < 3:
                await asyncio.sleep(0.01)

    script = script_chat(["First reply.", "Second reply."], hook=hook)
    calls: list[int] = []
    answer_now = env.runner.answer_now

    async def counting(*args, **kwargs):
        calls.append(1)
        return await answer_now(*args, **kwargs)

    env.runner.answer_now = counting
    env.start()
    await env.transport.wait_until(has_mark("turn-0"))
    env.say("where is my order")
    await env.transport.wait_until(has_mark("turn-2"))

    assert spoken(env.transport)[2:] == ["First reply.", "Second reply."]
    assert len(calls) == 2
    second_prompt = " ".join(str(m.content) for m in script.reply_calls[1].messages)
    assert "and another thing" in second_prompt and "one more" in second_prompt
    await env.hang_up()
    assert len(await env.turns()) == 2


async def test_escalation_speaks_callback_and_requests_callback(env):
    script_chat([])
    env.start()
    await env.transport.wait_until(has_mark("turn-0"))
    env.say("I want a refund for my order")
    await env.done()

    assert spoken(env.transport)[-1] == EN["voice_callback_phone"]
    assert marks(env.transport)[-1] == "goodbye"
    assert env.transport.closed
    call, conversation = await env.call(), await env.conversation()
    assert call.end_reason == "handoff_callback"
    assert (conversation.status, conversation.callback_status, conversation.callback_phone) == (
        "open",
        "requested",
        CALLER,
    )


async def test_escalation_on_browser_call_has_no_callback_phone(browser_env):
    env = browser_env
    script_chat([])
    env.start("browser")
    await env.transport.wait_until(has_mark("turn-0"))
    env.say("I want a refund for my order")
    await env.done()

    assert spoken(env.transport)[-1] == EN["voice_callback_browser"]
    call, conversation = await env.call(), await env.conversation()
    assert call.end_reason == "handoff_callback"
    assert (conversation.status, conversation.callback_status, conversation.callback_phone) == ("open", None, None)


async def test_silence_prompts_then_hangs_up(env, monkeypatch):
    monkeypatch.setattr(voice_session, "SILENCE_PROMPT_SECONDS", 0.05)
    monkeypatch.setattr(voice_session, "SILENCE_HANGUP_SECONDS", 0.05)
    env.start()
    await env.done()

    assert spoken(env.transport) == [GREETING, NOTICE, EN["voice_still_there"], EN["voice_goodbye"]]
    assert marks(env.transport)[-1] == "goodbye"
    assert env.transport.closed
    call, conversation = await env.call(), await env.conversation()
    assert call.end_reason == "silence"
    assert conversation.callback_status is None


async def test_speech_restarts_the_silence_timer_and_resets_the_prompt(env, monkeypatch):
    monkeypatch.setattr(voice_session, "SILENCE_PROMPT_SECONDS", 0.2)
    monkeypatch.setattr(voice_session, "SILENCE_HANGUP_SECONDS", 0.2)
    env.start()
    await env.transport.wait_until(has_mark("turn-0"))
    stream = env.stt.streams[0]
    # The caller keeps talking (interim words, no finished utterance) for well past the prompt delay.
    for _ in range(10):
        stream.inject(Transcript("um", False, False))
        await asyncio.sleep(0.05)
    assert EN["voice_still_there"] not in env.tts.texts  # each word restarted the running timer
    await env.transport.wait_until(has_mark("turn-1"))  # "Are you still there?" played (stage 1)
    stream.inject(Transcript("um", False, False))  # speech after the prompt: back to stage 0
    await env.done()

    still, bye = EN["voice_still_there"], EN["voice_goodbye"]
    assert spoken(env.transport) == [GREETING, NOTICE, still, still, bye]


async def test_max_duration_requests_callback(env):
    env.setup = replace(env.setup, max_call_seconds=0.2)
    env.start()
    await env.done()

    assert spoken(env.transport)[-1] == EN["voice_max_duration"]
    assert marks(env.transport)[-1] == "goodbye"
    assert env.transport.closed
    call, conversation = await env.call(), await env.conversation()
    assert call.end_reason == "max_duration"
    assert (conversation.callback_status, conversation.callback_phone) == ("requested", CALLER)


async def _integration_rows(env) -> list[tuple[str, str, str, int]]:
    async with env.factory() as s:
        rows = (await s.execute(select(IntegrationEvent).where(IntegrationEvent.organisation_id == env.world.org_id)))
        return [(r.kind, r.provider, r.error_type, r.count) for r in rows.scalars()]


async def test_stt_auth_failure_ends_with_error_and_callback(env):
    before = sample("support_voice_provider_errors_total", {"provider": "deepgram", "error_type": "auth_failed"})
    env.start()
    await env.transport.wait_until(has_mark("turn-0"))
    env.transport.push(AudioIn(b"FAKE-STT-AUTHFAIL"))
    await env.done()

    assert await _integration_rows(env) == [("voice", "deepgram", "auth_failed", 1)]
    after = sample("support_voice_provider_errors_total", {"provider": "deepgram", "error_type": "auth_failed"})
    assert after == before + 1
    assert spoken(env.transport) == [GREETING, NOTICE]  # the <Connect action> says the trouble message
    assert env.transport.closed
    call, conversation = await env.call(), await env.conversation()
    assert call.end_reason == "error"
    assert (conversation.status, conversation.callback_status) == ("open", "requested")


async def test_tts_failure_ends_with_error_and_callback(env):
    script_chat(["This will FAIL now."])
    env.start()
    await env.transport.wait_until(has_mark("turn-0"))
    env.say("hello")
    await env.done()

    assert await _integration_rows(env) == [("voice", "openai", "auth_failed", 1)]
    assert env.transport.closed
    call, conversation = await env.call(), await env.conversation()
    assert call.end_reason == "error"
    assert conversation.callback_status == "requested"
    turns = await env.turns()  # the turn that was speaking still persisted
    assert len(turns) == 1 and turns[0].reply_message_id is not None


async def test_markdown_not_spoken(env):
    script_chat(["## Delivery\n**Shipping** takes [three days](https://example.com/ship).\n- Free *returns* 🎉\n"])
    env.start()
    await env.transport.wait_until(has_mark("turn-0"))
    env.say("tell me about delivery")
    await env.transport.wait_until(has_mark("turn-1"))

    said = spoken(env.transport)[2:]
    assert said == ["Delivery.", "Shipping takes three days.", "Free returns."]
    for text in said:
        for forbidden in ("*", "#", "[", "http", "🎉"):
            assert forbidden not in text
    await env.hang_up()


async def test_long_reply_capped(env):
    reply = " ".join(f"Sentence number {i} is right here." for i in range(100))
    script_chat([reply])
    env.start()
    await env.transport.wait_until(has_mark("turn-0"))
    env.say("tell me everything")
    await env.transport.wait_until(has_mark("turn-1"))

    said = spoken(env.transport)[2:]
    total = sum(len(t) for t in said)
    assert voice_session.MAX_SPOKEN_CHARS == 1200
    assert 1100 < total <= 1200
    assert said[-1].endswith(".")
    await env.hang_up()
    bot = [m.text for m in await env.messages() if m.role == "bot"]
    assert bot == [reply]  # the transcript keeps the whole reply


async def test_usage_recorded(env):
    script_chat(["Sure thing."])
    env.start()
    await env.transport.wait_until(has_mark("turn-0"))
    for _ in range(5):
        env.transport.push(AudioIn(SILENCE))
    env.say("hello")
    await env.transport.wait_until(has_mark("turn-1"))
    await env.hang_up()

    call = await env.call()
    assert call.stt_seconds == pytest.approx(0.1)
    assert call.tts_characters == sum(len(t) for t in env.tts.texts) == len(GREETING) + len(NOTICE) + len("Sure thing.")


async def test_turn_failure_speaks_turn_failed(env, monkeypatch):
    script_chat(["unused"])

    async def busy(*args, **kwargs):
        raise TimeoutError("lease busy")

    env.runner.answer_now = busy
    env.start()
    await env.transport.wait_until(has_mark("turn-0"))
    env.say("hello")
    await env.transport.wait_until(has_mark("turn-1"))
    assert spoken(env.transport)[2:] == [EN["voice_turn_failed"]]
    await env.hang_up()


async def test_hangup_mid_turn_persists_turn_and_speaks_nothing_more(env):
    started = asyncio.Event()
    release = asyncio.Event()

    async def hook(messages):
        started.set()
        await release.wait()

    script_chat(["Late reply."], hook=hook)
    env.start()
    await env.transport.wait_until(has_mark("turn-0"))
    env.say("hello")
    await asyncio.wait_for(started.wait(), 5)
    env.transport.push(Stop())
    await env.transport.wait_until(lambda t: t.closed)
    release.set()
    await env.done()

    assert spoken(env.transport) == [GREETING, NOTICE]
    turns = await env.turns()
    assert len(turns) == 1 and turns[0].reply_message_id is not None
    assert (await env.call()).end_reason == "caller_hangup"


async def test_hangup_mid_turn_that_escalates_still_requests_the_callback(env):
    """The caller asks for a person and hangs up before the turn finishes: the turn still routes the conversation,
    and it lands in the Callbacks view with the number to call back. The first end reason still wins."""
    started = asyncio.Event()
    release = asyncio.Event()
    real_answer_now = env.runner.answer_now

    async def held_answer_now(organisation_id, conversation_id, sink=None):
        started.set()
        await release.wait()  # the caller hangs up while the turn runs
        return await real_answer_now(organisation_id, conversation_id, sink)

    env.runner.answer_now = held_answer_now
    script_chat([])
    env.start()
    await env.transport.wait_until(has_mark("turn-0"))
    env.say("I want a refund for my order")
    await asyncio.wait_for(started.wait(), 5)
    env.transport.push(Stop())
    await env.transport.wait_until(lambda t: t.closed)
    release.set()
    await env.done()

    assert spoken(env.transport) == [GREETING, NOTICE]
    call, conversation = await env.call(), await env.conversation()
    assert call.end_reason == "caller_hangup"
    assert (conversation.status, conversation.callback_status, conversation.callback_phone) == (
        "open",
        "requested",
        CALLER,
    )


async def test_no_session_open_while_awaiting_providers(env, monkeypatch):
    script_chat(["Sure thing. Anything else?"])
    tracking = TrackingFactory(env.factory)
    env.checks.factory = tracking
    env.transport.on_send = lambda entry: env.checks("transport")
    answer_now = env.runner.answer_now

    async def checked(*args, **kwargs):
        env.checks("answer_now")
        return await answer_now(*args, **kwargs)

    # After a turn the session queues the reply's tail, then reads the conversation's status. Here that read
    # waits until the tail has been synthesized, so every TTS frame stays clear of the call's database work
    # and the across-task check is deterministic.
    left_bot = VoiceSession._left_bot

    async def left_bot_after_tail(self):
        await env.transport.wait_until(lambda t: "Anything else?" in spoken(t))
        return await left_bot(self)

    monkeypatch.setattr(VoiceSession, "_left_bot", left_bot_after_tail)

    env.runner.answer_now = checked
    async with tracking() as probe:  # the checks can fail: an open session is seen from any task
        await probe.execute(select(1))
        assert tracking.held(any_task=True)
    tracking.sessions.clear()
    # The inbox publisher's after-commit publishes run detached and read through their own (untracked) factory.
    env.start(session_factory=tracking, publisher=InboxPublisher(env.adapters.pubsub, env.factory))
    await env.transport.wait_until(has_mark("turn-0"))
    env.transport.push(AudioIn(SILENCE))
    env.say("hello")
    await env.transport.wait_until(has_mark("turn-1"))
    await env.hang_up()

    assert tracking.sessions  # the session did use the database...
    assert set(env.checks.seen) == {"stt", "tts", "transport", "answer_now"}
    assert {where: any(states) for where, states in env.checks.seen.items()} == {
        "stt": False,
        "tts": False,
        "transport": False,
        "answer_now": False,
    }


# --- review fixes -----------------------------------------------------------------------------------


async def eventually(check, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not await check():
            await asyncio.sleep(0.01)


def fake_answers(env, first):
    """Replaces answer_now: the first call runs `first(sink)`, later calls have nothing to answer."""
    calls: list[int] = []

    async def answer_now(organisation_id, conversation_id, sink=None):
        calls.append(1)
        if len(calls) == 1:
            return await first(sink)
        return None

    env.runner.answer_now = answer_now
    return calls


async def _first_frame_held(env, text: str = "Sentence one is here.") -> None:
    env.transport.auto_marks = False
    env.start()
    await env.transport.wait_until(has_mark("turn-0"))
    env.transport.push(MarkPlayed("turn-0"))
    env.transport.hold_audio()
    env.say("a question")
    await env.transport.wait_until(lambda t: text in env.tts.texts and len(spoken(t)) == 3)


async def test_reset_keeps_the_previous_turns_speech(env):
    pushed = False

    async def hook(messages):
        nonlocal pushed
        if pushed:
            return
        pushed = True
        env.say("hello?")  # heard before any reply audio: no barge-in, answered by the next turn
        async with asyncio.timeout(5):
            while len([m for m in await env.messages() if m.role == "customer"]) < 2:
                await asyncio.sleep(0.01)

    script_chat(["First answer is here. It has two sentences.", RuntimeError("model down")], hook=hook)
    env.start()
    await env.transport.wait_until(has_mark("turn-0"))
    env.transport.hold_audio()  # the first answer stays queued behind its first frame
    env.say("where is my order")

    async def second_turn_done() -> bool:
        return len(await env.turns()) == 2

    await eventually(second_turn_done)  # its failure reset the sink while the first answer was queued
    env.transport.release_audio()
    await env.transport.wait_until(lambda t: "It has two sentences." in spoken(t))

    assert spoken(env.transport)[2:4] == ["First answer is here.", "It has two sentences."]
    assert ("clear",) not in env.transport.sent
    if not env.task.done():
        env.transport.push(Stop())
    await env.done()


async def test_noise_does_not_hold_off_the_silence_hangup(env, monkeypatch):
    monkeypatch.setattr(voice_session, "SILENCE_PROMPT_SECONDS", 0.1)
    monkeypatch.setattr(voice_session, "SILENCE_HANGUP_SECONDS", 0.1)

    async def noisy_line() -> None:
        while True:
            env.transport.push(AudioIn(b"FAKE-NOISE"))
            await asyncio.sleep(0.02)

    noise = asyncio.create_task(noisy_line())
    try:
        env.start()
        await env.done(timeout=3)
    finally:
        noise.cancel()

    assert spoken(env.transport)[-2:] == [EN["voice_still_there"], EN["voice_goodbye"]]
    assert (await env.call()).end_reason == "silence"


async def test_no_turn_failed_message_after_barge_in(env):
    async def first(sink):
        await sink.token("Sentence one is here. Sentence two is here. ")
        await env.transport.wait_until(lambda t: ("clear",) in t.sent)
        raise RuntimeError("the turn failed after the caller interrupted")

    calls = fake_answers(env, first)
    await _first_frame_held(env)
    env.say("stop")
    await env.transport.wait_until(lambda t: ("clear",) in t.sent)
    env.transport.release_audio()
    await env.transport.wait_until(has_mark("turn-2"))

    assert len(calls) == 2
    assert EN["voice_turn_failed"] not in env.tts.texts
    await env.hang_up()


async def test_deltas_after_barge_in_are_not_spoken(env):
    async def first(sink):
        await sink.token("Sentence one is here. ")
        await env.transport.wait_until(lambda t: ("clear",) in t.sent)
        await sink.token("Late words come here. ")
        await sink.token("And a last one.")

    fake_answers(env, first)
    await _first_frame_held(env)
    env.say("stop")
    await env.transport.wait_until(lambda t: ("clear",) in t.sent)
    env.transport.release_audio()
    await env.transport.wait_until(has_mark("turn-2"))

    assert env.tts.texts[2:] == ["Sentence one is here."]
    await env.hang_up()


async def test_latency_observed_once_per_answered_utterance(env):
    pushed = False

    async def hook(messages):
        nonlocal pushed
        if pushed:
            return
        pushed = True
        env.say("and another thing")
        env.say("one more")
        async with asyncio.timeout(5):
            while len([m for m in await env.messages() if m.role == "customer"]) < 3:
                await asyncio.sleep(0.01)

    script_chat(["First reply.", "Second reply."], hook=hook)
    before = sample("support_voice_response_latency_seconds_count")
    env.start()
    await env.transport.wait_until(has_mark("turn-0"))
    env.say("where is my order")
    await env.transport.wait_until(has_mark("turn-2"))

    # One observation per utterance, each at the first frame of the turn that answered it.
    assert sample("support_voice_response_latency_seconds_count") == before + 3
    await env.hang_up()


async def test_stt_failure_during_a_turn_ends_with_error_and_the_turn_persists(env):
    triggered = False

    async def hook(messages):
        nonlocal triggered
        if triggered:
            return
        triggered = True
        env.transport.push(AudioIn(b"FAKE-STT-AUTHFAIL"))
        await env.transport.wait_until(lambda t: t.closed)  # the error path closes the line at once

    script_chat(["Reply after the failure."], hook=hook)
    env.start()
    await env.transport.wait_until(has_mark("turn-0"))
    env.say("hello")
    await env.done()

    assert await _integration_rows(env) == [("voice", "deepgram", "auth_failed", 1)]
    call, conversation = await env.call(), await env.conversation()
    assert call.end_reason == "error" and conversation.callback_status == "requested"
    assert "Reply after the failure." not in env.tts.texts
    # The turn persisted. The callback routed the conversation to the team mid-turn, so the runner keeps the
    # BotTurn without storing the reply (a superseded turn).
    assert len(await env.turns()) == 1


async def test_transport_closing_during_the_greeting(env):
    env.transport.hold_audio()
    env.start()
    await env.transport.wait_until(lambda t: len(spoken(t)) == 1)
    env.transport.disconnect()
    env.transport.release_audio()
    await env.done()

    assert env.task.result() is None
    call = await env.call()
    assert call.end_reason == "caller_hangup"
    assert call.tts_characters >= len(GREETING)


async def test_tts_failure_on_the_goodbye_ends_the_call(env, monkeypatch):
    monkeypatch.setattr(voice_session, "SILENCE_PROMPT_SECONDS", 0.05)
    monkeypatch.setattr(voice_session, "SILENCE_HANGUP_SECONDS", 0.05)
    monkeypatch.setattr(voice_session, "GOODBYE_MARK_TIMEOUT_SECONDS", 30)
    env.tts.fail_on = EN["voice_goodbye"]
    env.start()
    await env.done(timeout=5)  # not held for the goodbye timeout

    assert await _integration_rows(env) == [("voice", "openai", "auth_failed", 1)]
    assert (await env.call()).end_reason == "silence"
    assert env.transport.closed


async def test_max_duration_mid_turn(env):
    started, release = asyncio.Event(), asyncio.Event()

    async def hook(messages):
        started.set()
        await release.wait()

    script_chat(["Late reply."], hook=hook)
    env.setup = replace(env.setup, max_call_seconds=0.4)
    env.start()
    await env.transport.wait_until(has_mark("turn-0"))
    env.say("hello")
    await asyncio.wait_for(started.wait(), 5)
    await env.transport.wait_until(lambda t: t.closed)
    release.set()
    await env.done()

    assert spoken(env.transport)[-1] == EN["voice_max_duration"]
    assert "Late reply." not in env.tts.texts
    assert (await env.call()).end_reason == "max_duration"
    assert len(await env.turns()) == 1  # persisted (superseded by the callback routing, so without a reply)


@pytest.mark.parametrize("echo", [True, False])
async def test_barge_in_with_marks_echoed_on_clear(env, monkeypatch, echo):
    """Twilio echoes unplayed marks on `clear`, and a mark sent with nothing buffered at once. A barge-in
    during the next reply, with the previous turn's mark still unplayed: the stale echo of `turn-1` must be
    ignored, and the echo of `turn-2` (sent after the clear) is what lets the silence timer start again."""
    monkeypatch.setattr(voice_session, "SILENCE_PROMPT_SECONDS", 0.1)
    env.transport = FakeTransport(auto_marks=False, echo_marks_on_clear=echo)
    pushed = False

    async def hook(messages):
        nonlocal pushed
        if not pushed:  # turn 1: the next question arrives before any reply audio, so it is not a barge-in
            pushed = True
            env.say("and the second thing")
            async with asyncio.timeout(5):
                while len([m for m in await env.messages() if m.role == "customer"]) < 2:
                    await asyncio.sleep(0.01)
            return
        if any("and the second thing" in str(m.content) for m in messages):
            # turn 2: turn 1's audio and its mark are out, the mark not played back; hold turn 2's first frame
            await env.transport.wait_until(has_mark("turn-1"))
            env.transport.hold_audio()

    script_chat(["One.", "Two is here. Two more is here."], hook=hook)
    env.start()
    await env.transport.wait_until(has_mark("turn-0"))
    env.transport.push(MarkPlayed("turn-0"))
    env.say("first thing")
    await env.transport.wait_until(lambda t: "Two is here." in spoken(t))  # the next reply's audio started
    assert "turn-1" in env.transport._unplayed  # sent, not played back

    env.stt.streams[0].inject(Transcript("wait", False, False))  # words over the reply: barge in
    await env.transport.wait_until(lambda t: ("clear",) in t.sent)
    env.transport.release_audio()
    await env.transport.wait_until(has_mark("turn-2"))
    assert "Two more is here." not in env.tts.texts

    if echo:  # turn-2's echo (nothing buffered after the clear) re-arms silence: the prompt follows
        await env.transport.wait_until(lambda t: EN["voice_still_there"] in env.tts.texts)
    else:  # without echoes the latest mark never plays back, so silence never starts
        await asyncio.sleep(0.5)
        assert EN["voice_still_there"] not in env.tts.texts
    await env.hang_up()


async def test_line_closed_before_run_returns_and_usage_stored_first(env):
    started, release = asyncio.Event(), asyncio.Event()

    async def hook(messages):
        started.set()
        await release.wait()

    script_chat(["Late reply."], hook=hook)
    env.start()
    await env.transport.wait_until(has_mark("turn-0"))
    env.say("hello")
    await asyncio.wait_for(started.wait(), 5)
    env.transport.push(Stop())
    await asyncio.wait_for(env.voice.line_closed.wait(), 5)

    assert env.transport.closed

    async def usage_stored() -> bool:
        return (await env.call()).tts_characters == len(GREETING) + len(NOTICE)

    await eventually(usage_stored)  # stored while the turn is still held open...
    assert not env.task.done()  # ...and run() is still waiting for the turn to persist
    release.set()
    await env.done()
    assert len(await env.turns()) == 1


async def test_unknown_tts_provider_still_closes_and_records(env):
    env.setup = replace(env.setup, tts=replace(env.setup.tts, provider="nope"))
    env.start()
    await env.done()

    assert env.task.result() is None
    assert env.transport.closed
    assert (await env.call()).end_reason == "error"


def test_turn_constants_follow_the_runner():
    from supportcore.bot import nodes, turns

    assert voice_session.TURN_RETRY_SECONDS == turns.ANSWER_NOW_RETRY_SECONDS
    assert voice_session.TURN_LEASE_WAIT_SECONDS == turns.ANSWER_NOW_WAIT_SECONDS
    longest_turn = (
        turns.ANSWER_NOW_WAIT_SECONDS
        + nodes.CLASSIFIER_TIMEOUT_SECONDS
        + nodes.CHAT_TIMEOUT_SECONDS * (nodes.MAX_TOOL_ROUNDS + 1)
    )
    assert REAL_TURN_FINISH_TIMEOUT > longest_turn


# --- live transfer (Phase 7b, Task 7) ---------------------------------------------------------------

ALL_DAY = {day: [["00:00", "24:00"]] for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


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


async def _transfer_of(env):
    from supportcore.db.models import CallTransfer

    async with env.factory() as s:
        stmt = select(CallTransfer).where(CallTransfer.call_id == env.call_id)
        return (await s.execute(stmt)).scalar_one_or_none()


async def test_escalation_with_agent_available_speaks_transferring_and_begins_transfer(env):
    await _agent_available(env)
    at_close = []  # the committed transfer row, as seen from a fresh session when the transport is first closed
    real_close = env.transport.close

    async def close() -> None:
        if not env.transport.closed:
            at_close.append(await _transfer_of(env))
        await real_close()

    env.transport.close = close
    script_chat([])
    env.start()
    await env.transport.wait_until(has_mark("turn-0"))
    env.transport.auto_marks = False  # the goodbye mark plays only when the test says so
    env.say("I want a refund for my order")
    await env.transport.wait_until(has_mark("goodbye"))
    await asyncio.sleep(0.2)
    assert not env.transport.closed  # waiting for the closing words to play
    env.transport.push(MarkPlayed("goodbye"))
    await env.done()

    assert spoken(env.transport)[-1] == EN["voice_transferring"]
    assert marks(env.transport)[-1] == "goodbye"
    assert env.transport.sent.index(("mark", "goodbye")) < env.transport.sent.index(("close",))
    assert len(at_close) == 1 and at_close[0] is not None and at_close[0].status == "ringing"
    call, conversation = await env.call(), await env.conversation()
    assert call.end_reason is None  # the transfer decides how the call ends
    assert conversation.callback_status is None


async def test_escalation_without_agent_keeps_callback(env):
    await _memory_presence(env)  # nobody present: nobody can take the call
    script_chat([])
    env.start()
    await env.transport.wait_until(has_mark("turn-0"))
    env.say("I want a refund for my order")
    await env.done()

    assert spoken(env.transport)[-1] == EN["voice_callback_phone"]
    assert await _transfer_of(env) is None
    call, conversation = await env.call(), await env.conversation()
    assert call.end_reason == "handoff_callback"
    assert (conversation.callback_status, conversation.callback_phone) == ("requested", CALLER)


async def test_hangup_during_transferring_line_leaves_callback(env):
    from supportcore.voice.transfers import TransferService

    await _agent_available(env)
    script_chat([])
    env.start()
    await env.transport.wait_until(has_mark("turn-0"))
    env.transport.auto_marks = False
    env.say("I want a refund for my order")
    await env.transport.wait_until(has_mark("goodbye"))
    env.transport.push(Stop())  # the caller hangs up while "please hold" plays
    await env.done()

    assert (await _transfer_of(env)).status == "ringing"
    assert (await env.call()).end_reason is None
    # Twilio's status callback: the caller left while the transfer was ringing.
    async with UnitOfWork(env.factory) as uow:
        service = TransferService(
            uow.session, env.world.org_id, env.bus, InboxPublisher(env.adapters.pubsub, env.factory), env.adapters,
            env.settings, session_factory=env.factory,
        )
        await service.caller_left(env.call_id)
        await uow.commit()

    assert (await _transfer_of(env)).status == "abandoned"
    call, conversation = await env.call(), await env.conversation()
    assert call.end_reason == "transfer_no_answer"
    assert (conversation.callback_status, conversation.callback_phone) == ("requested", CALLER)
