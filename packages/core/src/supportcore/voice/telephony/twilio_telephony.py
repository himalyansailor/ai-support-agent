"""The real Twilio adapter behind the Telephony port.

Every blocking SDK call runs in a worker thread with a 15 s timeout. Phone numbers, tokens, secrets and recording
URLs are never logged; error types and HTTP statuses are all that leave this module.
"""

import asyncio
import json
from collections.abc import Callable, Mapping
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx
from requests.exceptions import Timeout as RequestsTimeout
from twilio.base.exceptions import (
    TwilioException,
    TwilioRestException,
    TwilioServiceException,
)
from twilio.http import HttpClient
from twilio.http.http_client import TwilioHttpClient
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from twilio.rest import Client

from supportcore.config import Settings
from supportcore.voice.telephony import signature
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

T = TypeVar("T")
CALL_TIMEOUT_SECONDS = 15
SDK_HTTP_TIMEOUT_SECONDS = 14  # just under the asyncio bound, so an abandoned worker thread ends on its own
PRICING_TIMEOUT_SECONDS = 5
DOWNLOAD_TOTAL_SECONDS = 120
MAX_REDIRECTS = 3
BROWSER_RESOURCE_NAME = "support-browser-calls"
API_HOST = "api.twilio.com"
_AUTH_CODE = 20003
_NOT_FOUND_CODE = 20404
_UNAVAILABLE_CODES = {21422, 21452}
_CALL_NOT_IN_PROGRESS_CODE = 21220  # updating a call that has already ended


def _rest_error(exc: BaseException) -> TwilioRestException | None:
    """List and page calls raise a bare TwilioException that carries the response; give it the REST shape."""
    if isinstance(exc, TwilioRestException):
        return exc
    if isinstance(exc, TwilioServiceException):
        status, code = getattr(exc, "status", None), getattr(exc, "code", None)
        if isinstance(status, int):
            return TwilioRestException(status, "", code=code if isinstance(code, int) else status)
        return None
    response = exc.args[1] if isinstance(exc, TwilioException) and len(exc.args) > 1 else None
    status = getattr(response, "status_code", None)
    if not isinstance(status, int):
        return None
    try:
        code = json.loads(response.text).get("code")
    except (ValueError, AttributeError):
        code = None
    return TwilioRestException(status, "", code=code if isinstance(code, int) else status)


def _map_error(exc: BaseException) -> Exception:
    if isinstance(exc, (TelephonyAuthError, TelephonyError)):
        return exc
    if isinstance(exc, (TimeoutError, RequestsTimeout)):
        return TelephonyError("timeout")
    if (rest := _rest_error(exc)) is not None:
        exc = rest
        if exc.status == 401 or exc.code == _AUTH_CODE:
            return TelephonyAuthError("credentials rejected")
        if exc.code in _UNAVAILABLE_CODES:
            return TelephonyError("number_unavailable", exc.status)
        if exc.status == 404 or exc.code == _NOT_FOUND_CODE:
            return TelephonyError("not_found", 404)
        return TelephonyError("connection_failed", exc.status)
    return TelephonyError("connection_failed")


