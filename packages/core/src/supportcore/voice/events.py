"""Voice channel audit events. Ids and changed field names only: never a token, key, phone number or URL."""

from dataclasses import dataclass, field
from uuid import UUID

from supportcore.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class TelephonyConnected(DomainEvent):
    account_id: UUID
    kind: str


@dataclass(frozen=True, kw_only=True)
class TelephonyDisconnected(DomainEvent):
    account_id: UUID


@dataclass(frozen=True, kw_only=True)
class PhoneNumberAdded(DomainEvent):
    number_id: UUID
    ownership: str


@dataclass(frozen=True, kw_only=True)
class PhoneNumberReleased(DomainEvent):
    number_id: UUID
    ownership: str


@dataclass(frozen=True, kw_only=True)
class VoiceSettingsUpdated(DomainEvent):
    changed: tuple[str, ...] = field(default_factory=tuple)
