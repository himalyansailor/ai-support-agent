"""The aiosmtplib adapter. Error mapping by phase against a scripted local SMTP server; the GreenMail test
(`make up-email`) sends a reply for real and reads it back over IMAP, and skips when GreenMail is not running."""

import asyncio
import base64
import socket
import uuid
from email import message_from_bytes
from email.policy import default as default_policy

import pytest

from supportcore.config import Settings
from supportcore.mail import smtp_outbound
from supportcore.mail.imap_inbound import ImapToolsInboundMailbox
from supportcore.mail.ports import MailAuthError, MailConnectionError, MailSendError, MailServer, OutboundEmail
from supportcore.mail.smtp_outbound import AiosmtplibOutboundSender

from .test_imap_adapter import IMAP, SMTP, _greenmail_up

pytestmark = pytest.mark.asyncio

PASSWORD = "smtp-pw-SENTINEL"


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, env="test", jwt_secret="test-secret-test-secret-test-secret-123", **kw)


def _email(**kw) -> OutboundEmail:
    base = dict(
        from_address="help@acme.example", from_name="Acme Help", to="cust@example.org", subject="Re: Printer",
        text="We are on it", message_id=f"<{uuid.uuid4().hex}@acme.example>", in_reply_to="<in1@example.org>",
        references=("<r0@example.org>", "<in1@example.org>"), mailbox_id=uuid.uuid4(),
    )
    base.update(kw)
    return OutboundEmail(**base)


class ScriptedSmtp:
    """A tiny SMTP server. `replies` overrides the reply to a command (`banner`, `auth`, `mail`, `rcpt`, `data`
    (the 354 step) and `end` (after the message)); a reply of "drop" closes the connection, "hang" never answers."""

    def __init__(self, **replies: str) -> None:
        self.replies = replies
        self.received: list[bytes] = []
        self.commands: list[bytes] = []
        self.connections = 0
        self.server: asyncio.base_events.Server | None = None
        self.port = 0

    async def __aenter__(self) -> "ScriptedSmtp":
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        self.server.close()
        self.server.close_clients()
        await self.server.wait_closed()

    def server_for(self, **kw) -> MailServer:
        return MailServer(**{"host": "127.0.0.1", "port": self.port, "security": "none", "username": "u",
                             "password": PASSWORD, **kw})

    async def _say(self, writer, step: str, default: str) -> bool:
        reply = self.replies.get(step, default)
        if reply == "drop":
            writer.close()
            return False
        if reply == "hang":
            await asyncio.sleep(3600)
        writer.write(reply.encode() + b"\r\n")
        await writer.drain()
        return True

    async def _handle(self, reader, writer) -> None:
        self.connections += 1
        try:
            if not await self._say(writer, "banner", "220 scripted ESMTP"):
                return
            while line := await reader.readline():
                self.commands.append(line)
                verb = line.split(b" ", 1)[0].strip().upper()
                if verb == b"EHLO":
                    writer.write(self.replies.get("ehlo", "250-scripted\r\n250-AUTH PLAIN\r\n250 8BITMIME").encode() + b"\r\n")
                elif verb == b"AUTH":
                    ok = await self._say(writer, "auth", "235 2.7.0 ok")
                    if not ok:
                        return
                    continue
                elif verb in (b"MAIL", b"RCPT"):
                    if not await self._say(writer, verb.decode().lower(), "250 ok"):
                        return
                    continue
                elif verb == b"DATA":
                    if not await self._say(writer, "data", "354 go ahead"):
                        return
                    if not self.replies.get("data", "354").startswith("354"):
                        continue
                    body = b""
                    while (chunk := await reader.readline()) and chunk != b".\r\n":
                        body += chunk
                    self.received.append(body)
                    if not await self._say(writer, "end", "250 queued"):
                        return
                    continue
                elif verb == b"QUIT":
                    if self.replies.get("quit") == "hang":
                        await asyncio.sleep(3600)
                    writer.write(b"221 bye\r\n")
                    await writer.drain()
                    writer.close()
                    return
                elif verb == b"RSET":
                    writer.write(b"250 ok\r\n")
                else:
                    writer.write(b"502 no\r\n")
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass


async def _fails(adapter, server, email=None) -> tuple[str, str]:
    with pytest.raises(MailSendError) as caught:
        await adapter.send(server, email or _email())
    assert PASSWORD not in repr(caught.value) and "cust@example.org" not in str(caught.value)
    assert caught.value.__cause__ is None
    return caught.value.kind, caught.value.error_type


