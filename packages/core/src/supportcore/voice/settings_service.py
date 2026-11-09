"""Per-organisation voice settings, the readiness check, and `load_voice_setup` for the voice process."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.ai.credentials import CredentialService, CredentialUnreadableError
from supportcore.config import Settings
from supportcore.db.models import (
    Organisation,
    PhoneNumber,
    ProviderCredential,
    TelephonyAccount,
    User,
    VoiceSettings,
    Widget,
)
from supportcore.db.models.voice import VOICE_MODES
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import NotFoundError
from supportcore.events import EventBus
from supportcore.secrets.ports import SecretDecryptError
from supportcore.voice.catalog import (
    credential_provider_for,
    get_realtime_spec,
    get_stt_spec,
    get_tts_spec,
    realtime_models,
    stt_models,
    tts_models,
)
from supportcore.voice.errors import InvalidVoiceSettingsError
from supportcore.voice.events import VoiceSettingsUpdated
from supportcore.voice.realtime.ports import RealtimeConfig
from supportcore.voice.stt.ports import SttConfig
from supportcore.voice.telephony.ports import TelephonyAccountRef
from supportcore.voice.tts.ports import TtsConfig

if TYPE_CHECKING:
    from supportcore.wiring import Adapters

__all__ = [
    "InvalidVoiceSettingsError",
    "VoiceReadiness",
    "VoiceSettingsService",
    "VoiceSetup",
    "load_voice_setup",
]

logger = structlog.get_logger()


class _Unavailable(Exception):
    """Voice cannot be set up right now; the message is a reason code."""


_BOOLS = ("phone_enabled", "browser_calls_enabled", "record_calls", "transfer_browser_enabled", "transfer_phone_enabled")
_RANGES = {"recording_retention_days": (1, 3650), "max_call_minutes": (1, 120)}
_SIDE = {
    "stt": ("stt_provider", "stt_model", "stt_credential_id"),
    "tts": ("tts_provider", "tts_model", "tts_credential_id"),
    "realtime": ("realtime_provider", "realtime_model", "realtime_credential_id"),
}
_VOICE_FIELD = {"tts": "tts_voice", "realtime": "realtime_voice"}
_SPECS = {
    "stt": (get_stt_spec, stt_models),
    "tts": (get_tts_spec, tts_models),
    "realtime": (get_realtime_spec, realtime_models),
}
_EDITABLE = {
    *_BOOLS,
    *_RANGES,
    "mode",
    "tts_voice",
    "realtime_voice",
    "browser_widget_id",
    *_SIDE["stt"],
    *_SIDE["tts"],
    *_SIDE["realtime"],
}


@dataclass(frozen=True)
class VoiceReadiness:
    phone: bool
    browser: bool
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class VoiceSetup:
    """What a call needs. Chained mode sets `stt` and `tts`; speech-to-speech sets `realtime` (no instructions or
    tools yet: those are added per call). The other side is None."""

    stt: SttConfig | None
    tts: TtsConfig | None
    record_calls: bool
    max_call_seconds: int
    organisation_name: str
    language: str
    telephony: TelephonyAccountRef = field(repr=False)
    mode: str = "chained"
    realtime: RealtimeConfig | None = None
    transfer_browser: bool = False
    transfer_phone: bool = False


class VoiceSettingsService:
    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        events: EventBus,
        *,
        include_fake: bool = False,
        voice_public_url: str | None = None,
    ) -> None:
        self._session = session
        self._org_id = organisation_id
        self._events = events
        self._include_fake = include_fake  # the test-only `fake` providers (settings.env == "test")
        self._public_url = voice_public_url
        self._settings = TenantScopedRepository(session, VoiceSettings, organisation_id)
        self._credentials = TenantScopedRepository(session, ProviderCredential, organisation_id)

    async def get(self, *, for_update: bool = False) -> VoiceSettings:
        """The org's settings; the defaults are created on first access."""
        row = await self._load(for_update)
        if row is None:
            # ON CONFLICT DO NOTHING: concurrent first reads must not fail on the primary key.
            await self._session.execute(
                insert(VoiceSettings)
                .values(organisation_id=self._org_id)
                .on_conflict_do_nothing(index_elements=[VoiceSettings.organisation_id])
            )
            row = await self._load(for_update)
            assert row is not None
        return row

    async def language(self) -> str:
        org = (await self._session.execute(select(Organisation).where(Organisation.id == self._org_id))).scalar_one_or_none()
        if org is None:
            raise NotFoundError("Organisation not found")
        return org.language

    async def update(self, actor: User, **fields: Any) -> VoiceSettings:
        unknown = set(fields) - _EDITABLE
        if unknown:
            raise InvalidVoiceSettingsError(f"Unknown field: {min(unknown)[:50]}")
        for name in _BOOLS:
            if name in fields and not isinstance(fields[name], bool):
                raise InvalidVoiceSettingsError(f"{name} must be true or false")
        for name, (low, high) in _RANGES.items():
            if name in fields:
                value = fields[name]
                if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                    raise InvalidVoiceSettingsError(f"{name} must be {low}-{high}")
        if "mode" in fields and fields["mode"] not in VOICE_MODES:
            raise InvalidVoiceSettingsError(f"mode must be one of: {', '.join(VOICE_MODES)}")
        language = await self.language()
        row = await self.get(for_update=True)
        new: dict[str, Any] = {name: fields[name] for name in (*_BOOLS, *_RANGES, "mode") if name in fields}
        new.update(await self._speech(row, fields, language))
        if "browser_widget_id" in fields:
            widget_id = fields["browser_widget_id"]
            if widget_id is not None and await TenantScopedRepository(self._session, Widget, self._org_id).get(widget_id) is None:
                raise NotFoundError("Widget not found")
            new["browser_widget_id"] = widget_id
        changed = [name for name, value in new.items() if getattr(row, name) != value]
        for name in changed:
            setattr(row, name, new[name])
        if changed:
            await self._session.flush()
            await self._events.publish(
                VoiceSettingsUpdated(organisation_id=self._org_id, actor_user_id=actor.id, changed=tuple(changed))
            )
        return row

    async def _speech(self, row: VoiceSettings, fields: dict[str, Any], language: str) -> dict[str, Any]:
        """The validated stt_*, tts_* and realtime_* values implied by `fields` on top of the stored row."""
        out: dict[str, Any] = {}
        for side in ("stt", "tts", "realtime"):
            provider_f, model_f, credential_f = _SIDE[side]
            voice_f = _VOICE_FIELD.get(side)
            touched = any(f in fields for f in (provider_f, model_f, credential_f)) or (voice_f is not None and voice_f in fields)
            if not touched:
                continue
            provider = fields.get(provider_f, getattr(row, provider_f))
            model = fields.get(model_f, getattr(row, model_f))
            if (provider is None) != (model is None):
                raise InvalidVoiceSettingsError(f"{provider_f} and {model_f} are set together")
            credential_id = fields.get(credential_f, getattr(row, credential_f))
            if credential_f not in fields and provider != getattr(row, provider_f):
                credential_id = None  # a key for the old provider does not carry over
            voice = fields.get(voice_f, getattr(row, voice_f)) if voice_f else None
            if provider is None:
                if credential_id is not None:
                    raise InvalidVoiceSettingsError("Choose a model before a credential")
                voice = None
            else:
                spec = self._spec(side, provider, model, language)
                if not spec.requires_credential:
                    credential_id = None
                elif credential_id is not None:
                    await self._check_credential(credential_id, spec.provider)
                if voice_f:
                    if voice_f not in fields and voice not in spec.voices:
                        voice = spec.default_voice
                    if voice not in spec.voices:
                        raise InvalidVoiceSettingsError(f"Unknown voice for {spec.model}")
            out.update({provider_f: provider, model_f: model, credential_f: credential_id})
            if voice_f:
                out[voice_f] = voice
        return out

    def _spec(self, side: str, provider: Any, model: Any, language: str) -> Any:
        if not isinstance(provider, str) or not isinstance(model, str):
            raise InvalidVoiceSettingsError(f"{side}_provider and {side}_model must be text")
        getter, lister = _SPECS[side]
        try:
            spec = getter(provider, model, include_fake=self._include_fake)
        except ValueError:
            raise InvalidVoiceSettingsError(f"Unknown {side} model") from None
        if spec not in lister(language, include_fake=self._include_fake):
            raise InvalidVoiceSettingsError(f"{spec.label} does not support the organisation's language")
        return spec

    async def _check_credential(self, credential_id: Any, voice_provider: str) -> None:
        if not isinstance(credential_id, UUID):
            raise InvalidVoiceSettingsError("credential id must be a UUID")
        # FOR UPDATE pairs with CredentialService.delete (its voice_settings_in_use check) so a credential
        # cannot be deleted while it is being assigned here.
        credential = await self._credentials.get(credential_id, for_update=True)
        if credential is None:
            raise NotFoundError("Credential not found")
        if credential.provider != credential_provider_for(voice_provider):
            raise InvalidVoiceSettingsError(f"Credential is for {credential.provider}, not {voice_provider}")

    async def readiness(self, *, create: bool = True) -> VoiceReadiness:
        """With `create=False` nothing is written: no settings row yet means voice is disabled."""
        row = await self.get() if create else await self._load(False)
        if row is None:
            return VoiceReadiness(phone=False, browser=False, reasons=["voice_disabled"])
        reasons: list[str] = []

        def add(reason: str) -> None:
            if reason not in reasons:
                reasons.append(reason)

        neither = not (row.phone_enabled or row.browser_calls_enabled)
        if neither:
            add("voice_disabled")
        if row.mode == "speech_to_speech":
            if not await self._speech_ready("realtime", row):
                add("realtime_not_configured")
        else:
            if not await self._speech_ready("stt", row):
                add("stt_not_configured")
            if not await self._speech_ready("tts", row):
                add("tts_not_configured")
        if not self._public_url:
            add("public_url_missing")
        account = (await self._session.execute(TenantScopedRepository(self._session, TelephonyAccount, self._org_id).select())).scalar_one_or_none()
        phone_wanted = row.phone_enabled or neither
        browser_wanted = row.browser_calls_enabled or neither
        phone_ok = browser_ok = False
        if account is None:
            add("telephony_not_connected")
        elif account.status != "active":
            add("telephony_auth_failed")
        else:
            numbers = await self._enabled_number_count()
            if phone_wanted and numbers == 0:
                add("no_numbers")
            if browser_wanted and not (account.api_key_sid and account.api_key_secret_encrypted and account.twiml_app_sid):
                add("browser_calling_not_set_up")
            phone_ok = numbers > 0
            browser_ok = bool(account.api_key_sid and account.api_key_secret_encrypted and account.twiml_app_sid)
        # Informational only: neither changes whether the phone or the browser can take calls.
        if not (row.transfer_browser_enabled or row.transfer_phone_enabled):
            add("transfer_off")
        if row.transfer_phone_enabled and await self._enabled_number_count() == 0:
            add("no_caller_id_number")
        base = not {"stt_not_configured", "tts_not_configured", "realtime_not_configured", "public_url_missing"} & set(reasons)
        return VoiceReadiness(
            phone=base and row.phone_enabled and phone_ok,
            browser=base and row.browser_calls_enabled and browser_ok,
            reasons=reasons,
        )

    async def _enabled_number_count(self) -> int:
        stmt = TenantScopedRepository(self._session, PhoneNumber, self._org_id).select(PhoneNumber.id).where(PhoneNumber.enabled.is_(True))
        return len((await self._session.execute(stmt)).all())

    async def _speech_ready(self, side: str, row: VoiceSettings) -> bool:
        provider_f, model_f, credential_f = _SIDE[side]
        provider, model, credential_id = getattr(row, provider_f), getattr(row, model_f), getattr(row, credential_f)
        if not provider or not model:
            return False
        try:
            spec = _SPECS[side][0](provider, model, include_fake=self._include_fake)
        except ValueError:
            return False
        if not spec.requires_credential:
            return True
        if credential_id is None:
            return False
        credential = await self._credentials.get(credential_id)
        return credential is not None and credential.provider == credential_provider_for(provider)

    async def _load(self, for_update: bool) -> VoiceSettings | None:
        stmt = self._settings.select(for_update=for_update)
        if for_update:
            stmt = stmt.execution_options(populate_existing=True)  # refresh after acquiring the lock
        return (await self._session.execute(stmt)).scalar_one_or_none()


