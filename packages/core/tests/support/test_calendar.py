import random
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from supportcore.support.calendar import (
    BusinessTimeHorizonError,
    Calendar,
    NoBusinessHoursError,
    add_business_minutes,
    business_minutes_between,
    is_open,
    parse_weekly_hours,
)

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def cal(tz="UTC", hours=None, holidays=()):
    """hours: {"mon": [["09:00", "17:00"]], ...}; default Mon-Fri 09:00-17:00."""
    if hours is None:
        hours = {d: [["09:00", "17:00"]] for d in WEEKDAYS[:5]}
    full = {d: hours.get(d, []) for d in WEEKDAYS}
    return Calendar(
        tz=ZoneInfo(tz),
        intervals=parse_weekly_hours(full),
        holidays=frozenset(date.fromisoformat(h) for h in holidays),
    )


def local(tz, y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(tz)).astimezone(UTC)


def to_local(tz, dt):
    return dt.astimezone(ZoneInfo(tz)).replace(tzinfo=None)


def test_is_open_respects_intervals_and_timezone():
    c = cal("Asia/Kolkata")  # 09:00 IST == 03:30Z; 2027-01-04 is a Monday
    assert not is_open(c, datetime(2027, 1, 4, 3, 29, tzinfo=UTC))
    assert is_open(c, datetime(2027, 1, 4, 3, 31, tzinfo=UTC))
    assert is_open(c, datetime(2027, 1, 4, 3, 30, tzinfo=UTC))  # start inclusive
    assert is_open(c, datetime(2027, 1, 4, 11, 29, tzinfo=UTC))
    assert not is_open(c, datetime(2027, 1, 4, 11, 30, tzinfo=UTC))  # end exclusive
    assert not is_open(c, datetime(2027, 1, 3, 6, 0, tzinfo=UTC))  # Sunday


def test_is_open_closed_on_holiday():
    c = cal(holidays=["2027-01-04"])
    assert not is_open(c, datetime(2027, 1, 4, 10, 0, tzinfo=UTC))
    assert is_open(c, datetime(2027, 1, 5, 10, 0, tzinfo=UTC))


def test_add_business_minutes_skips_weekend_and_holiday():
    c = cal("UTC", holidays=["2027-01-11"])  # Friday 2027-01-08; Monday 11th is a holiday
    end = add_business_minutes(c, local("UTC", 2027, 1, 8, 16, 30), 60)
    assert end == local("UTC", 2027, 1, 12, 9, 30)


def test_add_business_minutes_within_interval():
    c = cal("UTC")
    assert add_business_minutes(c, local("UTC", 2027, 1, 4, 10, 0), 45) == local("UTC", 2027, 1, 4, 10, 45)


def test_add_business_minutes_spans_days():
    c = cal("UTC")
    # 16:00 Monday + 600 min: 60 left Monday, 480 Tuesday, 60 Wednesday.
    assert add_business_minutes(c, local("UTC", 2027, 1, 4, 16, 0), 600) == local("UTC", 2027, 1, 6, 10, 0)


def test_zero_minutes_returns_start_when_open_else_next_opening():
    c = cal("UTC")
    open_at = local("UTC", 2027, 1, 4, 10, 0)
    assert add_business_minutes(c, open_at, 0) == open_at
    assert add_business_minutes(c, local("UTC", 2027, 1, 4, 18, 0), 0) == local("UTC", 2027, 1, 5, 9, 0)
    assert add_business_minutes(c, local("UTC", 2027, 1, 9, 12, 0), 0) == local("UTC", 2027, 1, 11, 9, 0)  # Saturday
    # The end of an interval is closed.
    assert add_business_minutes(c, local("UTC", 2027, 1, 4, 17, 0), 0) == local("UTC", 2027, 1, 5, 9, 0)


def test_closed_start_counts_from_next_opening():
    c = cal("UTC")
    assert add_business_minutes(c, local("UTC", 2027, 1, 4, 7, 0), 30) == local("UTC", 2027, 1, 4, 9, 30)


