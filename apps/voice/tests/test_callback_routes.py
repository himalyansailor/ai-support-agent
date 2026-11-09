"""The callback webhooks (Task 11): an agent's browser starting a callback (`callback_ticket`), the agent's phone
answering the bridge, and the `<Dial action>` that marks an answered callback done. Signed form posts against the
real app and Postgres."""

import uuid
import xml.etree.ElementTree as ET

import pytest
from sqlalchemy import select, update
from supportcore.db.models import (
    Call,
    Conversation,
    Membership,
    PhoneNumber,
    Role,
    User,
    VoiceSettings,
)
from supportcore.db.uow import UnitOfWork
from supportcore.voice.calls import CallService
from supportcore.voice.strings import strings_for
from supportcore.voice.tokens import issue_callback_ticket
from voice_helpers import (
    ACCOUNT_SID,
    CALLER_E164,
    NUMBER_E164,
    PUBLIC_URL,
    post_signed,
    seed_org,
)

BROWSER = "/twilio/voice/browser"
BRIDGE = "/twilio/voice/callback-bridge"
DONE = "/twilio/voice/callback-done"
STATUS = "/twilio/voice/status"
INBOUND_SID = "CA" + "1" * 32
BROWSER_SID = "CA" + "5" * 32


def parse(response) -> ET.Element:
    assert response.status_code == 200, response.status_code
    assert response.headers["content-type"].startswith("application/xml")
    return ET.fromstring(response.text)


class World:
    def __init__(self, org_id, membership_id, conversation_id, widget_id):
        self.org_id, self.membership_id, self.conversation_id, self.widget_id = (
            org_id, membership_id, conversation_id, widget_id,
        )


@pytest.fixture
async def world(app, session_factory) -> World:
    """An organisation with a number and a customer who phoned in and asked for a callback, and an agent."""
    seed = await seed_org(app, session_factory)
    container = app.state.container
    async with UnitOfWork(session_factory) as uow:
        s = uow.session
        user = User(email="agent@x.com", password_hash="x", name="Agent")
        s.add(user)
        await s.flush()
        role = Role(organisation_id=seed.organisation_id, name="Agent", permissions=["queries.answer"])
        s.add(role)
        await s.flush()
        membership = Membership(organisation_id=seed.organisation_id, user_id=user.id, role_id=role.id)
        s.add(membership)
        number = (await s.execute(select(PhoneNumber))).scalar_one()
        call = await CallService(
            s, seed.organisation_id, container.events, container.inbox_publisher
        ).start_phone_call(number, INBOUND_SID, CALLER_E164)
        conversation = (await s.execute(select(Conversation))).scalar_one()
        conversation.callback_status, conversation.callback_phone = "requested", CALLER_E164
        await uow.commit()
    return World(seed.organisation_id, membership.id, call.conversation_id, seed.widget_id)


def ticket_for(settings, world, *, membership_id=None, conversation_id=None, org_id=None):
    return issue_callback_ticket(
        settings, org_id or world.org_id, membership_id or world.membership_id, conversation_id or world.conversation_id
    )


def browser_params(ticket: str, world, *, sid=BROWSER_SID, from_=None) -> dict[str, str]:
    return {
        "AccountSid": ACCOUNT_SID,
        "CallSid": sid,
        "From": from_ or f"client:agent-{world.membership_id}",
        "callback_ticket": ticket,
    }


async def outbound_calls(session_factory) -> list[Call]:
    async with session_factory() as s:
        return list((await s.execute(select(Call).where(Call.direction == "outbound"))).scalars())


async def callback_status(session_factory) -> str | None:
    async with session_factory() as s:
        return (await s.execute(select(Conversation.callback_status))).scalar_one()


def dial_of(root: ET.Element) -> ET.Element:
    dial = root.find("Dial")
    assert dial is not None
    return dial


# --- /voice/browser (the agent branch) -----------------------------------------------------------------------


async def test_browser_callback_dials_customer_with_org_caller_id(client, voice_settings, world, session_factory):
    ticket, jti = ticket_for(voice_settings, world)
    root = parse(await post_signed(client, BROWSER, browser_params(ticket, world)))
    dial = dial_of(root)
    assert dial.attrib["callerId"] == NUMBER_E164
    assert dial.attrib["answerOnBridge"] == "true" and dial.attrib["timeout"] == "30"
    assert dial.attrib["method"] == "POST"
    assert dial.attrib["action"] == f"{PUBLIC_URL}/twilio/voice/callback-done?call_id={(await outbound_calls(session_factory))[0].id}"
    assert dial.find("Number").text == CALLER_E164
    assert "record" not in dial.attrib
    [call] = await outbound_calls(session_factory)
    assert (call.source, call.direction, call.end_reason, call.status) == ("callback", "outbound", "callback", "ringing")
    assert (call.agent_membership_id, call.provider_call_sid, call.ticket_jti) == (world.membership_id, BROWSER_SID, jti)
    assert call.conversation_id == world.conversation_id


