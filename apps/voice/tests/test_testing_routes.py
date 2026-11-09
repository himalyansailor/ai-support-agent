"""The test-only read-back of the fake Twilio's recorded calls (the e2e suite asserts what Twilio was asked to do).
The voice app is public-facing: the route exists only when env == "test" and telephony == "fake"."""

import httpx
import pytest
from httpx import ASGITransport
from supportcore.voice.telephony.ports import CallWebhooks, TelephonyAccountRef
from supportvoice.main import create_app
from voice_helpers import PUBLIC_URL

CALLS = "/test/fake-telephony/calls"


async def test_read_back_lists_created_and_updated_calls(app, client):
    telephony = app.state.container.adapters.telephony
    account = TelephonyAccountRef("AC" + "1" * 32, "t" * 32)
    webhooks = CallWebhooks(answer_url=f"{PUBLIC_URL}/answer?x=1", status_callback=f"{PUBLIC_URL}/status")
    sid = await telephony.create_call(account, "client:agent-1", "+15005550006", webhooks, 20)
    await telephony.update_call(account, "CA" + "2" * 32, twiml="<Response><Hangup/></Response>")
    await telephony.update_call(account, sid, end=True)
    response = await client.get(CALLS)
    assert response.status_code == 200
    assert response.json() == {
        "create_call": [
            {
                "account_sid": account.account_sid,
                "to": "client:agent-1",
                "from": "+15005550006",
                "answer_url": webhooks.answer_url,
                "status_callback": webhooks.status_callback,
                "sid": sid,
            }
        ],
        "update_call": [
            {"account_sid": account.account_sid, "call_sid": "CA" + "2" * 32,
             "twiml": "<Response><Hangup/></Response>", "end": False},
            {"account_sid": account.account_sid, "call_sid": sid, "twiml": None, "end": True},
        ],
    }


@pytest.mark.parametrize("telephony", ["twilio"])
async def test_read_back_absent_without_the_fake_telephony(voice_settings, db_engine, telephony):
    app = create_app(voice_settings.model_copy(update={"telephony": telephony}))
    try:
        async with httpx.AsyncClient(transport=ASGITransport(app), base_url=PUBLIC_URL) as client:
            assert (await client.get(CALLS)).status_code == 404
    finally:
        await app.state.container.aclose()
        await app.state.engine.dispose()


async def test_read_back_absent_outside_test(voice_settings, db_engine):
    # Outside env=test the fake telephony cannot even be built; a production-like app has no such route.
    settings = voice_settings.model_copy(update={"env": "dev", "telephony": "twilio"})
    app = create_app(settings)
    try:
        assert not any(getattr(route, "path", "").startswith("/test/") for route in app.router.routes)
        async with httpx.AsyncClient(transport=ASGITransport(app), base_url=PUBLIC_URL) as client:
            assert (await client.get(CALLS)).status_code == 404
    finally:
        await app.state.container.aclose()
        await app.state.engine.dispose()