async def test_sends_with_login_and_threading_headers():
    adapter = AiosmtplibOutboundSender(_settings(), timeout=5)
    async with ScriptedSmtp() as smtp:
        await adapter.send(smtp.server_for(), _email())
        await adapter.check(smtp.server_for())
    assert len(smtp.received) == 1
    sent = message_from_bytes(smtp.received[0], policy=default_policy)
    assert sent["In-Reply-To"] == "<in1@example.org>" and sent["Subject"] == "Re: Printer"
    auth = next(c for c in smtp.commands if c.startswith(b"AUTH PLAIN"))
    assert base64.b64decode(auth.split()[2]) == b"\x00u\x00" + PASSWORD.encode()
    assert b"MAIL FROM:<help@acme.example>" in b"".join(smtp.commands)
    assert b"RCPT TO:<cust@example.org>" in b"".join(smtp.commands)


@pytest.mark.parametrize(
    ("replies", "expected"),
    [
        ({"auth": "535 5.7.8 bad credentials"}, ("auth", "auth_failed")),
        ({"banner": "421 busy"}, ("transient", "smtp_4xx")),
        ({"mail": "451 4.3.0 try later"}, ("transient", "smtp_4xx")),
        ({"rcpt": "450 4.2.1 mailbox busy"}, ("transient", "smtp_4xx")),
        ({"mail": "550 5.7.1 sender refused"}, ("permanent", "smtp_5xx")),
        ({"rcpt": "550 5.1.1 no such user"}, ("permanent", "smtp_5xx")),
        ({"data": "554 5.5.1 no valid recipients"}, ("permanent", "smtp_5xx")),
        ({"end": "452 4.3.1 out of storage"}, ("transient", "smtp_4xx")),
        ({"end": "552 5.3.4 too big"}, ("permanent", "smtp_5xx")),
        ({"banner": "drop"}, ("transient", "connection_failed")),
        ({"mail": "drop"}, ("transient", "connection_failed")),
        ({"end": "drop"}, ("ambiguous", "ambiguous")),
        # After DATA starts only a real 4xx/5xx reply counts: no reply code at all is ambiguous (I1).
        ({"end": "this is not an smtp reply"}, ("ambiguous", "ambiguous")),
        ({"end": "250 " + "x" * 9000}, ("ambiguous", "ambiguous")),  # overlong: aiosmtplib reports code -1
        ({"end": "2"}, ("ambiguous", "ambiguous")),
        # A drop on the DATA command itself: counted as ambiguous (conservative: DATA had started).
        ({"data": "drop"}, ("ambiguous", "ambiguous")),
        # A malformed reply before DATA: nothing was accepted, so it is retried.
        ({"mail": "garbage"}, ("transient", "connection_failed")),
        # AUTH: a temporary failure is retried and does not mark the mailbox; no usable AUTH is an auth failure.
        ({"auth": "454 4.7.0 temporary authentication failure"}, ("transient", "smtp_4xx")),
        ({"ehlo": "250-scripted\r\n250 8BITMIME"}, ("auth", "auth_failed")),
    ],
)
async def test_error_mapping_by_phase(replies, expected):
    adapter = AiosmtplibOutboundSender(_settings(), timeout=5)
    async with ScriptedSmtp(**replies) as smtp:
        assert await _fails(adapter, smtp.server_for()) == expected


async def test_timeouts_before_and_during_data():
    adapter = AiosmtplibOutboundSender(_settings(), timeout=0.3)
    async with ScriptedSmtp(banner="hang") as smtp:
        assert await _fails(adapter, smtp.server_for()) == ("transient", "timeout")
    async with ScriptedSmtp(rcpt="hang") as smtp:
        assert await _fails(adapter, smtp.server_for()) == ("transient", "timeout")
    async with ScriptedSmtp(end="hang") as smtp:  # the data went out, no final reply: maybe delivered
        assert await _fails(adapter, smtp.server_for()) == ("ambiguous", "ambiguous")


