from collections.abc import Iterable

from supportcore.errors import DomainError
from supportcore.rbac.permissions import Permission


def has_permission(granted: Iterable[str], required: Permission) -> bool:
    return required.value in set(granted)


class PermissionEscalationError(DomainError):
    def __init__(self) -> None:
        super().__init__("You cannot grant permissions you do not have.")


def ensure_can_grant(actor_permissions: Iterable[str], granted: Iterable[str]) -> None:
    """An actor may only hand out (assign, invite with, or put into a role) permissions they hold themselves."""
    if not set(granted) <= set(actor_permissions):
        raise PermissionEscalationError()
