"""Chooses which inbound attachments are kept (pure) and stores them (`store_attachments`, no session open)."""

import hashlib
import unicodedata
from dataclasses import dataclass
from uuid import UUID

from supportcore.mail.limits import MAX_FILENAME
from supportcore.mail.parse import ParsedEmail, RawAttachment
from supportcore.storage.ports import FileStorage

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_MESSAGE_BYTES = 25 * 1024 * 1024
MAX_FILES = 20

_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# The one source of truth: extension -> canonical content type. An attachment is kept only when its extension is
# here and the declared type is that type (or application/octet-stream). The host's mimetypes is never used.
EXTENSION_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "pdf": "application/pdf",
    "txt": "text/plain",
    "csv": "text/csv",
    "log": "text/plain",
    "docx": _DOCX,
    "xlsx": _XLSX,
}
ALLOWED_TYPES = frozenset(EXTENSION_TYPES.values())
# Declared types that mean an allowed type; application/vnd.ms-excel counts only on a .csv name.
TYPE_ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg", "text/comma-separated-values": "text/csv"}
_CSV_ONLY_ALIASES = {"application/vnd.ms-excel": "text/csv"}
_GENERATED_EXTENSION = {
    "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp", "application/pdf": "pdf",
    "text/plain": "txt", "text/csv": "csv", _DOCX: "docx", _XLSX: "xlsx",
}  # fmt: skip
_REMOVED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def _truncate_bytes(name: str) -> str:
    """At most MAX_FILENAME UTF-8 bytes; the extension survives and no code point is split."""
    if len(name.encode("utf-8")) <= MAX_FILENAME:
        return name
    stem, dot, ext = name.rpartition(".")
    ext_bytes = len(ext.encode("utf-8"))
    if dot and stem and ext_bytes <= 16:
        budget = MAX_FILENAME - ext_bytes - 1
        return stem.encode("utf-8")[:budget].decode("utf-8", "ignore") + "." + ext
    return name.encode("utf-8")[:MAX_FILENAME].decode("utf-8", "ignore")


def full_filename(name: str | None) -> str:
    """Basename without control, format (bidi, zero-width) and line-separator characters; not yet truncated."""
    cleaned = "".join(ch for ch in (name or "") if unicodedata.category(ch) not in _REMOVED_CATEGORIES)
    base = cleaned.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return "attachment" if base in ("", ".", "..") else base


def clean_filename(name: str | None) -> str:
    return _truncate_bytes(full_filename(name))


def _declared(att: RawAttachment, ext: str) -> str:
    declared = att.content_type.lower()
    if declared in _CSV_ONLY_ALIASES and ext == "csv":
        return _CSV_ONLY_ALIASES[declared]
    return TYPE_ALIASES.get(declared, declared)


def _canonical_type(att: RawAttachment, full_name: str) -> str | None:
    ext = full_name.rpartition(".")[2].lower() if "." in full_name else ""
    mapped = EXTENSION_TYPES.get(ext)
    if mapped is None:
        return None
    declared = _declared(att, ext)
    return mapped if declared in (mapped, "application/octet-stream") else None


def select_attachments(parsed: ParsedEmail) -> tuple[list[RawAttachment], list[dict]]:
    """Kept and skipped attachments. A part with no name and an allowed type is kept as `attachment-{n}.{ext}`."""
    kept: list[RawAttachment] = []
    skipped: list[dict] = []
    total = 0
    for index, att in enumerate(parsed.attachments, start=1):
        full = full_filename(att.filename)
        nameless = not (att.filename or "").strip()
        if nameless:
            declared = _declared(att, "")
            canonical = declared if declared in ALLOWED_TYPES else None
            if canonical is not None:
                full = f"attachment-{index}.{_GENERATED_EXTENSION[canonical]}"
        else:
            canonical = _canonical_type(att, full)
        filename = _truncate_bytes(full)
        size = len(att.data)
        reason = None
        if canonical is None:
            reason = "type_not_allowed"
        elif size > MAX_FILE_BYTES:
            reason = "too_large"
        elif len(kept) >= MAX_FILES:
            reason = "too_many"
        elif total + size > MAX_MESSAGE_BYTES:
            reason = "message_total_exceeded"
        if reason:
            skipped.append({"filename": filename, "size": size, "reason": reason})
            continue
        total += size
        kept.append(RawAttachment(filename, canonical, att.data, att.content_id, att.inline))  # type: ignore[arg-type]
    if parsed.truncated:
        skipped.append({"filename": "", "size": 0, "reason": "structure_truncated"})
    return kept, skipped


@dataclass(frozen=True)
class StoredAttachment:
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    storage_key: str
    content_id: str | None


async def store_attachments(
    storage: FileStorage, organisation_id: UUID, rfc_message_id: str, kept: list[RawAttachment]
) -> list[StoredAttachment]:
    """Puts the kept attachments in storage. Call it with no database session open.

    The key is `orgs/{org}/email/{sha256(rfc_message_id)}/{index}-{sha256(data)}`: the same message always
    maps to the same keys, so a retry overwrites instead of duplicating. If the ingest never commits, the
    stored files are harmless orphans that the retry reuses.
    """
    folder = hashlib.sha256(rfc_message_id.encode("utf-8", "surrogatepass")).hexdigest()
    stored: list[StoredAttachment] = []
    for index, att in enumerate(kept):
        digest = hashlib.sha256(att.data).hexdigest()
        key = f"orgs/{organisation_id}/email/{folder}/{index}-{digest}"
        await storage.put(key, att.data, att.content_type)
        stored.append(
            StoredAttachment(
                filename=att.filename or "attachment",
                content_type=att.content_type,
                size_bytes=len(att.data),
                sha256=digest,
                storage_key=key,
                content_id=att.content_id,
            )
        )
    return stored
