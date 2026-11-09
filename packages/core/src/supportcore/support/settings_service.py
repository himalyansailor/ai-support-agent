import re
from functools import cache
from datetime import date
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, available_timezones

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.db.models import SupportSettings, User
from supportcore.db.models.support import DEFAULT_SLA_TARGETS, DEFAULT_WEEKLY_HOURS, WEEKDAYS
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import DomainError
from supportcore.events import EventBus
from supportcore.support.calendar import Calendar, format_hhmm, parse_weekly_hours
from supportcore.support.events import SupportSettingsUpdated

__all__ = [
    "DEFAULT_SLA_TARGETS",
    "DEFAULT_WEEKLY_HOURS",
    "InvalidBusinessHoursError",
    "InvalidSlaTargetsError",
    "SupportSettingsService",
]

MAX_HOLIDAYS = 100
MAX_TIMEZONE_LENGTH = 64
SLA_PRIORITIES = ("urgent", "high", "normal", "low")
SLA_KINDS = ("first_response", "resolution")
MIN_SLA_MINUTES, MAX_SLA_MINUTES = 1, 100000
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class InvalidBusinessHoursError(DomainError):
    """Bad timezone, weekly hours or holidays (the message never echoes the input)."""


class InvalidSlaTargetsError(DomainError):
    pass


@cache
def _known_timezones() -> frozenset[str]:
    return frozenset(available_timezones())


def _validate_timezone(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_TIMEZONE_LENGTH:
        raise InvalidBusinessHoursError("Timezone must be an IANA timezone name")
    name = value.strip()
    # Exact membership: a case-insensitive filesystem would otherwise accept "europe/london" or "posixrules".
    if name not in _known_timezones():
        raise InvalidBusinessHoursError("Unknown timezone")
    try:
        ZoneInfo(name)
    except (KeyError, ValueError, OSError):  # not found, malformed key, or a directory/unreadable file
        raise InvalidBusinessHoursError("Unknown timezone") from None
    return name


def _validate_weekly_hours(value: object) -> dict[str, list[list[str]]]:
    try:
        parsed = parse_weekly_hours(value)
    except ValueError as exc:
        raise InvalidBusinessHoursError(str(exc)) from None
    if not any(parsed):
        raise InvalidBusinessHoursError("At least one weekday needs an open interval")
    return {
        day: [[format_hhmm(start), format_hhmm(end)] for start, end in intervals]
        for day, intervals in zip(WEEKDAYS, parsed)
    }


def _validate_holidays(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_HOLIDAYS * 2:  # duplicates are folded below
        raise InvalidBusinessHoursError("Holidays must be a list of ISO dates")
    dates: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not _ISO_DATE.match(item):
            raise InvalidBusinessHoursError("Holidays must be ISO dates (YYYY-MM-DD)")
        try:
            date.fromisoformat(item)
        except ValueError:
            raise InvalidBusinessHoursError("Holidays must be valid dates") from None
        dates.add(item)
    if len(dates) > MAX_HOLIDAYS:
        raise InvalidBusinessHoursError(f"At most {MAX_HOLIDAYS} holidays")
    return sorted(dates)


def _validate_sla_targets(value: object) -> dict[str, dict[str, int]]:
    if not isinstance(value, dict) or set(value) != set(SLA_PRIORITIES):
        raise InvalidSlaTargetsError(f"SLA targets need exactly the priorities {', '.join(SLA_PRIORITIES)}")
    result: dict[str, dict[str, int]] = {}
    for priority in SLA_PRIORITIES:
        entry = value[priority]
        if not isinstance(entry, dict) or set(entry) != set(SLA_KINDS):
            raise InvalidSlaTargetsError(f"{priority} needs exactly {' and '.join(SLA_KINDS)}")
        for kind in SLA_KINDS:
            minutes = entry[kind]
            if isinstance(minutes, bool) or not isinstance(minutes, int) or not MIN_SLA_MINUTES <= minutes <= MAX_SLA_MINUTES:
                raise InvalidSlaTargetsError(
                    f"{priority} {kind} must be an integer from {MIN_SLA_MINUTES} to {MAX_SLA_MINUTES}"
                )
        result[priority] = {kind: entry[kind] for kind in SLA_KINDS}
    return result


class SupportSettingsService:
    def __init__(self, session: AsyncSession, organisation_id: UUID, events: EventBus) -> None:
        self._session = session
        self._org_id = organisation_id
        self._events = events
        self._settings = TenantScopedRepository(session, SupportSettings, organisation_id)

    async def get(self, *, for_update: bool = False) -> SupportSettings:
        """The org's settings; the defaults are created on first access."""
        row = await self._load(for_update)
        if row is None:
            # ON CONFLICT DO NOTHING: concurrent first reads must not fail on the primary key.
            await self._session.execute(
                insert(SupportSettings)
                .values(
                    organisation_id=self._org_id,
                    timezone="UTC",
                    weekly_hours=DEFAULT_WEEKLY_HOURS,
                    holidays=[],
                    sla_targets=DEFAULT_SLA_TARGETS,
                )
                .on_conflict_do_nothing(index_elements=[SupportSettings.organisation_id])
            )
            row = await self._load(for_update)
            assert row is not None
        return row

    async def update(
        self,
        timezone: str,
        weekly_hours: dict[str, Any],
        holidays: list[str],
        sla_targets: dict[str, Any],
        actor: User,
    ) -> SupportSettings:
        new_values: dict[str, Any] = {
            "timezone": _validate_timezone(timezone),
            "weekly_hours": _validate_weekly_hours(weekly_hours),
            "holidays": _validate_holidays(holidays),
            "sla_targets": _validate_sla_targets(sla_targets),
        }
        row = await self.get(for_update=True)
        changes: dict[str, Any] = {}
        for name, value in new_values.items():
            old = getattr(row, name)
            if old != value:
                changes[name] = {"from": old, "to": value}
                setattr(row, name, value)
        if changes:
            await self._session.flush()
            await self._events.publish(
                SupportSettingsUpdated(organisation_id=self._org_id, actor_user_id=actor.id, changes=changes)
            )
        return row

    async def calendar(self) -> Calendar:
        row = await self.get()
        return Calendar(
            tz=ZoneInfo(row.timezone),
            intervals=parse_weekly_hours(row.weekly_hours),
            holidays=frozenset(date.fromisoformat(h) for h in row.holidays),
        )

    async def _load(self, for_update: bool) -> SupportSettings | None:
        stmt = self._settings.select(for_update=for_update)
        if for_update:
            stmt = stmt.execution_options(populate_existing=True)  # refresh after acquiring the lock
        return (await self._session.execute(stmt)).scalar_one_or_none()
