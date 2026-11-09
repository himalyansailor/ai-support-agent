import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from supportcore.analytics import events, integration_errors
from supportcore.config import Settings
from supportcore.db.models import (
    Call,
    CallRecording,
    Conversation,
    Customer,
    PhoneNumber,
    ProviderCredential,
    TelephonyAccount,
    VoiceSettings,
    Widget,
)
from supportcore.voice.catalog import (
    credential_provider_for,
    get_stt_spec,
    stt_models,
    tts_models,
)
from supportcore.voice.stt.deepgram_stt import DeepgramSpeechToText
from supportcore.voice.stt.fake_stt import FakeSpeechToText
from supportcore.voice.stt.ports import (
    SpeechStarted,
    SttAuthError,
    SttConfig,
    Transcript,
)
from supportcore.voice.telephony.fake_telephony import FakeTelephony
from supportcore.voice.telephony.ports import TelephonyAccountRef, TelephonyAuthError
from supportcore.voice.telephony.twilio_telephony import TwilioTelephony
from supportcore.voice.tts.fake_tts import FakeTextToSpeech
from supportcore.voice.tts.openai_tts import OpenAITextToSpeech
from supportcore.voice.tts.ports import TtsAuthError, TtsConfig
from supportcore.wiring import build_adapters

SID = "AC" + "a" * 32


def _settings(**kw) -> Settings:
    kw.setdefault("billing", "stripe")  # prod refuses the dev billing adapter
    kw.setdefault("stripe_secret_key", "sk_x")
    kw.setdefault("stripe_webhook_secret", "whsec_x")
    return Settings(
        _env_file=None,
        env=kw.pop("env", "test"),
        database_url="postgresql+asyncpg://u:p@h/db",
        redis_url="redis://h:6379/0",
        jwt_secret="x" * 32,
        smtp_from="noreply@example.com",
        **kw,
    )


async def _account(session, org, sid=SID) -> TelephonyAccount:
    account = TelephonyAccount(
        organisation_id=org.id, kind="byo", account_sid=sid, auth_token_encrypted="CIPHER", auth_token_last4="1234"
    )
    session.add(account)
    await session.flush()
    return account


async def _number(session, org, account, e164="+15005550006") -> PhoneNumber:
    number = PhoneNumber(
        organisation_id=org.id,
        telephony_account_id=account.id,
        e164=e164,
        provider_sid="PN" + "0" * 32,
        ownership="byo",
    )
    session.add(number)
    await session.flush()
    return number


async def _conversation(session, org) -> Conversation:
    customer = Customer(organisation_id=org.id, phone="+15551230000")
    session.add(customer)
    await session.flush()
    conv = Conversation(organisation_id=org.id, customer_id=customer.id, channel="voice")
    session.add(conv)
    await session.flush()
    return conv


async def _call(session, org, sid="CA" + "1" * 32, **kw) -> Call:
    conv = await _conversation(session, org)
    call = Call(organisation_id=org.id, conversation_id=conv.id, source="phone", provider_call_sid=sid, **kw)
    session.add(call)
    await session.flush()
    return call


async def test_voice_tables_round_trip(session, org_a):
    widget = Widget(organisation_id=org_a.id, name="W", public_key="pk-" + uuid.uuid4().hex)
    cred = ProviderCredential(
        organisation_id=org_a.id, provider="deepgram", label="dg", encrypted_api_key="C", last4="abcd"
    )
    session.add_all([widget, cred])
    await session.flush()
    account = await _account(session, org_a)
    number = await _number(session, org_a, account)
    number.widget_id = widget.id
    settings = VoiceSettings(
        organisation_id=org_a.id, stt_provider="deepgram", stt_model="nova-3", stt_credential_id=cred.id
    )
    session.add(settings)
    call = await _call(session, org_a, phone_number_id=number.id, caller_e164="+15551230000")
    session.add(
        CallRecording(
            organisation_id=org_a.id,
            call_id=call.id,
            storage_key="k",
            size_bytes=10,
            duration_seconds=3,
            delete_after=datetime.now(UTC) + timedelta(days=90),
        )
    )
    await session.flush()
    await session.refresh(account)
    await session.refresh(number)
    await session.refresh(settings)
    await session.refresh(call)
    assert (account.status, account.api_key_sid) == ("active", None)
    assert (number.enabled, number.widget_id, number.webhook_url) == (True, widget.id, None)
    assert (settings.phone_enabled, settings.browser_calls_enabled, settings.mode) == (False, False, "chained")
    assert (settings.record_calls, settings.recording_retention_days, settings.max_call_minutes) == (False, 90, 30)
    assert (call.status, call.direction, call.finalised, call.stt_seconds, call.tts_characters) == (
        "ringing",
        "inbound",
        False,
        0,
        0,
    )
    assert call.started_at is not None and call.created_at is not None


