"""Voice channel domain errors (mapped to HTTP codes in supportapi.errors.DOMAIN_ERRORS)."""

from supportcore.errors import DomainError


class InvalidVoiceSettingsError(DomainError):
    pass


class TelephonyCredentialsError(DomainError):
    """The Twilio credentials are malformed or were rejected by Twilio."""


class TelephonyNotConfiguredError(DomainError):
    """A needed piece of telephony setup is missing (platform credentials, public URL, connected account)."""


class PhoneNumberUnavailableError(DomainError):
    """The number cannot be bought or connected (taken, already connected, or refused by Twilio)."""


class VoiceNotAvailableError(DomainError):
    """Voice is not ready for this organisation."""


class TelephonyUnavailableError(DomainError):
    """Twilio could not be reached or failed; the call can be retried later."""


class TelephonyAccountInUseError(DomainError):
    """The Twilio account is connected elsewhere, or still has numbers that depend on it."""


class InvalidVoiceTokenError(DomainError):
    """A stream token or browser call ticket is invalid, expired, or already used."""

    def __init__(self, message: str = "Invalid voice token") -> None:
        super().__init__(message)


class NoCallbackNumberError(DomainError):
    """The conversation has no number to call back (`callback_phone` is empty)."""


class AgentUnreachableError(DomainError):
    """A callback by phone, but the agent has no E.164 profile number to ring."""
