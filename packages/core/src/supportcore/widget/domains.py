"""Allowed-domain entries of a widget: normalisation, origin matching and the CSP frame-ancestors value.

An entry is a host with an optional port and an optional `*.` prefix (subdomains only, never the apex):
`shop.example.com`, `*.example.com`, `localhost:5500`. Stored lowercased, without scheme.
"""

import re
from urllib.parse import urlsplit

from supportcore.errors import DomainError

LOCAL_HOSTS = ("localhost", "127.0.0.1")
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST = rf"{_LABEL}(?:\.{_LABEL})*"
_ENTRY = re.compile(rf"^(\*\.)?({_HOST})(?::(\d{{1,5}}))?$")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://")


class InvalidDomainError(DomainError):
    def __init__(self, entry: str = "") -> None:
        super().__init__(f"Invalid domain: {entry[:100]!r}")


def normalize_domain(entry: str) -> str:
    value = (entry or "").strip().lower()
    value = _SCHEME.sub("", value, count=1)
    if value.endswith("/"):
        value = value[:-1]
    match = _ENTRY.match(value)
    if match is None:
        raise InvalidDomainError(entry or "")
    wildcard, host, port = match.groups()
    if port is not None and not 1 <= int(port) <= 65535:
        raise InvalidDomainError(entry)
    if wildcard and "." not in host:  # "*.com": a wildcard over a bare TLD
        raise InvalidDomainError(entry)
    return value


def _split(entry: str) -> tuple[bool, str, str | None]:
    match = _ENTRY.match(entry)
    assert match is not None, entry  # stored entries are normalised
    wildcard, host, port = match.groups()
    return bool(wildcard), host, port


def _origin_parts(origin: str) -> tuple[str, str, str | None] | None:
    try:
        parts = urlsplit(origin.strip().lower())
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host or parts.path or parts.query or parts.fragment:
        return None
    if parts.username or parts.password:
        return None
    if port == (443 if parts.scheme == "https" else 80):
        port = None
    return parts.scheme, host, str(port) if port is not None else None


def origin_allowed(origin: str, domains: list[str], *, web_origin: str) -> bool:
    """The dashboard (`web_origin`) is always allowed; an embedding site must be https unless local."""
    if origin == web_origin:
        return True
    parsed = _origin_parts(origin or "")
    if parsed is None:
        return False
    scheme, host, port = parsed
    if scheme == "http" and host not in LOCAL_HOSTS:
        return False
    for entry in domains:
        wildcard, entry_host, entry_port = _split(entry)
        if entry_port != port:
            continue
        if host == entry_host and not wildcard:
            return True
        if wildcard and host.endswith("." + entry_host):
            return True
    return False


def frame_ancestors(domains: list[str], web_origin: str) -> str:
    sources = [web_origin]
    for entry in domains:
        _, host, _ = _split(entry)
        sources.append(f"https://{entry}")
        if host in LOCAL_HOSTS:
            sources.append(f"http://{entry}")
    return "frame-ancestors " + " ".join(dict.fromkeys(sources))
