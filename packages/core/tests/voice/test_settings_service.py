import uuid

import pytest
from sqlalchemy import select
from supportcore.db.models import (
    AuditLog,
    PhoneNumber,
    ProviderCredential,
    VoiceSettings,
)
from supportcore.errors import NotFoundError
from supportcore.voice.errors import InvalidVoiceSettingsError
from supportcore.voice.numbers_service import TelephonyService
from supportcore.voice.settings_service import (
    VoiceReadiness,
    VoiceSettingsService,
    load_voice_setup,
)

from voice.conftest import BYO_SID, BYO_TOKEN, PUBLIC_URL

FAKE = {
    "stt_provider": "fake", "stt_model": "fake-stt", "tts_provider": "fake", "tts_model": "fake-tts", "tts_voice": "fake",
}


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


async def test_get_creates_defaults_lazily(service, session):
    row = await service.get()
    assert (row.phone_enabled, row.browser_calls_enabled, row.mode, row.record_calls) == (False, False, "chained", False)
    assert (row.recording_retention_days, row.max_call_minutes, row.stt_model) == (90, 30, None)
    assert (await service.get()).organisation_id == row.organisation_id
    assert len((await session.execute(select(VoiceSettings))).scalars().all()) == 1


async def test_update_stores_and_audits(service, owner, session, org, adapters):
    stt = await credential(session, org, adapters, "deepgram")
    tts = await credential(session, org, adapters, "openai")
    row = await service.update(
        owner, phone_enabled=True, stt_provider="deepgram", stt_model="nova-3", stt_credential_id=stt.id,
        tts_provider="openai", tts_model="gpt-4o-mini-tts", tts_credential_id=tts.id, record_calls=True,
        recording_retention_days=30, max_call_minutes=10,
    )
    assert (row.phone_enabled, row.tts_voice, row.recording_retention_days, row.max_call_minutes) == (True, "coral", 30, 10)
    audit = (await session.execute(select(AuditLog).where(AuditLog.action == "voice_settings.updated"))).scalar_one()
    assert "phone_enabled" in audit.metadata_["changed"] and "sk-secret" not in repr(audit.metadata_)
    await service.update(owner, phone_enabled=True)  # no change: no second event
    assert len((await session.execute(select(AuditLog).where(AuditLog.action == "voice_settings.updated"))).scalars().all()) == 1


async def test_settings_reject_model_for_other_language(service, owner, session, org, adapters):
    org.language = "zh"
    await session.flush()
    stt = await credential(session, org, adapters, "deepgram")
    with pytest.raises(InvalidVoiceSettingsError):
        await service.update(owner, stt_provider="deepgram", stt_model="nova-3", stt_credential_id=stt.id)


async def test_settings_reject_credential_of_wrong_provider(service, owner, session, org, adapters):
    openai = await credential(session, org, adapters, "openai")
    with pytest.raises(InvalidVoiceSettingsError):
        await service.update(owner, stt_provider="deepgram", stt_model="nova-3", stt_credential_id=openai.id)
    with pytest.raises(NotFoundError):
        await service.update(owner, stt_provider="deepgram", stt_model="nova-3", stt_credential_id=uuid.uuid4())


@pytest.mark.parametrize(
    "fields",
    [
        {"stt_provider": "deepgram", "stt_model": "nope"},
        {"stt_provider": "deepgram"},
        {"tts_provider": "openai", "tts_model": "tts-1", "tts_voice": "not-a-voice"},
        {"recording_retention_days": 0},
        {"recording_retention_days": 3651},
        {"max_call_minutes": 121},
        {"max_call_minutes": True},
        {"phone_enabled": "yes"},
        {"surprise": 1},
    ],
)
async def test_settings_reject_invalid_values(service, owner, fields):
    with pytest.raises(InvalidVoiceSettingsError):
        await service.update(owner, **fields)


async def test_fake_models_only_when_allowed(session, org, bus, owner):
    strict = VoiceSettingsService(session, org.id, bus, include_fake=False)
    with pytest.raises(InvalidVoiceSettingsError):
        await strict.update(owner, stt_provider="fake", stt_model="fake-stt")


