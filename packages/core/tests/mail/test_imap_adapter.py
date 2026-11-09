"""The imap-tools adapter. The GreenMail tests (`make up-email`) skip when nothing answers on localhost:3143; each
test mails a fresh recipient, which GreenMail auto-creates with the address as login and password."""

import imaplib
import smtplib
import socket
import uuid
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest

from supportcore.config import Settings
from supportcore.mail.imap_inbound import ImapToolsInboundMailbox
from supportcore.mail.ports import MailAuthError, MailConnectionError, MailServer

pytestmark = pytest.mark.asyncio

IMAP = ("localhost", 3143)
SMTP = ("localhost", 3025)


def _greenmail_up() -> bool:
    try:
        with socket.create_connection(IMAP, timeout=0.5):
            return True
    except OSError:
        return False


greenmail = pytest.mark.skipif(not _greenmail_up(), reason="GreenMail is not running (make up-email)")


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, env="test", jwt_secret="test-secret-test-secret-test-secret-123", **kw)


def _send(to: str, n: int, *, body: str = "hello") -> str:
    msg = EmailMessage()
    msg["From"] = f"customer{n}@example.test"
    msg["To"] = to
    msg["Subject"] = f"Question {n}"
    msg["Message-ID"] = f"<gm-{n}-{uuid.uuid4().hex}@example.test>"
    msg.set_content(body)
    with smtplib.SMTP(*SMTP, timeout=10) as smtp:
        smtp.send_message(msg)
    return msg["Message-ID"]


@pytest.fixture
def account() -> MailServer:
    address = f"t-{uuid.uuid4().hex[:12]}@support.test"
    return MailServer(host="localhost", port=IMAP[1], security="none", username=address, password=address)


@pytest.fixture
def adapter() -> ImapToolsInboundMailbox:
    return ImapToolsInboundMailbox(_settings(), timeout=10)


@pytest.mark.integration
@greenmail
async def test_greenmail_fetch_roundtrip(adapter, account):
    ids = [_send(account.username, n) for n in range(3)]
    await adapter.check(account, "INBOX")
    uidvalidity = await adapter.uidvalidity(account, "INBOX")

    first = await adapter.fetch(account, "INBOX", after_uid=0, since=None, limit=2, max_bytes=1_000_000)
    assert first.uidvalidity == uidvalidity
    uids = [m.uid for m in first.messages]
    assert len(uids) == 2 and uids == sorted(uids)
    assert ids[0].encode() in first.messages[0].raw and ids[1].encode() in first.messages[1].raw
    assert first.messages[0].size == len(first.messages[0].raw)
    assert first.uidnext is not None and first.uidnext > uids[-1]

    rest = await adapter.fetch(account, "INBOX", after_uid=uids[-1], since=None, limit=50, max_bytes=1_000_000)
    assert len(rest.messages) == 1 and ids[2].encode() in rest.messages[0].raw
    last = rest.messages[0].uid
    # `UID n:*` with n past the end: nothing, even where a server answers with the last message.
    assert (await adapter.fetch(account, "INBOX", after_uid=last, since=None, limit=50, max_bytes=1_000_000)).messages == []

    now = datetime.now(UTC)
    recent = await adapter.fetch(account, "INBOX", after_uid=None, since=now - timedelta(hours=1), limit=50, max_bytes=1_000_000)
    assert [m.uid for m in recent.messages] == [*uids, last]
    future = await adapter.fetch(account, "INBOX", after_uid=None, since=now + timedelta(hours=1), limit=50, max_bytes=1_000_000)
    assert future.messages == [] and future.uidnext == last + 1  # SINCE is by day; the exact time is applied here

    small = await adapter.fetch(account, "INBOX", after_uid=0, since=None, limit=1, max_bytes=50)
    assert small.messages[0].raw is None and small.messages[0].size > 50

    # Read-only: nothing was marked \Seen.
    client = imaplib.IMAP4(*IMAP, timeout=10)
    try:
        client.login(account.username, account.password)
        client.select("INBOX", readonly=True)
        _typ, data = client.uid("FETCH", "1:*", "(FLAGS)")
        assert data and all(b"\\Seen" not in line for line in data if isinstance(line, bytes))
    finally:
        client.logout()