async def test_dial_records_when_recording_on(client, voice_settings, world, session_factory):
    async with session_factory() as s:
        await s.execute(update(VoiceSettings).values(record_calls=True))
        await s.commit()
    ticket, _ = ticket_for(voice_settings, world)
    dial = dial_of(parse(await post_signed(client, BROWSER, browser_params(ticket, world))))
    [call] = await outbound_calls(session_factory)
    assert dial.attrib["record"] == "record-from-answer-dual"
    assert dial.attrib["recordingStatusCallback"] == f"{PUBLIC_URL}/twilio/recording/status?call_id={call.id}"
    assert dial.attrib["recordingStatusCallbackEvent"] == "completed"


async def test_browser_callback_ticket_is_single_use(client, voice_settings, world, session_factory):
    ticket, _ = ticket_for(voice_settings, world)
    first = parse(await post_signed(client, BROWSER, browser_params(ticket, world)))
    second = parse(await post_signed(client, BROWSER, browser_params(ticket, world, sid="CA" + "6" * 32)))
    assert first.find("Dial") is not None
    assert [child.tag for child in second] == ["Hangup"]
    assert len(await outbound_calls(session_factory)) == 1


async def test_browser_callback_must_come_from_the_ticket_agent(client, voice_settings, world, session_factory):
    ticket, _ = ticket_for(voice_settings, world)
    other = browser_params(ticket, world, from_=f"client:agent-{uuid.uuid4()}")
    customer = browser_params(ticket, world, from_="client:customer-" + str(uuid.uuid4()))
    for params in (other, customer, {**other, "From": CALLER_E164}):
        assert [c.tag for c in parse(await post_signed(client, BROWSER, params))] == ["Hangup"]
    assert await outbound_calls(session_factory) == []


async def test_browser_callback_rejects_bad_tickets(client, voice_settings, world, session_factory):
    other_org, _ = ticket_for(voice_settings, world, org_id=uuid.uuid4())
    other_conversation, _ = ticket_for(voice_settings, world, conversation_id=uuid.uuid4())
    for ticket in ("garbage", other_org, other_conversation):
        assert [c.tag for c in parse(await post_signed(client, BROWSER, browser_params(ticket, world)))] == ["Hangup"]
    assert await outbound_calls(session_factory) == []


async def test_browser_callback_bad_signature_is_403(client, voice_settings, world):
    ticket, _ = ticket_for(voice_settings, world)
    response = await post_signed(client, BROWSER, browser_params(ticket, world), token="x" * 32)
    assert (response.status_code, response.content) == (403, b"")


async def test_browser_callback_without_a_number_to_call_hangs_up(client, voice_settings, world, session_factory):
    async with session_factory() as s:
        await s.execute(update(Conversation).values(callback_phone=None))
        await s.commit()
    ticket, _ = ticket_for(voice_settings, world)
    assert [c.tag for c in parse(await post_signed(client, BROWSER, browser_params(ticket, world)))] == ["Hangup"]
    assert await outbound_calls(session_factory) == []


# --- the phone path ------------------------------------------------------------------------------------------------


async def make_pending_call(app, session_factory, world, sid="CA" + "7" * 32) -> Call:
    """What the API leaves behind for "call by phone": the outbound row with the agent's leg sid stored."""
    from supportcore.voice.callbacks import CallbackService

    container = app.state.container
    async with UnitOfWork(session_factory) as uow:
        membership = await uow.session.get(Membership, world.membership_id)
        service = CallbackService(
            uow.session, world.org_id, container.events, container.inbox_publisher, container.adapters, container.settings
        )
        call = await service.start_outbound(world.conversation_id, membership, call_sid=None, ticket_jti=None)
        await service.set_sid(call.id, sid)
        await uow.commit()
    return call


async def test_phone_callback_rings_agent_then_bridges(app, client, world, session_factory):
    call = await make_pending_call(app, session_factory, world)
    root = parse(
        await post_signed(
            client, BRIDGE, {"AccountSid": ACCOUNT_SID, "CallSid": "CA" + "7" * 32}, query=f"call_id={call.id}"
        )
    )
    assert [child.tag for child in root] == ["Say", "Dial"]
    assert root.find("Say").text == strings_for("en")["voice_callback_connecting"]
    dial = dial_of(root)
    assert dial.attrib["callerId"] == NUMBER_E164 and dial.find("Number").text == CALLER_E164
    assert dial.attrib["action"] == f"{PUBLIC_URL}/twilio/voice/callback-done?call_id={call.id}"


