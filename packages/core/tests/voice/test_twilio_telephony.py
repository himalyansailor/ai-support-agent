import json
import time

import httpx
import jwt
import pytest
from supportcore.config import Settings
from supportcore.voice.telephony import signature
from supportcore.voice.telephony.fake_telephony import FakeTelephony
from supportcore.voice.telephony.ports import (
    BrowserCalling,
    CallWebhooks,
    NumberWebhooks,
    TelephonyAccountRef,
    TelephonyAuthError,
    TelephonyError,
)
from supportcore.voice.telephony.twilio_telephony import TwilioTelephony
from twilio.http import HttpClient
from twilio.http.response import Response

pytestmark = pytest.mark.asyncio

ACCOUNT = TelephonyAccountRef(account_sid="AC" + "1" * 32, auth_token="secret-token")
HOOKS = NumberWebhooks(voice_url="https://v.example/twilio/voice/inbound", status_callback="https://v.example/s")
PN = "PN" + "2" * 32


class RecordingHttpClient(HttpClient):
    """Returns canned responses (a queue; the last one repeats) and remembers every request."""

    def __init__(self, *responses: Response) -> None:
        super().__init__(logger=None, is_async=False)  # type: ignore[arg-type]
        self.responses = list(responses)
        self.requests: list[dict] = []

    def request(self, method, uri, params=None, data=None, headers=None, auth=None, timeout=None, allow_redirects=False):
        self.requests.append({"method": method, "uri": uri, "data": data or {}, "params": params or {}})
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def ok(body: dict, status: int = 200) -> Response:
    return Response(status, json.dumps(body))


def adapter(*responses: Response) -> tuple[TwilioTelephony, RecordingHttpClient]:
    client = RecordingHttpClient(*responses)
    return TwilioTelephony(Settings(), http_client=client), client


def number_body(**extra) -> dict:
    return {"sid": PN, "phone_number": "+15005550006", "voice_url": HOOKS.voice_url, **extra}


async def test_buy_number_sets_webhooks():
    tel, http = adapter(ok(number_body(), 201))
    owned = await tel.buy_number(ACCOUNT, "+15005550006", HOOKS)
    assert owned.provider_sid == PN and owned.voice_url == HOOKS.voice_url
    data = http.requests[0]["data"]
    assert data["PhoneNumber"] == "+15005550006"
    assert data["VoiceUrl"] == HOOKS.voice_url and data["VoiceMethod"] == "POST"
    assert data["StatusCallback"] == HOOKS.status_callback and data["StatusCallbackMethod"] == "POST"


async def test_configure_none_clears_urls():
    tel, http = adapter(ok(number_body(voice_url="")))
    await tel.configure_number(ACCOUNT, PN, None)
    assert http.requests[0]["data"]["VoiceUrl"] == "" and http.requests[0]["data"]["StatusCallback"] == ""
    assert PN in http.requests[0]["uri"]


async def test_auth_error_mapped():
    tel, _ = adapter(ok({"code": 20003, "message": "Authenticate", "status": 401}, 401))
    with pytest.raises(TelephonyAuthError):
        await tel.verify(ACCOUNT)


async def test_number_unavailable_mapped():
    tel, _ = adapter(ok({"code": 21422, "message": "not available", "status": 400}, 400))
    with pytest.raises(TelephonyError) as caught:
        await tel.buy_number(ACCOUNT, "+15005550006", HOOKS)
    assert caught.value.error_type == "number_unavailable" and caught.value.status == 400


async def test_server_error_mapped_to_connection_failed():
    tel, _ = adapter(ok({"code": 20500, "message": "boom"}, 500))
    with pytest.raises(TelephonyError) as caught:
        await tel.list_numbers(ACCOUNT)
    assert caught.value.error_type == "connection_failed" and caught.value.status == 500


async def test_timeout_mapped(monkeypatch):
    monkeypatch.setattr("supportcore.voice.telephony.twilio_telephony.CALL_TIMEOUT_SECONDS", 0.05)

    class Slow(RecordingHttpClient):
        def request(self, *args, **kwargs):
            time.sleep(0.3)
            return super().request(*args, **kwargs)

    tel = TwilioTelephony(Settings(), http_client=Slow(ok({})))
    with pytest.raises(TelephonyError) as caught:
        await tel.verify(ACCOUNT)
    assert caught.value.error_type == "timeout"


