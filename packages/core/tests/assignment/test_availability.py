from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from supportcore.assignment.availability import available_agents
from supportcore.db.models import Membership, Role, User
from supportcore.presence.memory_presence import MemoryAgentPresence

pytestmark = pytest.mark.asyncio

MONDAY_10 = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)  # default hours: Mon-Fri 09:00-17:00 UTC
MONDAY_20 = datetime(2026, 10, 26, 20, 0, tzinfo=UTC)


@pytest.fixture
async def make_agent(session, org):
    async def make(email, *, role="Agent", status="online", member_status="active"):
        user = User(email=email, password_hash="x", name=email)
        session.add(user)
        await session.flush()
        role_id = (await session.execute(select(Role.id).where(Role.organisation_id == org.id, Role.name == role))).scalar_one()
        m = Membership(
            organisation_id=org.id, user_id=user.id, role_id=role_id, status=member_status, agent_status=status
        )
        session.add(m)
        await session.flush()
        return m

    return make


async def _available(session, org, presence, bus, at=MONDAY_10):
    return await available_agents(session, org.id, presence, at)


async def test_available_when_all_conditions_hold(session, org, bus, make_agent):
    m = await make_agent("a@x.com")
    presence = MemoryAgentPresence()
    await presence.beat(org.id, m.id)
    assert [x.id for x in await _available(session, org, presence, bus)] == [m.id]


async def test_offline_status_not_available(session, org, bus, make_agent):
    presence = MemoryAgentPresence()
    for i, status in enumerate(("offline", "away")):
        m = await make_agent(f"s{i}@x.com", status=status)
        await presence.beat(org.id, m.id)
    assert await _available(session, org, presence, bus) == []


async def test_not_present_not_available(session, org, bus, make_agent):
    await make_agent("a@x.com")
    assert await _available(session, org, MemoryAgentPresence(), bus) == []


async def test_no_queries_answer_not_available(session, org, bus, make_agent):
    m = await make_agent("v@x.com", role="Viewer")
    presence = MemoryAgentPresence()
    await presence.beat(org.id, m.id)
    assert await _available(session, org, presence, bus) == []


async def test_deactivated_member_not_available(session, org, bus, make_agent):
    m = await make_agent("d@x.com", member_status="deactivated")
    presence = MemoryAgentPresence()
    await presence.beat(org.id, m.id)
    assert await _available(session, org, presence, bus) == []


async def test_outside_hours_not_available(session, org, bus, make_agent):
    m = await make_agent("a@x.com")
    presence = MemoryAgentPresence()
    await presence.beat(org.id, m.id)
    assert await _available(session, org, presence, bus, at=MONDAY_20) == []
    assert len(await _available(session, org, presence, bus, at=MONDAY_10)) == 1


async def test_ordering_stable(session, org, bus, make_agent):
    presence = MemoryAgentPresence()
    members = [await make_agent(f"a{i}@x.com") for i in range(4)]
    for m in members:
        await presence.beat(org.id, m.id)
    # Three share a created_at (the id breaks the tie); one is created earlier than all of them.
    for m in members:
        m.created_at = datetime(2026, 1, 2, tzinfo=UTC)
    members[3].created_at = datetime(2026, 1, 1, tzinfo=UTC)
    await session.flush()
    expected = [members[3].id, *sorted(m.id for m in members[:3])]
    got = [x.id for x in await available_agents(session, org.id, presence, MONDAY_10)]
    assert got == expected
    assert [x.id for x in await available_agents(session, org.id, presence, MONDAY_10)] == expected
