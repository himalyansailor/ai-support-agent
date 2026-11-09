"""The SMTP adapter of `OutboundMailSender`, on aiosmtplib (registered in wiring.py as `smtp`).

Every call opens its own connection: `ssl` connects over TLS, `starttls` upgrades before logging in, `none` stays
plain. TLS verifies the certificate and the host name (`ssl.create_default_context()`).

- Timeouts: 30 s per SMTP operation, and an overall deadline (DEADLINE_SECONDS) around the whole call, so a server
  that drips cannot hold a send past the job's claim on the row.
- SSRF / DNS rebinding: when private addresses are not allowed (prod), the host is resolved once, every address
  must pass `public_addresses` (the same policy as the IMAP adapter), and the TCP connection goes to one of those
  validated addresses while TLS (SNI and the certificate check) still uses the host name.
- Phases: everything before the message data (connect, TLS, login, MAIL FROM, RCPT TO) failing means the
  message was not accepted, so it can be retried. Once DATA starts, only a real 4xx/5xx reply says what happened;
  anything else (a disconnect, a timeout, a malformed or overlong reply) is `ambiguous` (the server may have
  accepted it): never retried, so the customer never gets it twice. QUIT runs inside the deadline, and once the
  message is accepted nothing that happens after (QUIT, the deadline) can turn the send into a failure.
- Errors carry short codes only (`MailSendError(kind, error_type)`), never server text, addresses or content, and
  the original exception is not chained.

| failure | before DATA | during DATA |
|---|---|---|
| authentication refused (5xx, or no usable AUTH) | auth / auth_failed | - |
| 4xx reply (AUTH included) | transient / smtp_4xx | transient / smtp_4xx |
| 5xx reply | permanent / smtp_5xx | permanent / smtp_5xx |
| no real reply code (malformed, overlong) | transient / connection_failed | ambiguous / ambiguous |
| connect error, disconnect | transient / connection_failed | ambiguous / ambiguous |
| timeout (one operation, or the deadline) | transient / timeout | ambiguous / ambiguous |
| TLS handshake or certificate, no STARTTLS | transient / tls_error | - |
| the host resolves only to disallowed addresses | permanent / address_not_allowed | - |
| the host does not resolve | transient / connection_failed | - |
| a header with CR/LF, not one recipient | permanent / invalid_message | - |
"""

import asyncio
import socket
import ssl
from email.message import EmailMessage
from typing import Any

import aiosmtplib

from supportcore.ai.base_url import AddressNotPublicError, public_addresses
from supportcore.ai.errors import CredentialInvalidError
from supportcore.config import Settings
from supportcore.mail.compose import build_mime
from supportcore.mail.ports import MailAuthError, MailConnectionError, MailSendError, MailServer, OutboundEmail

__all__ = ["AiosmtplibOutboundSender", "DEADLINE_SECONDS", "SMTP_TIMEOUT_SECONDS"]

SMTP_TIMEOUT_SECONDS = 30.0  # per SMTP operation
DEADLINE_SECONDS = 110.0  # per call, whatever the server does; below the 120 s send claim (outbound.CLAIM_SECONDS)
_CONNECTION_ERRORS = frozenset({"connection_failed", "timeout", "tls_error"})


class _PhasedSMTP(aiosmtplib.SMTP):
    """Remembers whether the message data has started to go out."""

    in_data = False

    async def data(self, message: str | bytes, /, **kwargs: Any) -> aiosmtplib.SMTPResponse:
        self.in_data = True
        return await super().data(message, **kwargs)


class _Unreachable(Exception):
    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type


def _real_code(code: int) -> bool:
    return 400 <= code < 600


def _by_code(code: int) -> MailSendError:
    if 400 <= code < 500:
        return MailSendError("transient", "smtp_4xx")
    if 500 <= code < 600:
        return MailSendError("permanent", "smtp_5xx")
    return MailSendError("transient", "connection_failed")  # no real reply (aiosmtplib uses -1)


def _tls_failure(exc: BaseException) -> bool:
    """A TLS handshake or certificate failure (aiosmtplib wraps one met while connecting in SMTPConnectError)."""
    return any(isinstance(e, (ssl.SSLError, ssl.CertificateError)) for e in (exc, exc.__cause__, exc.__context__))


def _send_error(exc: BaseException, in_data: bool, logging_in: bool) -> MailSendError:
    if isinstance(exc, MailSendError):
        return exc
    if isinstance(exc, _Unreachable):
        return MailSendError("transient", exc.error_type)
    if in_data:
        # Only a real final reply says what happened to the data; aiosmtplib reports a malformed or overlong
        # reply as code -1, which says nothing.
        if isinstance(exc, aiosmtplib.SMTPResponseException) and _real_code(exc.code):
            return _by_code(exc.code)
        return MailSendError("ambiguous", "ambiguous")
    if isinstance(exc, aiosmtplib.SMTPAuthenticationError):
        if 400 <= exc.code < 500:  # e.g. 454: a temporary authentication failure
            return MailSendError("transient", "smtp_4xx")
        return MailSendError("auth", "auth_failed")
    if isinstance(exc, aiosmtplib.SMTPRecipientsRefused):
        codes = [r.code for r in exc.recipients]
        return _by_code(max(codes) if codes else -1)
    if isinstance(exc, aiosmtplib.SMTPResponseException):
        return _by_code(exc.code)
    if _tls_failure(exc):
        return MailSendError("transient", "tls_error")
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):  # aiosmtplib's timeouts are TimeoutErrors too
        return MailSendError("transient", "timeout")
    if isinstance(exc, (aiosmtplib.SMTPServerDisconnected, aiosmtplib.SMTPConnectError, OSError)):
        return MailSendError("transient", "connection_failed")
    if isinstance(exc, aiosmtplib.SMTPNotSupported):  # e.g. a non-ASCII address without SMTPUTF8
        return MailSendError("permanent", "invalid_message")
    if isinstance(exc, ValueError):  # aiosmtplib found no usable sender or recipient
        return MailSendError("permanent", "invalid_message")
    if logging_in and isinstance(exc, aiosmtplib.SMTPException):  # the server offers no AUTH we can use
        return MailSendError("auth", "auth_failed")
    if isinstance(exc, aiosmtplib.SMTPException):  # e.g. STARTTLS not offered
        return MailSendError("transient", "tls_error")
    return MailSendError("transient", "connection_failed")


