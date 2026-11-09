import uuid

from supportcore.mail.compose import build_mime, make_message_id, reply_subject
from supportcore.mail.fake_outbound import FakeOutboundMailSender
from supportcore.mail.ports import MailServer, MailSendError, OutboundEmail


def _email(**kw) -> OutboundEmail:
    base = dict(
        from_address="help@example.com",
        from_name="Help Desk",
        to="cust@example.org",
        subject="Re: Hello",
        text="body",
        message_id="<abc@example.com>",
        in_reply_to="<parent@example.org>",
        references=("<r0@x>",),
        mailbox_id=uuid.uuid4(),
    )
    base.update(kw)
    return OutboundEmail(**base)


def test_make_message_id_uses_mailbox_domain():
    mid = make_message_id("help@Example.com")
    assert mid.startswith("<") and mid.endswith("@Example.com>")
    assert make_message_id("help@example.com") != make_message_id("help@example.com")


def test_build_mime_threading_headers():
    refs = tuple(f"<r{i}@x>" for i in range(25))
    email = _email(references=refs)
    msg = build_mime(email)
    assert msg["In-Reply-To"] == "<parent@example.org>"
    got = msg["References"].split()
    assert len(got) == 20 and got[0] == "<r0@x>" and got[-1] == "<r24@x>"
    assert got[1] == "<r6@x>"
    assert msg["Message-ID"] == "<abc@example.com>"
    assert msg["X-Support-Mailbox"] == str(email.mailbox_id)
    assert msg["Auto-Submitted"] is None
    assert msg["Date"] is not None
    assert msg["To"] == "cust@example.org" and msg["Subject"] == "Re: Hello"
    assert "help@example.com" in msg["From"] and "Help Desk" in msg["From"]
    assert msg.get_content().strip() == "body"


def test_build_mime_without_parent():
    msg = build_mime(_email(in_reply_to=None, references=()))
    assert msg["In-Reply-To"] is None and msg["References"] is None


def test_reply_subject_prefix_not_doubled():
    assert reply_subject("Hello") == "Re: Hello"
    assert reply_subject("re: Hello") == "re: Hello"
    assert reply_subject("RE: Hello") == "RE: Hello"
    assert reply_subject(None, "Your support request") == "Your support request"


async def test_fake_outbound_records_and_fails():
    fake = FakeOutboundMailSender()
    server = MailServer(host="h", port=587, security="starttls", username="u", password="p")
    fake.fail_next(MailSendError("transient", "smtp_4xx"))
    try:
        await fake.send(server, _email())
    except MailSendError as e:
        assert (e.kind, e.error_type) == ("transient", "smtp_4xx")
    else:
        raise AssertionError("expected failure")
    await fake.send(server, _email())
    assert len(fake.sent) == 1 and fake.sent_mime()[0]["To"] == "cust@example.org"