async def test_browser_widget_must_belong_to_org(service, owner, widget):
    row = await service.update(owner, browser_widget_id=widget.id)
    assert row.browser_widget_id == widget.id
    with pytest.raises(NotFoundError):
        await service.update(owner, browser_widget_id=uuid.uuid4())


async def test_readiness_reasons(service, owner, telephony, widget, session):
    r = await service.readiness()
    assert isinstance(r, VoiceReadiness) and (r.phone, r.browser) == (False, False)
    assert "voice_disabled" in r.reasons and "stt_not_configured" in r.reasons and "tts_not_configured" in r.reasons
    assert "telephony_not_connected" in r.reasons

    await service.update(owner, phone_enabled=True, browser_calls_enabled=True, **FAKE)
    r = await service.readiness()
    assert r.reasons == ["telephony_not_connected"] and (r.phone, r.browser) == (False, False)

    await telephony.connect_byo(BYO_SID, BYO_TOKEN, owner)
    r = await service.readiness()
    assert r.reasons == ["no_numbers"] and (r.phone, r.browser) == (False, True)

    sid = (await telephony.list_account_numbers())[0].provider_sid
    number = await telephony.connect_number(sid, widget.id, owner)
    r = await service.readiness()
    assert r.reasons == [] and (r.phone, r.browser) == (True, True)

    await telephony.update_number(number.id, enabled=False)
    assert (await service.readiness()).reasons == ["no_numbers"]
    await telephony.update_number(number.id, enabled=True)

    account = await telephony.get_account()
    account.status = "auth_failed"
    await session.flush()
    r = await service.readiness()
    assert r.reasons == ["telephony_auth_failed"] and (r.phone, r.browser) == (False, False)
    account.status = "active"
    account.twiml_app_sid = None
    await session.flush()
    r = await service.readiness()
    assert r.reasons == ["browser_calling_not_set_up"] and (r.phone, r.browser) == (True, False)

    no_url = VoiceSettingsService(session, service._org_id, service._events, include_fake=True)
    assert "public_url_missing" in (await no_url.readiness()).reasons


async def test_readiness_needs_credentials_for_real_models(service, owner, session, org, adapters):
    await service.update(owner, phone_enabled=True, stt_provider="deepgram", stt_model="nova-3",
                         tts_provider="openai", tts_model="tts-1")
    reasons = (await service.readiness()).reasons
    assert "stt_not_configured" in reasons and "tts_not_configured" in reasons


async def test_load_voice_setup(session, org, bus, owner, adapters, settings, telephony, widget):
    service = VoiceSettingsService(session, org.id, bus, include_fake=True, voice_public_url=PUBLIC_URL)
    assert await load_voice_setup(session, org.id, adapters, settings, bus) is None
    stt = await credential(session, org, adapters, "deepgram", key="dg-secret-key-9999")
    tts = await credential(session, org, adapters, "openai", key="oa-secret-key-8888")
    await service.update(
        owner, phone_enabled=True, stt_provider="deepgram", stt_model="nova-3", stt_credential_id=stt.id,
        tts_provider="openai", tts_model="gpt-4o-mini-tts", tts_voice="sage", tts_credential_id=tts.id,
        record_calls=True, max_call_minutes=5,
    )
    assert await load_voice_setup(session, org.id, adapters, settings, bus) is None  # no telephony yet
    await telephony.connect_byo(BYO_SID, BYO_TOKEN, owner)
    sid = (await telephony.list_account_numbers())[0].provider_sid
    await telephony.connect_number(sid, widget.id, owner)
    setup = await load_voice_setup(session, org.id, adapters, settings, bus)
    assert setup is not None
    assert (setup.stt.provider, setup.stt.model, setup.stt.api_key, setup.stt.language) == ("deepgram", "nova-3", "dg-secret-key-9999", "en")
    assert (setup.tts.model, setup.tts.voice, setup.tts.api_key) == ("gpt-4o-mini-tts", "sage", "oa-secret-key-8888")
    assert (setup.record_calls, setup.max_call_seconds, setup.organisation_name, setup.language) == (True, 300, "Acme", "en")
    assert (setup.telephony.account_sid, setup.telephony.auth_token) == (BYO_SID, BYO_TOKEN)
    text = repr(setup)
    for secret in ("dg-secret", "oa-secret", BYO_TOKEN):
        assert secret not in text


