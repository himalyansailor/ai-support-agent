"""Parses a raw RFC 5322 message into a `ParsedEmail`. Pure; never logs; one bad message must not raise.

`UnparseableEmailError` is raised when the message has no headers at all, and also when the standard-library
parser itself crashes on a pathological structure (for example thousands of nested multiparts); the caller treats
both as the `unparseable` skip. Everything else is best effort: bad charsets are replaced, bad dates become None,
the MIME walk is bounded in parts, and every header is bounded before it is parsed (see `_BoundedPolicy`). Every
string field is free of NUL and lone surrogates and fits its database column (`limits.py`).
"""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import Message
from email.parser import BytesHeaderParser, BytesParser
from email.policy import EmailPolicy, linesep_splitter
from email.utils import getaddresses, parsedate_to_datetime
from functools import lru_cache

from supportcore.mail.compose import trim_references
from supportcore.mail.limits import (
    MAX_ADDRESS,
    MAX_ADDRESSES,
    MAX_CONTENT_TYPE,
    MAX_FROM_NAME,
    MAX_MESSAGE_ID,
    MAX_SUBJECT,
)
from supportcore.mail.sanitize import html_to_text
from supportcore.mail.strings import strings_for

MAX_PARTS = 200
MAX_TEXT_CHARS = 100_000
MAX_HTML_CHARS = 5_000_000
MAX_HEADER_VALUE = 16 * 1024
MAX_HEADER_NAME = 256
MAX_HEADER_BLOCK = 256 * 1024
MAX_ENCODED_WORDS = 64
MAX_PARAMETERS = 100
MAX_PARAMETER_HEADER = 4096
_PARAMETER_HEADERS = frozenset({"content-type", "content-disposition"})
_PARAMETER_SEPARATOR = re.compile(r"[;(]|\s+")
_ID = re.compile(r"<([^<>\s]+)>")
_BAD_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean(value: str, limit: int | None = None) -> str:
    """No NUL or other control characters (keeps tab and newlines), no lone surrogates, at most `limit` chars."""
    if limit is not None:
        value = value[:limit]
    value = value.encode("utf-8", "replace").decode("utf-8")
    return _BAD_CHARS.sub("", value)


def _bound_header_value(value: str) -> str:
    value = value[:MAX_HEADER_VALUE]
    idx = -1
    for _ in range(MAX_ENCODED_WORDS + 1):
        idx = value.find("=?", idx + 1)
        if idx < 0:
            return value
    return value[:idx]


def _bound_parameters(value: str) -> str:
    """At most 100 parameters (and 100 words or comments) and 4 KB, for the two headers with MIME parameters.

    The standard library's MIME-parameter parser spends about 10 microseconds per parameter, word or comment, and
    the walk reads Content-Type and Content-Disposition several times per part (up to 200 parts): 2000 parameters
    per header took about 30 s. Real headers have a handful of parameters; a long RFC 2231 file name is a few
    long tokens and fits in 4 KB. The value is cut at the 100th separator (`;`, whitespace or `(`).
    """
    value = value[:MAX_PARAMETER_HEADER]
    for count, match in enumerate(_PARAMETER_SEPARATOR.finditer(value)):
        if count >= MAX_PARAMETERS:
            return value[: match.start()]
    return value


@lru_cache(maxsize=64)
def _parameter_header(name: str, value: str) -> object:
    """Parsed Content-Type/Content-Disposition, reused: the walk asks for the same header several times per part."""
    return _POLICY.header_factory(name, value)


class _BoundedPolicy(EmailPolicy):
    """`policy.default` with every stored header bounded, so header parsing cannot be made super-linear.

    Header parsing (encoded words, address lists) is quadratic or worse in the value length. The bound applies to
    the headers of every part, because the standard-library parser reads Content-Type while it parses.
    """

    def header_source_parse(self, sourcelines):  # noqa: ANN001, ANN201
        name, value = super().header_source_parse(sourcelines)
        value = _bound_header_value(value)
        if name.strip().lower() in _PARAMETER_HEADERS:
            value = _bound_parameters(value)
        return name[:MAX_HEADER_NAME], value

    def header_fetch_parse(self, name, value):  # noqa: ANN001, ANN201
        if isinstance(value, str) and not hasattr(value, "name") and name.strip().lower() in _PARAMETER_HEADERS:
            return _parameter_header(name, "".join(linesep_splitter.split(value)))
        return super().header_fetch_parse(name, value)