async def test_overall_deadline_bounds_the_call():
    adapter = AiosmtplibOutboundSender(_settings(), timeout=30, deadline=0.4)
    loop = asyncio.get_running_loop()
    async with ScriptedSmtp(banner="hang") as smtp:
        started = loop.time()
        assert await _fails(adapter, smtp.server_for()) == ("transient", "timeout")
        assert loop.time() - started < 5
    async with ScriptedSmtp(end="hang") as smtp:
        assert await _fails(adapter, smtp.server_for()) == ("ambiguous", "ambiguous")


async def test_connection_refused_is_transient():
    adapter = AiosmtplibOutboundSender(_settings(), timeout=2)
    async with ScriptedSmtp() as smtp:
        port = smtp.port
    assert await _fails(adapter, MailServer("127.0.0.1", port, "none", "u", PASSWORD)) == (
        "transient", "connection_failed",
    )


async def test_invalid_message_is_permanent_and_never_connects():
    adapter = AiosmtplibOutboundSender(_settings(), timeout=2)
    async with ScriptedSmtp() as smtp:
        bad = [
            _email(subject="Hi\r\nBcc: x@evil.example"),
            _email(to="a@example.org, b@example.org"),
            _email(from_address="no-at-sign"),
            _email(to=""),
        ]
        for email in bad:
            assert await _fails(adapter, smtp.server_for(), email) == ("permanent", "invalid_message")
    assert smtp.connections == 0


async def test_tls_failures_are_tls_errors_with_verification_on(monkeypatch):
    contexts = []
    real = smtp_outbound.ssl.create_default_context

    def spy(*args, **kwargs):
        context = real(*args, **kwargs)
        contexts.append(context)
        return context

    monkeypatch.setattr(smtp_outbound.ssl, "create_default_context", spy)
    adapter = AiosmtplibOutboundSender(_settings(), timeout=2)
    async with ScriptedSmtp() as smtp:  # plain server: the TLS handshake fails
        assert await _fails(adapter, smtp.server_for(security="ssl")) == ("transient", "tls_error")
        # No STARTTLS offered: never falls back to sending the password in clear.
        assert await _fails(adapter, smtp.server_for(security="starttls")) == ("transient", "tls_error")
    assert not any(c.startswith(b"AUTH") for c in smtp.commands)
    assert contexts and all(c.verify_mode == smtp_outbound.ssl.CERT_REQUIRED and c.check_hostname for c in contexts)


async def test_private_addresses_refused_when_not_allowed():
    """A host that resolves only to disallowed addresses cannot work until the settings change: permanent. One that
    does not resolve (now) may later: transient."""
    adapter = AiosmtplibOutboundSender(_settings(allow_private_base_urls=False), timeout=2)
    async with ScriptedSmtp() as smtp:
        assert await _fails(adapter, smtp.server_for()) == ("permanent", "address_not_allowed")
        assert await _fails(adapter, smtp.server_for(host="localhost")) == ("permanent", "address_not_allowed")
        assert await _fails(adapter, smtp.server_for(host="no-such-host.invalid")) == (
            "transient", "connection_failed",
        )
    assert smtp.connections == 0


async def test_address_policy_matched_by_type_not_message(monkeypatch):
    """The adapter tells a disallowed address from an unresolvable host by the exception's type: a rewording of
    the policy's message cannot turn a permanent `address_not_allowed` into endless retries."""
    from supportcore.ai.base_url import AddressNotPublicError
    from supportcore.ai.errors import CredentialInvalidError

    async def not_public(host, port):
        raise AddressNotPublicError("reworded: that host is private")

    async def unresolvable(host, port):
        raise CredentialInvalidError("Base URL must be a public address")  # the old message, plain class

    adapter = AiosmtplibOutboundSender(_settings(allow_private_base_urls=False), timeout=2)
    server = MailServer("smtp.example.test", 25, "none", "u", PASSWORD)
    monkeypatch.setattr(smtp_outbound, "public_addresses", not_public)
    assert await _fails(adapter, server) == ("permanent", "address_not_allowed")
    monkeypatch.setattr(smtp_outbound, "public_addresses", unresolvable)
    assert await _fails(adapter, server) == ("transient", "connection_failed")


async def test_check_raises_connection_and_auth_errors_like_imap():
    """A refused connection on Test connection is a MailConnectionError (not an unexpected error); a refused login
    a MailAuthError."""
    with socket.socket() as probe:  # a port nothing listens on
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    adapter = AiosmtplibOutboundSender(_settings(), timeout=2)
    with pytest.raises(MailConnectionError) as raised:
        await adapter.check(MailServer("127.0.0.1", port, "none", "u", PASSWORD))
    assert raised.value.error_type == "connection_failed" and raised.value.__cause__ is None
    async with ScriptedSmtp(auth="535 5.7.8 bad credentials") as smtp:
        with pytest.raises(MailAuthError):
            await adapter.check(smtp.server_for())


