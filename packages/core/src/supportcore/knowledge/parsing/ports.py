"""Document parsing port: bytes in, text out. Parsers are synchronous; callers use `to_thread`."""

import io
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Protocol

from supportcore.errors import DomainError

EMPTY_TEXT_MESSAGE = "No extractable text found (is this a scanned PDF?)"

_EXTENSION_TYPES = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".txt": "text",
    ".md": "markdown",
    ".markdown": "markdown",
    ".html": "html",
    ".htm": "html",
}


class UnsupportedFileTypeError(DomainError):
    """The extension is not allowed or the content does not match it."""


class ParseError(DomainError):
    """The document is supported but its text could not be extracted."""


@dataclass(frozen=True)
class PageSpan:
    """Character range [char_start, char_end) of one page within ParsedDocument.text."""

    page: int
    char_start: int
    char_end: int


@dataclass
class ParsedDocument:
    text: str
    pages: list[PageSpan] = field(default_factory=list)  # PDF only
    title: str | None = None


class DocumentParser(Protocol):
    def parse(self, data: bytes, filename: str) -> ParsedDocument: ...


def decode_text(data: bytes) -> str:
    """Decode UTF-8 (BOM tolerated). Anything else, including NUL bytes, is unsupported."""
    if b"\x00" in data:
        raise UnsupportedFileTypeError("File is not a text file")
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UnsupportedFileTypeError("File is not valid UTF-8 text")


def require_text(text: str) -> str:
    text = text.replace("\x00", "")  # Postgres text columns reject NUL
    if not text.strip():
        raise ParseError(EMPTY_TEXT_MESSAGE)
    return text


def detect_file_type(filename: str, data: bytes) -> str:
    """Return one of pdf, docx, text, markdown, html; sniff content so extensions cannot lie."""
    suffix = PurePath(filename).suffix.lower()
    file_type = _EXTENSION_TYPES.get(suffix)
    if file_type is None:
        raise UnsupportedFileTypeError(f"Unsupported file type {suffix or '(no extension)'!r}")
    if file_type == "pdf":
        if not data.startswith(b"%PDF-"):
            raise UnsupportedFileTypeError("File is not a valid PDF")
    elif file_type == "docx":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if "word/document.xml" not in z.namelist():
                    raise UnsupportedFileTypeError("File is not a valid DOCX")
        except zipfile.BadZipFile:
            raise UnsupportedFileTypeError("File is not a valid DOCX") from None
    else:
        decode_text(data)
    return file_type