_POLICY = _BoundedPolicy()


_CONTENT_TYPE = re.compile(rb"content-type", re.IGNORECASE)
_BOUNDARY_HINT = re.compile(rb"boundary|=\?", re.IGNORECASE)  # a parameter name, or an encoded word that may hide one
_EMPTY_LINE = re.compile(rb"\r?\n\r?\n")  # the only thing that ends a header block (a line of spaces is a continuation)
MAX_BOUNDARY_LINES = 5000
MAX_BOUNDARY_VALUES = 100
MAX_PART_HEADER_LINES = 20_000
MAX_EMBEDDED_LEVELS = 5
MAX_LINES = 3_000_000
MAX_LONE_CR = 5000
_MESSAGE_TYPE = re.compile(rb"^content-type:[ \t]*message/", re.IGNORECASE | re.MULTILINE)


def _header_end(raw: bytes) -> int:
    ends = [i for i in (raw.find(b"\r\n\r\n"), raw.find(b"\n\n")) if i >= 0]
    return min(ends) if ends else len(raw)


def _add_boundaries(block: bytes, found: set[bytes]) -> None:
    """Adds the boundary that the block's Content-Type declares, read exactly as the parser will read it.

    The value comes from the standard library's own header-parameter parsing (same policy, block bounded to
    256 KB), so RFC 2231 continuations (`boundary*0=`) and charsets (`boundary*=`), quoted pairs, comments and
    encoded words all give the value the parser splits on. A Content-Type the standard library cannot parse adds
    nothing (the parser fails on it too); anything left over is caught by the hard bound in `isolate.py`.
    """
    block = block[:MAX_HEADER_BLOCK]
    if len(found) > MAX_BOUNDARY_VALUES or not _CONTENT_TYPE.search(block) or not _BOUNDARY_HINT.search(block):
        return
    if block.startswith(b"\r\n"):
        block = block[2:]
    elif block.startswith(b"\n"):
        block = block[1:]
    try:
        value = BytesHeaderParser(policy=_POLICY).parsebytes(block + b"\n\n").get_boundary()
    except Exception:  # noqa: BLE001 - e.g. RecursionError on nested comments; the parser itself fails the same way
        return
    if not value:
        return
    try:
        encoded = value.encode("ascii", "surrogateescape")  # how the parser sees the raw bytes of a line
    except UnicodeEncodeError:
        encoded = value.encode("utf-8")
    found.add(encoded)


def _boundary_pattern(found: set[bytes]) -> re.Pattern[bytes]:
    alternatives = b"|".join(re.escape(v) for v in sorted(found, key=len, reverse=True))
    return re.compile(rb"^--(?:" + alternatives + rb")(?:--)?[ \t]*\r?$", re.MULTILINE)


