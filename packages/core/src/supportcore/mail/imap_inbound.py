"""The IMAP adapter of `InboundMailbox`, on imap-tools (registered in wiring.py as `imap`).

Every call opens its own connection inside `asyncio.to_thread` (imap-tools and imaplib are blocking), with a
30 s socket timeout per operation, logs in, SELECTs the folder read-only (EXAMINE) and logs out. The mailbox is
read-only to us: bodies are fetched with `BODY.PEEK[]`, so no flag changes, and nothing is moved or deleted.

- Size: `RFC822.SIZE` is fetched first and a message over `max_bytes` comes back with raw=None, body unread. The
  body fetch itself is a partial fetch of at most `max_bytes + 1` octets, so a server that under-reports the size
  (GreenMail reports the body size only) still cannot make us read more than that; `size` is then at least the
  number of octets actually read.
- Bodies are not parsed here: the raw bytes go to `supportcore.mail.isolate` (imap-tools' MailMessage would parse
  them in this process, so it is not used).
- `uid > after_uid` is filtered here as well as in the search: `UID n:*` returns the last message even when its
  uid is below n.
- IMAP SINCE is date-granular (and in the server's time zone): the search asks for a day earlier and the exact
  `since` is applied to INTERNALDATE here.
- SSRF / DNS rebinding: when private addresses are not allowed (prod), the host is resolved once, every address
  must pass `public_addresses`, and the socket connects to that validated address while TLS still uses the host
  name for SNI and certificate verification. A port-only change is covered too, since this runs on every call.
  A host that resolves to a disallowed address is `MailConnectionError("address_not_allowed")` (the settings must
  change); one that does not resolve is `connection_failed`.
- TLS (`ssl` and `starttls`) uses `ssl.create_default_context()`: certificates and host names are verified.
- Errors carry short codes only (`MailAuthError`, `MailConnectionError(error_type)`), never server text, and the
  original exception is not chained (its message may echo the server or the account).
"""

import asyncio
import imaplib
import re
import socket
import ssl
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from imap_tools import MailBox, MailBoxStartTls, MailBoxUnencrypted
from imap_tools.errors import MailboxLoginError

from supportcore.ai.base_url import AddressNotPublicError, public_addresses
from supportcore.ai.errors import CredentialInvalidError
from supportcore.config import Settings
from supportcore.mail.ports import FetchedMessage, FetchResult, MailAuthError, MailConnectionError, MailServer

__all__ = ["CALL_DEADLINE_SECONDS", "IMAP_TIMEOUT_SECONDS", "ImapToolsInboundMailbox"]

IMAP_TIMEOUT_SECONDS = 30.0  # per socket operation
CALL_DEADLINE_SECONDS = 120.0  # per adapter call, whatever the server does
LITERAL_SLACK_BYTES = 64 * 1024  # a fetch accepts literals up to max_bytes + this
SMALL_LITERAL_BYTES = LITERAL_SLACK_BYTES  # check / uidvalidity read no bodies
# Everything one call may read (lines and literals, asked for or not): max_bytes (+ the batch's body cap for a
# fetch) + this much protocol overhead.
CALL_BUDGET_SLACK_BYTES = 1024 * 1024
_META_CHUNK = 200  # uids per metadata FETCH
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_UID = re.compile(rb"\bUID (\d+)")
_SIZE = re.compile(rb"\bRFC822\.SIZE (\d+)")
_INTERNALDATE = re.compile(rb'\bINTERNALDATE "([^"]+)"')

T = TypeVar("T")


class _ProtocolError(Exception):
    """The server answered a command with NO/BAD or something unreadable."""


# --- connections pinned to validated addresses, with per-call limits -----------------------------------


class _Oversized(Exception):
    """The server sent more than this call allows (one literal, or the call's whole byte budget)."""


class _Expired(Exception):
    """The call's overall deadline passed."""


class _StartTlsFailed(Exception):
    """STARTTLS was not offered, refused, or its handshake failed."""