async def test_ensure_browser_calling_creates_app_and_key_once():
    tel, http = adapter(
        ok({"sid": "AP" + "3" * 32}, 201),
        ok({"sid": "SK" + "4" * 32, "secret": "key-secret-key-secret-key-secret-0"}, 201),
        ok({"sid": "AP" + "3" * 32}),
    )
    hooks = NumberWebhooks(
        voice_url="https://v.example/twilio/voice/browser", status_callback="https://v.example/twilio/voice/status"
    )
    created = await tel.ensure_browser_calling(ACCOUNT, hooks, None)
    assert created == BrowserCalling("SK" + "4" * 32, "key-secret-key-secret-key-secret-0", "AP" + "3" * 32)
    app_request, key_request = http.requests
    assert app_request["data"]["FriendlyName"] == "support-browser-calls"
    assert app_request["data"]["VoiceUrl"] == "https://v.example/twilio/voice/browser"
    # Calls placed through the TwiML App report their status only to the App's status callback.
    assert app_request["data"]["StatusCallback"] == "https://v.example/twilio/voice/status"
    assert app_request["data"]["StatusCallbackMethod"] == "POST"
    assert key_request["data"]["FriendlyName"] == "support-browser-calls" and "Keys" in key_request["uri"]
    new_hooks = NumberWebhooks(
        voice_url="https://new.example/twilio/voice/browser", status_callback="https://new.example/twilio/voice/status"
    )
    again = await tel.ensure_browser_calling(ACCOUNT, new_hooks, created)
    assert again == created
    assert len(http.requests) == 3  # only the app update; no second app or key
    assert "Applications/" + created.twiml_app_sid in http.requests[2]["uri"]
    assert http.requests[2]["data"]["VoiceUrl"] == "https://new.example/twilio/voice/browser"
    assert http.requests[2]["data"]["StatusCallback"] == "https://new.example/twilio/voice/status"
    assert http.requests[2]["data"]["StatusCallbackMethod"] == "POST"


async def test_access_token_has_voice_grant():
    tel, _ = adapter(ok({}))
    calling = BrowserCalling("SK" + "4" * 32, "key-secret-key-secret-key-secret-0", "AP" + "3" * 32)
    token = tel.browser_access_token(ACCOUNT.account_sid, calling, "browser-1", 600)
    claims = jwt.decode(token, "key-secret-key-secret-key-secret-0", algorithms=["HS256"], options={"verify_aud": False})
    assert claims["iss"] == calling.api_key_sid and claims["sub"] == ACCOUNT.account_sid
    voice = claims["grants"]["voice"]
    assert voice["outgoing"]["application_sid"] == calling.twiml_app_sid
    assert not voice.get("incoming", {}).get("allow", False)  # the SDK omits a disabled incoming grant
    assert claims["exp"] - claims["nbf"] == 600  # the SDK stamps nbf (not iat) at creation time
    assert claims["grants"]["identity"] == "browser-1"


async def test_start_recording_requests_dual_channel():
    tel, http = adapter(ok({"sid": "RE" + "5" * 32}, 201))
    sid = await tel.start_recording(ACCOUNT, "CA" + "6" * 32, "https://v.example/twilio/recording/status?call_id=1")
    assert sid == "RE" + "5" * 32
    data = http.requests[0]["data"]
    assert data["RecordingChannels"] == "dual"
    assert data["RecordingStatusCallbackEvent"] == ["completed"]
    assert data["RecordingStatusCallback"].endswith("call_id=1")


async def test_signature_uses_shared_helper():
    tel, _ = adapter(ok({}))
    params = {"CallSid": "CA1", "From": "+1"}
    good = signature.twilio_signature("tok", "https://v.example/x", params)
    assert tel.is_valid_signature("tok", "https://v.example/x", params, good) is True
    assert tel.is_valid_signature("tok", "https://v.example/x", params, "bad") is False


def with_transport(tel: TwilioTelephony, handler) -> None:
    tel._download_transport = httpx.MockTransport(handler)


async def test_download_recording_follows_redirect_without_leaking_credentials():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.twilio.com":
            assert request.url.path.endswith("/Recordings/RE1.mp3")
            return httpx.Response(302, headers={"location": "https://media.twiliocdn.com/abc?sig=1"})
        return httpx.Response(200, content=b"audio-bytes")

    tel, _ = adapter(ok({}))
    with_transport(tel, handler)
    assert await tel.download_recording(ACCOUNT, "RE1", 100) == b"audio-bytes"
    assert seen[0].headers.get("authorization", "").startswith("Basic")
    assert "authorization" not in seen[1].headers