@pytest.mark.integration
@greenmail
async def test_greenmail_bad_password_raises_auth_error(adapter):
    server = MailServer(host="localhost", port=IMAP[1], security="none", username="support", password="wrong")
    with pytest.raises(MailAuthError) as raised:
        await adapter.check(server, "INBOX")
    assert raised.value.__cause__ is None and str(raised.value) == ""  # no server text, nothing chained


@pytest.mark.integration
@greenmail
async def test_greenmail_tls_against_plain_port_is_tls_error(adapter, account):
    _send(account.username, 1)
    with pytest.raises(MailConnectionError) as raised:
        await adapter.check(MailServer("localhost", IMAP[1], "ssl", account.username, account.password), "INBOX")
    assert raised.value.error_type in ("tls_error", "timeout")


@pytest.mark.integration
@greenmail
async def test_greenmail_missing_folder_is_connection_failed(adapter, account):
    _send(account.username, 1)
    with pytest.raises(MailConnectionError) as raised:
        await adapter.check(account, "No/Such/Folder")
    assert raised.value.error_type == "connection_failed"


@pytest.mark.integration
@greenmail
async def test_greenmail_connects_to_the_validated_address_not_a_new_lookup(monkeypatch, account):
    """With the address policy on, the host is resolved once (here: stubbed to GreenMail's address) and the socket
    goes to that address; the unresolvable host name is never looked up again."""
    calls = []

    async def validated(host, port):
        calls.append((host, port))
        return ["127.0.0.1"]

    monkeypatch.setattr("supportcore.mail.imap_inbound.public_addresses", validated)
    adapter = ImapToolsInboundMailbox(_settings(allow_private_base_urls=False), timeout=10)
    _send(account.username, 1)
    pinned = MailServer("imap.does-not-resolve.invalid", IMAP[1], "none", account.username, account.password)

    assert await adapter.uidvalidity(pinned, "INBOX") > 0
    assert calls == [("imap.does-not-resolve.invalid", IMAP[1])]


async def test_private_address_refused_when_the_policy_is_on():
    adapter = ImapToolsInboundMailbox(_settings(allow_private_base_urls=False), timeout=2)
    with pytest.raises(MailConnectionError) as raised:
        await adapter.check(MailServer("127.0.0.1", 3143, "none", "u", "p"), "INBOX")
    assert raised.value.error_type == "address_not_allowed"  # the settings must change
    with pytest.raises(MailConnectionError) as raised:  # does not resolve (now): may work later
        await adapter.check(MailServer("no-such-host.invalid", 3143, "none", "u", "p"), "INBOX")
    assert raised.value.error_type == "connection_failed"


async def test_refused_connection_is_connection_failed():
    with socket.socket() as probe:  # a port nothing listens on
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    adapter = ImapToolsInboundMailbox(_settings(), timeout=2)
    with pytest.raises(MailConnectionError) as raised:
        await adapter.check(MailServer("127.0.0.1", port, "none", "u", "p"), "INBOX")
    assert raised.value.error_type == "connection_failed"


class FakeImapServer:
    """A scripted IMAP server on 127.0.0.1 (a thread per connection). `handlers` maps a command name to
    `fn(conn, tag)`; CAPABILITY, LOGIN and LOGOUT have defaults."""

    def __init__(self, *, greeting: bytes = b"* OK ready\r\n", caps: bytes = b"IMAP4rev1", handlers=None) -> None:
        import threading

        self.greeting, self.caps, self.handlers = greeting, caps, dict(handlers or {})
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.stopped = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        import threading

        while not self.stopped.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn) -> None:
        try:
            if callable(self.greeting):
                self.greeting(conn)
            else:
                conn.sendall(self.greeting)
            reader = conn.makefile("rb")
            while not self.stopped.is_set():
                line = reader.readline()
                if not line:
                    return
                tag, _, rest = line.strip().partition(b" ")
                command = rest.split(b" ", 1)[0].upper().decode("ascii", "replace")
                if command in self.handlers:
                    self.handlers[command](conn, tag)
                elif command == "CAPABILITY":
                    conn.sendall(b"* CAPABILITY " + self.caps + b"\r\n" + tag + b" OK done\r\n")
                elif command == "LOGIN":
                    conn.sendall(tag + b" OK logged in\r\n")
                elif command == "LOGOUT":
                    conn.sendall(b"* BYE\r\n" + tag + b" OK bye\r\n")
                    return
                else:
                    conn.sendall(tag + b" BAD unknown\r\n")
        except (OSError, ValueError):  # the client went away, or sent TLS bytes
            pass
        finally:
            conn.close()

    def server(self, security="none") -> MailServer:
        return MailServer("127.0.0.1", self.port, security, "user", "pass")

    def close(self) -> None:
        self.stopped.set()
        self.sock.close()


