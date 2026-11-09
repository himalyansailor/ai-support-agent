import uuid
import xml.etree.ElementTree as ET

from pydantic import SecretStr
from sqlalchemy import func, select, update
from supportcore.db.models import (
    Call,
    Conversation,
    ConversationEvent,
    Customer,
    Message,
    VoiceSettings,
    Widget,
)
from supportcore.voice.strings import strings_for
from supportcore.voice.telephony.signature import twilio_signature
from supportcore.voice.tokens import issue_call_ticket, verify_stream_token
from supportvoice import twiml
from voice_helpers import (
    ACCOUNT_SID,
    AUTH_TOKEN,
    CALLER_E164,
    NUMBER_E164,
    PUBLIC_URL,
    post_signed,
    seed_org,
)

INBOUND = "/twilio/voice/inbound"
BROWSER = "/twilio/voice/browser"
AFTER_STREAM = "/twilio/voice/after-stream"
STATUS = "/twilio/voice/status"
RECORDING = "/twilio/recording/status"


def inbound_params(call_sid: str = "CA" + "1" * 32, caller: str = CALLER_E164) -> dict[str, str]:
    return {"AccountSid": ACCOUNT_SID, "CallSid": call_sid, "From": caller, "To": NUMBER_E164, "CallStatus": "ringing"}


def parse(response) -> ET.Element:
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    return ET.fromstring(response.text)


async def count(session_factory, model) -> int:
    async with session_factory() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def only_call(session_factory) -> Call:
    async with session_factory() as s:
        return (await s.execute(select(Call))).scalar_one()


async def events(session_factory, kind: str) -> int:
    async with session_factory() as s:
        return (
            await s.execute(select(func.count()).select_from(ConversationEvent).where(ConversationEvent.kind == kind))
        ).scalar_one()


# --- inbound ------------------------------------------------------------------------------------


async def test_inbound_connects_stream_with_token(app, client, session_factory, voice_settings):
    seed = await seed_org(app, session_factory)
    root = parse(await post_signed(client, INBOUND, inbound_params()))
    call = await only_call(session_factory)

    connect = root.find("Connect")
    stream = connect.find("Stream")
    assert connect.get("action") == f"{PUBLIC_URL}{AFTER_STREAM}?call_id={call.id}"
    assert stream.get("url") == "wss://voice.example.com/twilio/media"
    parameter = stream.find("Parameter")
    assert parameter.get("name") == "token"
    assert verify_stream_token(voice_settings, parameter.get("value")) == (call.id, seed.organisation_id)
    assert (call.source, call.status, call.caller_e164, call.organisation_id) == (
        "phone", "ringing", CALLER_E164, seed.organisation_id,
    )
    async with session_factory() as s:
        conversation = (await s.execute(select(Conversation))).scalar_one()
    assert (conversation.channel, conversation.status, conversation.widget_id) == ("voice", "bot", seed.widget_id)
    assert await events(session_factory, "call_started") == 1


async def test_inbound_rejects_bad_signature(app, client, session_factory):
    await seed_org(app, session_factory)
    response = await post_signed(client, INBOUND, inbound_params(), token="wrong-token" + "0" * 21)
    assert (response.status_code, response.content) == (403, b"")
    missing = await client.post(INBOUND, data=inbound_params())
    assert (missing.status_code, missing.content) == (403, b"")
    for model in (Call, Customer, Conversation):
        assert await count(session_factory, model) == 0


async def test_unknown_number_hangs_up_without_records(app, client, session_factory):
    await seed_org(app, session_factory)
    params = {**inbound_params(), "To": "+15005550999"}
    root = parse(await post_signed(client, INBOUND, params))
    assert [child.tag for child in root] == ["Hangup"]
    for model in (Call, Customer, Conversation):
        assert await count(session_factory, model) == 0


async def test_inbound_reuses_customer_by_phone(app, client, session_factory):
    await seed_org(app, session_factory)
    await post_signed(client, INBOUND, inbound_params("CA" + "1" * 32))
    await post_signed(client, INBOUND, inbound_params("CA" + "2" * 32))
    assert await count(session_factory, Customer) == 1
    assert await count(session_factory, Call) == 2
    assert await count(session_factory, Conversation) == 2