async def test_download_recording_follows_redirect_to_any_https_host_without_credentials():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.twilio.com":
            return httpx.Response(302, headers={"location": "https://bucket.s3.amazonaws.com/rec.mp3?sig=1"})
        return httpx.Response(200, content=b"audio")

    tel, _ = adapter(ok({}))
    with_transport(tel, handler)
    assert await tel.download_recording(ACCOUNT, "RE1", 100) == b"audio"
    assert seen[1].url.host == "bucket.s3.amazonaws.com"
    assert "authorization" not in seen[1].headers


async def test_download_recording_refuses_http_redirect():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.twilio.com":
            return httpx.Response(302, headers={"location": "http://media.example/x.mp3"})
        raise AssertionError("plain http must not be contacted")

    tel, _ = adapter(ok({}))
    with_transport(tel, handler)
    with pytest.raises(TelephonyError) as caught:
        await tel.download_recording(ACCOUNT, "RE1", 100)
    assert caught.value.error_type == "recording_failed"


async def test_download_recording_redirect_cap():
    hops = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(1)
        return httpx.Response(302, headers={"location": f"https://m.example/{len(hops)}"})

    tel, _ = adapter(ok({}))
    with_transport(tel, handler)
    with pytest.raises(TelephonyError) as caught:
        await tel.download_recording(ACCOUNT, "RE1", 100)
    assert caught.value.error_type == "recording_failed" and len(hops) == 4


async def test_download_recording_size_cap_applies_after_redirect():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.twilio.com":
            return httpx.Response(302, headers={"location": "https://m.example/a"})
        return httpx.Response(200, content=b"x" * 50)

    tel, _ = adapter(ok({}))
    with_transport(tel, handler)
    with pytest.raises(TelephonyError) as caught:
        await tel.download_recording(ACCOUNT, "RE1", 10)
    assert caught.value.error_type == "recording_failed"


async def test_download_recording_content_length_precheck():
    read = []

    async def body():
        read.append(1)
        yield b"x"

    tel, _ = adapter(ok({}))
    with_transport(
        tel, lambda r: httpx.Response(200, headers={"content-length": "999"}, content=body())
    )
    with pytest.raises(TelephonyError) as caught:
        await tel.download_recording(ACCOUNT, "RE1", 10)
    assert caught.value.error_type == "recording_failed" and not read


async def test_download_recording_401_is_auth_error():
    tel, _ = adapter(ok({}))
    with_transport(tel, lambda request: httpx.Response(401))
    with pytest.raises(TelephonyAuthError):
        await tel.download_recording(ACCOUNT, "RE1", 10)


async def test_download_recording_aborts_past_max_bytes():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        async def body():
            for _ in range(100):
                sent.append(1)
                yield b"x" * 10

        return httpx.Response(200, content=body())

    tel, _ = adapter(ok({}))
    with_transport(tel, handler)
    with pytest.raises(TelephonyError) as caught:
        await tel.download_recording(ACCOUNT, "RE1", 25)
    assert caught.value.error_type == "recording_failed"
    assert len(sent) < 100  # stopped reading early


async def test_download_recording_not_found_is_recording_failed():
    tel, _ = adapter(ok({}))
    with_transport(tel, lambda request: httpx.Response(404))
    with pytest.raises(TelephonyError) as caught:
        await tel.download_recording(ACCOUNT, "RE1", 100)
    assert caught.value.error_type == "recording_failed" and caught.value.status == 404


async def test_default_sdk_client_has_timeout():
    tel = TwilioTelephony(Settings())
    assert tel._http_client.timeout == 14


AVAILABLE = {
    "meta": {"key": "available_phone_numbers", "next_page_url": None},
    "available_phone_numbers": [{"phone_number": "+15005550007", "locality": "Town", "region": "CA"}],
}
PRICES = {
    "country": "US",
    "price_unit": "USD",
    "phone_number_prices": [{"number_type": "local", "base_price": "1.00", "current_price": "1.15"}],
}


async def test_search_numbers_with_price():
    tel, http = adapter(ok(AVAILABLE), ok(PRICES))
    found = await tel.search_numbers(ACCOUNT, "US", "415", 5)
    assert (found[0].e164, found[0].locality, found[0].region) == ("+15005550007", "Town", "CA")
    assert (found[0].monthly_price, found[0].price_unit) == ("1.15", "USD")
    assert http.requests[0]["params"]["AreaCode"] == "415"