@pytest.fixture
def fake_server():
    servers = []

    def make(**kw) -> FakeImapServer:
        servers.append(FakeImapServer(**kw))
        return servers[-1]

    yield make
    for server in servers:
        server.close()


def _examine(conn, tag) -> None:
    conn.sendall(
        b"* 1 EXISTS\r\n* OK [UIDVALIDITY 7] ok\r\n* OK [UIDNEXT 2] ok\r\n" + tag + b" OK [READ-ONLY] done\r\n"
    )


async def test_oversized_literal_aborts_the_call(fake_server):
    """I3: a server announcing a literal over max_bytes + 64 KiB is refused before anything is read."""

    calls = []

    def uid_command(conn, tag):
        calls.append(tag)
        if len(calls) == 1:  # UID SEARCH
            conn.sendall(b"* SEARCH 1\r\n" + tag + b" OK done\r\n")
        elif len(calls) == 2:  # metadata: claims a small message
            conn.sendall(
                b'* 1 FETCH (UID 1 RFC822.SIZE 10 INTERNALDATE "26-Oct-2026 12:00:00 +0000")\r\n' + tag + b" OK done\r\n"
            )
        else:  # the body: a 4 GB literal
            conn.sendall(b"* 1 FETCH (UID 1 BODY[]<0> {4000000000}\r\n" + b"x" * 1024)

    server = fake_server(handlers={"EXAMINE": _examine, "UID": uid_command})
    adapter = ImapToolsInboundMailbox(_settings(), timeout=5)
    with pytest.raises(MailConnectionError) as raised:
        await adapter.fetch(server.server(), "INBOX", after_uid=0, since=None, limit=10, max_bytes=1000)
    assert raised.value.error_type == "oversized_response"
    assert raised.value.__cause__ is None


async def test_oversized_literal_in_the_greeting_is_refused(fake_server):
    server = fake_server(greeting=b"* OK {999999999}\r\n")
    adapter = ImapToolsInboundMailbox(_settings(), timeout=5)
    with pytest.raises(MailConnectionError) as raised:
        await adapter.check(server.server(), "INBOX")
    assert raised.value.error_type == "oversized_response"


async def test_slow_drip_hits_the_overall_deadline(fake_server):
    """I3: a server that keeps each read under the socket timeout still cannot hold a call past the deadline."""
    import time

    def drip(conn, tag):
        for _ in range(200):  # a byte every 0.1 s, never a full line
            conn.sendall(b"*")
            time.sleep(0.1)

    server = fake_server(handlers={"LOGIN": drip})
    adapter = ImapToolsInboundMailbox(_settings(), timeout=5, deadline=1.0)
    started = time.monotonic()
    with pytest.raises(MailConnectionError) as raised:
        await adapter.check(server.server(), "INBOX")
    assert raised.value.error_type == "timeout"
    assert time.monotonic() - started < 4


async def test_default_limits():
    from supportcore.mail import imap_inbound

    assert imap_inbound.CALL_DEADLINE_SECONDS == 120
    assert imap_inbound.LITERAL_SLACK_BYTES == 64 * 1024


