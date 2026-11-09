import time
from pathlib import Path

import pytest

from supportcore.mail.parse import MAX_TEXT_CHARS, UnparseableEmailError, parse_email
from supportcore.mail.strings import strings_for

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> bytes:
    return (FIX / name).read_bytes()


def test_plain_and_html_alternative():
    p = parse_email(load("plain_html.eml"))
    assert p.message_id == "m1@example.test"
    assert p.text == "Hello plain"
    assert p.html_raw is not None and "<b>html</b>" in p.html_raw
    assert p.from_address == "ann@example.test" and p.from_name == "Ann Customer"
    assert p.to == ("support@example.test",)
    assert p.cc == ("bob@example.test", "carol@example.test")
    assert p.date is not None and p.date.utcoffset().total_seconds() == 0 and p.date.hour == 8
    assert p.attachments == []


def test_html_only_gets_text_fallback():
    p = parse_email(load("html_only.eml"))
    assert p.text == "First\n\nSecond\nline"
    assert p.html_raw is not None


def test_bad_charset_replaced_not_raised():
    p = parse_email(load("bad_charset.eml"))
    assert "body" in p.text
    assert p.date is None


def test_missing_message_id_synthesised_deterministically():
    raw = load("no_message_id.eml")
    a, b = parse_email(raw), parse_email(raw)
    assert a.message_id == b.message_id
    assert a.message_id.endswith("@missing-message-id.invalid") and len(a.message_id.split("@")[0]) == 32
    assert parse_email(raw + b"x").message_id != a.message_id


def test_references_parsed_in_order():
    p = parse_email(load("references.eml"))
    assert p.references == ("r1@example.test", "r2@example.test", "r3@example.test")
    assert p.in_reply_to == "r3@example.test"


def test_auto_reply_headers_extracted():
    p = parse_email(load("autoreply.eml"))
    assert p.auto_submitted == "auto-replied"
    assert p.precedence == "bulk"
    assert p.has_autoreply_header and p.return_path_empty
    assert p.x_support_mailbox == "11111111-1111-1111-1111-111111111111"
    plain = parse_email(load("plain_html.eml"))
    assert not plain.has_autoreply_header and not plain.return_path_empty and plain.auto_submitted is None


def test_delivery_report_original_id():
    p = parse_email(load("dsn.eml"))
    assert p.is_delivery_report
    assert p.bounced_message_id == "orig-out@example.test"
    assert p.attachments == []


def test_text_truncated_with_marker():
    raw = b"From: a@example.test\nSubject: big\nMessage-ID: <b@example.test>\n\n" + b"x" * (MAX_TEXT_CHARS + 500)
    p = parse_email(raw)
    assert p.text.endswith(strings_for(None)["email_truncated"])
    assert p.text.startswith("x" * 100) and len(p.text) < MAX_TEXT_CHARS + 100


def test_no_headers_raises():
    with pytest.raises(UnparseableEmailError):
        parse_email(b"")
    with pytest.raises(UnparseableEmailError):
        parse_email(b"\x00\x01 just garbage without a colon\n\nbody")


def test_garbage_headers_and_dates_do_not_raise():
    raw = b"From: <<<>>> ,,, @\nTo: ;;;\nDate: 99 Foo 99999 99:99:99\nMessage-ID: \nSubject: =?bogus?q?x?=\n\nhi"
    p = parse_email(raw)
    assert p.date is None and p.text == "hi"


def test_deep_and_wide_mime_is_bounded():
    wide = ["From: a@example.test", "Message-ID: <w@example.test>", 'Content-Type: multipart/mixed; boundary="B"', ""]
    for i in range(1000):
        wide += ["--B", "Content-Type: application/pdf", f'Content-Disposition: attachment; filename="f{i}.pdf"', "", "x"]
    wide.append("--B--")
    p = parse_email("\n".join(wide).encode())
    assert len(p.attachments) <= 200

    depth = 300
    lines = ["From: a@example.test", "Message-ID: <d@example.test>"]
    for i in range(depth):
        lines += [f'Content-Type: multipart/mixed; boundary="b{i}"', "", f"--b{i}"]
    lines += ["Content-Type: text/plain", "", "deep"]
    for i in reversed(range(depth)):
        lines.append(f"--b{i}--")
    assert parse_email("\n".join(lines).encode()).message_id == "d@example.test"  # text may be dropped by the part cap


def _raw(headers: dict[str, str], body: str = "hi") -> bytes:
    lines = [f"{k}: {v}" for k, v in headers.items()]
    return ("\n".join(lines) + "\n\n" + body).encode()


