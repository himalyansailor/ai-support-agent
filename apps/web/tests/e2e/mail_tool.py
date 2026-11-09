"""Customer-side mail client for the e2e email spec (stdlib only), talking to GreenMail (`make up-email`).

    uv run python apps/web/tests/e2e/mail_tool.py send --from A --to B --subject S --body TEXT [--in-reply-to ID] [--references ID ...]
        SMTP localhost:3025 (no auth, no TLS); prints the Message-ID it generated, e.g. <...@example.test>.

    uv run python apps/web/tests/e2e/mail_tool.py fetch --user U --password P
        IMAP localhost:3143 (plain login); prints the INBOX as a JSON list of
        {message_id, in_reply_to, references, subject, from}. Message-IDs keep their angle brackets;
        references is a list. GreenMail auto-creates unknown recipients with their address as both
        login and password, so a customer reads their mail with --user ADDR --password ADDR.
"""

from __future__ import annotations

import argparse
import imaplib
import json
import re
import smtplib
import sys
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, make_msgid

HOST = "localhost"
SMTP_PORT = 3025
IMAP_PORT = 3143
TIMEOUT = 30
_MSGID = re.compile(r"<[^<>\s]+>")


def send(args: argparse.Namespace) -> None:
    msg = EmailMessage()
    domain = args.sender.rsplit("@", 1)[-1]
    message_id = make_msgid(domain=domain)
    msg["Message-ID"] = message_id
    msg["Date"] = formatdate(localtime=True)
    msg["From"] = args.sender
    msg["To"] = args.to
    msg["Subject"] = args.subject
    if args.in_reply_to:
        msg["In-Reply-To"] = args.in_reply_to
    if args.references:
        msg["References"] = " ".join(args.references)
    msg.set_content(args.body)
    with smtplib.SMTP(HOST, SMTP_PORT, timeout=TIMEOUT) as smtp:
        smtp.send_message(msg)
    print(message_id)


def _header(msg, name: str) -> str | None:
    value = msg.get(name)
    return None if value is None else " ".join(str(value).split())


def fetch(args: argparse.Namespace) -> None:
    out = []
    with imaplib.IMAP4(HOST, IMAP_PORT, timeout=TIMEOUT) as imap:
        imap.login(args.user, args.password)
        imap.select("INBOX", readonly=True)
        _, data = imap.uid("SEARCH", None, "ALL")
        for uid in data[0].split():
            _, parts = imap.uid("FETCH", uid, "(BODY.PEEK[HEADER])")
            raw = next(p[1] for p in parts if isinstance(p, tuple))
            msg = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
            out.append(
                {
                    "message_id": _header(msg, "Message-ID"),
                    "in_reply_to": _header(msg, "In-Reply-To"),
                    "references": _MSGID.findall(_header(msg, "References") or ""),
                    "subject": _header(msg, "Subject"),
                    "from": _header(msg, "From"),
                }
            )
    json.dump(out, sys.stdout)
    print()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("send", help="send one email over SMTP")
    s.add_argument("--from", dest="sender", required=True)
    s.add_argument("--to", required=True)
    s.add_argument("--subject", required=True)
    s.add_argument("--body", required=True)
    s.add_argument("--in-reply-to")
    s.add_argument("--references", nargs="+")
    s.set_defaults(run=send)
    f = sub.add_parser("fetch", help="list the INBOX headers over IMAP as JSON")
    f.add_argument("--user", required=True)
    f.add_argument("--password", required=True)
    f.set_defaults(run=fetch)
    args = parser.parse_args(argv)
    args.run(args)


if __name__ == "__main__":
    main()