class _Guard:
    """Limits of one adapter call, shared with its imaplib client.

    - `max_literal`: the largest `{N}` literal the server may announce.
    - `budget`: every byte read (literals and lines, solicited or not) counts against it.
    - The deadline: when it passes, a timer thread shuts down the socket the call is using right now. The socket
      is registered as soon as it exists (before the TCP connect, before and during a TLS handshake), so a server
      that stalls or drips during connect, the handshake, the greeting or any response cannot hold the call open.
    """

    def __init__(self, max_literal: int, budget: int) -> None:
        self.max_literal = max_literal
        self.remaining = budget
        self.expired = False
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()

    def track(self, sock: socket.socket) -> None:
        with self._lock:
            self._sock = sock
            expired = self.expired
        if expired:
            _shutdown_socket(sock)
            raise _Expired()

    def expire(self) -> None:
        with self._lock:
            self.expired = True
            sock = self._sock
        if sock is not None:
            _shutdown_socket(sock)

    def check(self) -> None:
        if self.expired:
            raise _Expired()

    def drop(self) -> None:
        """Shuts the current socket down (a failed call: no LOGOUT, the server may still be streaming)."""
        with self._lock:
            sock = self._sock
        if sock is not None:
            _shutdown_socket(sock)
            try:
                sock.close()
            except OSError:
                pass

    def spend(self, n: int) -> None:
        self.remaining -= n
        if self.remaining < 0:
            raise _Oversized()


