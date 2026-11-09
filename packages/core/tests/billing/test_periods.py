from datetime import UTC, datetime, timedelta

from supportcore.billing.dev_billing import DevBilling
from supportcore.billing.fake_billing import FakeBilling
from supportcore.billing.periods import Period, current_period, effective_status
from supportcore.db.models import BillingAccount

NOW = datetime(2026, 10, 28, 10, 0, tzinfo=UTC)


def test_trial_period_is_trial_window():
    start, end = NOW - timedelta(days=3), NOW + timedelta(days=11)
    account = BillingAccount(status="trialing", trial_started_at=start, trial_ends_at=end)
    assert current_period(account, NOW) == Period(start, end)


def test_active_period_is_provider_period():
    start, end = datetime(2026, 10, 15, tzinfo=UTC), datetime(2026, 11, 15, tzinfo=UTC)
    account = BillingAccount(status="active", current_period_start=start, current_period_end=end)
    assert current_period(account, NOW) == Period(start, end)


def test_no_plan_period_is_utc_month():
    account = BillingAccount(status="no_plan")
    assert current_period(account, NOW) == Period(datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC))


def test_december_month_rolls_into_next_year():
    account = BillingAccount(status="no_plan")
    period = current_period(account, datetime(2026, 12, 31, 23, tzinfo=UTC))
    assert period == Period(datetime(2026, 12, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC))


def test_expired_trial_falls_back_to_calendar_month():
    account = BillingAccount(
        status="trialing", trial_started_at=NOW - timedelta(days=20), trial_ends_at=NOW - timedelta(days=1)
    )
    assert current_period(account, NOW) == Period(datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC))


def test_trial_expired_status():
    account = BillingAccount(
        status="trialing", trial_started_at=NOW - timedelta(days=14), trial_ends_at=NOW - timedelta(seconds=1)
    )
    assert effective_status(account, NOW, FakeBilling()) == "trial_expired"
    account.trial_ends_at = NOW + timedelta(days=1)
    assert effective_status(account, NOW, FakeBilling()) == "trialing"


def test_dev_provider_status_is_dev():
    assert effective_status(BillingAccount(status="no_plan"), NOW, DevBilling()) == "dev"


def test_ended_paid_period_rolls_forward_by_whole_months():
    """M5: the renewal webhook has not landed yet: the period rolls on from the stored start, not the UTC month."""
    start = datetime(2026, 9, 15, 8, 30, tzinfo=UTC)
    account = BillingAccount(
        status="active", current_period_start=start, current_period_end=datetime(2026, 10, 15, 8, 30, tzinfo=UTC)
    )
    assert current_period(account, NOW) == Period(
        datetime(2026, 10, 15, 8, 30, tzinfo=UTC), datetime(2026, 11, 15, 8, 30, tzinfo=UTC)
    )
    account.status = "past_due"
    later = datetime(2027, 1, 20, tzinfo=UTC)  # several renewals missed
    assert current_period(account, later) == Period(
        datetime(2027, 1, 15, 8, 30, tzinfo=UTC), datetime(2027, 2, 15, 8, 30, tzinfo=UTC)
    )
    account.status = "canceled"  # not paying: the calendar month
    assert current_period(account, NOW).start == datetime(2026, 10, 1, tzinfo=UTC)


def test_rolled_period_keeps_month_end_anchor():
    start = datetime(2026, 1, 31, tzinfo=UTC)
    account = BillingAccount(status="active", current_period_start=start, current_period_end=datetime(2026, 2, 28, tzinfo=UTC))
    assert current_period(account, datetime(2026, 3, 5, tzinfo=UTC)) == Period(
        datetime(2026, 2, 28, tzinfo=UTC), datetime(2026, 3, 31, tzinfo=UTC)
    )
    assert current_period(account, datetime(2026, 4, 1, tzinfo=UTC)) == Period(
        datetime(2026, 3, 31, tzinfo=UTC), datetime(2026, 4, 30, tzinfo=UTC)
    )