def test_dst_spring_forward():
    c = cal("Europe/London")
    sunday = local("Europe/London", 2027, 3, 28, 12, 0)  # clocks go forward at 01:00Z
    assert not is_open(c, sunday)
    monday_open = add_business_minutes(c, sunday, 0)
    assert monday_open == datetime(2027, 3, 29, 8, 0, tzinfo=UTC)  # 09:00 BST
    end = add_business_minutes(c, local("Europe/London", 2027, 3, 26, 16, 45), 30)
    assert to_local("Europe/London", end) == datetime(2027, 3, 29, 9, 15)
    assert end == datetime(2027, 3, 29, 8, 15, tzinfo=UTC)


def test_dst_fall_back_day_has_extra_hour_of_wall_clock():
    c = cal("Europe/London", hours={"sun": [["00:00", "24:00"]]})
    start = local("Europe/London", 2027, 10, 31, 0, 0)  # clocks go back: 25-hour day
    end = start + timedelta(days=1, hours=1)
    assert business_minutes_between(c, start, end) == 25 * 60


def test_interval_inside_spring_forward_gap_starts_at_first_valid_instant():
    c = cal("Europe/London", hours={"sun": [["01:30", "03:00"]]})
    # 01:30 does not exist on 2027-03-28; the interval opens at the transition (01:00Z) and ends 03:00 BST (02:00Z).
    assert not is_open(c, datetime(2027, 3, 28, 0, 59, tzinfo=UTC))
    assert is_open(c, datetime(2027, 3, 28, 1, 0, tzinfo=UTC))
    assert not is_open(c, datetime(2027, 3, 28, 2, 0, tzinfo=UTC))
    assert business_minutes_between(c, datetime(2027, 3, 28, 0, 0, tzinfo=UTC), datetime(2027, 3, 29, 0, 0, tzinfo=UTC)) == 60


def test_multiple_intervals_lunch_break():
    c = cal("UTC", hours={d: [["09:00", "12:00"], ["13:00", "17:00"]] for d in WEEKDAYS[:5]})
    assert add_business_minutes(c, local("UTC", 2027, 1, 4, 11, 50), 20) == local("UTC", 2027, 1, 4, 13, 10)
    assert not is_open(c, local("UTC", 2027, 1, 4, 12, 30))
    assert business_minutes_between(c, local("UTC", 2027, 1, 4, 0), local("UTC", 2027, 1, 5, 0)) == 7 * 60


def test_touching_intervals_are_contiguous():
    c = cal("UTC", hours={"mon": [["09:00", "12:00"], ["12:00", "17:00"]]})
    assert add_business_minutes(c, local("UTC", 2027, 1, 4, 11, 0), 120) == local("UTC", 2027, 1, 4, 13, 0)
    assert business_minutes_between(c, local("UTC", 2027, 1, 4, 0), local("UTC", 2027, 1, 5, 0)) == 8 * 60


def test_24_00_end_means_midnight_end_of_day():
    c = cal("UTC", hours={"mon": [["22:00", "24:00"]], "tue": [["00:00", "01:00"]]})
    assert is_open(c, local("UTC", 2027, 1, 4, 23, 59))
    assert add_business_minutes(c, local("UTC", 2027, 1, 4, 23, 0), 90) == local("UTC", 2027, 1, 5, 0, 30)


def test_business_minutes_between_basics():
    c = cal("UTC")
    assert business_minutes_between(c, local("UTC", 2027, 1, 4, 10), local("UTC", 2027, 1, 4, 11, 30)) == 90
    assert business_minutes_between(c, local("UTC", 2027, 1, 8, 16), local("UTC", 2027, 1, 11, 10)) == 120  # Fri + Mon
    assert business_minutes_between(c, local("UTC", 2027, 1, 4, 12), local("UTC", 2027, 1, 4, 11)) == 0  # end before start
    assert business_minutes_between(c, local("UTC", 2027, 1, 9, 10), local("UTC", 2027, 1, 10, 10)) == 0  # weekend


