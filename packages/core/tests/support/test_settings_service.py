import pytest
from sqlalchemy import func, select

from supportcore.db.models import AuditLog, SupportSettings
from supportcore.support.calendar import add_business_minutes, is_open
from supportcore.support.settings_service import (
    DEFAULT_SLA_TARGETS,
    DEFAULT_WEEKLY_HOURS,
    InvalidBusinessHoursError,
    InvalidSlaTargetsError,
)

pytestmark = pytest.mark.asyncio

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


async def test_defaults_created_once(settings_service, session, org):
    row = await settings_service.get()
    assert row.timezone == "UTC"
    assert row.weekly_hours == DEFAULT_WEEKLY_HOURS
    assert row.weekly_hours["mon"] == [["09:00", "17:00"]] and row.weekly_hours["sun"] == []
    assert row.holidays == []
    assert row.sla_targets == DEFAULT_SLA_TARGETS
    assert row.sla_targets["urgent"] == {"first_response": 15, "resolution": 240}
    assert row.sla_targets["low"] == {"first_response": 480, "resolution": 2880}
    assert row.last_assigned_membership_id is None
    again = await settings_service.get()
    assert again.organisation_id == row.organisation_id
    count = (await session.execute(select(func.count()).select_from(SupportSettings).where(SupportSettings.organisation_id == org.id))).scalar_one()
    assert count == 1


async def test_get_for_update(settings_service):
    row = await settings_service.get(for_update=True)
    assert row.timezone == "UTC"


def _hours(**overrides):
    return {d: [] for d in WEEKDAYS} | overrides


async def _update(service, actor, **kw):
    args = {
        "timezone": "UTC",
        "weekly_hours": DEFAULT_WEEKLY_HOURS,
        "holidays": [],
        "sla_targets": DEFAULT_SLA_TARGETS,
    } | kw
    return await service.update(args["timezone"], args["weekly_hours"], args["holidays"], args["sla_targets"], actor)


async def test_update_persists_audits_and_builds_calendar(settings_service, session, owner):
    from datetime import UTC, datetime

    hours = _hours(mon=[["10:00", "12:00"], ["13:00", "24:00"]])
    row = await _update(
        settings_service, owner, timezone="Asia/Kolkata", weekly_hours=hours, holidays=["2027-12-25", "2027-01-01", "2027-01-01"]
    )
    assert row.timezone == "Asia/Kolkata"
    assert row.weekly_hours == hours
    assert row.holidays == ["2027-01-01", "2027-12-25"]  # deduplicated and sorted
    cal = await settings_service.calendar()
    assert is_open(cal, datetime(2027, 1, 4, 5, 0, tzinfo=UTC))  # 10:30 IST Monday
    assert not is_open(cal, datetime(2027, 1, 5, 5, 0, tzinfo=UTC))  # Tuesday has no hours
    log = (await session.execute(select(AuditLog).where(AuditLog.action == "support_settings.updated"))).scalar_one()
    assert log.target_type == "organisation"
    assert set(log.metadata_["changes"]) == {"timezone", "weekly_hours", "holidays"}
    assert log.metadata_["changes"]["timezone"] == {"from": "UTC", "to": "Asia/Kolkata"}


async def test_update_without_changes_publishes_nothing(settings_service, session, owner):
    await _update(settings_service, owner)
    n = (await session.execute(select(func.count()).select_from(AuditLog).where(AuditLog.action == "support_settings.updated"))).scalar_one()
    assert n == 0


@pytest.mark.parametrize(
    "hours",
    [
        _hours(mon=[["09:00", "17:00"], ["16:00", "18:00"]]),  # overlap
        _hours(mon=[["9:00", "17:00"]]),  # format
        _hours(mon=[["17:00", "09:00"]]),
        _hours(mon=[["09:00", "10:00"], ["11:00", "12:00"], ["13:00", "14:00"], ["15:00", "16:00"]]),
        _hours(),  # every weekday empty
        {"mon": [["09:00", "17:00"]], "xyz": []},
        {"mon": "nope"},
        ["mon"],
        {"mon": [[9, 17]]},
    ],
)
async def test_update_validates_overlap_and_format(settings_service, owner, hours):
    with pytest.raises(InvalidBusinessHoursError):
        await _update(settings_service, owner, weekly_hours=hours)


