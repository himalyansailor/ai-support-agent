"""Billing errors. The API layer maps these to HTTP statuses (see supportapi.errors)."""

from supportcore.errors import DomainError


class PlanLimitError(DomainError):
    """The organisation's plan does not allow this (402 plan_limit). `limit` is None when the plan has no such
    allowance at all."""

    def __init__(self, resource: str, limit: int | None) -> None:
        self.resource = resource
        self.limit = limit
        super().__init__(f"Your plan's {resource.replace('_', ' ')} limit has been reached")


class BillingProviderError(DomainError):
    """The billing provider failed or timed out. The provider's message is never carried here."""


class BillingNotConfiguredError(DomainError):
    """The selected billing provider has no credentials configured."""


class BillingDisabledError(DomainError):
    """Checkout and the billing portal are unavailable on the dev adapter."""


class OverageUnsupportedError(DomainError):
    """The billing provider cannot meter usage, so a plan with overage cannot be used."""


class InvalidWebhookError(DomainError):
    """A billing webhook failed signature or payload checks."""


class PlanNotAvailableError(DomainError):
    """The plan is inactive or not yet synced to the billing provider."""


class AlreadySubscribedError(DomainError):
    """The organisation already has a live subscription."""


class NotSubscribedError(DomainError):
    """The organisation has no subscription to manage."""


class PlanKeyTakenError(DomainError):
    """Another plan already uses this key."""


class TrialNotAvailableError(DomainError):
    """The organisation has already had a trial, has a plan, or the plan offers no trial."""
