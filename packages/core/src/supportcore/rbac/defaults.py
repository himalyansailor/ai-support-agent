from supportcore.rbac.permissions import ALL_PERMISSIONS, Permission

DEFAULT_ROLES: dict[str, frozenset[Permission]] = {
    "Owner": ALL_PERMISSIONS,
    "Admin": ALL_PERMISSIONS - {Permission.ORG_MANAGE, Permission.BILLING_MANAGE},
    "Agent": frozenset({Permission.QUERIES_ANSWER, Permission.CALLS_TAKE}),
    "Viewer": frozenset({Permission.ANALYTICS_VIEW}),
}
