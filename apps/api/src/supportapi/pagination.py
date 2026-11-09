"""Opaque keyset cursors for newest-first lists ordered by (timestamp, id)."""

import base64
import binascii
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException


def encode_cursor(created_at: datetime, id: UUID) -> str:
    """Opaque keyset cursor for (timestamp, id); clients must pass it back verbatim."""
    raw = f"{created_at.isoformat()}|{id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        created_at, id = raw.split("|")
        parsed = datetime.fromisoformat(created_at)
        if parsed.tzinfo is None:
            raise ValueError("naive timestamp")
        return parsed, UUID(id)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=422, detail="Invalid pagination cursor") from None