async def test_inbound_not_ready_says_unavailable_and_requests_callback(app, client, session_factory):
    await seed_org(app, session_factory, ready=False)
    root = parse(await post_signed(client, INBOUND, inbound_params()))
    assert root.find("Connect") is None
    assert root.find("Say").text == strings_for("en")["voice_unavailable"]
    assert root.find("Hangup") is not None
    call = await only_call(session_factory)
    assert call.end_reason == "voice_unavailable"
    async with session_factory() as s:
        conversation = (await s.execute(select(Conversation))).scalar_one()
    assert (conversation.status, conversation.callback_status, conversation.callback_phone) == (
        "open", "requested", CALLER_E164,
    )


async def test_inbound_at_capacity_requests_callback(app, client, session_factory):
    await seed_org(app, session_factory)
    active = app.state.container.active_calls
    assert active.acquire() and active.acquire() and not active.acquire()
    root = parse(await post_signed(client, INBOUND, inbound_params()))
    assert root.find("Connect") is None
    assert root.find("Say").text == strings_for("en")["voice_unavailable"]
    assert (await only_call(session_factory)).end_reason == "capacity"
    async with session_factory() as s:
        conversation = (await s.execute(select(Conversation))).scalar_one()
    assert conversation.callback_status == "requested"


async def test_inbound_retry_same_callsid_is_idempotent(app, client, session_factory):
    await seed_org(app, session_factory)
    first = parse(await post_signed(client, INBOUND, inbound_params()))
    second = parse(await post_signed(client, INBOUND, inbound_params()))
    assert first.find("Connect") is not None and second.find("Connect") is not None
    assert await count(session_factory, Call) == 1
    assert await count(session_factory, Conversation) == 1
    assert await events(session_factory, "call_started") == 1


async def test_retried_webhook_after_the_stream_connected_mints_no_token(app, client, session_factory):
    """A replayed signed inbound webhook (signatures carry no timestamp) gets a stream token only while the call is
    still ringing with no stream; once the stream connected, or the call finished, it gets a hangup."""
    await seed_org(app, session_factory)
    parse(await post_signed(client, INBOUND, inbound_params()))
    async with session_factory() as s:
        await s.execute(update(Call).values(status="in_progress", stream_connected_at=func.now()))
        await s.commit()
    connected = parse(await post_signed(client, INBOUND, inbound_params()))
    assert [child.tag for child in connected] == ["Hangup"]
    async with session_factory() as s:
        await s.execute(update(Call).values(status="completed", finalised=True))
        await s.commit()
    finished = parse(await post_signed(client, INBOUND, inbound_params()))
    assert [child.tag for child in finished] == ["Hangup"]
    assert await count(session_factory, Call) == 1
    assert await events(session_factory, "call_started") == 1


async def test_disabled_number_hangs_up(app, client, session_factory):
    from supportcore.db.models import PhoneNumber

    await seed_org(app, session_factory)
    async with session_factory() as s:
        await s.execute(update(PhoneNumber).values(enabled=False))
        await s.commit()
    root = parse(await post_signed(client, INBOUND, inbound_params()))
    assert [child.tag for child in root] == ["Hangup"]
    assert await count(session_factory, Call) == 0


def test_repeated_form_keys_are_flattened_as_twilio_signs_them(app):
    from supportvoice.twilio_routes import flatten_form

    class Form:
        @staticmethod
        def multi_items():
            return [("b", "2"), ("a", "z"), ("a", "y"), ("a", "z")]

    flat = flatten_form(Form)
    # Twilio signs key+value for each distinct value, values sorted: "ay" + "az" + "b2".
    assert twilio_signature("tok", "https://x", flat) == twilio_signature("tok", "https://x", {"a": "yaz", "b": "2"})


# --- browser ------------------------------------------------------------------------------------


async def make_ticket(session_factory, seed, voice_settings):
    async with session_factory() as s:
        customer = Customer(organisation_id=seed.organisation_id, visitor_id="v-" + uuid.uuid4().hex)
        s.add(customer)
        await s.commit()
    token, jti = issue_call_ticket(voice_settings, seed.organisation_id, seed.widget_id, customer.id)
    return token, jti, customer.id


