"""The integration error log (Phase 5, Task 3): hourly dedupe, own unit of work, no text or secrets, and the
date-range helper. Call-site fault injection lives next to each call site's tests:
tests/bot/test_integration_error_sites.py, and the embedding / rerank cases in
tests/knowledge/test_indexing.py and tests/knowledge/test_retrieval.py."""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from supportcore.analytics.definitions import InvalidDateRangeError, date_range_utc
from supportcore.analytics.integration_errors import KINDS, error_type_of, record_integration_error
from supportcore.db.models import IntegrationEvent, Organisation, User
from supportcore.db.uow import UnitOfWork
from supportcore.events import EventBus
from supportcore.orgs.service import OrganisationService

SECRET = "sk-live-SENTINEL-do-not-store"


@pytest.fixture
async def org_id(session_factory):
    async with UnitOfWork(session_factory) as uow:
        owner = User(email=f"o-{uuid.uuid4().hex[:6]}@x.com", password_hash="x", name="Owner")
        uow.session.add(owner)
        await uow.session.flush()
        org = await OrganisationService(uow.session, EventBus()).create(owner, "Acme")
        await uow.commit()
    return org.id


async def _rows(session_factory):
    async with session_factory() as s:
        return list((await s.execute(select(IntegrationEvent).order_by(IntegrationEvent.hour))).scalars())


async def test_dedup_per_hour_increments_count(session_factory, org_id):
    t = datetime(2026, 10, 25, 10, 5, tzinfo=UTC)
    await record_integration_error(session_factory, org_id, "llm", "anthropic", "timeout", now=t)
    await record_integration_error(session_factory, org_id, "llm", "anthropic", "timeout", now=t + timedelta(minutes=40))
    await record_integration_error(session_factory, org_id, "llm", "anthropic", "timeout", now=t + timedelta(minutes=60))
    await record_integration_error(session_factory, org_id, "llm", "anthropic", "RuntimeError", now=t)
    await record_integration_error(session_factory, org_id, "tool", "anthropic", "timeout", now=t)

    rows = await _rows(session_factory)

    assert len(rows) == 4
    first = next(r for r in rows if (r.kind, r.error_type, r.hour.hour) == ("llm", "timeout", 10))
    assert first.count == 2 and first.hour == datetime(2026, 10, 25, 10, tzinfo=UTC)
    assert first.first_seen_at == t and first.last_seen_at == t + timedelta(minutes=40)
    assert next(r for r in rows if (r.kind, r.error_type, r.hour.hour) == ("llm", "timeout", 11)).count == 1


async def test_recorded_even_when_turn_rolls_back(session_factory, org_id):
    with pytest.raises(RuntimeError):
        async with UnitOfWork(session_factory) as turn:
            org = await turn.session.get(Organisation, org_id)
            org.name = "changed by the failing turn"
            await record_integration_error(session_factory, org_id, "llm", "openai", "RuntimeError")
            raise RuntimeError("the turn fails")

    async with session_factory() as s:
        assert (await s.get(Organisation, org_id)).name == "Acme"  # the turn rolled back
    [row] = await _rows(session_factory)
    assert (row.kind, row.provider, row.count) == ("llm", "openai", 1)  # the record survived


async def test_no_text_or_secret_in_rows(session_factory, org_id):
    exc = RuntimeError(f"401 for key {SECRET}: customer said hello")
    await record_integration_error(session_factory, org_id, "llm", "anthropic", error_type_of(exc))
    await record_integration_error(session_factory, org_id, "tool", "get_order", "http_500")

    for row in await _rows(session_factory):
        values = " ".join(str(getattr(row, c.name)) for c in IntegrationEvent.__table__.columns)
        assert SECRET not in values and "hello" not in values
    assert {r.error_type for r in await _rows(session_factory)} == {"RuntimeError", "http_500"}
    assert error_type_of(TimeoutError("x")) == "timeout" and error_type_of(ValueError(SECRET)) == "ValueError"


async def test_never_raises(session_factory, org_id, caplog):
    await record_integration_error(session_factory, org_id, "telephony", "twilio", "x")  # unknown kind
    await record_integration_error(session_factory, uuid.uuid4(), "llm", "p", "x")  # unknown organisation (FK)
    await record_integration_error(None, org_id, "llm", "p", "x")  # broken factory
    assert await _rows(session_factory) == []
    assert set(KINDS) == {"llm", "embedding", "rerank", "tool", "classifier", "email", "voice"}


def test_date_range_in_org_timezone():
    now = datetime(2026, 10, 25, 23, 30, tzinfo=UTC)  # already 26 Oct in Tokyo
    lo, hi = date_range_utc("Asia/Tokyo", None, None, now=now)
    assert lo == datetime(2026, 10, 19, 15, tzinfo=UTC) and hi == datetime(2026, 10, 26, 15, tzinfo=UTC)
    lo, hi = date_range_utc("UTC", date(2026, 1, 1), date(2026, 1, 1))
    assert hi - lo == timedelta(days=1)
    assert date_range_utc("Not/AZone", date(2026, 1, 1), date(2026, 1, 1))[0] == lo  # falls back to UTC
    with pytest.raises(InvalidDateRangeError):
        date_range_utc("UTC", date(2026, 1, 2), date(2026, 1, 1))
    with pytest.raises(InvalidDateRangeError):
        date_range_utc("UTC", date(2026, 1, 1), date(2026, 4, 3))  # 93 days
    date_range_utc("UTC", date(2026, 1, 1), date(2026, 4, 2))  # 92 days is fine
