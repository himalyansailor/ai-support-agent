"""The billing alert emails: subject and body in plain English, each linking to the organisation's billing page."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

__all__ = ["AlertContent", "alert_email", "billing_url"]

_METER_LABELS = {"messages": "messages", "voice_minutes": "voice minutes"}


@dataclass(frozen=True)
class AlertContent:
    """What an alert email says, beyond its kind."""

    meter: str | None = None
    used: int | None = None
    limit: int | None = None
    trial_ends_at: datetime | None = None
    overage: bool = False  # usage past the limit continues and is billed as overage


def billing_url(web_origin: str, organisation_id: UUID) -> str:
    return f"{web_origin.rstrip('/')}/orgs/{organisation_id}/settings/billing"


def _date(value: datetime | None) -> str:
    return value.strftime("%-d %B %Y at %H:%M UTC") if value is not None else "soon"


def alert_email(
    kind: str, organisation_name: str, organisation_id: UUID, web_origin: str, content: AlertContent
) -> tuple[str, str]:
    """(subject, body) for an alert kind: `{meter}_80`, `{meter}_100`, `trial_ending`, `trial_ended` or
    `payment_failed`."""
    link = billing_url(web_origin, organisation_id)
    if kind.endswith(("_80", "_100")) and content.meter is not None:
        label = _METER_LABELS.get(content.meter, content.meter.replace("_", " "))
        usage = f"{content.used} of {content.limit} {label}"
        if kind.endswith("_80"):
            subject = f"{organisation_name} has used 80% of its {label} this billing period"
            lead = f"{organisation_name} has used {usage} included in its plan this billing period."
            advice = (
                "When the limit is reached, usage continues and is billed as overage."
                if content.overage
                else "When the limit is reached, new usage is blocked unless your plan bills overage."
            )
        elif content.overage:
            subject = f"{organisation_name} has used all of its included {label}"
            lead = f"{organisation_name} has used {usage} included in its plan this billing period."
            advice = (
                "Usage continues, and anything over the limit is billed as overage. "
                "You can upgrade your plan to include more."
            )
        else:
            subject = f"{organisation_name} has reached its {label} limit"
            lead = f"{organisation_name} has used {usage} included in its plan this billing period."
            advice = (
                "New usage is now blocked until the next billing period, unless your plan bills overage. "
                "You can upgrade your plan or turn on overage billing."
            )
        body = f"{lead}\n\n{advice}\n\nManage billing: {link}\n"
        return subject, body
    if kind == "trial_ending":
        subject = f"The {organisation_name} trial ends soon"
        body = (
            f"The free trial for {organisation_name} ends on {_date(content.trial_ends_at)}.\n\n"
            "Choose a plan to keep your team and your bot working without a break.\n\n"
            f"Choose a plan: {link}\n"
        )
        return subject, body
    if kind == "trial_ended":
        subject = f"The {organisation_name} trial has ended"
        body = (
            f"The free trial for {organisation_name} ended on {_date(content.trial_ends_at)}.\n\n"
            "Your data is kept, but the bot and new usage are paused until you choose a plan.\n\n"
            f"Choose a plan: {link}\n"
        )
        return subject, body
    if kind == "payment_failed":
        subject = f"Payment failed for {organisation_name}"
        body = (
            f"We could not take the latest payment for {organisation_name}. "
            "We will try again, and your service continues for now.\n\n"
            "Please update your payment method to avoid an interruption.\n\n"
            f"Update payment details: {link}\n"
        )
        return subject, body
    raise ValueError(f"Unknown alert kind: {kind}")