async def test_bridge_line_uses_the_organisation_language(app, client, world, session_factory):
    """The agent hears the line, so it is in the organisation's language, not the customer's."""
    from supportcore.db.models import Organisation

    call = await make_pending_call(app, session_factory, world)
    async with session_factory() as s:
        await s.execute(update(Organisation).where(Organisation.id == world.org_id).values(language="es"))
        await s.execute(update(Conversation).where(Conversation.id == world.conversation_id).values(language="fr"))
        await s.commit()
    root = parse(
        await post_signed(
            client, BRIDGE, {"AccountSid": ACCOUNT_SID, "CallSid": "CA" + "7" * 32}, query=f"call_id={call.id}"
        )
    )
    say = root.find("Say")
    assert (say.text, say.attrib["language"]) == (strings_for("es")["voice_callback_connecting"], "es-ES")


async def test_bridge_for_unknown_or_other_organisation_call_hangs_up(app, client, world, session_factory):
    call = await make_pending_call(app, session_factory, world)
    for query in (f"call_id={uuid.uuid4()}", "call_id=nope", ""):
        root = parse(await post_signed(client, BRIDGE, {"AccountSid": ACCOUNT_SID}, query=query))
        assert [child.tag for child in root] == ["Hangup"]
    assert (await post_signed(client, BRIDGE, {"AccountSid": ACCOUNT_SID}, token="x" * 32, query=f"call_id={call.id}")).status_code == 403


async def test_bridge_ignores_a_call_that_is_not_an_outbound_callback(client, world, session_factory):
    async with session_factory() as s:
        inbound = (await s.execute(select(Call).where(Call.direction == "inbound"))).scalar_one()
    root = parse(await post_signed(client, BRIDGE, {"AccountSid": ACCOUNT_SID}, query=f"call_id={inbound.id}"))
    assert [child.tag for child in root] == ["Hangup"]


# --- the Dial action -------------------------------------------------------------------------------------------------


async def test_answered_callback_marks_done(app, client, world, session_factory):
    call = await make_pending_call(app, session_factory, world)
    root = parse(
        await post_signed(
            client, DONE, {"AccountSid": ACCOUNT_SID, "DialCallStatus": "completed"}, query=f"call_id={call.id}"
        )
    )
    assert [child.tag for child in root] == ["Hangup"]
    assert await callback_status(session_factory) == "done"


@pytest.mark.parametrize("dial_status", ["no-answer", "busy", "failed", "canceled"])
async def test_unanswered_callback_leaves_requested(app, client, world, session_factory, dial_status):
    call = await make_pending_call(app, session_factory, world)
    root = parse(
        await post_signed(
            client, DONE, {"AccountSid": ACCOUNT_SID, "DialCallStatus": dial_status}, query=f"call_id={call.id}"
        )
    )
    assert [child.tag for child in root] == ["Hangup"]
    assert await callback_status(session_factory) == "requested"


async def test_done_with_bad_input_just_hangs_up(app, client, world, session_factory):
    call = await make_pending_call(app, session_factory, world)
    for query in ("call_id=nope", f"call_id={uuid.uuid4()}"):
        root = parse(await post_signed(client, DONE, {"AccountSid": ACCOUNT_SID, "DialCallStatus": "completed"}, query=query))
        assert [child.tag for child in root] == ["Hangup"]
    assert (
        await post_signed(
            client, DONE, {"AccountSid": ACCOUNT_SID, "DialCallStatus": "completed"}, token="x" * 32, query=f"call_id={call.id}"
        )
    ).status_code == 403
    assert await callback_status(session_factory) == "requested"


async def test_status_callback_finalises_a_callback_call(client, voice_settings, world, session_factory):
    ticket, _ = ticket_for(voice_settings, world)
    parse(await post_signed(client, BROWSER, browser_params(ticket, world)))
    response = await post_signed(
        client, STATUS, {"AccountSid": ACCOUNT_SID, "CallSid": BROWSER_SID, "CallStatus": "completed", "CallDuration": "42"}
    )
    assert response.status_code == 204
    [call] = await outbound_calls(session_factory)
    assert (call.finalised, call.status, call.duration_seconds, call.end_reason) == (True, "completed", 42, "callback")