async def test_touching_intervals_allowed(settings_service, owner):
    row = await _update(settings_service, owner, weekly_hours=_hours(mon=[["09:00", "12:00"], ["12:00", "17:00"]]))
    assert row.weekly_hours["mon"] == [["09:00", "12:00"], ["12:00", "17:00"]]


async def test_intervals_are_stored_sorted(settings_service, owner):
    row = await _update(settings_service, owner, weekly_hours=_hours(mon=[["13:00", "17:00"], ["09:00", "12:00"]]))
    assert row.weekly_hours["mon"] == [["09:00", "12:00"], ["13:00", "17:00"]]


@pytest.mark.parametrize("tz", ["Mars/Olympus", "", "  ", "America", "europe/london", "posixrules", "utc", "../etc/passwd", "x" * 500, None, 5])
async def test_unknown_timezone_422(settings_service, owner, tz):
    with pytest.raises(InvalidBusinessHoursError):
        await _update(settings_service, owner, timezone=tz)


@pytest.mark.parametrize("holidays", [["2027-13-01"], ["25/12/2027"], ["20271225"], [5], "2027-12-25"])
async def test_bad_holidays_rejected(settings_service, owner, holidays):
    with pytest.raises(InvalidBusinessHoursError):
        await _update(settings_service, owner, holidays=holidays)


async def test_hundred_holidays_allowed_hundred_one_not(settings_service, owner):
    from datetime import date, timedelta

    days = [(date(2027, 1, 1) + timedelta(days=i)).isoformat() for i in range(101)]
    await _update(settings_service, owner, holidays=days[:100])
    with pytest.raises(InvalidBusinessHoursError):
        await _update(settings_service, owner, holidays=days)


def _targets(**overrides):
    return {p: dict(v) for p, v in DEFAULT_SLA_TARGETS.items()} | overrides


@pytest.mark.parametrize(
    "targets",
    [
        _targets(urgent={"first_response": 0, "resolution": 240}),
        _targets(urgent={"first_response": 15, "resolution": 100001}),
        _targets(urgent={"first_response": 1.5, "resolution": 240}),
        _targets(urgent={"first_response": True, "resolution": 240}),
        _targets(urgent={"first_response": "15", "resolution": 240}),
        _targets(urgent={"first_response": 15}),
        _targets(urgent={"first_response": 15, "resolution": 240, "extra": 1}),
        {k: v for k, v in _targets().items() if k != "low"},
        _targets(bogus={"first_response": 1, "resolution": 1}),
        {"urgent": 5},
        [],
    ],
)
async def test_invalid_sla_targets(settings_service, owner, targets):
    with pytest.raises(InvalidSlaTargetsError):
        await _update(settings_service, owner, sla_targets=targets)


async def test_sla_targets_bounds_accepted(settings_service, owner):
    t = _targets(low={"first_response": 1, "resolution": 100000})
    row = await _update(settings_service, owner, sla_targets=t)
    assert row.sla_targets["low"] == {"first_response": 1, "resolution": 100000}


async def test_calendar_defaults_work(settings_service):
    from datetime import UTC, datetime

    cal = await settings_service.calendar()
    assert add_business_minutes(cal, datetime(2027, 1, 8, 16, 30, tzinfo=UTC), 60) == datetime(2027, 1, 11, 9, 30, tzinfo=UTC)


async def test_long_bad_values_are_not_echoed(settings_service, owner):
    bad = "SECRET" * 8
    with pytest.raises(InvalidBusinessHoursError) as exc:
        await _update(settings_service, owner, weekly_hours=_hours(mon=[[bad, "17:00"]]))
    assert "SECRET" not in str(exc.value)
    with pytest.raises(InvalidBusinessHoursError) as exc:
        await _update(settings_service, owner, timezone=bad)
    assert "SECRET" not in str(exc.value)