def _bound_structure(raw: bytes) -> bytes:
    """Cuts the message when it has too many MIME boundary lines or part-header lines; body text is never counted.

    The standard-library parser is quadratic in the number of parts (a million-part message took minutes) and
    spends microseconds per header line of every part. Only two things are counted, after the top-level header
    block (already capped): lines that are exactly `--` plus a boundary declared in some Content-Type header seen
    so far (read by the standard library's parameter parsing, see `_add_boundaries`), and the lines of each header run (from a boundary line
    to the next empty line, as the standard library ends a header block, plus the embedded headers of a
    `message/*` part, or of a top-level `message/*` body). Real mail has tens of each; the caps are 5 000 and
    20 000. A cut message loses its tail, and the caller records that (`ParsedEmail.truncated`). This is the
    fast path; `isolate.py` is the hard bound.
    """
    start = _header_end(raw)
    found: set[bytes] = set()
    _add_boundaries(raw[:start], found)
    header_lines = 0
    pos = start

    def run(block_start: int) -> tuple[int | None, int]:
        """Counts one header run and the embedded ones after it; returns (cut position or None, end position)."""
        nonlocal header_lines
        end = block_start
        for _ in range(MAX_EMBEDDED_LEVELS + 1):
            blank = _EMPTY_LINE.search(raw, block_start)
            block_end = blank.start() if blank else len(raw)
            lines = raw.count(b"\n", block_start, block_end)
            if header_lines + lines > MAX_PART_HEADER_LINES:
                cut = block_start
                for _ in range(MAX_PART_HEADER_LINES - header_lines):
                    cut = raw.find(b"\n", cut) + 1
                return cut, block_end
            header_lines += lines
            end = block_end
            _add_boundaries(raw[block_start:block_end], found)
            if blank is None or not _MESSAGE_TYPE.search(raw, block_start, block_end):
                return None, end
            block_start = blank.end()
        return end, end  # messages embedded deeper than the limit

    if _MESSAGE_TYPE.search(raw, 0, start) and start < len(raw):
        separator = _EMPTY_LINE.match(raw, start)
        cut, pos = run(separator.end() if separator else len(raw))
        if cut is not None:
            return raw[:cut]
    boundaries = 0
    while found:
        match = _boundary_pattern(found).search(raw, pos)
        if match is None:
            break
        boundaries += 1
        if boundaries > MAX_BOUNDARY_LINES:
            return raw[: match.start()]
        cut, pos = run(match.end())
        if cut is not None:
            return raw[:cut]
        if len(found) > MAX_BOUNDARY_VALUES:
            return raw[: match.start()]
    return raw


def _bound_lines(raw: bytes) -> bytes:
    """Cuts a message of tens of millions of tiny lines (the parser spends about 0.4 microseconds per line).

    A lone CR is a line break to the standard library but not to the scans above, so a message with more than
    5 000 of them is cut at the 5 000th.
    """
    lone = raw.count(b"\r") - raw.count(b"\r\n")
    if lone > MAX_LONE_CR:
        for i, match in enumerate(re.finditer(rb"\r(?!\n)", raw)):
            if i >= MAX_LONE_CR:
                return raw[: match.start()]
    if raw.count(b"\n") <= MAX_LINES:
        return raw
    cut = 0
    for _ in range(MAX_LINES):
        cut = raw.find(b"\n", cut) + 1
    return raw[:cut]


def _bound_header_block(raw: bytes) -> bytes:
    """Bounds the top-level header block before parsing: each header to 16 KB, the block to 256 KB; the body is kept.

    An over-long header keeps its first 16 KB; headers past the 256 KB total are dropped.
    """
    end = _header_end(raw)
    if end <= MAX_HEADER_BLOCK:
        return raw
    out: list[bytes] = []
    total = 0
    current = 0
    for line in raw[:end].split(b"\n"):
        if line[:1] in (b" ", b"\t"):
            current += len(line)
            if current > MAX_HEADER_VALUE:
                continue
        else:
            current = len(line)
            line = line[:MAX_HEADER_VALUE]
        total += len(line) + 1
        if total > MAX_HEADER_BLOCK:
            break
        out.append(line)
    return b"\n".join(out) + b"\n\n" + raw[end:].lstrip(b"\r\n")


class UnparseableEmailError(Exception):
    """The raw bytes have no headers at all, or the standard-library parser crashed on a pathological structure."""


@dataclass(frozen=True)
class RawAttachment:
    filename: str | None
    content_type: str
    data: bytes = field(repr=False)
    content_id: str | None
    inline: bool


@dataclass(frozen=True)
class ParsedEmail:
    message_id: str
    in_reply_to: str | None
    references: tuple[str, ...]
    subject: str
    from_address: str
    from_name: str
    reply_to: str | None
    to: tuple[str, ...]
    cc: tuple[str, ...]
    date: datetime | None
    text: str
    html_raw: str | None
    attachments: list[RawAttachment]
    auto_submitted: str | None
    precedence: str | None
    has_autoreply_header: bool
    return_path_empty: bool
    is_delivery_report: bool
    bounced_message_id: str | None
    x_support_mailbox: str | None
    truncated: bool = False  # the structure cut (too many parts or part-header lines) dropped the tail


