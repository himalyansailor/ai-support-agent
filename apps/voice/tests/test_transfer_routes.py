"""The live-transfer webhooks (Task 6): after-stream joins the conference and rings an agent, the agent legs'
answer / accept / status callbacks, the hold music, the caller's status callback and the deadline watchdog.

Signed form posts against the real app, real Postgres, memory presence and softphones, and the fake telephony
(whose calls are asserted). The Twilio work runs as tracked container tasks after each commit; `settle` waits for it."""

import asyncio
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from supportcore.db.models import (
    Call,
    CallTransfer,
    Conversation,
    ConversationEvent,
    Membership,
    PhoneNumber,
    Role,
    TelephonyAccount,
    User,
    VoiceSettings,
)
from supportcore.db.uow import UnitOfWork
from supportcore.orgs.service import OrganisationService
from supportcore.realtime.spawn import drain_pending
from supportcore.support.settings_service import SupportSettingsService
from supportcore.voice.calls import CallService
from supportcore.voice.settings_service import VoiceSettingsService
from supportcore.voice.strings import strings_for
from supportcore.voice.telephony.ports import TelephonyAccountRef, TelephonyError
from supportcore.voice.transfers import EndLeg, TransferService
from voice_helpers import (
    ACCOUNT_SID,
    AUTH_TOKEN,
    CALLER_E164,
    NUMBER_E164,
    PUBLIC_URL,
    post_signed,
    signed_headers,
)

AFTER_STREAM = "/twilio/voice/after-stream"
STATUS = "/twilio/voice/status"
HOLD = "/twilio/voice/hold"
AGENT_ANSWER = "/twilio/voice/agent-answer"
AGENT_ACCEPT = "/twilio/voice/agent-accept"
AGENT_STATUS = "/twilio/voice/agent-status"

