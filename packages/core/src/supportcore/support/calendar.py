"""Business-hours arithmetic. Pure: no DB, no I/O, only `zoneinfo`.

A `Calendar` is a timezone, up to three open intervals per weekday, and a set of holiday dates.
Every datetime in or out is timezone-aware UTC (a naive datetime raises ValueError).

Conventions:
- An interval is `[start, end)`: open at its start instant, closed at its end instant. `24:00` as an end
  is midnight at the end of that local day. Touching intervals (`12:00` end, `12:00` start) are contiguous.
- Intervals are expressed on the local wall clock and converted with `fold=0`. A wall time that does not
  exist (spring-forward gap) moves to the first valid instant after the gap. A day with a repeated hour
  (fall-back) is a 25-hour day on the wall clock, and its intervals are converted instant by instant.
- A holiday closes its whole local day.
- `add_business_minutes(cal, start, 0)` returns `start` when open at `start`, else the next opening instant.
  A result that lands exactly on an interval's end is that end (Friday 16:00 + 60 min is Friday 17:00).
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MAX_INTERVALS_PER_DAY = 3
MAX_ITERATED_DAYS = 3660  # about ten years; beyond this a search is considered runaway
_MINUTES_PER_DAY = 1440
_HHMM = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")

Intervals = tuple[tuple[tuple[int, int], ...], ...]  # [weekday Mon=0..Sun=6][(start_minute, end_minute), ...]


class NoBusinessHoursError(ValueError):
    """The calendar has no open interval on any weekday."""

    def __init__(self) -> None:
        super().__init__("The calendar has no business hours")


class BusinessTimeHorizonError(ValueError):
    """No answer was found within MAX_ITERATED_DAYS days."""

    def __init__(self) -> None:
        super().__init__(f"No business time found within {MAX_ITERATED_DAYS} days")


@dataclass(frozen=True)
class Calendar:
    tz: ZoneInfo
    intervals: Intervals  # seven entries, Monday first
    holidays: frozenset[date]


def _parse_hhmm(value: object, *, is_end: bool) -> int:
    if not isinstance(value, str):
        raise ValueError("Times must be HH:MM strings")
    if value == "24:00" and is_end:
        return _MINUTES_PER_DAY
    if not _HHMM.match(value):
        raise ValueError("Invalid time; expected HH:MM")
    return int(value[:2]) * 60 + int(value[3:])


def parse_weekly_hours(weekly_hours: object) -> Intervals:
    """`{"mon": [["09:00", "17:00"]], ...}` to minute intervals. Missing weekdays are closed; each day's
    intervals are sorted. Raises ValueError for bad shape, format, empty or overlapping intervals."""
    if not isinstance(weekly_hours, dict):
        raise ValueError("weekly_hours must be an object keyed by weekday")
    unknown = set(weekly_hours) - set(WEEKDAY_KEYS)
    if unknown:
        raise ValueError("Unknown weekday key")
    days: list[tuple[tuple[int, int], ...]] = []
    for key in WEEKDAY_KEYS:
        raw = weekly_hours.get(key, [])
        if not isinstance(raw, list):
            raise ValueError(f"{key} must be a list of intervals")
        if len(raw) > MAX_INTERVALS_PER_DAY:
            raise ValueError(f"{key} has more than {MAX_INTERVALS_PER_DAY} intervals")
        parsed: list[tuple[int, int]] = []
        for pair in raw:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ValueError(f"{key} intervals must be [start, end] pairs")
            start, end = _parse_hhmm(pair[0], is_end=False), _parse_hhmm(pair[1], is_end=True)
            if start >= end:
                raise ValueError(f"{key} has an interval that ends before it starts")
            parsed.append((start, end))
        parsed.sort()
        for (_, prev_end), (next_start, _) in zip(parsed, parsed[1:]):
            if next_start < prev_end:
                raise ValueError(f"{key} has overlapping intervals")
        days.append(tuple(parsed))
    return tuple(days)


def format_hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _as_utc(value: datetime) -> datetime:
    """Any aware datetime as UTC; naive raises ValueError."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Datetimes must be timezone-aware")
    return value.astimezone(UTC)