def normalise_message_id(value: str | None) -> str | None:
    """Strips `<>` and all whitespace; the case is kept. None when empty or longer than a column."""
    if not value:
        return None
    found = _ID.search(value)
    result = found.group(1) if found else re.sub(r"\s+", "", value).strip("<>")
    result = _clean(result)
    return result if result and len(result) <= MAX_MESSAGE_ID else None


def _raw_header(msg: Message, name: str) -> str | None:
    """The stored (bounded, unparsed) value; used for ids so the header parser cannot rewrite them."""
    try:
        for key, value in msg.raw_items():
            if key.lower() == name:
                return re.sub(r"\s+", " ", str(value)).strip()
    except Exception:  # noqa: BLE001
        pass
    return None


def _header(msg: Message, name: str) -> str | None:
    try:
        value = msg.get(name)
        if value is None:
            return None
        return _clean(re.sub(r"\s+", " ", str(value)).strip())
    except Exception:  # noqa: BLE001 - a malformed header reads as absent
        return None


def _addresses(msg: Message, name: str) -> list[tuple[str, str]]:
    value = _header(msg, name)
    if not value:
        return []
    try:
        return [(n, _clean(a.strip().lower())) for n, a in getaddresses([value]) if a and a.strip()]
    except Exception:  # noqa: BLE001
        return []


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except Exception:  # noqa: BLE001 - TypeError, ValueError, OverflowError on garbage
        return None


def _decode(part: Message) -> bytes:
    try:
        payload = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001
        return b""
    return payload if isinstance(payload, bytes) else b""


def _decode_text(part: Message) -> str:
    data = _decode(part)
    try:
        charset = part.get_content_charset() or "utf-8"
    except Exception:  # noqa: BLE001
        charset = "utf-8"
    try:
        return data.decode(charset, errors="replace")
    except (LookupError, ValueError):
        return data.decode("utf-8", errors="replace")


def _is_delivery_report(msg: Message) -> bool:
    try:
        return msg.get_content_type() == "multipart/report" and (
            (msg.get_param("report-type") or "").lower() == "delivery-status"  # type: ignore[union-attr]
        )
    except Exception:  # noqa: BLE001
        return False


def _bounced_id_from_part(part: Message) -> str | None:
    ctype = part.get_content_type()
    try:
        if ctype in ("message/rfc822", "message/global"):
            payload = part.get_payload()
            inner = payload[0] if isinstance(payload, list) and payload else None
            return normalise_message_id(_header(inner, "Message-ID")) if inner is not None else None
        if ctype == "text/rfc822-headers":
            found = re.search(r"^message-id:\s*(.+)$", _decode_text(part), re.IGNORECASE | re.MULTILINE)
            return normalise_message_id(found.group(1)) if found else None
    except Exception:  # noqa: BLE001
        return None
    return None


def _walk(msg: Message) -> tuple[list[Message], bool]:
    """Leaves in document order and whether parts were dropped; iterative, bounded by MAX_PARTS visited parts."""
    leaves: list[Message] = []
    stack: list[Message] = [msg]
    visited = 0
    while stack and visited < MAX_PARTS:
        part = stack.pop()
        visited += 1
        try:
            if part.is_multipart() and not part.get_content_type().startswith("message/"):
                stack.extend(reversed(part.get_payload()))  # type: ignore[arg-type]
            else:
                leaves.append(part)
        except Exception:  # noqa: BLE001
            continue
    return leaves, bool(stack)


def _address_list(msg: Message, name: str) -> tuple[str, ...]:
    return tuple(a for _, a in _addresses(msg, name) if len(a) <= MAX_ADDRESS)[:MAX_ADDRESSES]


def _cap_text(text: str) -> str:
    if len(text) <= MAX_TEXT_CHARS:
        return text
    return text[:MAX_TEXT_CHARS].rstrip() + "\n\n" + strings_for(None)["email_truncated"]


