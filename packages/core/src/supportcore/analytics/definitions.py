"""Analytics definitions: what every dashboard number means, and how a date range becomes UTC buckets.

Metric definitions
------------------

Every metric is computed from the event log (supportcore.analytics.events), one UTC hour at a time
(supportcore.analytics.rollup), and summed over hours. Events of test conversations (`is_test`) are always
excluded. A metric with a duration is attributed to the hour of the event that closes it.

- Conversation started: a `conversation_created` event.
- Entered team: a conversation's first `entered_team` event ever (later re-entries do not count), so a sum over
  any range counts each conversation once.
- Escalation rate: entered team / conversations started.
- Resolved: a `resolved` event (any `by`).
- AI-resolved: a `resolved` event with `by="bot"` (the customer ended a bot chat, or a `bot` conversation idled
  out) of a conversation with no `entered_team` event before it.
- AI resolution rate: AI-resolved / resolved.
- First response time: the `first_agent_response` time minus the latest `entered_team` time before it in the
  same conversation (the cycle's start; a reopen starts no new cycle), in wall seconds. A response with no
  `entered_team` before it is not counted. Business-hours SLA stays the Phase 4 SLA columns.
- Resolution time: the `resolved` time minus the conversation's `conversation_created` time, in seconds.
- CSAT: `csat` events: the count, the sum of the 1-5 scores (average = sum / count) and the count of scores
  >= 4 (positive share = positive / count).
- SLA breaches: `sla_breached` events (either kind).
- Messages: `message` events by role: customer, bot, agent (system messages are not counted).
- Bot turns: `bot_turn` events.
- Knowledge gap: a bot turn with `no_answer=true`, or outcome `offered_human` with `rule_key="low_confidence"`.
- Tokens and cost: summed from bot turns (their totals include the turn's classifier calls). Cost is integer
  micro-USD from the platform model prices and is never estimated: an unknown cost adds nothing. A turn that
  called a model and has a cost is a priced turn; one that called a model but has no cost is an unpriced turn.
  A range or bucket shows the sum over its priced turns, and null (unknown) when it has no priced turn; its
  unpriced turns are always counted beside it.
- Agent assigned: `assigned` events to that membership.
- Agent handled: `resolved` events by that membership (`by="agent"`).
- Handle time: that `resolved` time minus the agent's first `assigned` time in the cycle (at or after the latest
  `entered_team`); without one, minus the cycle's start, else the conversation's creation.
- Agent first responses: that membership's `first_agent_response` events, timed as first response time.
- Agent CSAT: a `csat` event counts for the agent who made the latest `resolved` before it.
- Top escalation reasons: `entered_team` events by `rule_key` (labelled by the rule name), or by `source`
  (claim, assign) when there is no rule.
- Top topics: bot turns whose risk decision `rule_key` starts with `topic:` (classifier and keyword matches
  alike), counted by rule (labelled by the rule name).
- Knowledge gaps by KB: knowledge gap turns per knowledge base the turn searched (labelled by the KB's name).

Labels are rule or knowledge base names, never customer text.

Date ranges and buckets
-----------------------

`from` / `to` are dates in the organisation's support timezone (SupportSettings.timezone), both inclusive.
The default range is the last 7 days including today; a range is at most 92 days. `date_range_utc` turns a
range into the exact UTC interval between local midnights.

Dashboard buckets (`buckets`) are built from whole UTC hours, since rollups are hourly. A UTC hour belongs to
the local day its start falls in (`local_day`), so a day bucket runs from the first UTC hour starting at or
after local midnight to the first one starting at or after the next local midnight. A daylight-saving day has
23 or 25 hourly buckets. In a timezone with a :30 or :45 offset (India, Nepal, Newfoundland, parts of
Australia) local midnight falls inside a UTC hour, and that hour, holding the first 30 or 15 minutes of the
local day, is counted in the previous day: a day bucket is shifted by that part of an hour.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from supportcore.errors import DomainError

__all__ = [
    "DEFAULT_DAYS",
    "HOUR",
    "INTERVALS",
    "MAX_RANGE_DAYS",
    "RECENT_HOURS",
    "InvalidDateRangeError",
    "buckets",
    "date_range_utc",
    "day_bounds_utc",
    "hour_floor",
    "local_day",
    "zone",
]

DEFAULT_DAYS = 7
MAX_RANGE_DAYS = 92
HOUR = timedelta(hours=1)
INTERVALS = ("hour", "day")
# The rollup job recomputes this many closed hours on every run (supportcore.analytics.jobs).
RECENT_HOURS = 3


class InvalidDateRangeError(DomainError):
    pass


def zone(timezone: str) -> ZoneInfo:
    """The organisation's zone; an unknown zone stored earlier falls back to UTC rather than failing."""
    try:
        return ZoneInfo(timezone)
    except Exception:
        return ZoneInfo("UTC")


def date_range_utc(
    timezone: str, start: date | None, end: date | None, *, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """The UTC interval [start, end) covering the local dates `start`..`end` inclusive in `timezone`."""
    tz = zone(timezone)
    today = (now or datetime.now(UTC)).astimezone(tz).date()
    last = end or today
    first = start or last - timedelta(days=DEFAULT_DAYS - 1)
    if first > last:
        raise InvalidDateRangeError("'from' must not be after 'to'")
    if (last - first).days + 1 > MAX_RANGE_DAYS:
        raise InvalidDateRangeError(f"The range is at most {MAX_RANGE_DAYS} days")
    lo = datetime.combine(first, time.min, tz).astimezone(UTC)
    hi = datetime.combine(last + timedelta(days=1), time.min, tz).astimezone(UTC)
    return lo, hi


def hour_floor(at: datetime) -> datetime:
    """The start of the UTC hour `at` falls in."""
    return at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _hour_ceil(at: datetime) -> datetime:
    floor = hour_floor(at)
    return floor if floor == at else floor + HOUR


def local_day(hour: datetime, timezone: str) -> date:
    """The local date (in `timezone`) of the day bucket the UTC hour starting at `hour` belongs to."""
    return hour.astimezone(zone(timezone)).date()


def day_bounds_utc(day: date, timezone: str) -> tuple[datetime, datetime]:
    """The UTC hours [start, end) of local `day`'s bucket: whole hours whose start falls on that day."""
    tz = zone(timezone)
    start = datetime.combine(day, time.min, tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tz)
    return _hour_ceil(start), _hour_ceil(end)


def buckets(start: date, end: date, timezone: str, interval: str) -> list[tuple[datetime, datetime]]:
    """The UTC buckets [(start, end), ...] covering the local dates `start`..`end` inclusive: one per UTC
    hour (`hour`) or one per local day (`day`, built from whole UTC hours, see the module docstring). The
    range is not validated here (date_range_utc does that)."""
    if interval not in INTERVALS:
        raise ValueError(f"Unknown interval {interval!r}")
    if interval == "day":
        days = (end - start).days + 1
        return [day_bounds_utc(start + timedelta(days=n), timezone) for n in range(days)]
    lo, _ = day_bounds_utc(start, timezone)
    _, hi = day_bounds_utc(end, timezone)
    hours = int((hi - lo) / HOUR)
    return [(lo + n * HOUR, lo + (n + 1) * HOUR) for n in range(hours)]
