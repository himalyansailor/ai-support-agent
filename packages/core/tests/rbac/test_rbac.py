import pytest

from supportcore.rbac.checker import has_permission
from supportcore.rbac.defaults import DEFAULT_ROLES
from supportcore.rbac.permissions import (
    ALL_PERMISSIONS,
    PERMISSION_LABELS,
    InvalidPermissionError,
    Permission,
    validate_permissions,
)


def test_permission_keys_are_exact():
    assert {p.value for p in Permission} == {
        "org.manage",
        "kb.manage",
        "bot.configure",
        "queries.answer",
        "calls.take",
        "team.manage",
        "analytics.view",
        "billing.manage",
        "channels.manage",
    }
    assert Permission.KB_MANAGE == "kb.manage"
    assert Permission.QUERIES_ANSWER == "queries.answer"


def test_all_permissions_matches_enum():
    assert ALL_PERMISSIONS == frozenset(Permission)


def test_every_permission_has_a_label():
    assert set(PERMISSION_LABELS) == set(Permission)
    assert all(label.strip() for label in PERMISSION_LABELS.values())


def test_owner_has_all():
    assert DEFAULT_ROLES["Owner"] == ALL_PERMISSIONS


def test_admin_lacks_billing_and_org_manage():
    admin = DEFAULT_ROLES["Admin"]
    assert Permission.BILLING_MANAGE not in admin
    assert Permission.ORG_MANAGE not in admin
    assert admin == ALL_PERMISSIONS - {Permission.BILLING_MANAGE, Permission.ORG_MANAGE}


def test_agent_permissions_exact():
    assert DEFAULT_ROLES["Agent"] == {Permission.QUERIES_ANSWER, Permission.CALLS_TAKE}


def test_viewer_permissions_exact():
    assert DEFAULT_ROLES["Viewer"] == {Permission.ANALYTICS_VIEW}


def test_default_roles_keys():
    assert set(DEFAULT_ROLES) == {"Owner", "Admin", "Agent", "Viewer"}


def test_validate_permissions_rejects_unknown():
    with pytest.raises(InvalidPermissionError) as exc:
        validate_permissions(["kb.manage", "nope"])
    assert exc.value.unknown == ["nope"]


def test_validate_permissions_returns_enum_members():
    assert validate_permissions(["kb.manage", "calls.take"]) == [
        Permission.KB_MANAGE,
        Permission.CALLS_TAKE,
    ]


def test_has_permission():
    assert has_permission(["queries.answer"], Permission.QUERIES_ANSWER)
    assert not has_permission(["queries.answer"], Permission.KB_MANAGE)
    assert not has_permission([], Permission.KB_MANAGE)