async def _open_socket(addresses: list[str], port: int, timeout: float) -> socket.socket:
    """A TCP connection to the first validated address that answers."""
    loop = asyncio.get_running_loop()
    error_type = "connection_failed"
    for address in addresses:
        sock = socket.socket(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setblocking(False)
            await asyncio.wait_for(loop.sock_connect(sock, (address, port)), timeout)
            return sock
        except BaseException as exc:  # cancellation (the deadline) included: never leak the socket
            sock.close()
            if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
                error_type = "timeout"
            elif isinstance(exc, OSError):
                error_type = "connection_failed"
            else:
                raise
    raise _Unreachable(error_type)


class AiosmtplibOutboundSender:
    def __init__(
        self, settings: Settings, *, timeout: float = SMTP_TIMEOUT_SECONDS, deadline: float = DEADLINE_SECONDS
    ) -> None:
        self._check_addresses = not settings.effective_allow_private_base_urls()
        self._timeout = timeout
        self._deadline = deadline

    async def check(self, server: MailServer) -> None:
        """Connects, logs in and quits. Raises like the IMAP adapter's check where the failure is the same:
        MailAuthError (login refused), MailConnectionError (connection_failed, timeout, tls_error: e.g. a refused
        connection); MailSendError for anything else (smtp_4xx, smtp_5xx, address_not_allowed)."""
        try:
            await self._session(server, None)
        except MailSendError as exc:
            if exc.kind == "auth":
                raise MailAuthError() from None
            if exc.error_type in _CONNECTION_ERRORS:
                raise MailConnectionError(exc.error_type) from None
            raise

    async def send(self, server: MailServer, email: OutboundEmail) -> None:
        if "@" not in email.from_address or not email.to or "@" not in email.to or "," in email.to:
            raise MailSendError("permanent", "invalid_message")
        try:
            mime = build_mime(email)
        except ValueError:  # CR/LF in a header value: never retried
            raise MailSendError("permanent", "invalid_message") from None
        await self._session(server, (email, mime))

    # --- internals ------------------------------------------------------------------------------------

    async def _addresses(self, server: MailServer) -> list[str] | None:
        if not self._check_addresses:
            return None
        try:
            return await public_addresses(server.host, server.port)
        except AddressNotPublicError:  # it resolves, but not (only) to public addresses: settings must change
            raise MailSendError("permanent", "address_not_allowed") from None
        except CredentialInvalidError:  # does not resolve (now)
            raise MailSendError("transient", "connection_failed") from None

    async def _session(self, server: MailServer, message: tuple[OutboundEmail, EmailMessage] | None) -> None:
        smtp: _PhasedSMTP | None = None
        logging_in = False
        done = False  # the message was accepted (or, for check, the login worked)
        failure: MailSendError | None = None
        try:
            async with asyncio.timeout(self._deadline):
                addresses = await self._addresses(server)
                smtp = await self._connect(server, addresses)
                logging_in = True
                await smtp.login(server.username, server.password)
                logging_in = False
                if message is not None:
                    email, mime = message
                    await smtp.send_message(mime, sender=email.from_address, recipients=[email.to])
                done = True
                try:  # inside the deadline; its failure changes nothing
                    await smtp.quit()
                except Exception:
                    pass
        except Exception as exc:  # cancellation passes through (the connection is still dropped below)
            if not done:  # e.g. the deadline hit during QUIT: the message is already accepted
                failure = _send_error(exc, smtp is not None and smtp.in_data, logging_in)
        finally:
            if smtp is not None:
                smtp.close()
        if failure is not None:
            raise failure from None

    async def _connect(self, server: MailServer, addresses: list[str] | None) -> _PhasedSMTP:
        common: dict[str, Any] = {
            "username": None,  # login is a separate step, so its failure is told apart from the connect
            "timeout": self._timeout,
            "use_tls": server.security == "ssl",
            "start_tls": server.security == "starttls",
            "tls_context": ssl.create_default_context(),  # certificates and host names are verified
        }
        sock: socket.socket | None = None
        try:
            if addresses:
                sock = await _open_socket(addresses, server.port, self._timeout)
                # The host name, not the address, is what TLS checks the certificate against (and the SNI).
                smtp = _PhasedSMTP(hostname=server.host, sock=sock, **common)
            else:
                smtp = _PhasedSMTP(hostname=server.host, port=server.port, **common)
        except BaseException:
            if sock is not None:
                sock.close()
            raise
        try:
            await smtp.connect()
        except BaseException:  # cancellation included
            smtp.close()
            if sock is not None:
                sock.close()  # a no-op once the transport owns and closed it
            raise
        return smtp
