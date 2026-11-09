from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


# Bounds here only limit request size; the services validate (with the stable invalid_voice_settings code).
class VoiceSettingsOut(BaseModel):
    phone_enabled: bool
    browser_calls_enabled: bool
    mode: str
    stt_provider: str | None
    stt_model: str | None
    stt_credential_id: UUID | None
    tts_provider: str | None
    tts_model: str | None
    tts_voice: str | None
    tts_credential_id: UUID | None
    record_calls: bool
    recording_retention_days: int
    max_call_minutes: int
    browser_widget_id: UUID | None
    realtime_provider: str | None
    realtime_model: str | None
    realtime_voice: str | None
    realtime_credential_id: UUID | None
    transfer_browser_enabled: bool
    transfer_phone_enabled: bool


class UpdateVoiceSettingsRequest(BaseModel):
    """Only the fields sent are changed; null clears a nullable field."""

    phone_enabled: bool | None = None
    browser_calls_enabled: bool | None = None
    mode: str | None = Field(default=None, max_length=24)
    stt_provider: str | None = Field(default=None, max_length=64)
    stt_model: str | None = Field(default=None, max_length=255)
    stt_credential_id: UUID | None = None
    tts_provider: str | None = Field(default=None, max_length=64)
    tts_model: str | None = Field(default=None, max_length=255)
    tts_voice: str | None = Field(default=None, max_length=64)
    tts_credential_id: UUID | None = None
    record_calls: bool | None = None
    recording_retention_days: int | None = None
    max_call_minutes: int | None = None
    browser_widget_id: UUID | None = None
    realtime_provider: str | None = Field(default=None, max_length=32)
    realtime_model: str | None = Field(default=None, max_length=64)
    realtime_voice: str | None = Field(default=None, max_length=32)
    realtime_credential_id: UUID | None = None
    transfer_browser_enabled: bool | None = None
    transfer_phone_enabled: bool | None = None


class VoiceReadinessOut(BaseModel):
    phone: bool
    browser: bool
    reasons: list[str]


class SttModelOut(BaseModel):
    provider: str
    model: str
    label: str
    recommended: bool
    requires_credential: bool
    credential_provider: str | None


class TtsModelOut(SttModelOut):
    voices: list[str]
    default_voice: str


class VoiceCatalogOut(BaseModel):
    language: str
    stt: list[SttModelOut]
    tts: list[TtsModelOut]
    realtime: list[TtsModelOut]


class ConnectTelephonyRequest(BaseModel):
    """Bring-your-own Twilio credentials. The token is write-only: no response carries it."""

    account_sid: str = Field(max_length=100)
    auth_token: str = Field(max_length=200, repr=False)


class TelephonyNotConnectedOut(BaseModel):
    connected: Literal[False] = False
    platform_available: bool
    public_url_set: bool


class TelephonyOut(BaseModel):
    connected: Literal[True] = True
    platform_available: bool
    public_url_set: bool
    kind: str
    account_sid: str
    auth_token_last4: str
    status: str
    last_error_type: str | None
    browser_calling_ready: bool


class PhoneNumberOut(BaseModel):
    id: UUID
    e164: str
    label: str | None
    widget_id: UUID | None
    enabled: bool
    ownership: str
    webhooks_out_of_date: bool


class AvailableNumberOut(BaseModel):
    e164: str
    locality: str | None
    region: str | None
    monthly_price: str | None
    price_unit: str | None


class AccountNumberOut(BaseModel):
    provider_sid: str
    e164: str
    connected: bool


class BuyNumberRequest(BaseModel):
    e164: str = Field(max_length=32)
    widget_id: UUID | None = None


class ConnectNumberRequest(BaseModel):
    provider_sid: str = Field(max_length=64)
    widget_id: UUID | None = None


class UpdateNumberRequest(BaseModel):
    label: str | None = Field(default=None, max_length=500)
    widget_id: UUID | None = None
    enabled: bool | None = None


class SyncWebhooksOut(BaseModel):
    updated: int


