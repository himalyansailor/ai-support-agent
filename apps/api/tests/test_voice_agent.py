from datetime import UTC, datetime
from uuid import UUID

import pytest
from kb_helpers import create_org, join_org, signup
from pydantic import SecretStr
from sqlalchemy import select, update
from supportcore.db.models import Membership, User

PUBLIC_URL = "https://voice.example.com"
SID = "AC" + "b" * 32
TOKEN = "t" * 32


@pytest.fixture
def test_settings(test_settings):
    return test_settings.model_copy(
        update={
            "telephony": "fake",
            "voice_public_url": PUBLIC_URL,
            "twilio_account_sid": "AC" + "a" * 32,
            "twilio_auth_token": SecretStr("platform-master-token-" + "p" * 10),
        }
    )


@pytest.fixture(autouse=True)
def _truncate(session_factory):
    """Real-commit tests: session_factory truncates every table at teardown."""


@pytest.fixture
async def setup(raw_client, session_factory):
    headers = await signup(raw_client, "owner@x.com")
    org_id = await create_org(raw_client, headers)
    async with session_factory() as s:
        owner_mid = (
            await s.execute(
                select(Membership.id).join(User, User.id == Membership.user_id).where(User.email == "owner@x.com")
            )
        ).scalar_one()
    return raw_client, headers, f"/orgs/{org_id}/voice/agent", UUID(org_id), owner_mid


async def connect(client, headers, org_id):
    r = await client.put(
        f"/orgs/{org_id}/voice/telephony", json={"account_sid": SID, "auth_token": TOKEN}, headers=headers
    )
    assert r.status_code == 200, r.text