async def test_quit_runs_inside_the_deadline_and_never_fails_a_sent_message():
    adapter = AiosmtplibOutboundSender(_settings(), timeout=30, deadline=0.5)
    loop = asyncio.get_running_loop()
    async with ScriptedSmtp(quit="hang") as smtp:
        started = loop.time()
        await adapter.send(smtp.server_for(), _email())  # accepted: the hanging QUIT changes nothing
        assert loop.time() - started < 3
    assert len(smtp.received) == 1


async def test_socket_closed_when_the_deadline_cancels_the_connect(monkeypatch):
    """The pinned connect is cancelled by the overall deadline: its socket is closed, not leaked."""
    made = []
    real_socket = smtp_outbound.socket.socket

    def tracking(*args, **kwargs):
        sock = real_socket(*args, **kwargs)
        made.append(sock)
        return sock

    async def validated(host, port):
        return ["127.0.0.1"]

    async def never(sock, address):
        await asyncio.sleep(3600)

    monkeypatch.setattr(smtp_outbound, "public_addresses", validated)
    monkeypatch.setattr(smtp_outbound.socket, "socket", tracking)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "sock_connect", never)
    adapter = AiosmtplibOutboundSender(_settings(allow_private_base_urls=False), timeout=30, deadline=0.3)
    assert await _fails(adapter, MailServer("smtp.example.test", 25, "none", "u", PASSWORD)) == ("transient", "timeout")
    assert len(made) == 1 and made[0].fileno() == -1  # closed


async def test_connects_to_the_validated_address_not_a_new_lookup(monkeypatch):
    """DNS rebinding: the host is resolved once and validated; the connection goes to that address (here the
    test name does not resolve at all, so any second lookup would fail)."""
    lookups = []

    async def validated(host, port):
        lookups.append((host, port))
        return ["127.0.0.1"]

    monkeypatch.setattr(smtp_outbound, "public_addresses", validated)
    adapter = AiosmtplibOutboundSender(_settings(allow_private_base_urls=False), timeout=5)
    async with ScriptedSmtp() as smtp:
        await adapter.send(smtp.server_for(host="smtp.pinned.invalid"), _email())
    assert lookups == [("smtp.pinned.invalid", smtp.port)]
    assert len(smtp.received) == 1


@pytest.mark.integration
@pytest.mark.skipif(not _greenmail_up(), reason="GreenMail is not running (make up-email)")
async def test_greenmail_send_threads():
    """A reply sent through GreenMail's SMTP is read back from the customer's mailbox over IMAP, threaded."""
    customer = f"c-{uuid.uuid4().hex[:12]}@support.test"  # auto-created on delivery: login and password are the address
    sender = AiosmtplibOutboundSender(_settings(), timeout=10)
    email = _email(from_address="support@support.test", to=customer, text="Threaded reply")
    # SMTP AUTH needs an existing account: the one infra/docker-compose.yml configures.
    smtp = MailServer(SMTP[0], SMTP[1], "none", "support", "support-dev-pass")
    await sender.check(smtp)
    await sender.send(smtp, email)

    inbox = ImapToolsInboundMailbox(_settings(), timeout=10)
    imap = MailServer(IMAP[0], IMAP[1], "none", customer, customer)
    fetched = None
    for _ in range(50):
        fetched = await inbox.fetch(imap, "INBOX", after_uid=0, since=None, limit=10, max_bytes=1_000_000)
        if fetched.messages:
            break
        await asyncio.sleep(0.1)
    assert fetched is not None and len(fetched.messages) == 1
    got = message_from_bytes(fetched.messages[0].raw, policy=default_policy)
    assert got["In-Reply-To"] == "<in1@example.org>"
    assert got["References"].split() == ["<r0@example.org>", "<in1@example.org>"]
    assert got["Message-ID"] == email.message_id and got["Subject"] == "Re: Printer"
    assert got["X-Support-Mailbox"] == str(email.mailbox_id) and got["Auto-Submitted"] is None
    assert got.get_content().strip() == "Threaded reply"