def test_limits_match_model_columns():
    from supportcore.db.models.mail import EmailAttachment, EmailMessage
    from supportcore.mail import limits

    cols = EmailMessage.__table__.c
    assert cols.rfc_message_id.type.length == cols.in_reply_to.type.length == cols.subject.type.length == limits.MAX_MESSAGE_ID
    assert limits.MAX_SUBJECT == cols.subject.type.length
    assert cols.from_name.type.length == limits.MAX_FROM_NAME
    assert cols.from_address.type.length == cols.reply_to.type.length == limits.MAX_ADDRESS
    a = EmailAttachment.__table__.c
    assert a.filename.type.length == limits.MAX_FILENAME and a.content_type.type.length == limits.MAX_CONTENT_TYPE
    assert a.content_id.type.length == limits.MAX_MESSAGE_ID


def test_nul_stripped_from_every_string_field():
    p = parse_email(
        b"From: =?utf-8?b?QQBC?= <a@example.test>\nSubject: =?utf-8?b?aGkAdGhlcmU=?=\nMessage-ID: <m@example.test>\n"
        b"Content-Type: multipart/mixed; boundary=B\n\n--B\nContent-Type: text/plain\n\nbo\x00dy\n--B\n"
        b'Content-Type: application/pdf; name="f\x00.pdf"\nContent-Disposition: attachment; filename="f\x00.pdf"\n\nx\n--B--\n'
    )
    for value in (p.subject, p.from_name, p.text, p.attachments[0].filename):
        assert "\x00" not in value
    assert p.subject == "hithere" and p.text == "body"


def test_fields_capped_to_column_lengths():
    refs = " ".join(f"<r{i}@example.test>" for i in range(30))
    p = parse_email(_raw({
        "From": "N" * 400 + " <a@example.test>", "Subject": "s" * 5000, "Message-ID": "<" + "i" * 2000 + "@example.test>",
        "In-Reply-To": "<" + "j" * 2000 + "@example.test>", "References": refs,
        "Reply-To": "<" + "r" * 400 + "@example.test>", "To": "ok@example.test, " + "t" * 400 + "@example.test",
    }))
    assert len(p.subject) <= 998 and len(p.from_name) <= 255
    assert p.message_id.endswith("@missing-message-id.invalid")
    assert p.in_reply_to is None and p.reply_to is None
    assert p.to == ("ok@example.test",)
    assert len(p.references) == 20 and p.references[0] == "r0@example.test" and p.references[-1] == "r29@example.test"


def test_message_id_with_inner_whitespace_has_whitespace_removed():
    assert parse_email(_raw({"From": "a@example.test", "Message-ID": "<a b@x.test>"})).message_id == "ab@x.test"


# Time-budget regressions: each shape took more than 10 s before the bounds existed.
BUDGET = 5.0


def _timed(fn):
    import time

    t = time.perf_counter()
    result = fn()
    assert time.perf_counter() - t < BUDGET
    return result


def test_dos_encoded_word_subject():
    # a header over 16 KB is cut, then cut again to 64 encoded words
    p = _timed(lambda: parse_email(_raw({"From": "a@example.test", "Subject": "=?utf-8?q?aN?=" * 40_000})))
    assert p.subject.startswith("aN") and p.from_address == "a@example.test"
    p = _timed(lambda: parse_email(_raw({"From": "a@example.test", "Subject": "=?utf-8?q?aN?= " * 10_000})))
    assert p.subject.startswith("aN") and len(p.subject) <= 64 * 2


def test_dos_huge_address_list():
    for n in (800, 50_000):
        p = _timed(lambda n=n: parse_email(_raw({"From": "a@example.test", "To": ",".join(f"u{i}@example.test" for i in range(n))})))
        assert 0 < len(p.to) <= 100 and p.from_address == "a@example.test"


def test_dos_encoded_words_in_part_headers():
    body = "--B\nContent-Type: application/pdf\nContent-Disposition: attachment; filename=\"" + "=?utf-8?q?aN?=" * 40_000 + '"\n\nx\n--B--\n'
    raw = _raw({"From": "a@example.test", "Content-Type": "multipart/mixed; boundary=B"}, body)
    _timed(lambda: parse_email(raw))


def test_dos_giant_header_block_and_30mb_raw():
    raw = b"From: a@example.test\n" + b"X-A: b\n" * 1_000_000 + b"\nbody"
    _timed(lambda: parse_email(raw))
    big = b"From: a@example.test\nMessage-ID: <b@example.test>\n\n" + b"x" * 30_000_000
    p = _timed(lambda: parse_email(big))
    assert p.text.endswith(strings_for(None)["email_truncated"])


def test_dos_html_only_flat_blocks_text_fallback():
    html = "<p>x</p>" * 300_000
    p = _timed(lambda: parse_email(_raw({"From": "a@example.test", "Content-Type": "text/html"}, html)))
    assert p.text.startswith("x")


