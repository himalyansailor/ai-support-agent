"""TransferService (Task 5): the ringing state machine of a live transfer. Real Postgres with committed data (the
session_factory fixture truncates at teardown), memory presence and softphones, a recording memory pub/sub. No
telephony runs here: the Twilio work is the actions the service returns, and those are asserted."""

import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from supportcore.db.models import (
    Call,
    CallTransfer,
    Conversation,
    IntegrationEvent,
    Membership,
    PhoneNumber,
    Role,
    TelephonyAccount,
    User,
    VoiceSettings,
)
from supportcore.db.uow import UnitOfWork
from supportcore.errors import NotFoundError
from supportcore.events import EventBus
from supportcore.inbox.publisher import InboxPublisher
from supportcore.orgs.service import OrganisationService
from supportcore.presence.memory_presence import MemoryAgentPresence
from supportcore.pubsub.memory_pubsub import MemoryPubSub
from supportcore.realtime.spawn import drain_pending
from supportcore.support.settings_service import SupportSettingsService
from supportcore.voice.calls import CallService
from supportcore.voice.settings_service import VoiceSettingsService
from supportcore.voice.transfers import (
    MAX_TRANSFER_ATTEMPTS,
    EndLeg,
    RedirectCaller,
    Ring,
    TransferService,
    transfer_candidates,
)

pytestmark = pytest.mark.asyncio