class TwilioTelephony:
    """A call that raises `timeout` may still have taken effect at Twilio (e.g. a purchase); reconcile with
    `list_numbers`."""

    def __init__(self, settings: Settings, *, http_client: HttpClient | None = None) -> None:
        self._settings = settings
        # The SDK's own timeout lets a worker thread abandoned by wait_for end instead of piling up.
        self._http_client = http_client or TwilioHttpClient(timeout=SDK_HTTP_TIMEOUT_SECONDS)
        self._download_transport: httpx.AsyncBaseTransport | None = None  # tests swap in a mock transport

    def _client(self, account: TelephonyAccountRef) -> Client:
        return Client(account.account_sid, account.auth_token, http_client=self._http_client)

    async def _run(self, fn: Callable[[], T]) -> T:
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn), CALL_TIMEOUT_SECONDS)
        except Exception as exc:
            mapped = _map_error(exc)
            if mapped is exc:
                raise
            raise mapped from None

    async def verify(self, account: TelephonyAccountRef) -> None:
        await self._run(lambda: self._client(account).api.v2010.accounts(account.account_sid).fetch())

    async def create_subaccount(self, master: TelephonyAccountRef, friendly_name: str) -> TelephonyAccountRef:
        created = await self._run(lambda: self._client(master).api.v2010.accounts.create(friendly_name=friendly_name))
        return TelephonyAccountRef(account_sid=created.sid, auth_token=created.auth_token)

    async def close_subaccount(self, master: TelephonyAccountRef, account_sid: str) -> None:
        await self._run(lambda: self._client(master).api.v2010.accounts(account_sid).update(status="closed"))

    async def search_numbers(
        self, account: TelephonyAccountRef, country: str, area_code: str | None, limit: int = 20
    ) -> list[AvailableNumber]:
        def search() -> list:
            kwargs: dict[str, Any] = {"limit": limit, "voice_enabled": True}
            if area_code:
                kwargs["area_code"] = area_code
            return self._client(account).available_phone_numbers(country).local.list(**kwargs)

        found = await self._run(search)
        price, unit = await self._local_price(account, country) if found else (None, None)
        return [
            AvailableNumber(
                e164=n.phone_number,
                locality=n.locality,
                region=n.region,
                monthly_price=str(price) if price is not None else None,
                price_unit=unit,
            )
            for n in found
        ]

    async def _local_price(self, account: TelephonyAccountRef, country: str) -> tuple[Any, str | None]:
        """The country's local-number price; informational, so any failure or slowness just means no price."""

        def fetch() -> tuple[Any, str | None]:
            prices = self._client(account).pricing.v1.phone_numbers.countries(country).fetch()
            local = next((p for p in prices.phone_number_prices if p.get("number_type") == "local"), None)
            if not local:
                return None, None
            return local.get("current_price") or local.get("base_price"), prices.price_unit

        try:
            return await asyncio.wait_for(asyncio.to_thread(fetch), PRICING_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001
            return None, None

    async def buy_number(self, account: TelephonyAccountRef, e164: str, webhooks: NumberWebhooks) -> OwnedNumber:
        created = await self._run(
            lambda: self._client(account).incoming_phone_numbers.create(
                phone_number=e164,
                voice_url=webhooks.voice_url,
                voice_method="POST",
                status_callback=webhooks.status_callback,
                status_callback_method="POST",
            )
        )
        return OwnedNumber(provider_sid=created.sid, e164=created.phone_number, voice_url=created.voice_url or None)

    async def list_numbers(self, account: TelephonyAccountRef) -> list[OwnedNumber]:
        numbers = await self._run(lambda: self._client(account).incoming_phone_numbers.list())
        return [OwnedNumber(provider_sid=n.sid, e164=n.phone_number, voice_url=n.voice_url or None) for n in numbers]

    async def configure_number(
        self, account: TelephonyAccountRef, provider_sid: str, webhooks: NumberWebhooks | None
    ) -> None:
        voice_url = webhooks.voice_url if webhooks else ""
        status_callback = webhooks.status_callback if webhooks else ""
        await self._run(
            lambda: self._client(account)
            .incoming_phone_numbers(provider_sid)
            .update(
                voice_url=voice_url,
                voice_method="POST",
                status_callback=status_callback,
                status_callback_method="POST",
            )
        )

    async def release_number(self, account: TelephonyAccountRef, provider_sid: str) -> None:
        await self._run(lambda: self._client(account).incoming_phone_numbers(provider_sid).delete())

    async def ensure_browser_calling(
        self, account: TelephonyAccountRef, webhooks: NumberWebhooks, existing: BrowserCalling | None
    ) -> BrowserCalling:
        # A call placed from the Voice SDK through this App reports its status only to the App's status callback.
        urls = {
            "voice_url": webhooks.voice_url,
            "voice_method": "POST",
            "status_callback": webhooks.status_callback,
            "status_callback_method": "POST",
        }

        def ensure() -> BrowserCalling:
            client = self._client(account)
            if existing is not None:
                client.applications(existing.twiml_app_sid).update(**urls)
                return existing
            app = client.applications.create(friendly_name=BROWSER_RESOURCE_NAME, **urls)
            try:
                key = client.new_keys.create(friendly_name=BROWSER_RESOURCE_NAME)
            except Exception:
                try:  # do not leave an orphan app behind for the retry to duplicate
                    client.applications(app.sid).delete()
                except Exception:  # noqa: BLE001, S110
                    pass
                raise
            return BrowserCalling(api_key_sid=key.sid, api_key_secret=key.secret, twiml_app_sid=app.sid)

        return await self._run(ensure)

    async def create_call(
        self, account: TelephonyAccountRef, to: str, from_: str, webhooks: CallWebhooks, timeout_seconds: int
    ) -> str:
        created = await self._run(
            lambda: self._client(account).calls.create(
                to=to,
                from_=from_,
                url=webhooks.answer_url,
                method="POST",
                status_callback=webhooks.status_callback,
                status_callback_method="POST",
                status_callback_event=["completed"],
                timeout=timeout_seconds,
            )
        )
        return created.sid

    async def update_call(
        self, account: TelephonyAccountRef, call_sid: str, *, twiml: str | None = None, end: bool = False
    ) -> None:
        if (twiml is None) == (not end):
            raise ValueError("update_call takes exactly one of twiml / end")

        def update() -> None:
            call = self._client(account).calls(call_sid)
            try:
                if twiml is not None:
                    call.update(twiml=twiml)
                    return
                try:
                    call.update(status="canceled")  # a call still ringing
                except TwilioRestException as exc:
                    if exc.code != _CALL_NOT_IN_PROGRESS_CODE:
                        raise
                    call.update(status="completed")  # already answered: cancel is refused, hang up instead
            except TwilioRestException as exc:
                if exc.code != _CALL_NOT_IN_PROGRESS_CODE:  # a call already over is not an error
                    raise

        try:
            await self._run(update)
        except TelephonyError as exc:
            if exc.error_type != "not_found":
                raise

    def browser_access_token(
        self, account_sid: str, calling: BrowserCalling, identity: str, ttl_seconds: int, *, incoming: bool = False
    ) -> str:
        token = AccessToken(account_sid, calling.api_key_sid, calling.api_key_secret, identity=identity, ttl=ttl_seconds)
        token.add_grant(VoiceGrant(outgoing_application_sid=calling.twiml_app_sid, incoming_allow=incoming))
        jwt = token.to_jwt()
        return jwt.decode() if isinstance(jwt, bytes) else jwt

    def is_valid_signature(self, auth_token: str, url: str, params: Mapping[str, str], signature_header: str) -> bool:
        return signature.is_valid_signature(auth_token, url, params, signature_header)

    async def start_recording(self, account: TelephonyAccountRef, call_sid: str, status_callback: str) -> str:
        recording = await self._run(
            lambda: self._client(account)
            .calls(call_sid)
            .recordings.create(
                recording_status_callback=status_callback,
                recording_status_callback_event=["completed"],
                recording_channels="dual",
            )
        )
        return recording.sid

    async def download_recording(self, account: TelephonyAccountRef, recording_sid: str, max_bytes: int) -> bytes:
        url = f"https://{API_HOST}/2010-04-01/Accounts/{account.account_sid}/Recordings/{recording_sid}.mp3"
        try:
            async with asyncio.timeout(DOWNLOAD_TOTAL_SECONDS):
                return await self._fetch(url, account, max_bytes)
        except TimeoutError:
            raise TelephonyError("timeout") from None
        except (TelephonyAuthError, TelephonyError):
            raise
        except httpx.TimeoutException:
            raise TelephonyError("timeout") from None
        except httpx.HTTPError:
            raise TelephonyError("recording_failed") from None

    async def _fetch(self, url: str, account: TelephonyAccountRef, max_bytes: int) -> bytes:
        # Redirects are followed by hand so the credentials can be kept off every hop but the first.
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=httpx.Timeout(CALL_TIMEOUT_SECONDS), transport=self._download_transport
        ) as http:
            for _ in range(MAX_REDIRECTS + 1):
                parts = urlsplit(url)
                # The first URL is built here (Twilio's API host); a redirect may go to any https host (a signed
                # media URL), but never over plain http, and it never receives the account credentials.
                if parts.scheme != "https":
                    raise TelephonyError("recording_failed")
                auth = (account.account_sid, account.auth_token) if parts.hostname == API_HOST else None
                async with http.stream("GET", url, auth=auth) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise TelephonyError("recording_failed", response.status_code)
                        url = str(httpx.URL(url).join(location))
                        continue
                    if response.status_code == 401:
                        raise TelephonyAuthError("credentials rejected")
                    if response.status_code != 200:
                        raise TelephonyError("recording_failed", response.status_code)
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise TelephonyError("recording_failed", 200)
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise TelephonyError("recording_failed", 200)
                    return bytes(body)
        raise TelephonyError("recording_failed")

    async def delete_recording(self, account: TelephonyAccountRef, recording_sid: str) -> None:
        await self._run(lambda: self._client(account).recordings(recording_sid).delete())