def _wall_to_utc(tz: ZoneInfo, day: date, minute: int) -> datetime:
    """The UTC instant of wall-clock `day` + `minute` (0..1440), fold=0; a nonexistent time becomes the
    first instant after the gap."""
    wall = datetime.combine(day, time()) + timedelta(minutes=minute)
    before = wall.replace(tzinfo=tz, fold=0)
    candidate = before.astimezone(UTC)
    if candidate.astimezone(tz).replace(tzinfo=None) == wall:
        return candidate
    # In a gap: fold=0 used the pre-transition offset, fold=1 the post-transition one.
    off_before = before.utcoffset()
    off_after = wall.replace(tzinfo=tz, fold=1).utcoffset()
    assert off_before is not None and off_after is not None
    lo = (wall - off_after).replace(tzinfo=UTC)  # still before the transition
    hi = (wall - off_before).replace(tzinfo=UTC)  # already after it
    while hi - lo > timedelta(seconds=1):  # find the transition instant
        mid = lo + (hi - lo) / 2
        mid = mid.replace(microsecond=0)
        if mid.astimezone(tz).utcoffset() == off_after:
            hi = mid
        else:
            lo = mid
    return hi


def _day_windows(cal: Calendar, day: date) -> list[tuple[datetime, datetime]]:
    """The open [start, end) UTC windows of one local day (empty on a holiday)."""
    if day in cal.holidays:
        return []
    windows = []
    for start_min, end_min in cal.intervals[day.weekday()]:
        start, end = _wall_to_utc(cal.tz, day, start_min), _wall_to_utc(cal.tz, day, end_min)
        if end > start:
            windows.append((start, end))
    return windows


def _has_hours(cal: Calendar) -> bool:
    return any(cal.intervals)


def is_open(cal: Calendar, at: datetime) -> bool:
    at = _as_utc(at)
    day = at.astimezone(cal.tz).date()
    return any(start <= at < end for start, end in _day_windows(cal, day))


def add_business_minutes(cal: Calendar, start: datetime, minutes: int) -> datetime:
    """The instant `minutes` business minutes after `start`. See the module docstring for edge conventions."""
    start = _as_utc(start)
    if minutes < 0:
        raise ValueError("minutes must be >= 0")
    if not _has_hours(cal):
        raise NoBusinessHoursError()
    remaining = timedelta(minutes=minutes)
    first_day = start.astimezone(cal.tz).date() - timedelta(days=1)  # an interval can reach into the next UTC day
    for offset in range(MAX_ITERATED_DAYS):
        for window_start, window_end in _day_windows(cal, first_day + timedelta(days=offset)):
            if window_end <= start:
                continue
            window_start = max(window_start, start)
            available = window_end - window_start
            if remaining <= available:
                return window_start + remaining
            remaining -= available
    raise BusinessTimeHorizonError()


def business_minutes_between(cal: Calendar, start: datetime, end: datetime) -> int:
    """Whole business minutes in [start, end); 0 when end <= start."""
    start, end = _as_utc(start), _as_utc(end)
    if end <= start:
        return 0
    first_day = start.astimezone(cal.tz).date() - timedelta(days=1)
    last_day = end.astimezone(cal.tz).date()
    if (last_day - first_day).days > MAX_ITERATED_DAYS:
        raise BusinessTimeHorizonError()
    total = timedelta()
    day = first_day
    while day <= last_day:
        for window_start, window_end in _day_windows(cal, day):
            overlap = min(window_end, end) - max(window_start, start)
            if overlap > timedelta():
                total += overlap
        day += timedelta(days=1)
    return int(total.total_seconds() // 60)
