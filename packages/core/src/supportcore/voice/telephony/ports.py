"""The telephony port: one organisation's Twilio account, its numbers, browser calling and recordings."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class TelephonyAccountRef:
    account_sid: str
    auth_token: str = field(repr=False)


@dataclass(frozen=True)
class BrowserCalling:
    api_key_sid: str
    api_key_secret: str = field(repr=False)
    twiml_app_sid: str


@dataclass(frozen=True)
class AvailableNumber:
    e164: str
    locality: str | None
    region: str | None
    monthly_price: str | None  # as Twilio reports it, e.g. "1.15"
    price_unit: str | None  # e.g. "USD"


@dataclass(frozen=True)
class OwnedNumber:
    provider_sid: str
    e164: str
    voice_url: str | None


@dataclass(frozen=True)
class NumberWebhooks:
    voice_url: str
    status_callback: str


@dataclass(frozen=True)
class CallWebhooks:
    answer_url: str
    status_callback: str


class TelephonyAuthError(Exception):
    """The credentials were rejected (Twilio 401 / error 20003)."""


class TelephonyError(Exception):
    """Anything else that failed; `error_type` is one of the voice integration error types, or `not_found`
    (Twilio 404: the thing is already gone; not an integration error)."""

    def __init__(self, error_type: str, status: int | None = None) -> None:
        super().__init__(error_type)
        self.error_type = error_type
        self.status = status


class Telephony(Protocol):
    async def verify(self, account: TelephonyAccountRef) -> None: ...

    async def create_subaccount(self, master: TelephonyAccountRef, friendly_name: str) -> TelephonyAccountRef: ...

    async def close_subaccount(self, master: TelephonyAccountRef, account_sid: str) -> None: ...

    async def search_numbers(
        self, account: TelephonyAccountRef, country: str, area_code: str | None, limit: int = 20
    ) -> list[AvailableNumber]: ...

    async def buy_number(self, account: TelephonyAccountRef, e164: str, webhooks: NumberWebhooks) -> OwnedNumber: ...

    async def list_numbers(self, account: TelephonyAccountRef) -> list[OwnedNumber]: ...

    async def configure_number(
        self, account: TelephonyAccountRef, provider_sid: str, webhooks: NumberWebhooks | None
    ) -> None: ...  # None clears our URLs

    async def release_number(self, account: TelephonyAccountRef, provider_sid: str) -> None: ...

    async def ensure_browser_calling(
        self, account: TelephonyAccountRef, webhooks: NumberWebhooks, existing: BrowserCalling | None
    ) -> BrowserCalling: ...  # the TwiML App's voice URL and its call status callback

    async def create_call(
        self, account: TelephonyAccountRef, to: str, from_: str, webhooks: CallWebhooks, timeout_seconds: int
    ) -> str: ...  # the new call's sid

    async def update_call(
        self, account: TelephonyAccountRef, call_sid: str, *, twiml: str | None = None, end: bool = False
    ) -> None: ...  # exactly one of twiml / end; a call already over is not an error

    def browser_access_token(
        self, account_sid: str, calling: BrowserCalling, identity: str, ttl_seconds: int, *, incoming: bool = False
    ) -> str: ...

    def is_valid_signature(self, auth_token: str, url: str, params: Mapping[str, str], signature: str) -> bool: ...

    async def start_recording(
        self, account: TelephonyAccountRef, call_sid: str, status_callback: str
    ) -> str: ...  # recording sid

    async def download_recording(
        self, account: TelephonyAccountRef, recording_sid: str, max_bytes: int
    ) -> bytes: ...

    async def delete_recording(self, account: TelephonyAccountRef, recording_sid: str) -> None: ...