async def test_phone_number_e164_unique_across_orgs(session, org_a, org_b):
    await _number(session, org_a, await _account(session, org_a))
    other = await _account(session, org_b, sid="AC" + "b" * 32)
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await _number(session, org_b, other)


async def test_call_provider_sid_unique(session, org_a):
    await _call(session, org_a)
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await _call(session, org_a)


async def test_voice_settings_checks(session, org_a):
    for bad in ({"max_call_minutes": 0}, {"recording_retention_days": 3651}, {"mode": "walkie"}):
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(VoiceSettings(organisation_id=org_a.id, **bad))
                await session.flush()


async def test_conversation_callback_status_check(session, org_a):
    conv = await _conversation(session, org_a)
    conv.callback_status = "requested"
    conv.callback_phone = "+15551230000"
    await session.flush()
    conv.callback_status = "maybe"
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.flush()


def test_telephony_account_repr_hides_token():
    account = TelephonyAccount(
        id=uuid.uuid4(), kind="byo", account_sid=SID, auth_token_encrypted="SECRET-CIPHER", auth_token_last4="1234"
    )
    text = repr(account)
    assert "SECRET" not in text and "CIPHER" not in text and "1234" not in text
    assert "SECRET" not in repr(TelephonyAccountRef(SID, "SECRET-TOKEN"))


def test_fakes_only_in_test_env():
    with pytest.raises(ValueError):
        build_adapters(_settings(env="dev", telephony="fake"))
    adapters = build_adapters(_settings(env="test", telephony="fake"))
    assert isinstance(adapters.telephony, FakeTelephony)
    assert isinstance(adapters.speech_to_text.create("fake"), FakeSpeechToText)
    assert isinstance(adapters.text_to_speech.create("fake"), FakeTextToSpeech)
    assert adapters.speech_to_text.create("fake") is adapters.speech_to_text.create("fake")
    assert adapters.text_to_speech.create("fake") is adapters.text_to_speech.create("fake")
    dev = build_adapters(_settings(env="dev"))
    with pytest.raises(ValueError):
        dev.speech_to_text.create("fake")
    with pytest.raises(ValueError):
        dev.text_to_speech.create("fake")
    assert isinstance(dev.telephony, TwilioTelephony)
    assert isinstance(dev.speech_to_text.create("deepgram"), DeepgramSpeechToText)
    assert isinstance(dev.text_to_speech.create("openai"), OpenAITextToSpeech)


def test_prod_refuses_http_voice_url():
    common = {"env": "prod", "secrets_key": "x" * 43 + "="}
    with pytest.raises(ValueError, match="https"):
        _settings(voice_public_url="http://example.com", **common)
    assert _settings(voice_public_url="https://example.com", **common).voice_public_url == "https://example.com"
    assert _settings(voice_public_url="http://localhost:8002", env="dev").voice_public_url


