import uuid

import pytest
from supportcore.db.models import ProviderCredential
from supportcore.voice.errors import InvalidVoiceSettingsError
from supportcore.voice.numbers_service import TelephonyService
from supportcore.voice.settings_service import VoiceSettingsService, load_voice_setup

from voice.conftest import BYO_SID, BYO_TOKEN, PUBLIC_URL

FAKE_RT = {"mode": "speech_to_speech", "realtime_provider": "fake", "realtime_model": "fake-realtime"}


@pytest.fixture
def service(session, org, bus):
    return VoiceSettingsService(session, org.id, bus, include_fake=True, voice_public_url=PUBLIC_URL)


@pytest.fixture
def telephony(session, org, bus, adapters, settings):
    return TelephonyService(session, org.id, bus, adapters, settings)


async def credential(session, org, adapters, provider, key="sk-secret-value-1234"):
    row = ProviderCredential(
        organisation_id=org.id, provider=provider, label=f"{provider}-{uuid.uuid4().hex[:6]}",
        encrypted_api_key=adapters.cipher.encrypt(key), last4=key[-4:],
    )
    session.add(row)
    await session.flush()
    return row


async def connect_number(telephony, owner, widget):
    await telephony.connect_byo(BYO_SID, BYO_TOKEN, owner)
    sid = (await telephony.list_account_numbers())[0].provider_sid
    await telephony.connect_number(sid, widget.id, owner)


async def test_speech_to_speech_needs_realtime_not_stt_tts(service, owner, telephony, widget):
    await service.update(owner, phone_enabled=True, mode="speech_to_speech")
    await connect_number(telephony, owner, widget)
    ready = await service.readiness()
    assert "realtime_not_configured" in ready.reasons
    assert "stt_not_configured" not in ready.reasons and "tts_not_configured" not in ready.reasons
    assert not ready.phone
    await service.update(owner, realtime_provider="fake", realtime_model="fake-realtime")
    ready = await service.readiness()
    assert ready.phone and "realtime_not_configured" not in ready.reasons
    await service.update(owner, mode="chained")  # chained again: STT/TTS are required again
    ready = await service.readiness()
    assert {"stt_not_configured", "tts_not_configured"} <= set(ready.reasons) and not ready.phone


async def test_realtime_model_checked_against_catalog_and_language(service, owner, session, org, adapters):
    key = await credential(session, org, adapters, "openai")
    for bad in (
        {"realtime_provider": "openai", "realtime_model": "gpt-9", "realtime_credential_id": key.id},
        {"realtime_provider": "nope", "realtime_model": "gpt-realtime"},
        {"realtime_provider": "openai"},
        {"mode": "telepathy"},
        {"realtime_provider": "openai", "realtime_model": "gpt-realtime", "realtime_voice": "nobody"},
    ):
        with pytest.raises(InvalidVoiceSettingsError):
            await service.update(owner, **bad)
    row = await service.update(
        owner, realtime_provider="openai", realtime_model="gpt-realtime-mini", realtime_credential_id=key.id
    )
    assert (row.realtime_model, row.realtime_voice) == ("gpt-realtime-mini", "marin")
    row = await service.update(owner, realtime_voice="cedar")
    assert row.realtime_voice == "cedar"


async def test_realtime_credential_must_be_openai(service, owner, session, org, adapters):
    deepgram = await credential(session, org, adapters, "deepgram")
    with pytest.raises(InvalidVoiceSettingsError):
        await service.update(
            owner, realtime_provider="openai", realtime_model="gpt-realtime", realtime_credential_id=deepgram.id
        )
    row = await service.update(owner, **FAKE_RT)
    assert row.realtime_credential_id is None  # `fake` takes no key


async def test_model_change_resets_invalid_voice(service, owner, session, org, adapters):
    key = await credential(session, org, adapters, "openai")
    await service.update(owner, **FAKE_RT, realtime_voice="fake")
    row = await service.update(
        owner, realtime_provider="openai", realtime_model="gpt-realtime", realtime_credential_id=key.id
    )
    assert row.realtime_voice == "marin"  # "fake" is not an OpenAI voice


async def test_load_setup_realtime_mode_has_no_stt_tts(session, org, bus, owner, adapters, settings, service, telephony, widget):
    key = await credential(session, org, adapters, "openai", key="oa-secret-key-8888")
    await service.update(
        owner, phone_enabled=True, mode="speech_to_speech", realtime_provider="openai", realtime_model="gpt-realtime",
        realtime_credential_id=key.id, transfer_phone_enabled=True,
    )
    # An unreadable chained-mode key must not matter in this mode.
    stale = await credential(session, org, adapters, "deepgram")
    stale.encrypted_api_key = "not-a-ciphertext"
    await service.update(owner, stt_provider="deepgram", stt_model="nova-3", stt_credential_id=stale.id)
    await connect_number(telephony, owner, widget)
    setup = await load_voice_setup(session, org.id, adapters, settings, bus)
    assert setup is not None
    assert setup.stt is None and setup.tts is None and setup.mode == "speech_to_speech"
    assert (setup.realtime.model, setup.realtime.voice, setup.realtime.api_key) == ("gpt-realtime", "marin", "oa-secret-key-8888")
    assert setup.realtime.instructions == "" and setup.realtime.tools == ()
    assert (setup.transfer_browser, setup.transfer_phone) == (True, True)
    await service.update(owner, mode="chained", tts_provider="fake", tts_model="fake-tts", stt_provider="fake", stt_model="fake-stt")
    chained = await load_voice_setup(session, org.id, adapters, settings, bus)
    assert chained is not None and chained.realtime is None and chained.mode == "chained"
    assert chained.stt is not None and chained.tts is not None


async def test_transfer_toggles_round_trip(service, owner, telephony, widget):
    row = await service.get()
    assert (row.transfer_browser_enabled, row.transfer_phone_enabled) == (True, False)
    row = await service.update(owner, transfer_browser_enabled=False)
    ready = await service.readiness()
    assert "transfer_off" in ready.reasons
    row = await service.update(owner, transfer_phone_enabled=True)
    assert (row.transfer_browser_enabled, row.transfer_phone_enabled) == (False, True)
    ready = await service.readiness()
    assert "transfer_off" not in ready.reasons and "no_caller_id_number" in ready.reasons
    with pytest.raises(InvalidVoiceSettingsError):
        await service.update(owner, transfer_phone_enabled="yes")
    await connect_number(telephony, owner, widget)
    assert "no_caller_id_number" not in (await service.readiness()).reasons
    assert len(await telephony.list_numbers()) == 1


async def test_realtime_credential_in_use_cannot_be_deleted(service, owner, session, org, adapters, bus):
    from supportcore.ai.credentials import CredentialInUseError, CredentialService

    key = await credential(session, org, adapters, "openai")
    await service.update(owner, realtime_provider="openai", realtime_model="gpt-realtime", realtime_credential_id=key.id)
    with pytest.raises(CredentialInUseError) as caught:
        await CredentialService(session, org.id, adapters.cipher, None, bus).delete(key.id, owner)
    assert "voice" in str(caught.value).lower()
