"""SSRF guard for org-supplied base URLs (the openai_compatible provider).

In prod a tenant must not be able to point the server at loopback, private, link-local, reserved or
multicast addresses (cloud metadata, internal services). The check runs when a credential is created
and again right before each outbound call; the hardened HTTP client (supportcore.ai.http) repeats it
for the address each connection is actually opened to, because DNS answers can change in between.
"""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from supportcore.ai.errors import CredentialInvalidError

NOT_PUBLIC_MESSAGE = "Base URL must be a public address"


class AddressNotPublicError(CredentialInvalidError):
    """The host resolves (or is) only partly or not at all to public addresses: the settings must change. A host
    that does not resolve raises the plain CredentialInvalidError (it may resolve later)."""

    def __init__(self, message: str = NOT_PUBLIC_MESSAGE) -> None:
        super().__init__(message)

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _is_public(ip: IPAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _literal_ip(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])  # drop an IPv6 zone id
    except ValueError:
        return None


async def public_addresses(host: str, port: int | None) -> list[str]:
    """The addresses `host` resolves to, if every one is public (a literal IP must be public itself).
    Raises CredentialInvalidError otherwise. Used by ensure_public_base_url and, at connect time, by
    the hardened HTTP client (supportcore.ai.http), which connects to one of the returned addresses.
    AddressNotPublicError (a CredentialInvalidError) when an address is not public."""
    host = (host or "").rstrip(".").lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host:
        raise AddressNotPublicError()
    if host == "localhost" or host.endswith(".localhost"):
        raise AddressNotPublicError()
    literal = _literal_ip(host)
    if literal is not None:
        if not _is_public(literal):
            raise AddressNotPublicError()
        return [str(literal)]
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise CredentialInvalidError("Base URL host could not be resolved") from None
    addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
    parsed = [_literal_ip(a) for a in addresses]
    if not addresses or any(ip is None or not _is_public(ip) for ip in parsed):
        raise AddressNotPublicError()
    return addresses


async def ensure_public_base_url(url: str, *, allow_private: bool) -> None:
    """Raises CredentialInvalidError unless `url`'s host is (and resolves only to) public addresses.

    A first gate only: the answer can change before the request is sent, so outbound calls also go
    through supportcore.ai.http, which re-checks the address it actually connects to."""
    if allow_private:
        return
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        raise CredentialInvalidError("Base URL has an invalid port") from None
    await public_addresses(parts.hostname or "", port)