def _multipart(*parts: bytes) -> bytes:
    head = b"From: a@example.test\nMessage-ID: <x@example.test>\nContent-Type: multipart/mixed; boundary=B\n\n"
    return head + b"".join(b"--B\n" + p for p in parts) + b"--B--\n"


PDF = b'Content-Type: application/pdf\nContent-Disposition: attachment; filename="a.pdf"\n\nPDFDATA\n'


def test_dos_million_parts_and_part_header_lines():
    parts = _multipart(*[b"Content-Type: text/plain\n\nx\n"] * 400_000)
    p = _timed(lambda: parse_email(parts))
    assert p.message_id == "x@example.test" and p.text.startswith("x") and p.truncated
    headers = _multipart(b"X-A: b\n" * 1_500_000 + b"\nbody\n")
    assert _timed(lambda: parse_email(headers)).truncated


def test_dos_part_header_lines_without_space_after_colon():
    raw = _multipart(b"X:a\n" * 6_000_000 + b"\nbody\n")
    assert _timed(lambda: parse_email(raw)).truncated


def test_body_lines_are_not_counted_towards_the_structure_cut():
    log = b"".join(b"INFO: request %d handled\n" % i for i in range(6000))
    p = parse_email(_multipart(b"Content-Type: text/plain\n\n" + log, PDF))
    assert not p.truncated and [a.filename for a in p.attachments] == ["a.pdf"]
    csv = b"".join(b"note: row %d,1,2\n" % i for i in range(6000))
    p = parse_email(_multipart(
        b'Content-Type: text/csv\nContent-Disposition: attachment; filename="r.csv"\nContent-Transfer-Encoding: 7bit\n\n' + csv
    ))
    assert not p.truncated and p.attachments[0].data.count(b"row ") == 6000
    dashes = b"".join(b"--- divider %d\n" % i for i in range(6000))  # not boundary-shaped (space in it)
    assert not parse_email(_multipart(b"Content-Type: text/plain\n\n" + dashes, PDF)).truncated


def test_many_junk_top_level_headers_do_not_lose_the_body_or_later_headers():
    raw = b"".join(b"X-Jn: v\n" for _ in range(6000)) + b"From: a@example.test\nAuto-Submitted: auto-replied\nMessage-ID: <z@example.test>\n\nbody"
    p = parse_email(raw)
    assert p.from_address == "a@example.test" and p.auto_submitted == "auto-replied" and p.text == "body" and not p.truncated


def test_dos_big_attachment_message_is_not_cut():
    import base64

    data = b"A" * 6_000_000
    raw = (
        b"From: a@example.test\nMessage-ID: <x@example.test>\nContent-Type: multipart/mixed; boundary=B\n\n"
        b"--B\nContent-Type: text/plain\n\nhi\n--B\nContent-Type: application/pdf\nContent-Transfer-Encoding: base64\n"
        b'Content-Disposition: attachment; filename="a.pdf"\n\n' + base64.encodebytes(data) + b"--B--\n"
    )
    p = _timed(lambda: parse_email(raw))
    assert len(p.attachments[0].data) == len(data)


@pytest.mark.parametrize("n", [4000, 40000])
def test_dos_html_only_formatting_reconstruction(n):
    html = "".join(f"<div><b title={i}></div>" for i in range(n)) + "x"
    p = _timed(lambda: parse_email(_raw({"From": "a@example.test", "Content-Type": "text/html"}, html)))
    assert p.text.endswith("x") or "x" in p.text


def test_dos_embedded_message_headers_and_tiny_lines():
    raw = _multipart(b"Content-Type: message/rfc822\n\n" + b"X:a\n" * 7_000_000 + b"\nbody\n")
    assert _timed(lambda: parse_email(raw)).truncated
    tiny = b"From: a@example.test\nMessage-ID: <x@example.test>\n\n" + b"a\n" * 15_000_000
    assert _timed(lambda: parse_email(tiny)).truncated


def test_dos_spaced_boundary_and_other_declared_boundary_shapes():
    spaced = b"From: a@example.test\nContent-Type: multipart/mixed; boundary=\"a b\"\n\n" + b"--a b\n\n" * 400_000
    assert _timed(lambda: parse_email(spaced)).truncated
    nested = (
        b"From: a@example.test\nContent-Type: multipart/mixed; boundary=OUT\n\n--OUT\n"
        b"Content-Type: multipart/mixed;\n boundary=\"in ner\"\n\n" + b"--in ner\n\n" * 400_000
    )
    assert _timed(lambda: parse_email(nested)).truncated
    lone_cr = b"From: a@example.test\nContent-Type: multipart/mixed; boundary=B\n\n" + b"--B\rContent-Type: text/plain\r\rx\r" * 300_000
    assert _timed(lambda: parse_email(lone_cr)).truncated