@pytest.mark.parametrize("tz", ["Asia/Kolkata", "Europe/London", "America/New_York", "UTC"])
def test_business_minutes_between_is_inverse_of_add(tz):
    rng = random.Random(20271025)
    c = cal(tz, hours={d: [["09:00", "12:00"], ["13:00", "17:30"]] for d in WEEKDAYS[:5]} | {"sat": [["10:00", "14:00"]]},
            holidays=["2027-03-29", "2027-12-24"])
    base = datetime(2027, 1, 1, tzinfo=UTC)
    for _ in range(50):
        start = base + timedelta(seconds=rng.randrange(0, 365 * 86400))
        minutes = rng.randrange(0, 4000)
        end = add_business_minutes(c, start, minutes)
        assert end >= start
        assert business_minutes_between(c, start, end) == minutes


def test_all_closed_raises():
    c = cal("UTC", hours={})
    with pytest.raises(NoBusinessHoursError):
        add_business_minutes(c, datetime(2027, 1, 4, 10, tzinfo=UTC), 10)


def test_horizon_cap_raises():
    # Every weekday is open, but all in the next ~11 years are holidays.
    days = [date(2027, 1, 4) + timedelta(days=i) for i in range(3700)]
    c = Calendar(tz=ZoneInfo("UTC"), intervals=parse_weekly_hours({d: [["09:00", "17:00"]] for d in WEEKDAYS}), holidays=frozenset(days))
    with pytest.raises(BusinessTimeHorizonError):
        add_business_minutes(c, datetime(2027, 1, 4, 10, tzinfo=UTC), 10)


def test_naive_datetimes_rejected():
    c = cal("UTC")
    naive = datetime(2027, 1, 4, 10)
    with pytest.raises(ValueError):
        is_open(c, naive)
    with pytest.raises(ValueError):
        add_business_minutes(c, naive, 5)
    with pytest.raises(ValueError):
        business_minutes_between(c, naive, datetime(2027, 1, 5, tzinfo=UTC))
    with pytest.raises(ValueError):
        business_minutes_between(c, datetime(2027, 1, 5, tzinfo=UTC), naive)


def test_negative_minutes_rejected():
    with pytest.raises(ValueError):
        add_business_minutes(cal(), datetime(2027, 1, 4, 10, tzinfo=UTC), -1)


@pytest.mark.parametrize(
    "hours",
    [
        {"mon": [["09:00", "17:00"], ["16:00", "18:00"]]},  # overlap
        {"mon": [["09:00", "09:00"]]},  # empty
        {"mon": [["17:00", "09:00"]]},  # reversed
        {"mon": [["9:00", "17:00"]]},  # format
        {"mon": [["09:00", "25:00"]]},
        {"mon": [["24:00", "24:00"]]},
        {"mon": [["09:60", "17:00"]]},
        {"mon": [["09:00", "10:00"], ["11:00", "12:00"], ["13:00", "14:00"], ["15:00", "16:00"]]},  # >3
        {"funday": []},
        {"mon": "09:00-17:00"},
        {"mon": [["09:00"]]},
    ],
)
def test_parse_weekly_hours_rejects_bad_input(hours):
    with pytest.raises(ValueError):
        parse_weekly_hours({d: [] for d in WEEKDAYS} | hours)


def test_parse_weekly_hours_sorts_intervals_within_a_day():
    parsed = parse_weekly_hours({d: [] for d in WEEKDAYS} | {"mon": [["13:00", "17:00"], ["09:00", "12:00"]]})
    assert parsed[0] == ((540, 720), (780, 1020))


def test_inputs_in_other_timezones_are_normalised_to_utc():
    c = cal("UTC")
    ny = ZoneInfo("America/New_York")
    start = datetime(2027, 1, 4, 5, 30, tzinfo=ny)  # 10:30Z Monday
    end = add_business_minutes(c, start, 45)
    assert end == datetime(2027, 1, 4, 11, 15, tzinfo=UTC)
    assert end.utcoffset() == timedelta(0) and end.tzinfo is UTC
    assert is_open(c, start)
    assert business_minutes_between(c, start, datetime(2027, 1, 4, 7, 0, tzinfo=ny)) == 90


def test_parse_errors_do_not_echo_input():
    bad = "SECRET" * 50
    with pytest.raises(ValueError) as exc:
        parse_weekly_hours({"mon": [[bad, "17:00"]]})
    assert "SECRET" not in str(exc.value)