def _shutdown_socket(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _connect(host: str, addresses: list[str] | None, port: int, timeout: float | None, guard: _Guard) -> socket.socket:
    """A TCP connection to one of `addresses` (validated ones), else to what `host` resolves to. Each socket is
    handed to the guard before it connects."""
    if addresses:
        targets = [(socket.AF_INET6 if ":" in a else socket.AF_INET, a) for a in addresses]
    else:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        targets = list(dict.fromkeys((info[0], info[4][0]) for info in infos))
    last: OSError | None = None
    for family, address in targets:
        guard.check()
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            guard.track(sock)
            sock.settimeout(timeout)
            sock.connect((address, port))
            return sock
        except OSError as exc:
            sock.close()
            last = exc
    guard.check()
    raise last or OSError("no address")


class _GuardedContext:
    """Wraps an SSLContext so the TLS socket is registered with the guard before its handshake starts."""

    def __init__(self, context: ssl.SSLContext, guard: _Guard) -> None:
        self.context = context
        self._guard = guard

    def wrap_socket(self, sock: socket.socket, server_hostname: str | None = None, **_kw: Any) -> ssl.SSLSocket:
        wrapped = self.context.wrap_socket(sock, server_hostname=server_hostname, do_handshake_on_connect=False)
        self._guard.track(wrapped)  # the raw socket is detached now: this is the one to shut down
        wrapped.do_handshake()
        return wrapped


class _Limited:
    """Reads through the guard: the deadline, the literal limit and the call's byte budget. (imaplib otherwise
    reads any `{N}` literal in full, and lines up to 20 MB, from solicited and unsolicited responses alike.)"""

    _guard: _Guard
    file: Any

    def read(self, size: int) -> bytes:
        self._guard.check()
        if size > self._guard.max_literal or size > self._guard.remaining:
            raise _Oversized()
        data = super().read(size)  # type: ignore[misc]
        self._guard.spend(len(data))
        return data

    def readline(self) -> bytes:
        self._guard.check()
        limit = min(imaplib._MAXLINE, max(self._guard.remaining, 0))
        line = self.file.readline(limit + 1)
        self._guard.spend(len(line))
        if len(line) > imaplib._MAXLINE:
            raise imaplib.IMAP4.error("line too long")
        return line


class _PinnedIMAP4(_Limited, imaplib.IMAP4):
    """imaplib.IMAP4 that connects to one of `addresses` (when given) instead of resolving `host` again."""

    def __init__(self, host: str, port: int, *, timeout: float, addresses: list[str] | None, guard: _Guard) -> None:
        self._addresses = addresses
        self._guard = guard
        super().__init__(host, port, timeout=timeout)

    def _create_socket(self, timeout: float | None) -> socket.socket:
        return _connect(self.host, self._addresses, self.port, timeout, self._guard)


class _PinnedIMAP4_SSL(_Limited, imaplib.IMAP4_SSL):  # noqa: N801
    def __init__(
        self,
        host: str,
        port: int,
        *,
        ssl_context: ssl.SSLContext,
        timeout: float,
        addresses: list[str] | None,
        guard: _Guard,
    ) -> None:
        self._addresses = addresses
        self._guard = guard
        super().__init__(host, port, ssl_context=ssl_context, timeout=timeout)

    def _create_socket(self, timeout: float | None) -> socket.socket:
        raw = _connect(self.host, self._addresses, self.port, timeout, self._guard)
        # The host name, not the address, is what the certificate is checked against (and the SNI).
        return _GuardedContext(self.ssl_context, self._guard).wrap_socket(raw, server_hostname=self.host)


class _SslMailBox(MailBox):
    def __init__(self, host: str, port: int, timeout: float, addresses: list[str] | None, guard: _Guard) -> None:
        self._addresses = addresses
        self._guard = guard
        super().__init__(host, port, timeout=timeout, ssl_context=ssl.create_default_context())

    def _get_mailbox_client(self) -> imaplib.IMAP4:
        return _PinnedIMAP4_SSL(
            self._host,
            self._port,
            ssl_context=self._ssl_context,
            timeout=self._timeout,
            addresses=self._addresses,
            guard=self._guard,
        )


class _StartTlsMailBox(MailBoxStartTls):
    def __init__(self, host: str, port: int, timeout: float, addresses: list[str] | None, guard: _Guard) -> None:
        self._addresses = addresses
        self._guard = guard
        super().__init__(host, port, timeout=timeout, ssl_context=ssl.create_default_context())

    def _get_mailbox_client(self) -> imaplib.IMAP4:
        client = _PinnedIMAP4(
            self._host, self._port, timeout=self._timeout, addresses=self._addresses, guard=self._guard
        )
        try:
            # imaplib raises IMAP4.abort when STARTTLS is not advertised, IMAP4.error when it is refused, and
            # ssl.SSLError when the handshake (or the certificate check) fails. It never returns a non-OK result.
            client.starttls(_GuardedContext(self._ssl_context, self._guard))
        except (imaplib.IMAP4.error, ssl.SSLError, ssl.CertificateError):
            _shutdown(client)
            if self._guard.expired:
                raise _Expired() from None
            raise _StartTlsFailed() from None
        except BaseException:
            _shutdown(client)
            raise
        return client


class _PlainMailBox(MailBoxUnencrypted):
    def __init__(self, host: str, port: int, timeout: float, addresses: list[str] | None, guard: _Guard) -> None:
        self._addresses = addresses
        self._guard = guard
        super().__init__(host, port, timeout=timeout)

    def _get_mailbox_client(self) -> imaplib.IMAP4:
        return _PinnedIMAP4(
            self._host, self._port, timeout=self._timeout, addresses=self._addresses, guard=self._guard
        )


def _shutdown(client: imaplib.IMAP4) -> None:
    try:
        client.shutdown()
    except Exception:
        pass


_MAILBOXES = {"ssl": _SslMailBox, "starttls": _StartTlsMailBox, "none": _PlainMailBox}


# --- response parsing ---------------------------------------------------------------------------------


def _ok(result: tuple[str, list[Any]]) -> list[Any]:
    if result[0] != "OK":
        raise _ProtocolError()
    return result[1]


def _search(client: imaplib.IMAP4, criteria: str) -> list[int]:
    data = _ok(client.uid("SEARCH", criteria))
    line = data[0] if data else b""
    return sorted({int(n) for n in (line or b"").split() if n.isdigit()})


def _internaldate(value: bytes) -> datetime | None:
    try:
        return datetime.strptime(value.decode("ascii").strip(), "%d-%b-%Y %H:%M:%S %z")
    except (UnicodeDecodeError, ValueError):
        return None


def _metadata(client: imaplib.IMAP4, uids: list[int]) -> dict[int, tuple[int, datetime]]:
    """uid -> (RFC822.SIZE, INTERNALDATE). Messages expunged meanwhile (or unreadable lines) are left out."""
    found: dict[int, tuple[int, datetime]] = {}
    data = _ok(client.uid("FETCH", ",".join(str(u) for u in uids), "(UID RFC822.SIZE INTERNALDATE)"))
    for item in data:
        line = item[0] if isinstance(item, tuple) else item
        if not isinstance(line, bytes):
            continue
        uid, size, date = _UID.search(line), _SIZE.search(line), _INTERNALDATE.search(line)
        when = _internaldate(date.group(1)) if date else None
        if uid and size and when is not None:
            found[int(uid.group(1))] = (int(size.group(1)), when)
    return found


def _body(client: imaplib.IMAP4, uid: int, max_bytes: int) -> bytes | None:
    """At most max_bytes + 1 octets of the message (BODY.PEEK: the \\Seen flag is not set). None if it is gone."""
    data = _ok(client.uid("FETCH", str(uid), f"(UID BODY.PEEK[]<0.{max_bytes + 1}>)"))
    for item in data:
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bytes):
            match = _UID.search(item[0]) if isinstance(item[0], bytes) else None
            if match is None or int(match.group(1)) == uid:
                return item[1]
    return None