async def test_starttls_not_offered_is_tls_error(fake_server):
    """M1: real imaplib behaviour: STARTTLS missing from CAPABILITY raises IMAP4.abort inside starttls()."""
    server = fake_server(caps=b"IMAP4rev1")
    with pytest.raises(MailConnectionError) as raised:
        await ImapToolsInboundMailbox(_settings(), timeout=5).check(server.server("starttls"), "INBOX")
    assert raised.value.error_type == "tls_error"


async def test_starttls_refused_is_tls_error(fake_server):
    server = fake_server(caps=b"IMAP4rev1 STARTTLS", handlers={"STARTTLS": lambda conn, tag: conn.sendall(tag + b" NO not now\r\n")})
    with pytest.raises(MailConnectionError) as raised:
        await ImapToolsInboundMailbox(_settings(), timeout=5).check(server.server("starttls"), "INBOX")
    assert raised.value.error_type == "tls_error"


def _accept_starttls_then_speak_plain(conn, tag) -> None:
    conn.sendall(tag + b" OK begin TLS\r\n")
    conn.recv(4096)  # the ClientHello
    conn.sendall(b"this is not a TLS record\r\n")


async def test_tls_handshake_failures_are_tls_errors_with_verifying_contexts(fake_server, monkeypatch):
    """Implicit TLS against a plain server, and STARTTLS accepted but no TLS spoken: both tls_error. Both
    contexts verify certificates and host names."""
    import ssl

    from supportcore.mail import imap_inbound

    contexts = []
    real = ssl.create_default_context

    def recording(*args, **kwargs):
        contexts.append(real(*args, **kwargs))
        return contexts[-1]

    monkeypatch.setattr(imap_inbound.ssl, "create_default_context", recording)
    adapter = ImapToolsInboundMailbox(_settings(), timeout=3)
    plain = fake_server()
    with pytest.raises(MailConnectionError) as raised:
        await adapter.check(plain.server("ssl"), "INBOX")
    assert raised.value.error_type == "tls_error"
    fake_tls = fake_server(
        caps=b"IMAP4rev1 STARTTLS",
        handlers={"STARTTLS": _accept_starttls_then_speak_plain},
    )
    with pytest.raises(MailConnectionError) as raised:
        await adapter.check(fake_tls.server("starttls"), "INBOX")
    assert raised.value.error_type == "tls_error"
    assert len(contexts) == 2
    assert all(c.verify_mode == ssl.CERT_REQUIRED and c.check_hostname for c in contexts)


@pytest.mark.integration
@greenmail
async def test_greenmail_batch_byte_cap_and_more(adapter, account):
    """I2: the byte cap keeps at least one message and says `more`; a complete scan says not."""
    for n in range(3):
        _send(account.username, n, body="b" * 2000)
    capped = await adapter.fetch(
        account, "INBOX", after_uid=0, since=None, limit=50, max_bytes=1_000_000, max_batch_bytes=1
    )
    assert len(capped.messages) == 1 and capped.more
    limited = await adapter.fetch(account, "INBOX", after_uid=0, since=None, limit=2, max_bytes=1_000_000)
    assert len(limited.messages) == 2 and limited.more
    complete = await adapter.fetch(
        account, "INBOX", after_uid=None, since=datetime.now(UTC) - timedelta(hours=1), limit=50, max_bytes=1_000_000,
        max_batch_bytes=64 * 1024 * 1024,
    )
    assert len(complete.messages) == 3 and not complete.more


# --- fix round 2: the deadline from the first socket on, the per-call byte budget, unreadable metadata ------


def _stall(conn) -> None:
    """Accept the TCP connection, then say nothing (a TLS ClientHello goes unanswered)."""
    import time

    time.sleep(10)


def _drip_greeting(conn) -> None:
    import time

    for _ in range(100):  # a byte every 0.1 s: each read is well inside the socket timeout
        conn.sendall(b"*")
        time.sleep(0.1)