def browser_params(ticket: str, call_sid: str = "CA" + "3" * 32) -> dict[str, str]:
    return {"AccountSid": ACCOUNT_SID, "CallSid": call_sid, "From": "client:abc", "ticket": ticket}


async def test_browser_call_uses_ticket_customer(app, client, session_factory, voice_settings):
    seed = await seed_org(app, session_factory)
    ticket, jti, customer_id = await make_ticket(session_factory, seed, voice_settings)
    root = parse(await post_signed(client, BROWSER, browser_params(ticket)))
    assert root.find("Connect/Stream") is not None
    call = await only_call(session_factory)
    async with session_factory() as s:
        conversation = (await s.execute(select(Conversation))).scalar_one()
    assert (call.source, call.ticket_jti, call.caller_e164) == ("browser", jti, None)
    assert (conversation.customer_id, conversation.widget_id, conversation.channel) == (
        customer_id, seed.widget_id, "voice",
    )
    assert await count(session_factory, Customer) == 1


async def test_browser_ticket_single_use(app, client, session_factory, voice_settings):
    seed = await seed_org(app, session_factory)
    ticket, _, _ = await make_ticket(session_factory, seed, voice_settings)
    first = parse(await post_signed(client, BROWSER, browser_params(ticket, "CA" + "3" * 32)))
    second = parse(await post_signed(client, BROWSER, browser_params(ticket, "CA" + "4" * 32)))
    assert first.find("Connect") is not None
    assert [child.tag for child in second] == ["Hangup"]
    assert await count(session_factory, Call) == 1
    assert await count(session_factory, Conversation) == 1


async def test_browser_call_rejects_bad_ticket_and_signature(app, client, session_factory, voice_settings):
    seed = await seed_org(app, session_factory)
    ticket, _, _ = await make_ticket(session_factory, seed, voice_settings)
    bad_sig = await post_signed(client, BROWSER, browser_params(ticket), token="x" * 32)
    assert (bad_sig.status_code, bad_sig.content) == (403, b"")
    garbage = parse(await post_signed(client, BROWSER, browser_params("not-a-ticket")))
    assert [child.tag for child in garbage] == ["Hangup"]
    unknown = {**browser_params(ticket), "AccountSid": "AC" + "9" * 32}
    assert (await post_signed(client, BROWSER, unknown)).status_code == 403
    assert await count(session_factory, Call) == 0


async def test_browser_calls_disabled_hangs_up(app, client, session_factory, voice_settings):
    seed = await seed_org(app, session_factory, browser=False)
    ticket, _, _ = await make_ticket(session_factory, seed, voice_settings)
    root = parse(await post_signed(client, BROWSER, browser_params(ticket)))
    assert [child.tag for child in root] == ["Hangup"]
    assert await count(session_factory, Call) == 0


# --- after-stream and status ---------------------------------------------------------------------


async def start_call(client, session_factory) -> Call:
    await post_signed(client, INBOUND, inbound_params())
    return await only_call(session_factory)


def after_stream_params(call: Call) -> dict[str, str]:
    return {"AccountSid": ACCOUNT_SID, "CallSid": call.provider_call_sid}


async def test_after_stream_says_trouble_after_error(app, client, session_factory):
    await seed_org(app, session_factory)
    call = await start_call(client, session_factory)
    async with session_factory() as s:
        await s.execute(update(Call).values(end_reason="error"))
        await s.commit()
    root = parse(await post_signed(client, AFTER_STREAM, after_stream_params(call), query=f"call_id={call.id}"))
    assert root.find("Say").text == strings_for("en")["voice_trouble"]
    assert root.find("Hangup") is not None


async def test_after_stream_hangs_up_otherwise(app, client, session_factory):
    await seed_org(app, session_factory)
    call = await start_call(client, session_factory)
    root = parse(await post_signed(client, AFTER_STREAM, after_stream_params(call), query=f"call_id={call.id}"))
    assert [child.tag for child in root] == ["Hangup"]
    forged = await post_signed(client, AFTER_STREAM, after_stream_params(call), query=f"call_id={call.id}", token="x" * 32)
    assert forged.status_code == 403
    other = await post_signed(client, AFTER_STREAM, after_stream_params(call), query=f"call_id={uuid.uuid4()}")
    assert [child.tag for child in parse(other)] == ["Hangup"]