def _code(client: imaplib.IMAP4, name: str) -> int | None:
    """A response code of the last SELECT (`[UIDVALIDITY n]`, `[UIDNEXT n]`), which imaplib keeps aside."""
    _typ, values = client.response(name)
    for value in values or ():
        if isinstance(value, bytes) and value.strip().isdigit():
            return int(value.strip())
    return None


def _imap_date(when: datetime) -> str:
    return f"{when.day:02d}-{_MONTHS[when.month - 1]}-{when.year:04d}"


# --- the adapter --------------------------------------------------------------------------------------


class ImapToolsInboundMailbox:
    def __init__(
        self, settings: Settings, *, timeout: float = IMAP_TIMEOUT_SECONDS, deadline: float = CALL_DEADLINE_SECONDS
    ) -> None:
        self._check_addresses = not settings.effective_allow_private_base_urls()
        self._timeout = timeout
        self._deadline = deadline

    async def check(self, server: MailServer, folder: str) -> None:
        await self._call(server, folder, lambda box, client: None, SMALL_LITERAL_BYTES, CALL_BUDGET_SLACK_BYTES)

    async def uidvalidity(self, server: MailServer, folder: str) -> int:
        return await self._call(
            server,
            folder,
            lambda box, client: self._uidvalidity(box, client, folder),
            SMALL_LITERAL_BYTES,
            CALL_BUDGET_SLACK_BYTES,
        )

    async def fetch(
        self,
        server: MailServer,
        folder: str,
        *,
        after_uid: int | None,
        since: datetime | None,
        limit: int,
        max_bytes: int,
        max_batch_bytes: int | None = None,
    ) -> FetchResult:
        def run(box: Any, client: imaplib.IMAP4) -> FetchResult:
            uidvalidity = self._uidvalidity(box, client, folder)
            uidnext = _code(client, "UIDNEXT")
            if after_uid is not None:
                candidates = [u for u in _search(client, f"UID {after_uid + 1}:*") if u > after_uid]
            elif since is not None:
                candidates = _search(client, f"SINCE {_imap_date(since - timedelta(days=1))}")
            else:
                candidates = _search(client, "ALL")
            picked: list[tuple[int, int, datetime | None]] = []
            more = False
            for start in range(0, len(candidates), _META_CHUNK):
                if len(picked) > limit:
                    more = True  # at least one qualifying message is left for the next run
                    break
                chunk = candidates[start : start + _META_CHUNK]
                meta = _metadata(client, chunk)
                for uid in chunk:
                    if uid not in meta:  # listed by SEARCH, but no readable RFC822.SIZE / INTERNALDATE
                        picked.append((uid, 0, None))
                        continue
                    size, when = meta[uid]
                    if after_uid is None and since is not None and when < since:
                        continue
                    picked.append((uid, size, when))
            more = more or len(picked) > limit
            messages: list[FetchedMessage] = []
            total = 0
            for uid, size, when in picked[:limit]:
                if when is None:
                    messages.append(
                        FetchedMessage(uid=uid, internal_date=datetime.now(UTC), size=0, raw=None, unreadable=True)
                    )
                    continue
                body_size = size if size <= max_bytes else 0  # a too-large message is not downloaded
                if messages and max_batch_bytes is not None and total + body_size > max_batch_bytes:
                    more = True  # the byte cap (always at least one message): the rest comes in the next run
                    break
                raw = None
                if size <= max_bytes:
                    raw = _body(client, uid, max_bytes)
                    if raw is None:  # expunged since the search
                        continue
                    size = max(size, len(raw))  # some servers (GreenMail) under-report RFC822.SIZE
                    total += len(raw)
                    if len(raw) > max_bytes:  # the partial fetch stopped at max_bytes + 1
                        raw = None
                messages.append(FetchedMessage(uid=uid, internal_date=when, size=size, raw=raw))
            return FetchResult(uidvalidity=uidvalidity, messages=messages, uidnext=uidnext, more=more)

        # The bodies stop at max_batch_bytes plus at most one more body, so legitimate traffic stays inside this.
        budget = (max_batch_bytes or 0) + max_bytes + 1 + CALL_BUDGET_SLACK_BYTES
        return await self._call(server, folder, run, max_bytes + LITERAL_SLACK_BYTES, budget)

    # --- internals ------------------------------------------------------------------------------------

    @staticmethod
    def _uidvalidity(box: Any, client: imaplib.IMAP4, folder: str) -> int:
        value = _code(client, "UIDVALIDITY")
        if value is None:  # not in the SELECT response: ask with STATUS
            value = box.folder.status(folder, ("UIDVALIDITY",)).get("UIDVALIDITY")
        if not isinstance(value, int):
            raise _ProtocolError()
        return value

    async def _addresses(self, server: MailServer) -> list[str] | None:
        if not self._check_addresses:
            return None
        try:
            return await public_addresses(server.host, server.port)
        except AddressNotPublicError:  # it resolves, but not (only) to public addresses: settings must change
            raise MailConnectionError("address_not_allowed") from None
        except CredentialInvalidError:  # does not resolve (now)
            raise MailConnectionError("connection_failed") from None

    async def _call(
        self,
        server: MailServer,
        folder: str,
        op: Callable[[Any, imaplib.IMAP4], T],
        max_literal: int,
        budget: int,
    ) -> T:
        addresses = await self._addresses(server)
        return await asyncio.to_thread(self._blocking, server, folder, addresses, op, _Guard(max_literal, budget))

    def _blocking(
        self,
        server: MailServer,
        folder: str,
        addresses: list[str] | None,
        op: Callable[[Any, imaplib.IMAP4], T],
        guard: _Guard,
    ) -> T:
        error: Exception | None = None
        box = None
        timer = threading.Timer(self._deadline, guard.expire)
        timer.daemon = True
        timer.start()
        try:
            box = _MAILBOXES[server.security](server.host, server.port, self._timeout, addresses, guard)
            try:
                if server.username.isascii() and server.password.isascii():
                    box.login(server.username, server.password, initial_folder=None)
                else:  # LOGIN is ASCII-only: AUTHENTICATE PLAIN carries UTF-8
                    box.login_utf8(server.username, server.password, initial_folder=None)
            except (MailboxLoginError, imaplib.IMAP4.error) as exc:
                if isinstance(exc, imaplib.IMAP4.abort) or guard.expired:
                    raise
                error = MailAuthError()
            else:
                box.folder.set(folder, readonly=True)  # EXAMINE: nothing we do can change the mailbox
                result = op(box, box.client)
                _close(box)
                return result
        except Exception as exc:
            error = _connection_error(exc, guard)
        finally:
            timer.cancel()
            if error is not None:
                guard.drop()  # no LOGOUT on a failed connection: the server may still be streaming
        assert error is not None
        raise error from None


def _connection_error(exc: Exception, guard: _Guard) -> Exception:
    if isinstance(exc, (MailAuthError, MailConnectionError)):
        return exc
    if isinstance(exc, _Oversized):
        return MailConnectionError("oversized_response")
    if guard.expired or isinstance(exc, (_Expired, TimeoutError)):  # socket.timeout included
        return MailConnectionError("timeout")
    if isinstance(exc, (ssl.SSLError, ssl.CertificateError, _StartTlsFailed)):
        return MailConnectionError("tls_error")
    # OSError (refused, unreachable, DNS), imaplib aborts, NO/BAD answers (e.g. no such folder), garbage.
    return MailConnectionError("connection_failed")


def _close(box: Any) -> None:
    """LOGOUT after a successful call, or just drop the socket when that fails."""
    try:
        box.client.logout()
    except Exception:
        _shutdown(box.client)