async def test_load_voice_setup_with_fake_models_needs_no_keys(session, org, bus, owner, adapters, settings, telephony, widget):
    service = VoiceSettingsService(session, org.id, bus, include_fake=True, voice_public_url=PUBLIC_URL)
    await service.update(owner, phone_enabled=True, **FAKE)
    await telephony.connect_byo(BYO_SID, BYO_TOKEN, owner)
    sid = (await telephony.list_account_numbers())[0].provider_sid
    await telephony.connect_number(sid, widget.id, owner)
    setup = await load_voice_setup(session, org.id, adapters, settings, bus)
    assert setup is not None and setup.stt.api_key is None
    numbers = (await session.execute(select(PhoneNumber))).scalars().all()
    assert len(numbers) == 1


async def _ready_with_keys(session, org, bus, owner, adapters, telephony, widget):
    service = VoiceSettingsService(session, org.id, bus, include_fake=True, voice_public_url=PUBLIC_URL)
    stt = await credential(session, org, adapters, "deepgram", key="dg-secret-key-9999")
    tts = await credential(session, org, adapters, "openai", key="oa-secret-key-8888")
    await service.update(
        owner, phone_enabled=True, stt_provider="deepgram", stt_model="nova-3", stt_credential_id=stt.id,
        tts_provider="openai", tts_model="tts-1", tts_credential_id=tts.id,
    )
    await telephony.connect_byo(BYO_SID, BYO_TOKEN, owner)
    sid = (await telephony.list_account_numbers())[0].provider_sid
    await telephony.connect_number(sid, widget.id, owner)
    return stt, tts


async def test_load_voice_setup_none_when_credential_deleted(session, org, bus, owner, adapters, settings, telephony, widget):
    stt, _ = await _ready_with_keys(session, org, bus, owner, adapters, telephony, widget)
    assert await load_voice_setup(session, org.id, adapters, settings, bus) is not None
    await session.delete(stt)
    await session.flush()
    await session.refresh(await VoiceSettingsService(session, org.id, bus).get())  # the FK set it to NULL
    assert await load_voice_setup(session, org.id, adapters, settings, bus) is None


async def test_load_voice_setup_none_when_credential_deleted_after_readiness(
    session, org, bus, owner, adapters, settings, telephony, widget, monkeypatch
):
    from supportcore.db.tenancy import TenantScopedRepository

    await _ready_with_keys(session, org, bus, owner, adapters, telephony, widget)
    original = TenantScopedRepository.get

    async def get(self, id, **kwargs):  # the key is gone by the time it is fetched
        return None if self.model is ProviderCredential else await original(self, id, **kwargs)

    monkeypatch.setattr(VoiceSettingsService, "_speech_ready", lambda self, side, row: _true())
    monkeypatch.setattr(TenantScopedRepository, "get", get)
    assert await load_voice_setup(session, org.id, adapters, settings, bus) is None


async def _true():
    return True


@pytest.mark.parametrize("which", ["stt", "tts", "twilio"])
async def test_load_voice_setup_none_when_a_secret_cannot_be_decrypted(
    session, org, bus, owner, adapters, settings, telephony, widget, which
):
    stt, tts = await _ready_with_keys(session, org, bus, owner, adapters, telephony, widget)
    if which == "stt":
        stt.encrypted_api_key = "not-a-ciphertext"
    elif which == "tts":
        tts.encrypted_api_key = "not-a-ciphertext"
    else:
        (await telephony.get_account()).auth_token_encrypted = "not-a-ciphertext"
    await session.flush()
    assert await load_voice_setup(session, org.id, adapters, settings, bus) is None


async def test_credential_used_by_voice_settings_cannot_be_deleted(session, org, bus, owner, adapters, telephony, widget):
    from supportcore.ai.credentials import CredentialInUseError, CredentialService

    stt, _ = await _ready_with_keys(session, org, bus, owner, adapters, telephony, widget)
    service = CredentialService(session, org.id, adapters.cipher, None, bus)
    with pytest.raises(CredentialInUseError) as caught:
        await service.delete(stt.id, owner)
    assert "voice" in str(caught.value).lower()