def status_params(call: Call, status: str = "completed", duration: str | None = "42") -> dict[str, str]:
    params = {"AccountSid": ACCOUNT_SID, "CallSid": call.provider_call_sid, "CallStatus": status}
    if duration is not None:
        params["CallDuration"] = duration
    return params


async def test_status_callback_idempotent(app, client, session_factory):
    await seed_org(app, session_factory)
    call = await start_call(client, session_factory)
    first = await post_signed(client, STATUS, status_params(call))
    second = await post_signed(client, STATUS, status_params(call))
    assert (first.status_code, second.status_code) == (204, 204)
    assert await events(session_factory, "call_ended") == 1
    done = await only_call(session_factory)
    assert (done.status, done.duration_seconds, done.end_reason, done.finalised) == ("completed", 42, "caller_hangup", True)
    async with session_factory() as s:
        conversation = (await s.execute(select(Conversation))).scalar_one()
    assert conversation.status == "resolved"


async def test_browser_call_is_finalised_by_status_callback_after_its_stream_ends(
    app, client, session_factory, voice_settings
):
    """Browser calls get Twilio's status through the TwiML App's status callback (posted with the account's
    AccountSid), and the call finalises as soon as that arrives after the stream's session is done."""
    from datetime import UTC, datetime, timedelta

    from supportcore.voice.calls import CallService

    seed = await seed_org(app, session_factory)
    ticket, _, _ = await make_ticket(session_factory, seed, voice_settings)
    parse(await post_signed(client, BROWSER, browser_params(ticket)))
    call = await only_call(session_factory)
    async with session_factory() as s:
        await s.execute(
            update(Call).values(
                answered_at=datetime.now(UTC) - timedelta(seconds=120),
                stream_connected_at=datetime.now(UTC) - timedelta(seconds=120),
                status="in_progress",
            )
        )
        await s.commit()
    container = app.state.container
    async with session_factory() as s:  # the media stream's session is done
        service = CallService(s, seed.organisation_id, container.events, container.inbox_publisher)
        assert await service.stream_ended(call.id) is False  # waits for the status callback
        await s.commit()
    assert (await only_call(session_factory)).finalised is False
    response = await post_signed(client, STATUS, status_params(call, "completed", "118"))
    assert response.status_code == 204
    done = await only_call(session_factory)
    assert (done.source, done.status, done.duration_seconds, done.end_reason, done.finalised) == (
        "browser", "completed", 118, "caller_hangup", True,
    )
    assert await events(session_factory, "call_ended") == 1
    async with session_factory() as s:
        conversation = (await s.execute(select(Conversation))).scalar_one()
    assert conversation.status == "resolved"


async def test_status_callback_maps_statuses_and_ignores_the_rest(app, client, session_factory):
    await seed_org(app, session_factory)
    call = await start_call(client, session_factory)
    for ignored in ("ringing", "in-progress", "queued", "initiated"):
        assert (await post_signed(client, STATUS, status_params(call, ignored, None))).status_code == 204
    assert (await only_call(session_factory)).finalised is False
    await post_signed(client, STATUS, status_params(call, "no-answer", None))
    done = await only_call(session_factory)
    assert (done.status, done.finalised) == ("no_answer", True)
    forged = await post_signed(client, STATUS, status_params(call), token="x" * 32)
    assert forged.status_code == 403


# --- recording ----------------------------------------------------------------------------------


async def test_recording_status_enqueues_fetch_after_completion_only(app, client, session_factory):
    seed = await seed_org(app, session_factory)
    call = await start_call(client, session_factory)
    queue = app.state.container.adapters.job_queue
    base = {"AccountSid": ACCOUNT_SID, "CallSid": call.provider_call_sid, "RecordingSid": "RE" + "e" * 32}
    query = f"call_id={call.id}"
    await post_signed(client, RECORDING, {**base, "RecordingStatus": "in-progress"}, query=query)
    assert queue.jobs == []
    response = await post_signed(client, RECORDING, {**base, "RecordingStatus": "completed"}, query=query)
    assert response.status_code == 204
    assert queue.jobs == [
        (
            "fetch_call_recording",
            {"organisation_id": str(seed.organisation_id), "call_id": str(call.id), "recording_sid": "RE" + "e" * 32},
        )
    ]
    forged = await post_signed(client, RECORDING, {**base, "RecordingStatus": "completed"}, query=query, token="x" * 32)
    assert forged.status_code == 403
    assert len(queue.jobs) == 1


