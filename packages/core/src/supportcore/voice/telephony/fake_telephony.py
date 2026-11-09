"""In-memory Telephony for tests (registered only when env == "test")."""

import re
import secrets
import uuid
from collections.abc import Mapping
from typing import Any

from supportcore.voice.telephony import signature as twilio_signature
from supportcore.voice.telephony.ports import (
    AvailableNumber,
    BrowserCalling,
    CallWebhooks,
    NumberWebhooks,
    OwnedNumber,
    TelephonyAccountRef,
    TelephonyAuthError,
    TelephonyError,
)

_ACCOUNT_SID = re.compile(r"^AC[0-9a-fA-F]{32}$")
BAD_TOKEN_PREFIX = "bad-token"
DEFAULT_NUMBER = OwnedNumber(provider_sid="PN" + "0" * 32, e164="+15005550006", voice_url=None)


class FakeTelephony:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.numbers: dict[str, list[OwnedNumber]] = {}  # account sid -> numbers bought through this fake
        self.webhooks: dict[str, NumberWebhooks | None] = {}  # provider sid -> what we configured
        self.recordings: dict[str, bytes] = {}
        self.browser_webhooks: dict[str, NumberWebhooks] = {}  # account sid -> the TwiML App's URLs
        self.created_sids: list[str | None] = []  # per create_call, in order: the sid returned (None: refused)

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))

    def _check(self, account: TelephonyAccountRef) -> None:
        if account.auth_token.startswith(BAD_TOKEN_PREFIX):
            raise TelephonyAuthError("credentials rejected")

    async def verify(self, account: TelephonyAccountRef) -> None:
        self._record("verify", account.account_sid)
        self._check(account)
        if not _ACCOUNT_SID.match(account.account_sid):
            raise TelephonyAuthError("credentials rejected")

    async def create_subaccount(self, master: TelephonyAccountRef, friendly_name: str) -> TelephonyAccountRef:
        self._record("create_subaccount", master.account_sid, friendly_name)
        self._check(master)
        return TelephonyAccountRef(account_sid="AC" + secrets.token_hex(16), auth_token=secrets.token_hex(16))

    async def close_subaccount(self, master: TelephonyAccountRef, account_sid: str) -> None:
        self._record("close_subaccount", master.account_sid, account_sid)
        self._check(master)

    async def search_numbers(
        self, account: TelephonyAccountRef, country: str, area_code: str | None, limit: int = 20
    ) -> list[AvailableNumber]:
        self._record("search_numbers", account.account_sid, country, area_code, limit)
        self._check(account)
        return [AvailableNumber("+15005550007", "Fakeville", "CA", "1.15", "USD")][:limit]

    async def buy_number(self, account: TelephonyAccountRef, e164: str, webhooks: NumberWebhooks) -> OwnedNumber:
        self._record("buy_number", account.account_sid, e164, webhooks)
        self._check(account)
        owned = OwnedNumber(provider_sid="PN" + secrets.token_hex(16), e164=e164, voice_url=webhooks.voice_url)
        self.numbers.setdefault(account.account_sid, []).append(owned)
        self.webhooks[owned.provider_sid] = webhooks
        return owned

    async def list_numbers(self, account: TelephonyAccountRef) -> list[OwnedNumber]:
        self._record("list_numbers", account.account_sid)
        self._check(account)
        return [DEFAULT_NUMBER, *self.numbers.get(account.account_sid, [])]

    async def configure_number(
        self, account: TelephonyAccountRef, provider_sid: str, webhooks: NumberWebhooks | None
    ) -> None:
        self._record("configure_number", account.account_sid, provider_sid, webhooks)
        self._check(account)
        self.webhooks[provider_sid] = webhooks

    async def release_number(self, account: TelephonyAccountRef, provider_sid: str) -> None:
        self._record("release_number", account.account_sid, provider_sid)
        self._check(account)
        self.numbers[account.account_sid] = [
            n for n in self.numbers.get(account.account_sid, []) if n.provider_sid != provider_sid
        ]
        self.webhooks.pop(provider_sid, None)

    async def ensure_browser_calling(
        self, account: TelephonyAccountRef, webhooks: NumberWebhooks, existing: BrowserCalling | None
    ) -> BrowserCalling:
        self._record("ensure_browser_calling", account.account_sid, webhooks.voice_url, webhooks.status_callback)
        self._check(account)
        self.browser_webhooks[account.account_sid] = webhooks  # what the TwiML App points at
        if existing is not None:
            return existing
        return BrowserCalling(
            api_key_sid="SK" + secrets.token_hex(16),
            api_key_secret=secrets.token_hex(16),
            twiml_app_sid="AP" + secrets.token_hex(16),
        )

    async def create_call(
        self, account: TelephonyAccountRef, to: str, from_: str, webhooks: CallWebhooks, timeout_seconds: int
    ) -> str:
        self._record("create_call", account.account_sid, to, from_, webhooks, timeout_seconds)
        self.created_sids.append(None)
        self._check(account)
        if "refuse" in to:
            raise TelephonyError("connection_failed")
        sid = "CA" + uuid.uuid4().hex
        self.created_sids[-1] = sid
        return sid

    async def update_call(
        self, account: TelephonyAccountRef, call_sid: str, *, twiml: str | None = None, end: bool = False
    ) -> None:
        if (twiml is None) == (not end):
            raise ValueError("update_call takes exactly one of twiml / end")
        self._record("update_call", account.account_sid, call_sid, twiml, end)
        self._check(account)

    def recorded_calls(self) -> dict[str, list[dict[str, Any]]]:
        """What the calls API was asked to do, for the test-only read-back route: each `create_call` (who was
        dialled, from where, its webhooks and the sid returned) and each `update_call` (the TwiML given, or end)."""
        created = [args for name, args in self.calls if name == "create_call"]
        updated = [args for name, args in self.calls if name == "update_call"]
        return {
            "create_call": [
                {
                    "account_sid": account_sid,
                    "to": to,
                    "from": from_,
                    "answer_url": webhooks.answer_url,
                    "status_callback": webhooks.status_callback,
                    "sid": sid,
                }
                for (account_sid, to, from_, webhooks, _timeout), sid in zip(created, self.created_sids, strict=False)
            ],
            "update_call": [
                {"account_sid": account_sid, "call_sid": call_sid, "twiml": twiml, "end": end}
                for account_sid, call_sid, twiml, end in updated
            ],
        }

    def browser_access_token(
        self, account_sid: str, calling: BrowserCalling, identity: str, ttl_seconds: int, *, incoming: bool = False
    ) -> str:
        self._record("browser_access_token", account_sid, identity, ttl_seconds, incoming)
        return f"fake-token:{identity}" + (":in" if incoming else "")

    def is_valid_signature(self, auth_token: str, url: str, params: Mapping[str, str], signature: str) -> bool:
        return twilio_signature.is_valid_signature(auth_token, url, params, signature)

    async def start_recording(self, account: TelephonyAccountRef, call_sid: str, status_callback: str) -> str:
        self._record("start_recording", account.account_sid, call_sid, status_callback)
        self._check(account)
        return "RE" + secrets.token_hex(16)

    async def download_recording(self, account: TelephonyAccountRef, recording_sid: str, max_bytes: int) -> bytes:
        self._record("download_recording", account.account_sid, recording_sid, max_bytes)
        self._check(account)
        return self.recordings.get(recording_sid, b"FAKE-MP3")[:max_bytes]

    async def delete_recording(self, account: TelephonyAccountRef, recording_sid: str) -> None:
        self._record("delete_recording", account.account_sid, recording_sid)
        self._check(account)
        self.recordings.pop(recording_sid, None)