def test_voice_public_url_normalised_and_limits():
    assert _settings(voice_public_url="  https://x.example.com/ ").voice_public_url == "https://x.example.com"
    assert _settings(voice_public_url="  ").voice_public_url is None
    common = {"env": "prod", "secrets_key": "x" * 43 + "="}
    assert _settings(voice_public_url="HTTPS://x.example.com/", **common).voice_public_url == "HTTPS://x.example.com"
    with pytest.raises(ValueError, match="https"):
        _settings(voice_public_url="HTTP://x.example.com", **common)
    with pytest.raises(ValueError):
        _settings(voice_max_calls=0)
    with pytest.raises(ValueError):
        _settings(voice_stream_token_ttl_seconds=0)


def test_kinds_include_voice():
    assert "voice" in integration_errors.KINDS
    assert {"call_started", "call_ended"} <= set(events.KINDS)


async def test_fake_telephony_auth_and_numbers():
    fake = FakeTelephony()
    good = TelephonyAccountRef(SID, "t" * 32)
    await fake.verify(good)
    assert [n.e164 for n in await fake.list_numbers(good)] == ["+15005550006"]
    bad = TelephonyAccountRef(SID, "bad-token" + "0" * 23)
    with pytest.raises(TelephonyAuthError):
        await fake.verify(bad)
    with pytest.raises(TelephonyAuthError):
        await fake.list_numbers(bad)
    with pytest.raises(TelephonyAuthError):
        await fake.verify(TelephonyAccountRef("nope", "t" * 32))
    assert [name for name, _ in fake.calls].count("verify") == 3
    assert fake.browser_access_token(SID, None, "visitor", 60) == "fake-token:visitor"  # type: ignore[arg-type]


async def test_fake_stt_emits_transcript_for_marker_frame():
    stream = await FakeSpeechToText().open(SttConfig("fake", "fake-stt", "en"))
    await stream.send(b"\xff" * 160)
    await stream.send(b"FAKE-STT:hello there")
    await stream.send(b"FAKE-NOISE")
    await stream.aclose()
    events_ = [e async for e in stream.events()]
    assert events_ == [SpeechStarted(), Transcript("hello there", True, True), SpeechStarted()]
    assert stream.audio_seconds == pytest.approx(0.02)


async def test_fake_stt_authfail_raises_from_events():
    stream = await FakeSpeechToText().open(SttConfig("fake", "fake-stt", "en"))
    await stream.send(b"FAKE-STT-AUTHFAIL")
    with pytest.raises(SttAuthError):
        async for _ in stream.events():
            pass


async def test_fake_tts_frames_are_160_bytes():
    config = TtsConfig("fake", "fake-tts", "fake", "en")
    frames = [f async for f in FakeTextToSpeech().synthesize("x" * 400, config)]
    assert len(frames) == 3 and all(len(f) == 160 for f in frames)
    assert frames[0].startswith(b"FAKE-TTS:xxx") and frames[-1].endswith(b"\xff")
    with pytest.raises(TtsAuthError):
        [f async for f in FakeTextToSpeech().synthesize("this will FAIL", config)]


def test_stt_models_filtered_by_language():
    assert "nova-3" in [s.model for s in stt_models("en")]
    assert "nova-3" in [s.model for s in stt_models("pt-BR")]
    assert stt_models("zz") == []
    assert [s.provider for s in stt_models("en", include_fake=True)].count("fake") == 1
    assert [s.provider for s in tts_models("zz")] == ["openai", "openai"]
    assert next(s for s in tts_models("en") if s.recommended).default_voice == "coral"


def test_catalog_lookups():
    assert get_stt_spec("deepgram", "nova-3").recommended
    with pytest.raises(ValueError):
        get_stt_spec("fake", "fake-stt")
    assert get_stt_spec("fake", "fake-stt", include_fake=True).requires_credential is False
    assert [credential_provider_for(p) for p in ("deepgram", "openai", "fake")] == ["deepgram", "openai", None]
    with pytest.raises(ValueError):
        credential_provider_for("nope")


def test_dev_wiring_has_real_openai_realtime():
    from supportcore.voice.realtime.openai_realtime import OpenAIRealtime

    assert isinstance(build_adapters(_settings(env="dev")).speech_to_speech.create("openai"), OpenAIRealtime)
