import io
import zipfile

import pytest

from supportcore.errors import DomainError
from supportcore.knowledge.parsing.ports import ParseError, UnsupportedFileTypeError, detect_file_type
from supportcore.wiring import parser_registry

EMPTY_MSG = "No extractable text found (is this a scanned PDF?)"


def _parse(file_type: str, data: bytes, filename: str):
    return parser_registry.create(file_type).parse(data, filename)


def test_detect_types_by_extension_and_sniff(two_page_pdf, sample_docx):
    assert detect_file_type("a.PDF", two_page_pdf) == "pdf"
    assert detect_file_type("a.docx", sample_docx) == "docx"
    assert detect_file_type("a.txt", b"hello") == "text"
    assert detect_file_type("a.md", b"# hi") == "markdown"
    assert detect_file_type("a.markdown", b"# hi") == "markdown"
    assert detect_file_type("a.html", b"<p>x</p>") == "html"
    assert detect_file_type("a.htm", b"<p>x</p>") == "html"
    with pytest.raises(UnsupportedFileTypeError):
        detect_file_type("a.exe", b"MZ")
    with pytest.raises(UnsupportedFileTypeError):
        detect_file_type("noext", b"hello")
    assert issubclass(UnsupportedFileTypeError, DomainError) and issubclass(ParseError, DomainError)


def test_disguised_pdf_rejected():
    with pytest.raises(UnsupportedFileTypeError):
        detect_file_type("a.pdf", b"just some text")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "x")
    with pytest.raises(UnsupportedFileTypeError):
        detect_file_type("a.docx", buf.getvalue())
    with pytest.raises(UnsupportedFileTypeError):
        detect_file_type("a.docx", b"not a zip")


def test_pdf_pages_spans(two_page_pdf):
    doc = _parse("pdf", two_page_pdf, "a.pdf")
    assert [p.page for p in doc.pages] == [1, 2]
    for span, expected in zip(doc.pages, ["Hello first page", "Second page here"]):
        assert doc.text[span.char_start : span.char_end] == expected
    assert doc.pages[1].char_start == doc.pages[0].char_end + 2
    assert doc.text == "Hello first page\n\nSecond page here"


def test_pdf_errors(blank_pdf):
    with pytest.raises(ParseError, match="could not be read"):
        _parse("pdf", b"%PDF-1.4 garbage", "a.pdf")
    with pytest.raises(ParseError) as e:
        _parse("pdf", blank_pdf, "a.pdf")
    assert str(e.value) == EMPTY_MSG


def test_docx_headings_become_markdown(sample_docx):
    doc = _parse("docx", sample_docx, "a.docx")
    lines = doc.text.split("\n")
    assert lines[:4] == ["# Refund Policy", "We refund within 30 days.", "## Shipping", "Ships in 3 days."]
    assert "Region" in lines and "EU" in lines
    assert lines.index("Ships in 3 days.") < lines.index("Region")
    assert doc.pages == []


def test_docx_zip_bomb_guard(sample_docx, monkeypatch):
    from supportcore.knowledge.parsing import docx as docx_mod

    monkeypatch.setattr(docx_mod, "MAX_MEMBERS", 3)
    with pytest.raises(ParseError, match="too large"):
        _parse("docx", sample_docx, "a.docx")
    monkeypatch.setattr(docx_mod, "MAX_MEMBERS", 2000)
    monkeypatch.setattr(docx_mod, "MAX_UNCOMPRESSED_BYTES", 1000)
    with pytest.raises(ParseError, match="too large"):
        _parse("docx", sample_docx, "a.docx")


def test_docx_merged_cells_not_repeated():
    import io as _io

    from docx import Document

    d = Document()
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].merge(t.rows[0].cells[1]).text = "Merged"
    buf = _io.BytesIO()
    d.save(buf)
    assert _parse("docx", buf.getvalue(), "a.docx").text == "Merged"


def _encrypted_pdf(build_pdf, user_password: str) -> bytes:
    from pypdf import PdfReader, PdfWriter

    w = PdfWriter(clone_from=PdfReader(io.BytesIO(build_pdf(["Secret words"]))))
    w.encrypt(user_password, owner_password="own")
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def test_pdf_empty_user_password_parses_and_real_password_rejected(pdf_builder):
    assert _parse("pdf", _encrypted_pdf(pdf_builder, ""), "a.pdf").text == "Secret words"
    with pytest.raises(ParseError, match="password-protected"):
        _parse("pdf", _encrypted_pdf(pdf_builder, "hunter2"), "a.pdf")


def test_nul_bytes_stripped_from_pdf_text(pdf_builder):
    doc = _parse("pdf", pdf_builder(["ab\\000cd", "next"]), "a.pdf")
    assert "\x00" not in doc.text
    for span in doc.pages:
        assert doc.text[span.char_start : span.char_end] in ("abcd", "next")


def test_html_strips_scripts_and_nav():
    html = (
        b"<html><head><title>My Page</title><style>p{}</style></head><body>"
        b"<header>HEAD</header><nav>MENU</nav><h1>Welcome   home</h1><script>evil()</script>"
        b"<p>Some\n   text</p><footer>FOOT</footer></body></html>"
    )
    doc = _parse("html", html, "a.html")
    assert doc.title == "My Page"
    assert "Welcome home" in doc.text and "Some text" in doc.text
    for bad in ("evil", "MENU", "HEAD", "FOOT", "p{}"):
        assert bad not in doc.text


def test_empty_text_raises_parse_error_message():
    for ft, data in [("text", b"  \n "), ("html", b"<html><script>x</script></html>")]:
        with pytest.raises(ParseError) as e:
            _parse(ft, data, "a")
        assert str(e.value) == EMPTY_MSG


def test_text_utf8_sig_ok_and_binary_rejected():
    assert detect_file_type("a.txt", "﻿hello".encode("utf-8")) == "text"
    assert _parse("text", "﻿hello é".encode("utf-8"), "a.txt").text == "hello é"
    assert _parse("markdown", b"# T\n\nbody", "a.md").text == "# T\n\nbody"
    with pytest.raises(UnsupportedFileTypeError):
        detect_file_type("a.txt", b"abc\x00def")
    with pytest.raises(UnsupportedFileTypeError):
        detect_file_type("a.md", b"\xff\xfe\x80\x81")
    with pytest.raises(UnsupportedFileTypeError):
        detect_file_type("a.html", b"\x89PNG\r\n\x00\x00")