async def load_voice_setup(
    session: AsyncSession,
    organisation_id: UUID,
    adapters: "Adapters",
    settings: Settings,
    events: EventBus,
    *,
    readiness: VoiceReadiness | None = None,
) -> VoiceSetup | None:
    """Database reads only (decrypts the speech keys and the Twilio token). None when voice is not ready.
    A caller that just computed `readiness` passes it in to skip the second computation."""
    service = VoiceSettingsService(
        session,
        organisation_id,
        events,
        include_fake=settings.env == "test",
        voice_public_url=settings.voice_public_url,
    )
    readiness = readiness or await service.readiness()
    if not (readiness.phone or readiness.browser):
        return None
    row = await service.get()
    organisation = (
        await session.execute(select(Organisation).where(Organisation.id == organisation_id))
    ).scalar_one_or_none()
    account = (
        await session.execute(TenantScopedRepository(session, TelephonyAccount, organisation_id).select())
    ).scalar_one_or_none()
    if organisation is None or account is None:
        return None
    credentials = CredentialService(
        session, organisation_id, adapters.cipher, adapters.credential_validator, events
    )

    async def key(credential_id: UUID | None) -> str | None:
        if credential_id is None:
            return None
        credential = await TenantScopedRepository(session, ProviderCredential, organisation_id).get(credential_id)
        if credential is None:  # readiness just saw it; deleted in between
            raise _Unavailable("credential_missing")
        try:
            return credentials.decrypt_key(credential)
        except CredentialUnreadableError:
            raise _Unavailable("credential_unreadable") from None

    try:
        try:
            token = adapters.cipher.decrypt(account.auth_token_encrypted)
        except SecretDecryptError:
            raise _Unavailable("telephony_token_unreadable") from None
        chained = row.mode != "speech_to_speech"
        stt_key = await key(row.stt_credential_id) if chained else None
        tts_key = await key(row.tts_credential_id) if chained else None
        realtime_key = None if chained else await key(row.realtime_credential_id)
    except _Unavailable as reason:
        logger.warning("voice_setup_unavailable", organisation_id=str(organisation_id), reason=str(reason))
        return None
    stt = tts = realtime = None
    if chained:
        stt = SttConfig(provider=row.stt_provider, model=row.stt_model, language=organisation.language, api_key=stt_key)
        tts = TtsConfig(
            provider=row.tts_provider,
            model=row.tts_model,
            voice=row.tts_voice,
            language=organisation.language,
            api_key=tts_key,
        )
    else:
        realtime = RealtimeConfig(
            provider=row.realtime_provider,
            model=row.realtime_model,
            voice=row.realtime_voice,
            language=organisation.language,
            instructions="",
            api_key=realtime_key,
        )
    return VoiceSetup(
        stt=stt,
        tts=tts,
        record_calls=row.record_calls,
        max_call_seconds=row.max_call_minutes * 60,
        organisation_name=organisation.name,
        language=organisation.language,
        telephony=TelephonyAccountRef(account_sid=account.account_sid, auth_token=token),
        mode=row.mode,
        realtime=realtime,
        transfer_browser=row.transfer_browser_enabled,
        transfer_phone=row.transfer_phone_enabled,
    )