async def test_recording_status_forwards_duration(app, client, session_factory):
    seed = await seed_org(app, session_factory)
    call = await start_call(client, session_factory)
    queue = app.state.container.adapters.job_queue
    params = {
        "AccountSid": ACCOUNT_SID, "CallSid": call.provider_call_sid, "RecordingSid": "RE" + "e" * 32,
        "RecordingStatus": "completed", "RecordingDuration": "42",
    }
    await post_signed(client, RECORDING, params, query=f"call_id={call.id}")
    assert queue.jobs == [
        (
            "fetch_call_recording",
            {
                "organisation_id": str(seed.organisation_id), "call_id": str(call.id),
                "recording_sid": "RE" + "e" * 32, "recording_duration": 42,
            },
        )
    ]


# --- logs, twiml, health ------------------------------------------------------------------------------


async def test_logs_never_contain_phone_numbers(app, client, session_factory, caplog, capsys):
    await seed_org(app, session_factory)
    caplog.set_level("DEBUG")
    await post_signed(client, INBOUND, inbound_params())
    call = await only_call(session_factory)
    await post_signed(client, STATUS, status_params(call))
    await post_signed(client, INBOUND, inbound_params("CA" + "5" * 32), token="x" * 32)
    out = capsys.readouterr()
    logged = caplog.text + out.out + out.err
    assert "twilio_signature_invalid" in logged
    for secret in (CALLER_E164, CALLER_E164.lstrip("+"), "4155550123", AUTH_TOKEN):
        assert secret not in logged


def test_twiml_escapes_values():
    nasty = 'a"b&c<d>e\'f'
    for xml in (
        twiml.connect_stream(f"wss://h/{nasty}", nasty, f"https://h/x?a=1&b={nasty}"),
        twiml.say_and_hangup(nasty, "en"),
        twiml.hangup(),
    ):
        ET.fromstring(xml)  # well-formed
    root = ET.fromstring(twiml.connect_stream("wss://h/m?a=1&b=2", nasty, "https://h/a?x=1&y=<"))
    assert root.find("Connect").get("action") == "https://h/a?x=1&y=<"
    assert root.find("Connect/Stream").get("url") == "wss://h/m?a=1&b=2"
    assert root.find("Connect/Stream/Parameter").get("value") == nasty
    assert ET.fromstring(twiml.say_and_hangup(nasty, "en")).find("Say").text == nasty
    assert ET.fromstring(twiml.hangup()).tag == "Response"


async def test_health_ready_and_metrics(app, client):
    assert (await client.get("/health")).json() == {"status": "ok"}
    assert (await client.get("/ready")).json() == {"status": "ok"}


# --- review fixes --------------------------------------------------------------------------------------


async def test_oversized_body_is_refused_before_parsing(app, client, session_factory):
    await seed_org(app, session_factory)
    big = {**inbound_params(), "Junk": "x" * 70_000}
    response = await post_signed(client, INBOUND, big)
    assert response.status_code == 413
    async def chunks():
        yield b"a=" + b"x" * 40_000
        yield b"x" * 40_000

    chunked = await client.post(INBOUND, content=chunks(), headers={"content-type": "application/x-www-form-urlencoded"})
    assert chunked.status_code == 413
    assert await count(session_factory, Call) == 0


async def test_non_form_bodies_are_refused(app, client, session_factory):
    await seed_org(app, session_factory)
    multipart = await client.post(INBOUND, files={"f": ("f.txt", b"hello")}, data={"To": NUMBER_E164})
    assert multipart.status_code == 415
    as_json = await client.post(STATUS, json={"a": "b"})
    assert as_json.status_code == 415
    assert await count(session_factory, Call) == 0