def parse_email(raw: bytes) -> ParsedEmail:
    headed = _bound_header_block(raw)
    bounded = _bound_lines(_bound_structure(headed))
    try:
        msg = BytesParser(policy=_POLICY).parsebytes(bounded)
    except Exception as exc:  # noqa: BLE001 - includes RecursionError from absurd nesting
        raise UnparseableEmailError("unparseable") from exc
    if not msg.keys():
        raise UnparseableEmailError("no headers")

    message_id = normalise_message_id(_raw_header(msg, "message-id")) or (
        f"{hashlib.sha256(raw).hexdigest()[:32]}@missing-message-id.invalid"
    )
    in_reply_to = normalise_message_id(_raw_header(msg, "in-reply-to"))
    refs_header = _raw_header(msg, "references") or ""
    found_refs = _ID.findall(refs_header) or refs_header.split()
    references = tuple(trim_references([r for r in (normalise_message_id(t) for t in found_refs) if r]))

    from_list = _addresses(msg, "From")
    from_name, from_address = (_clean(from_list[0][0].strip(), MAX_FROM_NAME), from_list[0][1]) if from_list else ("", "")
    if len(from_address) > MAX_ADDRESS:
        from_address = ""
    reply_list = [a for a in _addresses(msg, "Reply-To") if len(a[1]) <= MAX_ADDRESS]

    plain: list[str] = []
    html: list[str] = []
    attachments: list[RawAttachment] = []
    report = _is_delivery_report(msg)
    bounced: str | None = None
    leaves, parts_dropped = _walk(msg)
    for part in leaves:
        ctype = part.get_content_type()
        if report and bounced is None:
            bounced = _bounced_id_from_part(part)
        if report and (ctype.startswith("message/") or ctype == "text/rfc822-headers"):
            continue
        try:
            disposition = (part.get_content_disposition() or "").lower()
            filename = part.get_filename()
        except Exception:  # noqa: BLE001
            disposition, filename = "", None
        if ctype in ("text/plain", "text/html") and disposition != "attachment" and not filename:
            (plain if ctype == "text/plain" else html).append(_decode_text(part))
            continue
        if ctype.startswith("message/"):
            try:
                payload = part.get_payload()
                data = payload[0].as_bytes() if isinstance(payload, list) and payload else b""
            except Exception:  # noqa: BLE001
                data = b""
        else:
            data = _decode(part)
        content_id = normalise_message_id(_raw_header(part, "content-id"))
        attachments.append(
            RawAttachment(
                filename=_clean(filename, 1000) if isinstance(filename, str) else None,
                content_type=_clean(ctype, MAX_CONTENT_TYPE),
                data=data,
                content_id=content_id,
                inline=disposition == "inline" or (content_id is not None and disposition != "attachment"),
            )
        )

    html_raw = "\n".join(html)[:MAX_HTML_CHARS] if html else None
    if plain and "".join(plain).strip():
        text = "\n\n".join(p.strip("\r\n") for p in plain)[: MAX_TEXT_CHARS + 1]
    elif html_raw:
        text = html_to_text(html_raw)[: MAX_TEXT_CHARS + 1]
    else:
        text = ""

    return_path = _header(msg, "Return-Path")
    return ParsedEmail(
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=references,
        subject=_clean(_header(msg, "Subject") or "", MAX_SUBJECT),
        from_address=from_address,
        from_name=from_name,
        reply_to=reply_list[0][1] if reply_list else None,
        to=_address_list(msg, "To"),
        cc=_address_list(msg, "Cc"),
        date=_parse_date(_header(msg, "Date")),
        text=_cap_text(_clean(text)),
        html_raw=_clean(html_raw) if html_raw is not None else None,
        attachments=attachments,
        auto_submitted=(_header(msg, "Auto-Submitted") or "").lower()[:64] or None,
        precedence=(_header(msg, "Precedence") or "").lower()[:64] or None,
        has_autoreply_header=_header(msg, "X-Autoreply") is not None or _header(msg, "X-Autorespond") is not None,
        return_path_empty=return_path is not None and return_path.strip("<> ") == "",
        is_delivery_report=report,
        bounced_message_id=bounced,
        x_support_mailbox=(_header(msg, "X-Support-Mailbox") or "")[:64] or None,
        truncated=len(bounded) < len(headed) or parts_dropped,
    )