def test_dos_whitespace_only_line_inside_header_block_and_top_level_message_body():
    raw = _multipart(b"X:a\n \n" * 1_450_000 + b"Y:b\n\nbody\n")
    assert _timed(lambda: parse_email(raw)).truncated
    top = b"From: a@example.test\nContent-Type: message/rfc822\n\n" + b"X:a\n" * 7_000_000
    assert _timed(lambda: parse_email(top)).truncated


def test_dropped_parts_set_truncated():
    parts = [b"Content-Type: application/pdf\nContent-Disposition: attachment; filename=a.pdf\n\nx\n"] * 250
    p = parse_email(_multipart(*parts, b"Content-Type: text/plain\n\nhello\n"))
    assert p.truncated and p.text == ""
    from supportcore.mail.attachments import select_attachments

    assert select_attachments(p)[1][-1]["reason"] == "structure_truncated"
    assert not parse_email(_multipart(*parts[:50], b"Content-Type: text/plain\n\nhello\n")).truncated


# R3-2: boundaries the pre-scan used to miss; it now reads them with the standard library's parameter parsing.
_EXOTIC_BOUNDARIES = {
    "rfc2231-continuation": (b'multipart/mixed; boundary*0="a"; boundary*1="b"', b"ab"),
    "rfc2231-charset": (b"multipart/mixed; boundary*=us-ascii''zz", b"zz"),
    "quoted-pair": (b'multipart/mixed; boundary="a\\\\b"', b"a\\b"),
    "comment": (b'multipart/mixed; boundary=(c)"cc"', b"cc"),
    "encoded-word": (b"=?us-ascii?q?multipart/mixed=3B_boundary=3Dew?=", b"ew"),
}


@pytest.mark.parametrize("name", sorted(_EXOTIC_BOUNDARIES))
def test_dos_exotic_boundary_declarations_are_counted(name):
    from supportcore.mail.parse import _bound_structure

    ctype, boundary = _EXOTIC_BOUNDARIES[name]
    raw = b"From: a@example.test\nContent-Type: " + ctype + b"\n\n" + (b"--" + boundary + b"\n\n") * 20_000
    assert len(_bound_structure(raw)) < len(raw)  # the fast path cut it; the parser never sees 20 000 parts
    assert _timed(lambda: parse_email(raw)).truncated


def test_rfc2231_boundary_message_parses_normally():
    raw = (
        b"From: a@example.test\nMessage-ID: <r@example.test>\n"
        b'Content-Type: multipart/mixed; boundary*0="ab"; boundary*1="cd"\n\n'
        b"--abcd\nContent-Type: text/plain\n\nhello\n"
        b'--abcd\nContent-Type: application/pdf\nContent-Disposition: attachment; filename="a.pdf"\n\n%PDF\n'
        b"--abcd--\n"
    )
    p = parse_email(raw)
    assert p.text == "hello" and not p.truncated
    assert [(a.filename, a.data) for a in p.attachments] == [("a.pdf", b"%PDF")]


def test_dos_many_mime_parameters_per_part():
    """2000 parameters on Content-Type and Content-Disposition of 200 parts took about 30 s before the 100 cap."""
    head = b"From: a@example.test\nMessage-ID: <x@example.test>\nContent-Type: multipart/mixed; boundary=B\n\n"
    parts = b"".join(
        b"--B\nContent-Type: text/plain; n%d=1; " % i + b'x="y"; ' * 2000 + b"\n"
        b"Content-Disposition: attachment; n%d=1; " % i + b'x="y"; ' * 2000 + b"\n\nx\n"
        for i in range(200)
    )
    raw = head + parts + b"--B--\n"
    t = time.perf_counter()
    p = parse_email(raw)
    assert time.perf_counter() - t < 5.0  # the ruled budget (≤ 5 s); tighter bounds flaked under load
    assert len(p.attachments) == 199  # the walk cap; every part was still read


def test_long_rfc2231_file_name_survives_the_parameter_cap():
    name = "報告" * 120 + ".pdf"
    encoded = "".join(f"%{b:02X}" for b in name.encode()).encode()
    raw = (
        b"From: a@example.test\nMessage-ID: <f@example.test>\nContent-Type: multipart/mixed; boundary=B\n\n"
        b"--B\nContent-Type: application/pdf; name=x.pdf\n"
        b"Content-Disposition: attachment; filename*=utf-8''" + encoded + b"\n\n%PDF\n--B--\n"
    )
    assert parse_email(raw).attachments[0].filename == name