async def test_anonymous_and_sip_callers_get_their_own_customer(app, client, session_factory):
    await seed_org(app, session_factory)
    for sid, caller in (("CA" + "6" * 32, "anonymous"), ("CA" + "7" * 32, "+266696687"), ("CA" + "8" * 32, "sip:" + "a" * 40 + "@example.com")):
        root = parse(await post_signed(client, INBOUND, inbound_params(sid, caller)))
        assert root.find("Connect") is not None
    async with session_factory() as s:
        calls = list((await s.execute(select(Call))).scalars())
    assert [c.caller_e164 for c in calls] == [None, None, None]
    assert await count(session_factory, Customer) == 3
    assert await count(session_factory, Conversation) == 3


async def test_not_ready_without_caller_number_is_not_routed_and_promises_no_call_back(app, client, session_factory):
    """A withheld number and nothing said: nothing for an agent to act on, so no ticket; the call still finalises."""
    await seed_org(app, session_factory, ready=False)
    root = parse(await post_signed(client, INBOUND, inbound_params(caller="anonymous")))
    said = root.find("Say").text
    assert said == strings_for("en")["voice_unavailable_unknown"] and "call you back" not in said
    async with session_factory() as s:
        conversation = (await s.execute(select(Conversation))).scalar_one()
    assert (conversation.status, conversation.callback_status, conversation.callback_phone) == ("bot", None, None)
    call = await only_call(session_factory)
    assert call.end_reason == "voice_unavailable"
    await post_signed(client, STATUS, status_params(call, "completed", "3"))
    assert (await only_call(session_factory)).finalised is True
    async with session_factory() as s:
        assert (await s.execute(select(Conversation.status))).scalar_one() == "resolved"


async def test_withheld_caller_who_spoke_is_still_routed_to_the_team(app, client, session_factory):
    from supportcore.voice.calls import CallService

    seed = await seed_org(app, session_factory)
    parse(await post_signed(client, INBOUND, inbound_params(caller="anonymous")))
    call = await only_call(session_factory)
    container = app.state.container
    async with session_factory() as s:
        service = CallService(
            s, seed.organisation_id, container.events, container.inbox_publisher,
            adapters=container.adapters, settings=container.settings,
        )
        await service.add_caller_utterance(call.id, "My order never arrived")
        await service.request_callback(call.id, "error")
        await s.commit()
    async with session_factory() as s:
        conversation = (await s.execute(select(Conversation))).scalar_one()
    assert (conversation.status, conversation.callback_status, conversation.callback_phone) == ("open", None, None)


async def test_after_stream_trouble_wording_follows_who_can_be_reached(app, client, session_factory, voice_settings):
    seed = await seed_org(app, session_factory)
    ticket, _, _ = await make_ticket(session_factory, seed, voice_settings)
    parse(await post_signed(client, INBOUND, inbound_params("CA" + "5" * 32, caller="anonymous")))
    parse(await post_signed(client, BROWSER, browser_params(ticket, "CA" + "6" * 32)))
    async with session_factory() as s:
        await s.execute(update(Call).values(end_reason="error"))
        await s.commit()
        calls = {c.source: c for c in (await s.execute(select(Call))).scalars()}
    for source, key in (("phone", "voice_trouble_unknown"), ("browser", "voice_trouble_browser")):
        call = calls[source]
        root = parse(await post_signed(client, AFTER_STREAM, after_stream_params(call), query=f"call_id={call.id}"))
        assert root.find("Say").text == strings_for("en")[key]
        assert "call you back" not in root.find("Say").text


async def test_callback_decision_is_recorded_without_announcement_and_finalise_keeps_it_open(app, client, session_factory):
    await seed_org(app, session_factory, ready=False)
    await post_signed(client, INBOUND, inbound_params())
    call = await only_call(session_factory)
    await post_signed(client, STATUS, status_params(call))
    async with session_factory() as s:
        conversation = (await s.execute(select(Conversation))).scalar_one()
        messages = (await s.execute(select(func.count()).select_from(Message))).scalar_one()
    assert conversation.escalation == {
        "action": "escalate", "rule_key": "voice_callback", "rule_name": "Voice callback", "reason": None, "source": "voice",
    }
    assert messages == 0  # announce=False: no system message
    assert conversation.status == "open"
    assert await events(session_factory, "call_ended") == 1