CALL_SID = "CA" + "1" * 32
AGENT_PHONE = "+14155550199"
OTHER_AGENT_PHONE = "+14155550188"
ALL_DAY = {day: [["00:00", "24:00"]] for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


@dataclass
class World:
    org_id: uuid.UUID
    call_id: uuid.UUID
    conversation_id: uuid.UUID
    transfer_id: uuid.UUID


@pytest.fixture
def container(app):
    return app.state.container


@pytest.fixture
def telephony(container):
    return container.adapters.telephony


@pytest.fixture
async def world(app, session_factory, container) -> World:
    """An organisation with roles (agents need `queries.answer`), always open, a Twilio account and number, and a
    phone call whose stream has ended with a `ringing` transfer begun (as the session leaves it)."""
    return await make_world(session_factory, container)


async def make_world(
    session_factory, container, *, email="owner@x.com", account_sid=ACCOUNT_SID, e164=NUMBER_E164, call_sid=CALL_SID
) -> World:
    async with UnitOfWork(session_factory) as uow:
        s = uow.session
        owner = User(email=email, password_hash="x", name="Owner")
        s.add(owner)
        await s.flush()
        org = await OrganisationService(s, container.events).create(owner, "Acme")
        owner_membership = (await s.execute(select(Membership).where(Membership.user_id == owner.id))).scalar_one()
        owner_membership.agent_status = "offline"  # never a candidate here
        (await SupportSettingsService(s, org.id, container.events).get()).weekly_hours = ALL_DAY
        await VoiceSettingsService(s, org.id, container.events).get()  # browser transfer on, phone off
        account = TelephonyAccount(
            organisation_id=org.id,
            kind="byo",
            account_sid=account_sid,
            auth_token_encrypted=container.adapters.cipher.encrypt(AUTH_TOKEN),
            auth_token_last4=AUTH_TOKEN[-4:],
        )
        s.add(account)
        await s.flush()
        number = PhoneNumber(
            organisation_id=org.id,
            telephony_account_id=account.id,
            e164=e164,
            provider_sid="PN" + "d" * 32,
            ownership="byo",
        )
        s.add(number)
        await s.flush()
        call = await CallService(
            s, org.id, container.events, container.inbox_publisher, adapters=container.adapters,
            settings=container.settings,
        ).start_phone_call(number, call_sid, CALLER_E164)
        now = datetime.now(UTC)
        call.status, call.answered_at, call.stream_connected_at, call.stream_ended_at = "in_progress", now, now, now
        await s.flush()
        transfer = await _service(s, org.id, container, session_factory).begin(call.id)
        await uow.commit()
    await drain_pending()
    return World(org.id, call.id, call.conversation_id, transfer.id)


def _service(session, org_id, container, session_factory):
    return TransferService(
        session, org_id, container.events, container.inbox_publisher, container.adapters, container.settings,
        session_factory=session_factory,
    )


@pytest.fixture
def make_agent(session_factory, world, container):
    async def make(name, *, preference="browser", phone=None, softphone=True):
        async with UnitOfWork(session_factory) as uow:
            s = uow.session
            user = User(email=f"{uuid.uuid4().hex[:8]}@x.com", password_hash="x", name=name)
            s.add(user)
            await s.flush()
            role_id = (
                await s.execute(select(Role.id).where(Role.organisation_id == world.org_id, Role.name == "Agent"))
            ).scalar_one()
            m = Membership(
                organisation_id=world.org_id, user_id=user.id, role_id=role_id, agent_status="online",
                call_preference=preference, phone_number=phone,
            )
            s.add(m)
            await uow.commit()
        await container.adapters.presence.beat(world.org_id, m.id)
        if softphone:
            await container.adapters.softphones.beat(world.org_id, m.id)
        return m

    return make


async def settle(container) -> None:
    """Waits for the transfer actions running in the background (and anything they publish). Yields each round: a
    task that is already done leaves the set only once its done-callbacks have run."""
    while container._transfer_work:
        await asyncio.gather(*list(container._transfer_work))
        await asyncio.sleep(0)
    await drain_pending()


def parse(response) -> ET.Element:
    assert response.status_code == 200, response.status_code
    assert response.headers["content-type"].startswith("application/xml")
    return ET.fromstring(response.text)


def leg_query(world, attempt=1) -> str:
    return f"transfer_id={world.transfer_id}&attempt={attempt}"


def leg_url(world, endpoint, attempt=1) -> str:
    return f"{PUBLIC_URL}/twilio/voice/{endpoint}?{leg_query(world, attempt)}"


def form(**extra) -> dict[str, str]:
    return {"AccountSid": ACCOUNT_SID, **extra}


async def after_stream(client, world):
    return await post_signed(client, AFTER_STREAM, form(CallSid=CALL_SID, CallStatus="in-progress"), query=f"call_id={world.call_id}")


async def answer(client, world, attempt=1, sid="CA" + "a" * 32):
    return await post_signed(client, AGENT_ANSWER, form(CallSid=sid, CallStatus="in-progress"), query=leg_query(world, attempt))


async def accept(client, world, digits="1", attempt=1, sid="CA" + "a" * 32):
    return await post_signed(client, AGENT_ACCEPT, form(CallSid=sid, Digits=digits), query=leg_query(world, attempt))


async def leg_status(client, world, call_status, attempt=1, sid="CA" + "a" * 32):
    return await post_signed(client, AGENT_STATUS, form(CallSid=sid, CallStatus=call_status), query=leg_query(world, attempt))


def created(telephony) -> list[tuple]:
    """(to, from_, webhooks, timeout) of each create_call."""
    return [args[1:] for name, args in telephony.calls if name == "create_call"]


def updated(telephony) -> list[tuple]:
    """(call sid, twiml, end) of each update_call."""
    return [args[1:] for name, args in telephony.calls if name == "update_call"]


async def transfer_row(session_factory, world) -> CallTransfer:
    async with session_factory() as s:
        return await s.get(CallTransfer, world.transfer_id)


async def _phone_only(session_factory, world):
    async with session_factory() as s:
        await s.execute(
            update(VoiceSettings)
            .where(VoiceSettings.organisation_id == world.org_id)
            .values(transfer_browser_enabled=False, transfer_phone_enabled=True)
        )
        await s.commit()


def _conference(root: ET.Element) -> ET.Element:
    assert [child.tag for child in root] == ["Dial"]
    conference = root.find("Dial/Conference")
    assert conference is not None
    return conference


# --- after-stream and the first ring ------------------------------------------------------------------------------


async def test_after_stream_joins_conference_and_rings_first_agent(client, world, make_agent, container, telephony, session_factory):
    ann = await make_agent("Ann")
    conference = _conference(parse(await after_stream(client, world)))
    assert conference.text == f"transfer-{world.transfer_id}"
    assert conference.attrib == {
        "startConferenceOnEnter": "false",
        "endConferenceOnExit": "true",
        "beep": "false",
        "waitUrl": f"{PUBLIC_URL}{HOLD}?transfer_id={world.transfer_id}",
        "waitMethod": "POST",
        # The caller joins first, and Twilio takes the room's status callback from the first participant.
        "statusCallback": f"{PUBLIC_URL}/twilio/voice/conference-status?transfer_id={world.transfer_id}",
        "statusCallbackEvent": "join",
        "statusCallbackMethod": "POST",
    }
    await settle(container)
    [(to, from_, webhooks, timeout)] = created(telephony)
    assert (to, from_, timeout) == (f"client:agent-{ann.id}", NUMBER_E164, 20)
    assert webhooks.answer_url == leg_url(world, "agent-answer")
    assert webhooks.status_callback == leg_url(world, "agent-status")
    transfer = await transfer_row(session_factory, world)
    assert (transfer.status, transfer.attempt, transfer.current_membership_id) == ("ringing", 1, ann.id)
    assert transfer.current_leg_sid is not None and transfer.current_leg_sid.startswith("CA")
    assert len(container._watchdogs) == 1  # the deadline is watched

    # A retried after-stream rings nobody new.
    _conference(parse(await after_stream(client, world)))
    await settle(container)
    assert len(created(telephony)) == 1


async def test_after_stream_without_anyone_to_ring_says_failed_line(client, world, container, telephony, session_factory):
    root = parse(await after_stream(client, world))  # nobody online
    assert root.find("Say").text == strings_for("en")["voice_transfer_failed_phone"]
    assert root.find("Hangup") is not None
    await settle(container)
    assert created(telephony) == [] and updated(telephony) == []
    assert (await transfer_row(session_factory, world)).status == "no_answer"


async def test_after_stream_without_transfer_is_unchanged(client, world, session_factory):
    async with session_factory() as s:
        await s.execute(update(CallTransfer).where(CallTransfer.id == world.transfer_id).values(status="no_answer"))
        await s.commit()
    root = parse(await after_stream(client, world))
    assert [child.tag for child in root] == ["Hangup"]


# --- agent legs -----------------------------------------------------------------------------------------------------


async def test_browser_leg_answer_joins(client, world, make_agent, container, session_factory):
    ann = await make_agent("Ann")
    await after_stream(client, world)
    await settle(container)
    conference = _conference(parse(await answer(client, world)))
    assert conference.text == f"transfer-{world.transfer_id}"
    # No statusCallback here: Twilio would ignore it (the caller joined the room first and set it).
    assert conference.attrib == {"startConferenceOnEnter": "true", "endConferenceOnExit": "true", "beep": "false"}
    transfer = await transfer_row(session_factory, world)
    assert (transfer.status, transfer.connected_membership_id) == ("connected", ann.id)
    async with session_factory() as s:
        call = await s.get(Call, world.call_id)
        conversation = await s.get(Conversation, world.conversation_id)
    assert call.end_reason == "transferred"
    assert conversation.assignee_membership_id == ann.id


async def test_phone_leg_answer_gathers_then_digit_joins(client, world, make_agent, container, telephony, session_factory):
    await _phone_only(session_factory, world)
    bob = await make_agent("Bob", preference="phone", phone=AGENT_PHONE, softphone=False)
    await after_stream(client, world)
    await settle(container)
    [(to, from_, _webhooks, _timeout)] = created(telephony)
    assert (to, from_) == (AGENT_PHONE, NUMBER_E164)

    root = parse(await answer(client, world))
    assert [child.tag for child in root] == ["Gather", "Hangup"]
    gather = root.find("Gather")
    assert gather.attrib == {
        "numDigits": "1", "timeout": "10", "action": leg_url(world, "agent-accept"), "method": "POST",
    }
    assert gather.find("Say").text == "Call from a customer of Acme. Press 1 to accept."
    assert (await transfer_row(session_factory, world)).status == "ringing"  # not accepted by picking up

    conference = _conference(parse(await accept(client, world)))
    assert conference.get("startConferenceOnEnter") == "true"
    transfer = await transfer_row(session_factory, world)
    assert (transfer.status, transfer.connected_membership_id, transfer.current_target) == ("connected", bob.id, "phone")


async def test_wrong_digit_hangs_up_without_accepting(client, world, make_agent, container, session_factory):
    await _phone_only(session_factory, world)
    await make_agent("Bob", preference="phone", phone=AGENT_PHONE, softphone=False)
    await after_stream(client, world)
    await settle(container)
    root = parse(await accept(client, world, digits="2"))
    assert [child.tag for child in root] == ["Hangup"]
    assert (await transfer_row(session_factory, world)).status == "ringing"


async def test_voicemail_without_digit_rings_next(client, world, make_agent, container, telephony, session_factory):
    await _phone_only(session_factory, world)
    await make_agent("Bob", preference="phone", phone=AGENT_PHONE, softphone=False)
    cat = await make_agent("Cat", preference="phone", phone=OTHER_AGENT_PHONE, softphone=False)
    await after_stream(client, world)
    await settle(container)
    parse(await answer(client, world))  # voicemail picks up and hears the <Gather>, presses nothing
    response = await leg_status(client, world, "completed")
    assert response.status_code == 204
    await settle(container)
    assert [to for to, *_ in created(telephony)] == [AGENT_PHONE, OTHER_AGENT_PHONE]
    [_, (_to, _from, webhooks, _timeout)] = created(telephony)
    assert webhooks.answer_url == leg_url(world, "agent-answer", attempt=2)
    transfer = await transfer_row(session_factory, world)
    assert (transfer.status, transfer.attempt, transfer.current_membership_id) == ("ringing", 2, cat.id)
    # A late "1" from the first leg is refused.
    root = parse(await accept(client, world, attempt=1))
    assert root.find("Say").text == strings_for("en")["voice_agent_taken"]


async def test_second_accept_hears_taken(client, world, make_agent, container, session_factory):
    """The leg rung before (a stale attempt) and the current leg accept at once: exactly one joins, the other hears
    that the call was taken. The connected leg's own answer / accept replayed (a Twilio retry) joins it again."""
    await _phone_only(session_factory, world)
    await make_agent("Ann", preference="phone", phone=AGENT_PHONE, softphone=False)
    await make_agent("Bob", preference="phone", phone=OTHER_AGENT_PHONE, softphone=False)
    await after_stream(client, world)
    await settle(container)
    await leg_status(client, world, "no-answer", attempt=1)
    await settle(container)
    second = (await transfer_row(session_factory, world)).current_membership_id
    parse(await answer(client, world, attempt=2, sid="CA" + "b" * 32))
    stale, current = await asyncio.gather(
        accept(client, world, attempt=1), accept(client, world, attempt=2, sid="CA" + "b" * 32)
    )
    assert parse(stale).find("Say").text == strings_for("en")["voice_agent_taken"]
    assert _conference(parse(current)).text == f"transfer-{world.transfer_id}"
    transfer = await transfer_row(session_factory, world)
    assert (transfer.status, transfer.connected_membership_id) == ("connected", second)
    for replay in (answer(client, world, attempt=2, sid="CA" + "b" * 32), accept(client, world, attempt=2, sid="CA" + "b" * 32)):
        assert _conference(parse(await replay)).text == f"transfer-{world.transfer_id}"
    assert (await transfer_row(session_factory, world)).connected_at == transfer.connected_at  # connected once
    # Another leg's replay is still "taken".
    assert parse(await answer(client, world, attempt=1)).find("Say").text == strings_for("en")["voice_agent_taken"]


async def test_refused_leg_rings_next(client, world, make_agent, container, telephony, session_factory, monkeypatch):
    ann, ben = await make_agent("Ann"), await make_agent("Ben")
    original = telephony.create_call
    refused = []

    async def refuse_first(account, to, from_, webhooks, timeout_seconds):
        if not refused:
            refused.append(to)
            raise TelephonyError("connection_failed")
        return await original(account, to, from_, webhooks, timeout_seconds)

    monkeypatch.setattr(telephony, "create_call", refuse_first)
    await after_stream(client, world)
    await settle(container)
    assert refused == [f"client:agent-{ann.id}"]
    [(to, *_)] = created(telephony)
    assert to == f"client:agent-{ben.id}"
    transfer = await transfer_row(session_factory, world)
    assert (transfer.status, transfer.attempt, transfer.current_membership_id) == ("ringing", 2, ben.id)
    assert transfer.current_leg_sid is not None

    # Twilio failing the second leg (its status callback says `failed`) with nobody left: the transfer fails and the
    # caller hears the failure line.
    assert (await leg_status(client, world, "failed", attempt=2)).status_code == 204
    await settle(container)
    assert (await transfer_row(session_factory, world)).status == "failed"
    [(sid, twiml_body, end)] = updated(telephony)
    assert (sid, end) == (CALL_SID, False)
    assert strings_for("en")["voice_transfer_failed_phone"] in twiml_body


async def test_leg_status_ignores_stale_and_unfinished_legs(client, world, make_agent, container, telephony):
    await make_agent("Ann")
    await make_agent("Ben")
    await after_stream(client, world)
    await settle(container)
    assert (await leg_status(client, world, "ringing")).status_code == 204
    assert (await leg_status(client, world, "completed", attempt=7)).status_code == 204
    await settle(container)
    assert len(created(telephony)) == 1


# --- the caller ---------------------------------------------------------------------------------------------------


async def test_caller_hangup_during_hold_cancels_agent_leg(client, world, make_agent, container, telephony, session_factory):
    await make_agent("Ann")
    await after_stream(client, world)
    await settle(container)
    leg_sid = (await transfer_row(session_factory, world)).current_leg_sid
    caller_status = form(CallSid=CALL_SID, CallStatus="completed", CallDuration="30")
    assert (await post_signed(client, STATUS, caller_status)).status_code == 204
    assert (await post_signed(client, STATUS, caller_status)).status_code == 204  # retried
    await settle(container)
    assert updated(telephony) == [(leg_sid, None, True)]
    transfer = await transfer_row(session_factory, world)
    assert transfer.status == "abandoned" and transfer.ended_at is not None
    async with session_factory() as s:
        call = await s.get(Call, world.call_id)
        conversation = await s.get(Conversation, world.conversation_id)
        ended = (
            await s.execute(
                select(func.count()).select_from(ConversationEvent).where(ConversationEvent.kind == "call_ended")
            )
        ).scalar_one()
    assert (call.finalised, call.end_reason) == (True, "transfer_no_answer")
    assert ended == 1
    assert (conversation.callback_status, conversation.status) == ("requested", "open")

    # The agent's leg status arriving afterwards changes nothing.
    await leg_status(client, world, "canceled")
    await settle(container)
    assert len(created(telephony)) == 1


async def test_connected_transfer_ends_with_the_caller(client, world, make_agent, container, session_factory):
    await make_agent("Ann")
    await after_stream(client, world)
    await settle(container)
    parse(await answer(client, world))
    await post_signed(client, STATUS, form(CallSid=CALL_SID, CallStatus="completed", CallDuration="300"))
    transfer = await transfer_row(session_factory, world)
    assert (transfer.status, transfer.ended_at is not None) == ("connected", True)
    async with session_factory() as s:
        call = await s.get(Call, world.call_id)
    assert (call.finalised, call.end_reason, call.duration_seconds) == (True, "transferred", 300)


async def test_caller_left_failure_still_finalises_the_call(client, world, make_agent, container, session_factory, monkeypatch, capsys):
    await make_agent("Ann")
    await after_stream(client, world)
    await settle(container)
    parse(await answer(client, world))

    async def broken(self, call_id):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(TransferService, "caller_left", broken)
    response = await post_signed(client, STATUS, form(CallSid=CALL_SID, CallStatus="completed", CallDuration="300"))
    assert response.status_code == 204
    async with session_factory() as s:
        call = await s.get(Call, world.call_id)
    assert (call.finalised, call.end_reason, call.duration_seconds) == (True, "transferred", 300)
    # The finalise recorded the transfer's end that caller_left could not.
    assert (await transfer_row(session_factory, world)).ended_at is not None
    out = capsys.readouterr().out
    assert "transfer_caller_left_failed" in out and "RuntimeError" in out and "secret detail" not in out


async def test_watchdog_gives_up_at_deadline(client, world, make_agent, container, telephony, session_factory, monkeypatch):
    """after-stream asks for a watchdog at the transfer's deadline; the watchdog (run here with the deadline already
    behind it, so no wall-clock timing is involved) gives the still-ringing transfer up."""
    monkeypatch.setattr("supportvoice.container.TRANSFER_WATCHDOG_GRACE_SECONDS", 0)
    watched = []
    monkeypatch.setattr(container, "watch_transfer", lambda *args: watched.append(args))
    await make_agent("Ann")
    await after_stream(client, world)
    await settle(container)
    transfer = await transfer_row(session_factory, world)
    assert watched == [(world.transfer_id, world.org_id, transfer.deadline_at)]
    past = datetime.now(UTC) - timedelta(seconds=1)
    async with session_factory() as s:
        await s.execute(update(CallTransfer).where(CallTransfer.id == world.transfer_id).values(deadline_at=past))
        await s.commit()
    watchdog = type(container).watch_transfer(container, world.transfer_id, world.org_id, past)
    await asyncio.wait_for(watchdog, timeout=10)
    await settle(container)
    transfer = await transfer_row(session_factory, world)
    assert transfer.status == "no_answer"
    end_leg, redirect = updated(telephony)
    assert end_leg == (transfer.current_leg_sid, None, True)
    sid, body, end = redirect
    assert (sid, end) == (CALL_SID, False)
    assert strings_for("en")["voice_transfer_failed_phone"] in body and body.endswith("<Hangup/></Response>")
    async with session_factory() as s:
        conversation = await s.get(Conversation, world.conversation_id)
    assert conversation.callback_status == "requested"


async def test_watchdog_is_cancelled_at_shutdown(client, world, make_agent, container):
    await make_agent("Ann")
    await after_stream(client, world)
    [watchdog] = list(container._watchdogs)
    await container.aclose()
    assert watchdog.cancelled()
    # Once shutdown has begun no transfer task starts (it would outlive the adapters); a second close is a no-op.
    ref = TelephonyAccountRef(ACCOUNT_SID, AUTH_TOKEN)
    assert container.run_actions(world.org_id, ref, [EndLeg("CA" + "e" * 32)]) is None
    assert container.watch_transfer(world.transfer_id, world.org_id, datetime.now(UTC)) is None
    assert not container._transfer_work and not container._watchdogs
    await container.aclose()


# --- hold, signatures, logs ---------------------------------------------------------------------------------------


async def test_hold_plays_track_and_pauses_when_gone(client, world):
    root = parse(await post_signed(client, HOLD, form(CallSid=CALL_SID), query=f"transfer_id={world.transfer_id}"))
    [play] = list(root)
    assert (play.tag, play.get("loop"), play.text) == (
        "Play", "0", "https://com.twilio.music.classical.s3.amazonaws.com/BusyStrings.mp3",
    )
    for query in (f"transfer_id={uuid.uuid4()}", "transfer_id=nope", ""):
        # A waitUrl may only Play, Say, Pause or Redirect: no transfer means a short pause, never <Hangup/>.
        root = parse(await post_signed(client, HOLD, form(CallSid=CALL_SID), query=query))
        assert [child.tag for child in root] == ["Pause"]


async def test_transfer_routes_reject_bad_signature(client, world, make_agent, container, telephony, session_factory):
    await make_agent("Ann")
    bad = "x" * 32
    leg = leg_query(world)
    for path, params, query in (
        (HOLD, form(CallSid=CALL_SID), f"transfer_id={world.transfer_id}"),
        (AGENT_ANSWER, form(CallSid="CA" + "a" * 32), leg),
        (AGENT_ACCEPT, form(CallSid="CA" + "a" * 32, Digits="1"), leg),
        (AGENT_STATUS, form(CallSid="CA" + "a" * 32, CallStatus="completed"), leg),
        (AFTER_STREAM, form(CallSid=CALL_SID), f"call_id={world.call_id}"),
    ):
        response = await post_signed(client, path, params, token=bad, query=query)
        assert (response.status_code, response.content) == (403, b""), path
        unsigned = await client.post(f"{path}?{query}", data=params)
        assert (unsigned.status_code, unsigned.content) == (403, b""), path
    # A signature over another query string (the attempt changed) is refused too.
    params = form(CallSid="CA" + "a" * 32, CallStatus="completed")
    response = await client.post(
        f"{AGENT_STATUS}?{leg_query(world, 2)}",
        data=params,
        headers=signed_headers(AGENT_STATUS, params, query=leg_query(world, 1)),
    )
    assert (response.status_code, response.content) == (403, b"")
    # (the same signature on the query it was made for is accepted)
    response = await client.post(
        f"{AGENT_STATUS}?{leg_query(world, 1)}",
        data=params,
        headers=signed_headers(AGENT_STATUS, params, query=leg_query(world, 1)),
    )
    assert response.status_code == 204
    await settle(container)
    assert created(telephony) == []
    transfer = await transfer_row(session_factory, world)
    assert (transfer.status, transfer.attempt) == ("ringing", 0)


async def test_another_organisations_transfer_is_not_found(client, world, make_agent, container, session_factory):
    """A transfer id is looked up within the signing account's organisation only: org A's account cannot answer,
    accept, end or join org B's transfer."""
    other = await make_world(
        session_factory, container, email="other@x.com", account_sid="AC" + "9" * 32, e164="+15005550009",
        call_sid="CA" + "9" * 32,
    )
    async with session_factory() as s:
        owner = (await s.execute(select(Membership).where(Membership.organisation_id == other.org_id))).scalar_one()
        await s.execute(
            update(CallTransfer)
            .where(CallTransfer.id == other.transfer_id)
            .values(attempt=1, current_membership_id=owner.id, current_target="phone")
        )
        await s.commit()
    assert [c.tag for c in parse(await answer(client, other))] == ["Hangup"]
    assert [c.tag for c in parse(await accept(client, other))] == ["Hangup"]
    assert (await leg_status(client, other, "completed")).status_code == 204
    hold_query = f"transfer_id={other.transfer_id}"
    assert [c.tag for c in parse(await post_signed(client, HOLD, form(CallSid=CALL_SID), query=hold_query))] == [
        "Pause"
    ]
    transfer = await transfer_row(session_factory, other)
    assert (transfer.status, transfer.attempt, transfer.current_membership_id) == ("ringing", 1, owner.id)


async def test_no_phone_numbers_in_logs(client, world, make_agent, container, telephony, session_factory, caplog, capsys):
    caplog.set_level("DEBUG")
    await _phone_only(session_factory, world)
    await make_agent("Bob", preference="phone", phone=AGENT_PHONE, softphone=False)
    await make_agent("Cat", preference="phone", phone=OTHER_AGENT_PHONE, softphone=False)
    await after_stream(client, world)
    await settle(container)
    parse(await answer(client, world))
    await leg_status(client, world, "no-answer")
    await settle(container)
    parse(await answer(client, world, attempt=2))
    parse(await accept(client, world, attempt=2))
    await post_signed(client, STATUS, form(CallSid=CALL_SID, CallStatus="completed", CallDuration="9"))
    await settle(container)
    assert len(created(telephony)) == 2
    out = capsys.readouterr()
    logged = caplog.text + out.out + out.err
    assert "transfer_leg_placed" in logged
    for secret in (CALLER_E164, AGENT_PHONE, OTHER_AGENT_PHONE, NUMBER_E164, AUTH_TOKEN):
        for form_of in (secret, secret.lstrip("+"), secret[2:]):
            assert form_of not in logged, form_of


# --- fix round 1 ----------------------------------------------------------------------------------------------------


CONFERENCE_STATUS = "/twilio/voice/conference-status"


async def joined(client, world, sid, event="participant-join"):
    params = form(CallSid=sid, ConferenceSid="CF" + "0" * 32, StatusCallbackEvent=event)
    return await post_signed(client, CONFERENCE_STATUS, params, query=f"transfer_id={world.transfer_id}")


async def test_agent_leg_dropped_before_join_gives_up(client, world, make_agent, container, telephony, session_factory):
    """The agent answered (the transfer is connected) but their leg ended before it entered the room: the caller is
    not left on hold; they hear the failure line and the conversation becomes a callback."""
    await make_agent("Ann")
    await after_stream(client, world)
    await settle(container)
    _conference(parse(await answer(client, world)))
    assert (await transfer_row(session_factory, world)).status == "connected"
    assert (await joined(client, world, CALL_SID)).status_code == 204  # the caller's own join does not count
    assert (await transfer_row(session_factory, world)).agent_joined_at is None

    assert (await leg_status(client, world, "completed")).status_code == 204
    await settle(container)
    [(sid, body, end)] = updated(telephony)
    assert (sid, end) == (CALL_SID, False)
    assert strings_for("en")["voice_transfer_failed_phone"] in body
    transfer = await transfer_row(session_factory, world)
    assert (transfer.status, transfer.ended_at is not None) == ("no_answer", True)
    async with session_factory() as s:
        conversation = await s.get(Conversation, world.conversation_id)
        call = await s.get(Call, world.call_id)
    assert conversation.callback_status == "requested"
    assert call.end_reason == "transfer_no_answer"


async def test_agent_joined_then_hung_up_changes_nothing(client, world, make_agent, container, telephony, session_factory):
    await make_agent("Ann")
    await after_stream(client, world)
    await settle(container)
    _conference(parse(await answer(client, world)))
    leg_sid = (await transfer_row(session_factory, world)).current_leg_sid
    assert (await joined(client, world, leg_sid, event="participant-leave")).status_code == 204  # not a join
    assert (await transfer_row(session_factory, world)).agent_joined_at is None
    assert (await joined(client, world, leg_sid)).status_code == 204
    assert (await transfer_row(session_factory, world)).agent_joined_at is not None

    assert (await leg_status(client, world, "completed")).status_code == 204  # the agent hangs up
    await settle(container)
    assert updated(telephony) == []
    transfer = await transfer_row(session_factory, world)
    assert (transfer.status, transfer.ended_at) == ("connected", None)
    async with session_factory() as s:
        conversation = await s.get(Conversation, world.conversation_id)
        call = await s.get(Call, world.call_id)
    assert conversation.callback_status is None
    assert call.end_reason == "transferred"


async def test_conference_status_rejects_bad_signature(client, world):
    params = form(CallSid=CALL_SID, StatusCallbackEvent="participant-join")
    response = await post_signed(
        client, CONFERENCE_STATUS, params, token="x" * 32, query=f"transfer_id={world.transfer_id}"
    )
    assert (response.status_code, response.content) == (403, b"")


async def test_retried_after_stream_after_give_up_says_failed_line(client, world, container):
    for _ in range(2):  # nobody online: the first response gave up; a retry (first response lost) says it again
        root = parse(await after_stream(client, world))
        assert root.find("Say").text == strings_for("en")["voice_transfer_failed_phone"]
        assert root.find("Hangup") is not None


async def test_unexpected_create_call_error_rings_next(client, world, make_agent, container, telephony, session_factory, monkeypatch):
    ann, ben = await make_agent("Ann"), await make_agent("Ben")
    original = telephony.create_call
    failed = []

    async def boom_first(account, to, from_, webhooks, timeout_seconds):
        if not failed:
            failed.append(to)
            raise RuntimeError("unexpected")
        return await original(account, to, from_, webhooks, timeout_seconds)

    monkeypatch.setattr(telephony, "create_call", boom_first)
    await after_stream(client, world)
    await settle(container)
    assert failed == [f"client:agent-{ann.id}"]
    [(to, *_)] = created(telephony)
    assert to == f"client:agent-{ben.id}"
    transfer = await transfer_row(session_factory, world)
    assert (transfer.attempt, transfer.current_membership_id) == (2, ben.id)


async def test_leg_is_ended_when_its_sid_cannot_be_stored(client, world, make_agent, container, telephony, session_factory, monkeypatch):
    await make_agent("Ann")
    original = telephony.create_call
    placed = []

    async def remember(*args):
        sid = await original(*args)
        placed.append(sid)
        return sid

    async def broken(self, transfer_id, attempt, sid):
        raise RuntimeError("database went away")

    monkeypatch.setattr(telephony, "create_call", remember)
    monkeypatch.setattr(TransferService, "set_leg_sid", broken)
    await after_stream(client, world)
    await settle(container)
    assert updated(telephony) == [(placed[0], None, True)]
    assert (await transfer_row(session_factory, world)).current_leg_sid is None


async def test_agent_status_before_caller_status_after_join_keeps_transferred(
    client, world, make_agent, container, telephony, session_factory
):
    """The real ending: the agent and the caller leave the room together (endConferenceOnExit). When the agent leg's
    status callback wins the race, the call still counts as transferred: no redirect, no callback ticket."""
    await make_agent("Ann")
    await after_stream(client, world)
    await settle(container)
    _conference(parse(await answer(client, world)))
    leg_sid = (await transfer_row(session_factory, world)).current_leg_sid
    assert (await joined(client, world, CALL_SID)).status_code == 204  # the caller is in the room (first)
    assert (await joined(client, world, leg_sid)).status_code == 204  # the agent joins

    assert (await leg_status(client, world, "completed")).status_code == 204  # the agent's terminal arrives first
    await settle(container)
    caller_status = form(CallSid=CALL_SID, CallStatus="completed", CallDuration="240")
    assert (await post_signed(client, STATUS, caller_status)).status_code == 204
    await settle(container)
    assert updated(telephony) == []
    transfer = await transfer_row(session_factory, world)
    assert (transfer.status, transfer.ended_at is not None) == ("connected", True)
    async with session_factory() as s:
        call = await s.get(Call, world.call_id)
        conversation = await s.get(Conversation, world.conversation_id)
    assert (call.finalised, call.end_reason) == (True, "transferred")
    assert conversation.callback_status is None