async def test_search_numbers_survives_pricing_failure():
    tel, _ = adapter(ok(AVAILABLE), ok({"code": 20500}, 500))
    found = await tel.search_numbers(ACCOUNT, "US", None)
    assert found[0].e164 == "+15005550007" and found[0].monthly_price is None


async def test_search_numbers_pricing_has_its_own_short_bound(monkeypatch):
    monkeypatch.setattr("supportcore.voice.telephony.twilio_telephony.PRICING_TIMEOUT_SECONDS", 0.05)

    class SlowPricing(RecordingHttpClient):
        def request(self, method, uri, *args, **kwargs):
            if "Pricing" in uri or "pricing" in uri:
                time.sleep(0.3)
            return super().request(method, uri, *args, **kwargs)

    tel = TwilioTelephony(Settings(), http_client=SlowPricing(ok(AVAILABLE), ok(PRICES)))
    found = await tel.search_numbers(ACCOUNT, "US", None)
    assert found[0].monthly_price is None


async def test_release_number_and_delete_recording():
    tel, http = adapter(Response(204, ""))
    await tel.release_number(ACCOUNT, PN)
    await tel.delete_recording(ACCOUNT, "RE" + "5" * 32)
    assert [r["method"] for r in http.requests] == ["DELETE", "DELETE"]
    assert PN in http.requests[0]["uri"] and "Recordings/RE" in http.requests[1]["uri"]


async def test_create_subaccount():
    tel, _ = adapter(ok({"sid": "AC" + "9" * 32, "auth_token": "sub-token"}, 201))
    sub = await tel.create_subaccount(ACCOUNT, "org-1")
    assert sub.account_sid == "AC" + "9" * 32 and sub.auth_token == "sub-token"


async def test_service_exception_auth_is_mapped():
    from supportcore.voice.telephony.twilio_telephony import _map_error
    from twilio.base.exceptions import TwilioServiceException

    exc = TwilioServiceException("t", "title", 401, 20003)
    assert isinstance(_map_error(exc), TelephonyAuthError)
    other = _map_error(TwilioServiceException("t", "title", 503, 99))
    assert isinstance(other, TelephonyError) and other.status == 503


async def test_key_failure_deletes_new_app():
    tel, http = adapter(
        ok({"sid": "AP" + "3" * 32}, 201), ok({"code": 20500}, 500), Response(204, "")
    )
    with pytest.raises(TelephonyError):
        await tel.ensure_browser_calling(ACCOUNT, NumberWebhooks(voice_url="https://v.example/b", status_callback="https://v.example/s"), None)
    assert http.requests[-1]["method"] == "DELETE" and "Applications/AP" in http.requests[-1]["uri"]


async def test_close_subaccount_sets_status_closed():
    tel, http = adapter(ok({"sid": "AC" + "9" * 32, "status": "closed"}))
    await tel.close_subaccount(ACCOUNT, "AC" + "9" * 32)
    request = http.requests[0]
    assert request["method"] == "POST" and "Accounts/AC" + "9" * 32 in request["uri"]
    assert request["data"]["Status"] == "closed"


@pytest.mark.parametrize("body", [{"code": 20404, "message": "gone"}, {"message": "gone"}])
async def test_release_number_not_found_is_distinct(body):
    tel, _ = adapter(ok(body, 404))
    with pytest.raises(TelephonyError) as caught:
        await tel.release_number(ACCOUNT, PN)
    assert (caught.value.error_type, caught.value.status) == ("not_found", 404)


CALL_SID = "CA" + "5" * 32
CALL_HOOKS = CallWebhooks(answer_url="https://v.example/twilio/voice/agent-answer?transfer_id=1&attempt=1",
                          status_callback="https://v.example/twilio/voice/agent-status?transfer_id=1&attempt=1")
CALLING = BrowserCalling("SK" + "4" * 32, "key-secret-key-secret-key-secret-0", "AP" + "3" * 32)


def twilio_error(status: int, code: int) -> Response:
    return ok({"code": code, "message": "x", "status": status}, status)


async def test_create_call_params():
    tel, http = adapter(ok({"sid": CALL_SID}, 201))
    sid = await tel.create_call(ACCOUNT, "client:agent-1", "+15005550006", CALL_HOOKS, 20)
    assert sid == CALL_SID
    request = http.requests[0]
    assert request["method"] == "POST" and "Calls" in request["uri"]
    data = request["data"]
    assert data["To"] == "client:agent-1" and data["From"] == "+15005550006"
    assert data["Url"] == CALL_HOOKS.answer_url and data["Method"] == "POST"
    assert data["StatusCallback"] == CALL_HOOKS.status_callback and data["StatusCallbackMethod"] == "POST"
    assert data["StatusCallbackEvent"] == ["completed"] or data["StatusCallbackEvent"] == "completed"
    assert str(data["Timeout"]) == "20"