async def test_browser_ticket_from_another_organisation_is_refused(app, client, session_factory, voice_settings):
    first = await seed_org(app, session_factory)
    other = await seed_org(app, session_factory, suffix="b", e164="+15005550007", sid="AC" + "b" * 32)
    ticket, _, _ = await make_ticket(session_factory, other, voice_settings)
    root = parse(await post_signed(client, BROWSER, browser_params(ticket)))  # AccountSid is the first org's
    assert [child.tag for child in root] == ["Hangup"]
    assert first and await count(session_factory, Call) == 0


async def test_browser_ticket_for_another_widget_is_refused(app, client, session_factory, voice_settings):
    seed = await seed_org(app, session_factory)
    async with session_factory() as s:
        other = Widget(organisation_id=seed.organisation_id, name="Other", public_key="pk-" + uuid.uuid4().hex)
        s.add(other)
        await s.flush()
        await s.execute(update(VoiceSettings).values(browser_widget_id=other.id))
        await s.commit()
    ticket, _, _ = await make_ticket(session_factory, seed, voice_settings)
    root = parse(await post_signed(client, BROWSER, browser_params(ticket)))
    assert [child.tag for child in root] == ["Hangup"]
    assert await count(session_factory, Call) == 0


async def test_browser_call_not_ready_is_routed_without_callback_status(app, client, session_factory, voice_settings):
    seed = await seed_org(app, session_factory, ready=False)
    ticket, _, _ = await make_ticket(session_factory, seed, voice_settings)
    root = parse(await post_signed(client, BROWSER, browser_params(ticket)))
    said = root.find("Say").text  # a browser caller is followed up in the chat, not called back
    assert said == strings_for("en")["voice_unavailable_browser"] and "call you back" not in said
    call = await only_call(session_factory)
    async with session_factory() as s:
        conversation = (await s.execute(select(Conversation))).scalar_one()
    assert call.end_reason == "voice_unavailable"
    assert (conversation.status, conversation.callback_status) == ("open", None)


async def test_reused_ticket_creates_no_conversation(app, client, session_factory, voice_settings):
    seed = await seed_org(app, session_factory)
    ticket, _, _ = await make_ticket(session_factory, seed, voice_settings)
    await post_signed(client, BROWSER, browser_params(ticket, "CA" + "3" * 32))
    await post_signed(client, BROWSER, browser_params(ticket, "CA" + "4" * 32))
    assert await count(session_factory, Conversation) == 1
    assert await events(session_factory, "conversation_created") == 1


def test_flatten_form_matches_twilios_sdk_signature():
    from supportvoice.twilio_routes import flatten_form
    from twilio.request_validator import RequestValidator

    items = [("CallSid", "CA1"), ("Digits", "2"), ("Digits", "1"), ("To", "+1500"), ("Digits", "1")]

    class Form:
        @staticmethod
        def multi_items():
            return items

    url = "https://voice.example.com/twilio/voice/inbound?x=1"
    class MultiDict(dict):
        def getlist(self, key):
            return self[key] if isinstance(self[key], list) else [self[key]]

    expected = RequestValidator("tok").compute_signature(
        url, MultiDict({"CallSid": "CA1", "Digits": ["2", "1", "1"], "To": "+1500"})
    )
    assert twilio_signature("tok", url, flatten_form(Form)) == expected


async def test_metrics_need_the_token_when_exposed(voice_settings, db_engine):
    import httpx
    from httpx import ASGITransport
    from supportvoice.main import create_app

    async def status(settings, headers=None, host="127.0.0.1"):
        application = create_app(settings)
        try:
            async with httpx.AsyncClient(transport=ASGITransport(application, client=(host, 1)), base_url=PUBLIC_URL) as c:
                return (await c.get("/metrics", headers=headers or {})).status_code
        finally:
            await application.state.container.aclose()
            await application.state.engine.dispose()

    assert await status(voice_settings) == 404  # public URL set, no token: not served, even to loopback
    assert await status(voice_settings.model_copy(update={"voice_public_url": None})) == 200
    assert await status(voice_settings.model_copy(update={"voice_public_url": None}), host="203.0.113.9") == 404
    with_token = voice_settings.model_copy(update={"metrics_token": SecretStr("s3cret-metrics")})
    assert await status(with_token) == 401
    assert await status(with_token, {"Authorization": "Bearer s3cret-metrics"}) == 200
