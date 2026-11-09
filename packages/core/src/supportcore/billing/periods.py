"""The billing period an organisation's usage is counted in, and the account status the limits are checked against."""

from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, datetime

from supportcore.billing.ports import BillingProvider
from supportcore.db.models import BillingAccount

__all__ = ["Period", "current_period", "effective_status"]


@dataclass(frozen=True)
class Period:
    start: datetime
    end: datetime


def _utc_month(now: datetime) -> Period:
    now = now.astimezone(UTC)
    start = datetime(now.year, now.month, 1, tzinfo=UTC)
    end = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=UTC)
    return Period(start, end)


def _add_months(value: datetime, months: int) -> datetime:
    """`value` moved by whole months, its day clamped to the target month's length (31 Jan + 1 month = 28/29 Feb)."""
    years, month0 = divmod(value.month - 1 + months, 12)
    year, month = value.year + years, month0 + 1
    return value.replace(year=year, month=month, day=min(value.day, monthrange(year, month)[1]))


def _rolled_forward(start: datetime, now: datetime) -> Period:
    """The monthly period, anchored on `start`, that contains `now`."""
    months = 0
    while _add_months(start, months + 1) <= now:
        months += 1
    return Period(_add_months(start, months), _add_months(start, months + 1))


def current_period(account: BillingAccount, now: datetime) -> Period:
    """A trial counts over the trial window; a subscription over the provider's period, rolled forward by whole
    months from its start once it has ended (until the renewal webhook stores the next one); otherwise (no plan, a
    trial that has ended, a lapsed or cancelled subscription) the UTC calendar month."""
    if account.status == "trialing" and account.trial_started_at and account.trial_ends_at:
        if now < account.trial_ends_at:
            return Period(account.trial_started_at, account.trial_ends_at)
    elif account.status in ("active", "past_due") and account.current_period_start and account.current_period_end:
        if now < account.current_period_end:
            return Period(account.current_period_start, account.current_period_end)
        return _rolled_forward(account.current_period_start, now)
    return _utc_month(now)


def effective_status(account: BillingAccount, now: datetime, provider: BillingProvider) -> str:
    """The account's status, except `dev` when the provider grants everything and `trial_expired` for a trial
    whose end has passed."""
    if provider.grants_everything:
        return "dev"
    if account.status == "trialing" and account.trial_ends_at is not None and now >= account.trial_ends_at:
        return "trial_expired"
    return account.status