async def test_create_call_timeout_follows_the_caller():
    tel, http = adapter(ok({"sid": CALL_SID}, 201))
    await tel.create_call(ACCOUNT, "+15005550007", "+15005550006", CALL_HOOKS, 45)
    assert str(http.requests[0]["data"]["Timeout"]) == "45"


async def test_create_call_auth_error_mapped():
    tel, _ = adapter(twilio_error(401, 20003))
    with pytest.raises(TelephonyAuthError):
        await tel.create_call(ACCOUNT, "+15005550007", "+15005550006", CALL_HOOKS, 20)


async def test_create_call_error_does_not_leak_numbers():
    tel, _ = adapter(twilio_error(400, 21211))
    with pytest.raises(TelephonyError) as caught:
        await tel.create_call(ACCOUNT, "+15005550007", "+15005550006", CALL_HOOKS, 20)
    assert caught.value.error_type == "connection_failed"
    assert "+1500555" not in str(caught.value) and "+1500555" not in repr(caught.value)


async def test_update_call_twiml():
    tel, http = adapter(ok({"sid": CALL_SID}))
    await tel.update_call(ACCOUNT, CALL_SID, twiml="<Response><Hangup/></Response>")
    assert len(http.requests) == 1
    assert CALL_SID in http.requests[0]["uri"] and http.requests[0]["method"] == "POST"
    assert http.requests[0]["data"] == {"Twiml": "<Response><Hangup/></Response>"}


async def test_end_call_cancels_a_ringing_call():
    tel, http = adapter(ok({"sid": CALL_SID}))
    await tel.update_call(ACCOUNT, CALL_SID, end=True)
    assert [r["data"] for r in http.requests] == [{"Status": "canceled"}]


async def test_end_call_cancels_then_completes_when_answered():
    tel, http = adapter(twilio_error(400, 21220), ok({"sid": CALL_SID}))
    await tel.update_call(ACCOUNT, CALL_SID, end=True)
    assert [r["data"] for r in http.requests] == [{"Status": "canceled"}, {"Status": "completed"}]


async def test_update_call_gone_is_not_an_error():
    tel, _ = adapter(twilio_error(404, 20404))
    await tel.update_call(ACCOUNT, CALL_SID, twiml="<Response/>")
    await tel.update_call(ACCOUNT, CALL_SID, end=True)
    # Already over even after the retry.
    tel, http = adapter(twilio_error(400, 21220))
    await tel.update_call(ACCOUNT, CALL_SID, end=True)
    assert len(http.requests) == 2


async def test_update_call_other_errors_are_mapped():
    tel, _ = adapter(twilio_error(401, 20003))
    with pytest.raises(TelephonyAuthError):
        await tel.update_call(ACCOUNT, CALL_SID, end=True)
    tel, _ = adapter(twilio_error(500, 20500))
    with pytest.raises(TelephonyError) as caught:
        await tel.update_call(ACCOUNT, CALL_SID, twiml="<Response/>")
    assert caught.value.error_type == "connection_failed"


@pytest.mark.parametrize("kwargs", [{}, {"twiml": "<Response/>", "end": True}])
async def test_update_call_needs_exactly_one_action(kwargs):
    tel, http = adapter(ok({}))
    with pytest.raises(ValueError):
        await tel.update_call(ACCOUNT, CALL_SID, **kwargs)
    assert http.requests == []
    fake = FakeTelephony()
    with pytest.raises(ValueError):
        await fake.update_call(ACCOUNT, CALL_SID, **kwargs)
    assert fake.calls == []
    await fake.update_call(ACCOUNT, CALL_SID, end=True)
    assert fake.calls[0][0] == "update_call"


async def test_incoming_token_allows_incoming():
    tel, _ = adapter(ok({}))
    token = tel.browser_access_token(ACCOUNT.account_sid, CALLING, "agent-1", 600, incoming=True)
    claims = jwt.decode(token, CALLING.api_key_secret, algorithms=["HS256"], options={"verify_aud": False})
    voice = claims["grants"]["voice"]
    assert voice["incoming"]["allow"] is True
    assert voice["outgoing"]["application_sid"] == CALLING.twiml_app_sid