async def test_agent_token_incoming_and_identity(setup):
    client, headers, base, org_id, mid = setup
    await connect(client, headers, org_id)
    r = await client.post(f"{base}/token", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["identity"] == f"agent-{mid}" and body["expires_in"] == 600
    assert body["token"].endswith(f"agent-{mid}:in")
    assert r.headers["cache-control"] == "no-store"  # it carries a Twilio access token


async def test_agent_token_needs_telephony(setup):
    client, headers, base, _, _ = setup
    r = await client.post(f"{base}/token", headers=headers)
    assert r.status_code == 409 and r.json()["code"] == "voice_not_available"


async def test_agent_token_requires_queries_answer(setup, session_factory):
    client, headers, base, org_id, _ = setup
    await connect(client, headers, org_id)
    viewer = await join_org(client, session_factory, org_id, "viewer@x.com", role_name="Viewer")
    agent = await join_org(client, session_factory, org_id, "agent@x.com", role_name="Agent")
    for method, path in (("post", "/token"), ("put", "/softphone"), ("delete", "/softphone")):
        assert (await getattr(client, method)(base + path, headers=viewer)).status_code == 403, path
    assert (await client.post(f"{base}/token", headers=agent)).status_code == 200
    assert (await client.post(f"{base}/token")).status_code == 401


async def test_softphone_heartbeat_marks_present(setup, app):
    client, headers, base, org_id, mid = setup
    softphones = app.state.container.adapters.softphones
    assert await softphones.present(org_id) == set()
    assert (await client.put(f"{base}/softphone", headers=headers)).status_code == 204
    assert await softphones.present(org_id) == {mid}
    # Its own namespace: not the same thing as the agent being online.
    assert await app.state.container.adapters.presence.present(org_id) == set()
    assert (await client.delete(f"{base}/softphone", headers=headers)).status_code == 204
    assert await softphones.present(org_id) == set()
    assert (await client.delete(f"{base}/softphone", headers=headers)).status_code == 204  # idempotent


# --- calling a customer back (Task 11) ---------------------------------------------------------------------------

AGENT_PHONE = "+14155550199"
CUSTOMER_PHONE = "+14155550123"
SHOP_NUMBER = "+15005550006"


async def seed_callback(session_factory, org_id, *, callback_phone=CUSTOMER_PHONE, with_number=True):
    """A voice conversation (the customer phoned and asked for a callback); the organisation's Twilio account must
    already be connected when `with_number`. Returns the conversation id."""
    from supportcore.conversations.service import ConversationService
    from supportcore.db.models import Customer, PhoneNumber, TelephonyAccount
    from supportcore.events import EventBus

    async with session_factory() as s:
        customer = Customer(organisation_id=org_id, last_seen_at=datetime.now(UTC))
        s.add(customer)
        await s.flush()
        conversation = await ConversationService(s, org_id, EventBus()).create(
            customer.id, None, "en", is_test=False, channel="voice"
        )
        conversation.callback_status, conversation.callback_phone = "requested", callback_phone
        if with_number:
            account = (await s.execute(select(TelephonyAccount).where(TelephonyAccount.organisation_id == org_id))).scalar_one()
            s.add(
                PhoneNumber(
                    organisation_id=org_id, telephony_account_id=account.id, e164=SHOP_NUMBER,
                    provider_sid="PN" + "d" * 32, ownership="byo",
                )
            )
        await s.commit()
        return conversation.id


async def set_agent_phone(session_factory, mid, phone):
    async with session_factory() as s:
        (await s.get(Membership, mid)).phone_number = phone
        await s.commit()


async def outbound_calls(session_factory):
    from supportcore.db.models import Call

    async with session_factory() as s:
        return list((await s.execute(select(Call).where(Call.direction == "outbound"))).scalars())


def create_calls(app):
    return [c for c in app.state.container.adapters.telephony.calls if c[0] == "create_call"]


@pytest.fixture
async def callback_world(setup, session_factory):
    client, headers, base, org_id, mid = setup
    await connect(client, headers, org_id)
    conversation_id = await seed_callback(session_factory, org_id)
    await set_agent_phone(session_factory, mid, AGENT_PHONE)
    return client, headers, f"{base}/callbacks/{conversation_id}", org_id, mid, conversation_id


async def test_callback_ticket_is_bound_to_the_agent_and_conversation(callback_world, test_settings):
    from supportcore.voice.tokens import verify_callback_ticket

    client, headers, url, org_id, mid, conversation_id = callback_world
    r = await client.post(f"{url}/ticket", headers=headers)
    assert r.status_code == 200, r.text
    ticket = verify_callback_ticket(test_settings, r.json()["ticket"])
    assert (ticket.organisation_id, ticket.membership_id, ticket.conversation_id) == (org_id, mid, conversation_id)
    assert set(r.json()) == {"ticket"}
    assert r.headers["cache-control"] == "no-store"


async def test_callback_ticket_needs_browser_calling_and_a_number_to_call(setup, session_factory):
    client, headers, base, org_id, _ = setup
    conversation_id = await seed_callback(session_factory, org_id, with_number=False)
    url = f"{base}/callbacks/{conversation_id}/ticket"
    assert (await client.post(url, headers=headers)).json()["code"] == "voice_not_available"  # no Twilio account
    await connect(client, headers, org_id)
    other = await seed_callback(session_factory, org_id, callback_phone=None)
    r = await client.post(f"{base}/callbacks/{other}/ticket", headers=headers)
    assert (r.status_code, r.json()["code"]) == (409, "callback_number_missing")


async def test_phone_callback_rings_agent_then_bridges(callback_world, app, session_factory):
    client, headers, url, _, mid, conversation_id = callback_world
    r = await client.post(f"{url}/call", headers=headers)
    assert r.status_code == 202, r.text
    [call] = await outbound_calls(session_factory)
    assert r.json() == {"call_id": str(call.id)}
    assert (call.direction, call.source, call.end_reason, call.agent_membership_id) == ("outbound", "callback", "callback", mid)
    assert call.conversation_id == conversation_id and call.provider_call_sid is not None and call.status == "ringing"
    [(_, (_, to, from_, webhooks, timeout))] = create_calls(app)
    assert (to, from_, timeout) == (AGENT_PHONE, SHOP_NUMBER, 30)
    assert webhooks.answer_url == f"{PUBLIC_URL}/twilio/voice/callback-bridge?call_id={call.id}"
    assert webhooks.status_callback == f"{PUBLIC_URL}/twilio/voice/status"


@pytest.mark.parametrize("phone", [None, "", "415 555 0199", "4155550199", "+0123456"])
async def test_phone_callback_needs_e164_profile_number(callback_world, app, session_factory, phone):
    client, headers, url, _, mid, _ = callback_world
    await set_agent_phone(session_factory, mid, phone)
    r = await client.post(f"{url}/call", headers=headers)
    assert (r.status_code, r.json()["code"]) == (409, "agent_unreachable")
    assert await outbound_calls(session_factory) == [] and create_calls(app) == []


async def test_no_callback_phone_409(callback_world, session_factory):
    from supportcore.db.models import Conversation

    client, headers, url, *_ = callback_world
    async with session_factory() as s:
        await s.execute(update(Conversation).values(callback_phone=None))
        await s.commit()
    r = await client.post(f"{url}/call", headers=headers)
    assert (r.status_code, r.json()["code"]) == (409, "callback_number_missing")
    assert await outbound_calls(session_factory) == []


async def test_no_from_number_409(setup, app, session_factory):
    client, headers, base, org_id, mid = setup
    await connect(client, headers, org_id)
    conversation_id = await seed_callback(session_factory, org_id, with_number=False)
    await set_agent_phone(session_factory, mid, AGENT_PHONE)
    r = await client.post(f"{base}/callbacks/{conversation_id}/call", headers=headers)
    assert (r.status_code, r.json()["code"]) == (409, "voice_not_available")
    assert await outbound_calls(session_factory) == [] and create_calls(app) == []


async def test_cross_org_404(callback_world):
    client, headers, url, *_ = callback_world
    stranger = await signup(client, "stranger@x.com")
    their_org = await create_org(client, stranger, "Theirs")
    base = f"/orgs/{their_org}/voice/agent/callbacks/{url.rsplit('/', 1)[1]}"
    for path in ("/ticket", "/call"):
        assert (await client.post(base + path, headers=stranger)).status_code == 404, path
    assert (await client.post(f"{url}/call", headers={**headers})).status_code == 202  # still works for the owner


async def test_callback_routes_require_queries_answer(callback_world, session_factory):
    client, headers, url, org_id, *_ = callback_world
    viewer = await join_org(client, session_factory, org_id, "viewer@x.com", role_name="Viewer")
    for path in ("/ticket", "/call"):
        assert (await client.post(url + path, headers=viewer)).status_code == 403, path
        assert (await client.post(url + path)).status_code == 401, path


async def test_twilio_failure_is_502_and_the_call_is_finalised_failed(callback_world, app, session_factory):
    from supportcore.db.models import IntegrationEvent
    from supportcore.voice.telephony.ports import TelephonyError

    client, headers, url, *_ = callback_world

    async def refuse(*args, **kwargs):
        raise TelephonyError("connection_failed")

    app.state.container.adapters.telephony.create_call = refuse
    r = await client.post(f"{url}/call", headers=headers)
    assert (r.status_code, r.json()["code"]) == (502, "telephony_unavailable")
    [call] = await outbound_calls(session_factory)
    assert (call.status, call.finalised, call.provider_call_sid, call.end_reason) == ("failed", True, None, "callback")
    async with session_factory() as s:
        events = (await s.execute(select(IntegrationEvent))).scalars().all()
    assert [(e.kind, e.provider, e.error_type) for e in events] == [("voice", "twilio", "connection_failed")]


async def test_no_session_open_during_create_call(callback_world, app):
    client, headers, url, *_ = callback_world
    telephony = app.state.container.adapters.telephony
    original, seen = telephony.create_call, []

    async def watching(*args, **kwargs):
        seen.append(app.state.engine.pool.checkedout())  # connections held by any open session
        return await original(*args, **kwargs)

    telephony.create_call = watching
    assert (await client.post(f"{url}/call", headers=headers)).status_code == 202
    assert seen == [0]


async def test_fake_telephony_read_back_is_test_only(setup, app, test_settings):
    """The e2e suite reads back the calls the fake Twilio placed (the API dials an agent's phone for a callback);
    the route exists only with env == "test" and telephony == "fake"."""
    from supportapi.main import create_app

    client, *_ = setup
    r = await client.get("/test/fake-telephony/calls")
    assert r.status_code == 200 and set(r.json()) == {"create_call", "update_call"}
    for changed in ({"telephony": "twilio"}, {"env": "dev", "telephony": "twilio"}):
        other = create_app(test_settings.model_copy(update=changed), periodic_jobs=False)
        try:
            assert not any(getattr(route, "path", "").startswith("/test/") for route in other.router.routes)
        finally:
            await other.state.engine.dispose()
