from supportcore.mail.attachments import select_attachments
from supportcore.mail.parse import ParsedEmail, RawAttachment


def parsed(atts):
    return ParsedEmail(
        "m@example.test", None, (), "s", "a@example.test", "", None, (), (), None, "", None, atts,
        None, None, False, False, False, None, None,
    )  # fmt: skip


def att(name, ctype="application/pdf", size=10):
    return RawAttachment(name, ctype, b"x" * size, None, False)


def test_attachment_limits_and_types():
    kept, skipped = select_attachments(parsed([att("big.pdf", size=11 * 1024 * 1024), att("run.exe", "application/x-msdownload"), att("ok.pdf")]))
    assert [a.filename for a in kept] == ["ok.pdf"]
    assert [(s["filename"], s["reason"]) for s in skipped] == [("big.pdf", "too_large"), ("run.exe", "type_not_allowed")]
    assert skipped[0]["size"] == 11 * 1024 * 1024

    kept, skipped = select_attachments(parsed([att(f"f{i}.pdf") for i in range(21)]))
    assert len(kept) == 20 and [s["reason"] for s in skipped] == ["too_many"]


def test_exe_disguised_as_pdf_and_octet_stream():
    kept, skipped = select_attachments(parsed([att("evil.exe", "application/pdf"), att("a.pdf", "application/octet-stream"), att("b.bin", "application/octet-stream")]))
    assert [a.filename for a in kept] == ["a.pdf"]
    assert [s["reason"] for s in skipped] == ["type_not_allowed"] * 2


def test_message_total_exceeded():
    mb = 1024 * 1024
    kept, skipped = select_attachments(parsed([att(f"f{i}.pdf", size=9 * mb) for i in range(3)]))
    assert len(kept) == 2 and skipped[0]["reason"] == "message_total_exceeded"


def test_office_and_text_types_allowed():
    docx = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    kept, _ = select_attachments(parsed([att("a.docx", docx), att("b.xlsx", xlsx), att("c.csv", "text/csv"), att("d.txt", "text/plain"), att("e.png", "image/png")]))
    assert len(kept) == 5


def test_filename_path_stripped():
    kept, skipped = select_attachments(parsed([att("../../etc/pa\x00ss\x1fwd.pdf"), att("C:\\Users\\x\\inv.pdf"), att("a" * 300 + ".pdf")]))
    assert kept[0].filename == "passwd.pdf"
    assert kept[1].filename == "inv.pdf"
    assert len(kept[2].filename) == 255 and kept[2].filename.endswith("a.pdf")
    _, skipped = select_attachments(parsed([att(None, "application/octet-stream")]))
    assert skipped == [{"filename": "attachment", "size": 10, "reason": "type_not_allowed"}]


def test_extension_must_be_in_mapping_and_match_declared_type():
    names = ["a.hta", "a.scr", "a.vbs", "noext", "a.pdf.exe", "a.png"]
    kept, skipped = select_attachments(parsed([att(n, "image/png" if n == "a.png" else "application/pdf") for n in names]))
    assert [a.filename for a in kept] == ["a.png"]
    assert len(skipped) == 5 and {s["reason"] for s in skipped} == {"type_not_allowed"}
    # declared type contradicts the extension
    kept, skipped = select_attachments(parsed([att("a.pdf", "image/png")]))
    assert not kept and skipped[0]["reason"] == "type_not_allowed"


def test_octet_stream_uses_extension_and_stores_mapped_type():
    kept, _ = select_attachments(parsed([att("a.JPG", "application/octet-stream"), att("b.jpeg", "image/jpeg")]))
    assert [a.content_type for a in kept] == ["image/jpeg", "image/jpeg"]


def test_bidi_and_zero_width_characters_removed_and_type_checked_on_full_name():
    kept, skipped = select_attachments(parsed([att("invoice\u202efdp.hta", "application/pdf"), att("a\u200bb\u202e.pdf")]))
    assert [a.filename for a in kept] == ["ab.pdf"]
    assert skipped[0]["filename"] == "invoicefdp.hta"
    # a long name keeps its real extension for the check: .exe must not be hidden by truncation
    kept, skipped = select_attachments(parsed([att("a" * 300 + ".exe", "application/pdf")]))
    assert not kept and skipped[0]["reason"] == "type_not_allowed" and skipped[0]["filename"].endswith(".exe")


def test_filename_cut_by_utf8_bytes_without_splitting_code_points():
    kept, _ = select_attachments(parsed([att("\u4e2d" * 200 + ".pdf")]))
    name = kept[0].filename
    assert len(name.encode()) <= 255 and name.endswith(".pdf") and "\ufffd" not in name


def test_nameless_parts_with_allowed_type_get_generated_names():
    cid = RawAttachment(None, "image/png", b"x", "logo@example.test", True)
    kept, skipped = select_attachments(parsed([att(None, "application/pdf"), cid, att("", "text/plain"), att(None, "application/zip")]))
    assert [a.filename for a in kept] == ["attachment-1.pdf", "attachment-2.png", "attachment-3.txt"]
    assert kept[1].content_id == "logo@example.test" and kept[1].inline
    assert [s["reason"] for s in skipped] == ["type_not_allowed"]


def test_declared_type_aliases_and_log_extension():
    kept, skipped = select_attachments(parsed([
        att("a.jpg", "image/jpg"), att("b.jpg", "image/pjpeg"), att("c.csv", "application/vnd.ms-excel"),
        att("d.csv", "text/comma-separated-values"), att("e.log", "text/plain"), att("f.xlsx", "application/vnd.ms-excel"),
        att("g.pdf", "application/vnd.ms-excel"),
    ]))  # fmt: skip
    assert [(a.filename, a.content_type) for a in kept] == [
        ("a.jpg", "image/jpeg"), ("b.jpg", "image/jpeg"), ("c.csv", "text/csv"), ("d.csv", "text/csv"), ("e.log", "text/plain"),
    ]
    assert [s["filename"] for s in skipped] == ["f.xlsx", "g.pdf"]  # vnd.ms-excel only counts on .csv


def test_structure_truncated_is_reported_never_silent():
    p = parsed([att("a.pdf")])
    object.__setattr__(p, "truncated", True)
    _, skipped = select_attachments(p)
    assert skipped == [{"filename": "", "size": 0, "reason": "structure_truncated"}]