ALL_DAY = {day: [["00:00", "24:00"]] for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
CALLER = "+14155550123"
NUMBER = "+15005550006"
CALL_SID = "CA" + "c" * 32


class RecordingPubSub(MemoryPubSub):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[tuple[str, dict]] = []

    async def publish(self, channel: str, message: dict) -> None:
        self.published.append((channel, message))
        await super().publish(channel, message)


@dataclass
class World:
    org_id: uuid.UUID
    owner_membership_id: uuid.UUID
    call_id: uuid.UUID
    conversation_id: uuid.UUID
    number_id: uuid.UUID


@pytest.fixture
def settings(tmp_path):
    from cryptography.fernet import Fernet
    from supportcore.config import Settings
    from supportcore.testing import TEST_DATABASE_URL

    return Settings(
        _env_file=None,
        env="test",
        database_url=TEST_DATABASE_URL,
        redis_url="redis://localhost:6379/0",
        jwt_secret="test-secret-test-secret-test-secret-123",
        smtp_from="noreply@test.local",
        secrets_key=Fernet.generate_key().decode(),
        file_storage="local",
        local_storage_path=str(tmp_path / "uploads"),
        vector_store="chroma",
        rate_limiter="memory",
        pubsub="memory",
        presence="memory",
        checkpointer="memory",
    )


@pytest.fixture
async def adapters(settings):
    from supportcore.wiring import build_adapters

    built = replace(
        build_adapters(settings),
        pubsub=RecordingPubSub(),
        presence=MemoryAgentPresence(),
        softphones=MemoryAgentPresence(namespace="softphone"),
    )
    yield built
    await drain_pending()
    await built.aclose()


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def publisher(adapters, session_factory):
    return InboxPublisher(adapters.pubsub, session_factory)


async def _make_org(s, bus, email, name):
    owner = User(email=email, password_hash="x", name="Owner")
    s.add(owner)
    await s.flush()
    org = await OrganisationService(s, bus).create(owner, name)
    owner_membership = (await s.execute(select(Membership).where(Membership.user_id == owner.id))).scalar_one()
    owner_membership.agent_status = "offline"  # never a candidate here
    (await SupportSettingsService(s, org.id, bus).get()).weekly_hours = ALL_DAY
    await VoiceSettingsService(s, org.id, bus).get()  # defaults: browser transfer on, phone transfer off
    return org, owner_membership


@pytest.fixture
async def world(session_factory, bus, publisher, adapters, settings) -> World:
    async with UnitOfWork(session_factory) as uow:
        s = uow.session
        org, owner_membership = await _make_org(s, bus, "owner@x.com", "Acme")
        account = TelephonyAccount(
            organisation_id=org.id, kind="byo", account_sid="AC" + "f" * 32, auth_token_encrypted="x",
            auth_token_last4="xxxx",
        )
        s.add(account)
        await s.flush()
        number = PhoneNumber(
            organisation_id=org.id, telephony_account_id=account.id, e164=NUMBER, provider_sid="PN" + "0" * 32,
            ownership="byo",
        )
        s.add(number)
        await s.flush()
        call = await CallService(s, org.id, bus, publisher, adapters=adapters, settings=settings).start_phone_call(
            number, CALL_SID, CALLER
        )
        await uow.commit()
    return World(org.id, owner_membership.id, call.id, call.conversation_id, number.id)


@pytest.fixture
def make_agent(session_factory, world, adapters):
    async def make(
        name, *, preference="browser", phone=None, softphone=True, present=True, status="online", role="Agent"
    ):
        async with UnitOfWork(session_factory) as uow:
            s = uow.session
            user = User(email=f"{uuid.uuid4().hex[:8]}@x.com", password_hash="x", name=name)
            s.add(user)
            await s.flush()
            role_id = (
                await s.execute(select(Role.id).where(Role.organisation_id == world.org_id, Role.name == role))
            ).scalar_one()
            m = Membership(
                organisation_id=world.org_id, user_id=user.id, role_id=role_id, agent_status=status,
                call_preference=preference, phone_number=phone,
            )
            s.add(m)
            await uow.commit()
        if present:
            await adapters.presence.beat(world.org_id, m.id)
        if softphone:
            await adapters.softphones.beat(world.org_id, m.id)
        return m

    return make


@pytest.fixture
def act(session_factory, world, bus, publisher, adapters, settings):
    """Runs `fn(service)` in one committed unit of work (as a route would), then lets the publishes go out."""

    async def run(fn, org_id=None):
        async with UnitOfWork(session_factory) as uow:
            service = TransferService(
                uow.session, org_id or world.org_id, bus, publisher, adapters, settings, session_factory=session_factory
            )
            result = await fn(service)
            await uow.commit()
        await drain_pending()
        return result

    return run


@pytest.fixture
def begun(act, world):
    """Begins the transfer for the world's call and starts ringing: (transfer id, the first actions)."""

    async def run():
        transfer = await act(lambda s: s.begin(world.call_id))
        actions = await act(lambda s: s.start(transfer.id))
        return transfer.id, actions

    return run


async def _set(session_factory, model, row_id, **values):
    async with session_factory() as s:
        await s.execute(update(model).where(model.id == row_id).values(**values))
        await s.commit()


async def _voice(session_factory, org_id, **values):
    async with session_factory() as s:
        await s.execute(update(VoiceSettings).where(VoiceSettings.organisation_id == org_id).values(**values))
        await s.commit()


async def _get(session_factory, model, row_id):
    async with session_factory() as s:
        return await s.get(model, row_id)


async def _candidates(session_factory, world, adapters):
    async with session_factory() as s:
        conversation = await s.get(Conversation, world.conversation_id)
        voice = (await s.execute(select(VoiceSettings).where(VoiceSettings.organisation_id == world.org_id))).scalar_one()
        found = await transfer_candidates(s, world.org_id, adapters, conversation, voice, datetime.now(UTC))
    return [(m.id, target, to) for m, target, to in found]


def _inbox(adapters, world, kind=None):
    return [
        m for c, m in adapters.pubsub.published
        if c == f"inbox:{world.org_id.hex}" and (kind is None or m["type"] == kind)
    ]


def _browser(m):
    return f"client:agent-{m.id}"


# --- candidates ------------------------------------------------------------------------------------------------


async def test_can_transfer_false_without_reachable_agents(act, world, make_agent, adapters, session_factory):
    ann = await make_agent("Ann", softphone=False)  # online and present, but no softphone registered
    assert await act(lambda s: s.can_transfer(world.call_id)) is False
    await adapters.softphones.beat(world.org_id, ann.id)
    assert await act(lambda s: s.can_transfer(world.call_id)) is True
    await _voice(session_factory, world.org_id, transfer_browser_enabled=False)  # no target allowed
    assert await act(lambda s: s.can_transfer(world.call_id)) is False
    await _voice(session_factory, world.org_id, transfer_browser_enabled=True)
    await _set(session_factory, Call, world.call_id, source="callback", direction="outbound")
    assert await act(lambda s: s.can_transfer(world.call_id)) is False  # a callback call is never transferred


async def test_assignee_rung_first_then_round_robin_order(act, world, make_agent, begun, session_factory):
    a, b, c = [await make_agent(name) for name in ("A", "B", "C")]
    await _set(session_factory, Conversation, world.conversation_id, assignee_membership_id=c.id)
    transfer_id, actions = await begun()
    assert actions == [Ring(transfer_id, 1, c.id, "browser", _browser(c), NUMBER)]
    second = await act(lambda s: s.leg_ended(transfer_id, 1, "no-answer"))
    assert second == [Ring(transfer_id, 2, a.id, "browser", _browser(a), NUMBER)]
    third = await act(lambda s: s.leg_ended(transfer_id, 2, "busy"))
    assert third == [Ring(transfer_id, 3, b.id, "browser", _browser(b), NUMBER)]
    transfer = await _get(session_factory, CallTransfer, transfer_id)
    assert transfer.tried_membership_ids == [str(c.id), str(a.id), str(b.id)]
    assert (transfer.attempt, transfer.current_membership_id, transfer.current_target) == (3, b.id, "browser")
    assert "+1" not in repr(third)  # numbers stay out of the action's repr


async def test_browser_needs_softphone_present(world, make_agent, adapters, session_factory):
    await make_agent("NoTab", softphone=False)
    tab = await make_agent("Tab")
    assert await _candidates(session_factory, world, adapters) == [(tab.id, "browser", _browser(tab))]
    await adapters.softphones.clear(world.org_id, tab.id)
    assert await _candidates(session_factory, world, adapters) == []


async def test_phone_needs_e164_and_from_number(world, make_agent, adapters, session_factory):
    await _voice(session_factory, world.org_id, transfer_browser_enabled=False, transfer_phone_enabled=True)
    await make_agent("NoNumber", preference="phone")
    await make_agent("Local", preference="phone", phone="415 555 0100")  # not E.164
    good = await make_agent("Good", preference="phone", phone="+14155550199")
    assert await _candidates(session_factory, world, adapters) == [(good.id, "phone", "+14155550199")]
    # No number to call from: the call's number is gone and the organisation has no enabled one.
    await _set(session_factory, Call, world.call_id, phone_number_id=None)
    await _set(session_factory, PhoneNumber, world.number_id, enabled=False)
    assert await _candidates(session_factory, world, adapters) == []


async def test_preference_falls_back_to_allowed_target(world, make_agent, adapters, session_factory):
    # Phone transfer is off: an agent who prefers the phone is rung in the browser.
    phone_fan = await make_agent("PhoneFan", preference="phone", phone="+14155550101")
    assert await _candidates(session_factory, world, adapters) == [(phone_fan.id, "browser", _browser(phone_fan))]
    # Both allowed: the preference wins; a browser fan with no tab open falls back to the phone.
    await _voice(session_factory, world.org_id, transfer_phone_enabled=True)
    tabless = await make_agent("Tabless", preference="browser", phone="+14155550102", softphone=False)
    assert await _candidates(session_factory, world, adapters) == [
        (phone_fan.id, "phone", "+14155550101"),
        (tabless.id, "phone", "+14155550102"),
    ]


async def test_agent_on_live_transfer_skipped(world, make_agent, adapters, session_factory, bus, publisher, settings):
    busy, free = await make_agent("Busy"), await make_agent("Free")
    async with UnitOfWork(session_factory) as uow:  # Busy is on another caller's transferred call
        number = await uow.session.get(PhoneNumber, world.number_id)
        other = await CallService(uow.session, world.org_id, bus, publisher).start_phone_call(
            number, "CA" + "d" * 32, "+14155550777"
        )
        live = CallTransfer(
            organisation_id=world.org_id, call_id=other.id, conversation_id=other.conversation_id, status="connected",
            connected_membership_id=busy.id, deadline_at=datetime.now(UTC), connected_at=datetime.now(UTC),
        )
        uow.session.add(live)
        await uow.commit()
    assert await _candidates(session_factory, world, adapters) == [(free.id, "browser", _browser(free))]
    await _set(session_factory, CallTransfer, live.id, ended_at=datetime.now(UTC))  # that call is over
    assert [m for m, _, _ in await _candidates(session_factory, world, adapters)] == [busy.id, free.id]


async def _other_call(session_factory, world, bus, publisher, sid_char):
    async with UnitOfWork(session_factory) as uow:
        number = await uow.session.get(PhoneNumber, world.number_id)
        other = await CallService(uow.session, world.org_id, bus, publisher).start_phone_call(
            number, "CA" + sid_char * 32, "+14155550777"
        )
        await uow.commit()
    return other


async def _transfer_on(session_factory, world, call, **values):
    async with UnitOfWork(session_factory) as uow:
        transfer = CallTransfer(
            organisation_id=world.org_id, call_id=call.id, conversation_id=call.conversation_id,
            deadline_at=datetime.now(UTC), **values,
        )
        uow.session.add(transfer)
        await uow.commit()
    return transfer


async def test_connected_transfer_busy_only_while_recent_and_call_not_finalised(
    world, make_agent, adapters, session_factory, bus, publisher
):
    agent = await make_agent("Agent")
    other = await _other_call(session_factory, world, bus, publisher, "d")
    now = datetime.now(UTC)
    live = await _transfer_on(
        session_factory, world, other, status="connected", connected_membership_id=agent.id,
        connected_at=now - timedelta(hours=3, minutes=59),
    )
    assert await _candidates(session_factory, world, adapters) == []
    # Its end was never recorded, but it is older than MAX_TRANSFERRED_CALL_HOURS: no longer counted.
    await _set(session_factory, CallTransfer, live.id, connected_at=now - timedelta(hours=4, minutes=1))
    assert [m for m, _, _ in await _candidates(session_factory, world, adapters)] == [agent.id]
    # Recent again, but the call is finalised: not counted either.
    await _set(session_factory, CallTransfer, live.id, connected_at=now)
    await _set(session_factory, Call, other.id, finalised=True)
    assert [m for m, _, _ in await _candidates(session_factory, world, adapters)] == [agent.id]


async def test_finalise_records_the_connected_transfer_end(world, make_agent, adapters, session_factory, bus, publisher):
    agent = await make_agent("Agent")
    other = await _other_call(session_factory, world, bus, publisher, "d")
    live = await _transfer_on(
        session_factory, world, other, status="connected", connected_membership_id=agent.id,
        connected_at=datetime.now(UTC),
    )
    async with UnitOfWork(session_factory) as uow:
        assert await CallService(uow.session, world.org_id, bus, publisher).finalise(other.id, "completed", 60)
        await uow.commit()
    assert (await _get(session_factory, CallTransfer, live.id)).ended_at is not None
    assert [m for m, _, _ in await _candidates(session_factory, world, adapters)] == [agent.id]


async def test_lost_caller_status_then_sweep_frees_the_agent(
    world, make_agent, adapters, session_factory, bus, publisher, settings
):
    from types import SimpleNamespace

    from supportcore.voice.jobs import finalise_stale_calls

    agent = await make_agent("Agent")
    other = await _other_call(session_factory, world, bus, publisher, "d")
    now = datetime.now(UTC)
    # The caller's terminal status callback never arrived: the call and its transfer were never closed.
    await _set(session_factory, Call, other.id, status="in_progress", started_at=now - timedelta(hours=5))
    await _set(session_factory, Call, world.call_id, started_at=now)
    live = await _transfer_on(
        session_factory, world, other, status="connected", connected_membership_id=agent.id,
        connected_at=now - timedelta(hours=4, minutes=1),
    )
    runtime = SimpleNamespace(session_factory=session_factory, events=bus, adapters=adapters, settings=settings)
    assert await finalise_stale_calls(runtime) == 1
    await drain_pending()
    assert (await _get(session_factory, Call, other.id)).finalised is True
    assert (await _get(session_factory, CallTransfer, live.id)).ended_at is not None
    assert [m for m, _, _ in await _candidates(session_factory, world, adapters)] == [agent.id]


async def test_agent_ringing_for_another_call_is_busy(world, make_agent, adapters, session_factory, bus, publisher):
    rung, free = await make_agent("Rung"), await make_agent("Free")
    other = await _other_call(session_factory, world, bus, publisher, "d")
    ringing = await _transfer_on(
        session_factory, world, other, status="ringing", attempt=1, current_membership_id=rung.id,
        current_target="browser", tried_membership_ids=[str(rung.id)],
    )
    assert await _candidates(session_factory, world, adapters) == [(free.id, "browser", _browser(free))]
    await _set(session_factory, CallTransfer, ringing.id, status="no_answer", ended_at=datetime.now(UTC))
    assert [m for m, _, _ in await _candidates(session_factory, world, adapters)] == [rung.id, free.id]


async def test_unreachable_agents_skipped(act, world, make_agent, begun, session_factory):
    await make_agent("Away", status="away")
    await make_agent("Gone", present=False)
    await make_agent("NoTab", softphone=False)
    await make_agent("Viewer", role="Viewer")  # no queries.answer
    ok = await make_agent("Ok")
    transfer_id, actions = await begun()
    assert [a.membership_id for a in actions if isinstance(a, Ring)] == [ok.id]
    after = await act(lambda s: s.leg_ended(transfer_id, 1, "no-answer"))  # nobody else can be reached
    assert [type(a) for a in after] == [RedirectCaller]
    assert (await _get(session_factory, CallTransfer, transfer_id)).status == "no_answer"


# --- answering and accepting ------------------------------------------------------------------------------------


async def test_accept_connects_and_assigns(act, world, make_agent, begun, session_factory, adapters):
    ann = await make_agent("Ann")
    transfer_id, _ = await begun()
    assert await act(lambda s: s.accept(transfer_id, 1)) == (True, [])
    transfer = await _get(session_factory, CallTransfer, transfer_id)
    assert (transfer.status, transfer.connected_membership_id) == ("connected", ann.id)
    assert transfer.connected_at is not None and transfer.ended_at is None
    conversation = await _get(session_factory, Conversation, world.conversation_id)
    assert (conversation.status, conversation.assignee_membership_id) == ("open", ann.id)
    assert (await _get(session_factory, Call, world.call_id)).end_reason == "transferred"
    assert {"type": "call_offer_ended", "transfer_id": str(transfer_id), "membership_id": str(ann.id),
            "outcome": "accepted"} in _inbox(adapters, world)
    assert any(m["conversation"]["id"] == str(world.conversation_id) for m in _inbox(adapters, world, "conversation.upsert"))
    # The caller hangs up later: the connected transfer only records its end.
    assert await act(lambda s: s.caller_left(world.call_id)) == []
    transfer = await _get(session_factory, CallTransfer, transfer_id)
    assert transfer.status == "connected" and transfer.ended_at is not None
    assert (await _get(session_factory, Call, world.call_id)).end_reason == "transferred"


async def test_agent_answered_browser_joins_phone_gathers(act, world, make_agent, begun, session_factory):
    await _voice(session_factory, world.org_id, transfer_phone_enabled=True)
    phone = await make_agent("Phone", preference="phone", phone="+14155550101")
    browser = await make_agent("Browser")
    transfer_id, actions = await begun()
    assert actions[0].target == "phone" and actions[0].membership_id == phone.id
    assert await act(lambda s: s.agent_answered(transfer_id, 1)) == "gather"
    assert (await _get(session_factory, CallTransfer, transfer_id)).status == "ringing"  # waits for "press 1"
    await act(lambda s: s.leg_ended(transfer_id, 1, "completed"))  # voicemail took it and nobody pressed 1
    assert await act(lambda s: s.agent_answered(transfer_id, 1)) == "taken"
    assert await act(lambda s: s.agent_answered(transfer_id, 2)) == "join"  # the browser leg connects at once
    transfer = await _get(session_factory, CallTransfer, transfer_id)
    assert (transfer.status, transfer.connected_membership_id) == ("connected", browser.id)
    # A repeated answer for the leg that connected joins it again (a Twilio retry must not hang up the agent).
    assert await act(lambda s: s.agent_answered(transfer_id, 2)) == "join"
    assert await act(lambda s: s.agent_answered(transfer_id, 1)) == "taken"
    assert (await _get(session_factory, CallTransfer, transfer_id)).connected_at == transfer.connected_at  # once


async def test_accept_after_give_up_is_refused(act, world, make_agent, begun, session_factory):
    await make_agent("Ann")
    transfer_id, _ = await begun()
    await _set(session_factory, CallTransfer, transfer_id, deadline_at=datetime.now(UTC) - timedelta(seconds=1))
    assert [type(a) for a in await act(lambda s: s.deadline_passed(transfer_id))] == [RedirectCaller]
    assert await act(lambda s: s.accept(transfer_id, 1)) == (False, [])
    transfer = await _get(session_factory, CallTransfer, transfer_id)
    assert (transfer.status, transfer.connected_membership_id) == ("no_answer", None)
    assert (await _get(session_factory, Call, world.call_id)).end_reason == "transfer_no_answer"


async def test_only_current_attempt_can_accept(act, world, make_agent, begun, session_factory):
    a, b = await make_agent("A"), await make_agent("B")
    transfer_id, _ = await begun()
    await act(lambda s: s.leg_ended(transfer_id, 1, "no-answer"))
    assert await act(lambda s: s.accept(transfer_id, 1)) == (False, [])  # A's leg is no longer the one ringing
    assert await act(lambda s: s.accept(transfer_id, 3)) == (False, [])
    assert await act(lambda s: s.accept(transfer_id, 2)) == (True, [])
    connected = await _get(session_factory, CallTransfer, transfer_id)
    # The same leg's accept again (a retried "1"): it still holds the call; the earlier attempt still loses.
    assert await act(lambda s: s.accept(transfer_id, 2)) == (True, [])
    assert await act(lambda s: s.accept(transfer_id, 1)) == (False, [])
    transfer = await _get(session_factory, CallTransfer, transfer_id)
    assert transfer.connected_membership_id == b.id and a.id != b.id
    assert transfer.connected_at == connected.connected_at  # connected once
    await act(lambda s: s.caller_left(world.call_id))  # the call is over: a late repeat is refused
    assert await act(lambda s: s.accept(transfer_id, 2)) == (False, [])
    assert await act(lambda s: s.agent_answered(transfer_id, 2)) == "taken"


# --- moving on and giving up ------------------------------------------------------------------------------------


async def test_leg_ended_rings_next_until_three_attempts(act, world, make_agent, begun, session_factory):
    agents = [await make_agent(name) for name in ("A", "B", "C", "D")]
    transfer_id, actions = await begun()
    rung = [actions[0].membership_id]
    for attempt in (1, 2):
        (ring,) = await act(lambda s, n=attempt: s.leg_ended(transfer_id, n, "no-answer"))
        assert ring.attempt == attempt + 1
        rung.append(ring.membership_id)
    assert rung == [m.id for m in agents[:MAX_TRANSFER_ATTEMPTS]]
    assert await act(lambda s: s.leg_ended(transfer_id, 2, "no-answer")) == []  # a stale leg's callback is ignored
    final = await act(lambda s: s.leg_ended(transfer_id, 3, "no-answer"))
    assert final == [RedirectCaller(CALL_SID, "voice_transfer_failed_phone", "en")]  # the leg is over: no EndLeg
    transfer = await _get(session_factory, CallTransfer, transfer_id)
    assert (transfer.status, transfer.attempt, transfer.ended_at is not None) == ("no_answer", 3, True)
    assert await act(lambda s: s.leg_ended(transfer_id, 3, "no-answer")) == []


async def test_deadline_gives_up_with_redirect_and_callback(act, world, make_agent, begun, session_factory, adapters):
    ann = await make_agent("Ann")
    transfer_id, _ = await begun()
    assert await act(lambda s: s.set_leg_sid(transfer_id, 1, "CA" + "1" * 32)) == []
    assert await act(lambda s: s.deadline_passed(transfer_id)) == []  # not yet
    await _set(session_factory, CallTransfer, transfer_id, deadline_at=datetime.now(UTC) - timedelta(seconds=1))
    actions = await act(lambda s: s.deadline_passed(transfer_id))
    assert actions == [EndLeg("CA" + "1" * 32), RedirectCaller(CALL_SID, "voice_transfer_failed_phone", "en")]
    transfer = await _get(session_factory, CallTransfer, transfer_id)
    assert transfer.status == "no_answer" and transfer.ended_at is not None
    conversation = await _get(session_factory, Conversation, world.conversation_id)
    assert (conversation.callback_status, conversation.callback_phone) == ("requested", CALLER)
    assert conversation.status == "open"  # with the team as a callback ticket
    assert (await _get(session_factory, Call, world.call_id)).end_reason == "transfer_no_answer"
    assert {"type": "call_offer_ended", "transfer_id": str(transfer_id), "membership_id": str(ann.id),
            "outcome": "missed"} in _inbox(adapters, world)
    assert await act(lambda s: s.deadline_passed(transfer_id)) == []  # once


async def test_caller_left_cancels_ringing_leg(act, world, make_agent, begun, session_factory, adapters):
    ann = await make_agent("Ann")
    transfer_id, _ = await begun()
    await act(lambda s: s.set_leg_sid(transfer_id, 1, "CA" + "1" * 32))
    assert await act(lambda s: s.caller_left(world.call_id)) == [EndLeg("CA" + "1" * 32)]
    transfer = await _get(session_factory, CallTransfer, transfer_id)
    assert transfer.status == "abandoned" and transfer.ended_at is not None
    conversation = await _get(session_factory, Conversation, world.conversation_id)
    assert conversation.callback_status == "requested"
    assert (await _get(session_factory, Call, world.call_id)).end_reason == "transfer_no_answer"
    assert {"type": "call_offer_ended", "transfer_id": str(transfer_id), "membership_id": str(ann.id),
            "outcome": "cancelled"} in _inbox(adapters, world)
    assert await act(lambda s: s.leg_ended(transfer_id, 1, "canceled")) == []
    assert await act(lambda s: s.accept(transfer_id, 1)) == (False, [])
    assert await act(lambda s: s.caller_left(world.call_id)) == []
    # A leg sid that arrives after the transfer moved on: that leg is ended at once.
    assert await act(lambda s: s.set_leg_sid(transfer_id, 1, "CA" + "2" * 32)) == [EndLeg("CA" + "2" * 32)]


async def test_leg_refused_moves_on(act, world, make_agent, begun, session_factory):
    a, b = await make_agent("A"), await make_agent("B")
    transfer_id, actions = await begun()
    assert actions[0].membership_id == a.id
    assert await act(lambda s: s.ring_failed(transfer_id, 1)) == [
        Ring(transfer_id, 2, b.id, "browser", _browser(b), NUMBER)
    ]
    final = await act(lambda s: s.ring_failed(transfer_id, 2))
    assert final == [RedirectCaller(CALL_SID, "voice_transfer_failed_phone", "en")]
    assert (await _get(session_factory, CallTransfer, transfer_id)).status == "failed"
    async with session_factory() as s:
        rows = (await s.execute(select(IntegrationEvent))).scalars().all()
    assert [(r.organisation_id, r.kind, r.provider, r.error_type, r.count) for r in rows] == [
        (world.org_id, "voice", "twilio", "transfer_failed", 2)
    ]


# --- events and tenancy -----------------------------------------------------------------------------------------


async def test_call_offer_events_after_commit_ids_only(
    act, world, make_agent, session_factory, adapters, bus, publisher, settings
):
    a, b = await make_agent("A"), await make_agent("B")
    transfer = await act(lambda s: s.begin(world.call_id))
    assert (await act(lambda s: s.begin(world.call_id))).id == transfer.id  # idempotent per call
    async with UnitOfWork(session_factory) as uow:  # rolled back: nothing goes out
        await TransferService(uow.session, world.org_id, bus, publisher, adapters, settings).start(transfer.id)
    await drain_pending()
    assert _inbox(adapters, world, "call_offer") == []
    await act(lambda s: s.start(transfer.id))
    await act(lambda s: s.leg_ended(transfer.id, 1, "no-answer"))
    offers = [m for m in _inbox(adapters, world) if m["type"] in ("call_offer", "call_offer_ended")]
    assert offers == [
        {"type": "call_offer", "transfer_id": str(transfer.id), "conversation_id": str(world.conversation_id),
         "membership_id": str(a.id)},
        {"type": "call_offer_ended", "transfer_id": str(transfer.id), "membership_id": str(a.id), "outcome": "missed"},
        {"type": "call_offer", "transfer_id": str(transfer.id), "conversation_id": str(world.conversation_id),
         "membership_id": str(b.id)},
    ]
    async with session_factory() as s:
        assert len((await s.execute(select(CallTransfer))).scalars().all()) == 1
    assert await act(lambda s: s.start(transfer.id)) == []  # started once


async def test_cross_org_transfer_404(act, world, make_agent, begun, session_factory, bus):
    await make_agent("Ann")
    transfer_id, _ = await begun()
    async with UnitOfWork(session_factory) as uow:
        other, _ = await _make_org(uow.session, bus, "other@x.com", "Other")
        await uow.commit()
    for attempt in (
        lambda s: s.start(transfer_id),
        lambda s: s.accept(transfer_id, 1),
        lambda s: s.agent_answered(transfer_id, 1),
        lambda s: s.leg_ended(transfer_id, 1, "no-answer"),
        lambda s: s.deadline_passed(transfer_id),
        lambda s: s.set_leg_sid(transfer_id, 1, "CA" + "1" * 32),
        lambda s: s.begin(world.call_id),
        lambda s: s.can_transfer(world.call_id),
    ):
        with pytest.raises(NotFoundError):
            await act(attempt, org_id=other.id)
    assert await act(lambda s: s.caller_left(world.call_id), org_id=other.id) == []
    assert (await _get(session_factory, CallTransfer, transfer_id)).status == "ringing"


async def test_agent_joined_records_only_the_connected_agent_leg(act, world, make_agent, begun, session_factory):
    await make_agent("Ann")
    transfer_id, _ = await begun()
    leg = "CA" + "1" * 32
    await act(lambda s: s.set_leg_sid(transfer_id, 1, leg))
    await act(lambda s: s.agent_joined(transfer_id, leg))  # still ringing: not a join that counts
    assert (await _get(session_factory, CallTransfer, transfer_id)).agent_joined_at is None
    assert await act(lambda s: s.agent_answered(transfer_id, 1)) == "join"
    await act(lambda s: s.agent_joined(transfer_id, CALL_SID))  # the caller's leg
    await act(lambda s: s.agent_joined(transfer_id, "CA" + "8" * 32))  # not the stored agent leg
    assert (await _get(session_factory, CallTransfer, transfer_id)).agent_joined_at is None
    await act(lambda s: s.agent_joined(transfer_id, leg))
    joined_at = (await _get(session_factory, CallTransfer, transfer_id)).agent_joined_at
    assert joined_at is not None
    await act(lambda s: s.agent_joined(transfer_id, leg))  # a repeat keeps the first time
    assert (await _get(session_factory, CallTransfer, transfer_id)).agent_joined_at == joined_at

    # Joined, then the agent hangs up: nothing changes (the conference ends with them).
    assert await act(lambda s: s.leg_ended(transfer_id, 1, "completed")) == []
    transfer = await _get(session_factory, CallTransfer, transfer_id)
    assert (transfer.status, transfer.ended_at) == ("connected", None)
    assert (await _get(session_factory, Call, world.call_id)).end_reason == "transferred"
    assert (await _get(session_factory, Conversation, world.conversation_id)).callback_status is None


async def test_agent_joined_before_sid_stored_counts_the_non_caller(act, world, make_agent, begun, session_factory):
    """The leg's sid is not stored yet (create_call still returning): once connected, the only participant other
    than the caller is the agent who accepted."""
    await make_agent("Ann")
    transfer_id, _ = await begun()
    assert await act(lambda s: s.agent_answered(transfer_id, 1)) == "join"
    await act(lambda s: s.agent_joined(transfer_id, CALL_SID))  # the caller (who joins first) never counts
    assert (await _get(session_factory, CallTransfer, transfer_id)).agent_joined_at is None
    await act(lambda s: s.agent_joined(transfer_id, "CA" + "7" * 32))
    assert (await _get(session_factory, CallTransfer, transfer_id)).agent_joined_at is not None


async def test_agent_dropped_before_join_gives_up_as_no_answer(act, world, make_agent, begun, session_factory):
    await make_agent("Ann")
    transfer_id, _ = await begun()
    await act(lambda s: s.set_leg_sid(transfer_id, 1, "CA" + "1" * 32))
    assert await act(lambda s: s.agent_answered(transfer_id, 1)) == "join"
    assert (await _get(session_factory, Call, world.call_id)).end_reason == "transferred"
    actions = await act(lambda s: s.leg_ended(transfer_id, 1, "completed"))
    assert actions == [RedirectCaller(CALL_SID, "voice_transfer_failed_phone", "en")]  # the leg is already over
    transfer = await _get(session_factory, CallTransfer, transfer_id)
    assert transfer.status == "no_answer" and transfer.ended_at is not None
    # `transferred` (set at the accept) is replaced: the caller got a callback, not a person.
    assert (await _get(session_factory, Call, world.call_id)).end_reason == "transfer_no_answer"
    conversation = await _get(session_factory, Conversation, world.conversation_id)
    assert (conversation.callback_status, conversation.callback_phone) == ("requested", CALLER)
    assert await act(lambda s: s.leg_ended(transfer_id, 1, "completed")) == []  # once
    assert await act(lambda s: s.caller_left(world.call_id)) == []


async def test_dropped_before_join_keeps_an_earlier_end_reason(act, world, make_agent, begun, session_factory):
    await make_agent("Ann")
    transfer_id, _ = await begun()
    assert await act(lambda s: s.agent_answered(transfer_id, 1)) == "join"
    await _set(session_factory, Call, world.call_id, end_reason="error")  # not `transferred`: first wins
    await act(lambda s: s.leg_ended(transfer_id, 1, "completed"))
    assert (await _get(session_factory, Call, world.call_id)).end_reason == "error"
