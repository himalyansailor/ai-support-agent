"""Client IP for rate limiting. X-Forwarded-For is client-controlled, so it is trusted only for the
hops added by proxies we run (settings.trusted_proxy_hops)."""

from collections.abc import Mapping
from typing import Any

from starlette.requests import HTTPConnection


def client_ip(scope_or_request: "HTTPConnection | Mapping[str, Any]", trusted_proxy_hops: int) -> str:
    """With hops n > 0, the n-th address from the right of X-Forwarded-For; otherwise (or when the
    header is shorter than n) the socket peer."""
    conn = scope_or_request if isinstance(scope_or_request, HTTPConnection) else HTTPConnection(dict(scope_or_request))
    if trusted_proxy_hops > 0:
        parts = [p.strip() for p in conn.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if len(parts) >= trusted_proxy_hops:
            return parts[-trusted_proxy_hops]
    return conn.client.host if conn.client else "unknown"