@pytest.mark.parametrize(("security", "greeting"), [("ssl", _stall), ("none", _stall), ("none", _drip_greeting)])
async def test_deadline_covers_handshake_and_greeting(fake_server, security, greeting):
    """I3a: the deadline holds while the TLS handshake stalls, or the greeting drips, with a socket timeout far
    longer than the deadline."""
    import time

    server = fake_server(greeting=greeting)
    adapter = ImapToolsInboundMailbox(_settings(), timeout=30, deadline=1.0)
    started = time.monotonic()
    with pytest.raises(MailConnectionError) as raised:
        await adapter.check(server.server(security), "INBOX")
    assert raised.value.error_type == "timeout"
    assert time.monotonic() - started < 4


async def test_deadline_covers_starttls_handshake(fake_server):
    import time

    def accept_then_stall(conn, tag):
        conn.sendall(tag + b" OK begin TLS\r\n")
        time.sleep(10)

    server = fake_server(caps=b"IMAP4rev1 STARTTLS", handlers={"STARTTLS": accept_then_stall})
    adapter = ImapToolsInboundMailbox(_settings(), timeout=30, deadline=1.0)
    started = time.monotonic()
    with pytest.raises(MailConnectionError) as raised:
        await adapter.check(server.server("starttls"), "INBOX")
    assert raised.value.error_type == "timeout"
    assert time.monotonic() - started < 4


async def test_many_untagged_lines_exhaust_the_call_budget(fake_server):
    """I3b: unsolicited lines count: 2 MB of untagged OK lines before the LOGIN reply exceed check's 1 MiB."""

    def chatty_login(conn, tag):
        filler = b"* OK " + b"x" * 2000 + b"\r\n"
        try:
            for _ in range(1000):
                conn.sendall(filler)
            conn.sendall(tag + b" OK logged in\r\n")
        except OSError:
            pass

    server = fake_server(handlers={"LOGIN": chatty_login})
    with pytest.raises(MailConnectionError) as raised:
        await ImapToolsInboundMailbox(_settings(), timeout=5).check(server.server(), "INBOX")
    assert raised.value.error_type == "oversized_response"


async def test_many_untagged_literals_exhaust_the_call_budget(fake_server):
    """I3b: each literal is under the per-literal limit (64 KiB), but together they exceed the budget."""

    def literals_on_login(conn, tag):
        chunk = b"* 1 FETCH (BODY[] {50000}\r\n" + b"y" * 50000 + b")\r\n"
        try:
            for _ in range(40):  # ~2 MB
                conn.sendall(chunk)
            conn.sendall(tag + b" OK logged in\r\n")
        except OSError:
            pass

    server = fake_server(handlers={"LOGIN": literals_on_login})
    with pytest.raises(MailConnectionError) as raised:
        await ImapToolsInboundMailbox(_settings(), timeout=5).check(server.server(), "INBOX")
    assert raised.value.error_type == "oversized_response"


async def test_call_budget_slack_is_one_mib():
    from supportcore.mail import imap_inbound

    assert imap_inbound.CALL_BUDGET_SLACK_BYTES == 1024 * 1024


async def test_unreadable_metadata_is_reported_not_dropped(fake_server):
    """N2: SEARCH lists uids 1 and 2; the metadata FETCH answers only for 2. uid 1 comes back `unreadable`."""
    calls = []

    def uid_command(conn, tag):
        calls.append(tag)
        if len(calls) == 1:
            conn.sendall(b"* SEARCH 1 2\r\n" + tag + b" OK done\r\n")
        elif len(calls) == 2:
            conn.sendall(
                b'* 2 FETCH (UID 2 RFC822.SIZE 5 INTERNALDATE "26-Oct-2026 12:00:00 +0000")\r\n' + tag + b" OK done\r\n"
            )
        else:
            conn.sendall(b"* 2 FETCH (UID 2 BODY[]<0> {5}\r\nhello)\r\n" + tag + b" OK done\r\n")

    server = fake_server(handlers={"EXAMINE": _examine, "UID": uid_command})
    result = await ImapToolsInboundMailbox(_settings(), timeout=5).fetch(
        server.server(), "INBOX", after_uid=0, since=None, limit=10, max_bytes=1000
    )
    assert [(m.uid, m.unreadable, m.raw) for m in result.messages] == [(1, True, None), (2, False, b"hello")]
