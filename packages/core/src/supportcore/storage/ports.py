from typing import Protocol, runtime_checkable

from supportcore.errors import NotFoundError


class StoredFileNotFoundError(NotFoundError):
    """No file is stored under the requested key."""

    def __init__(self, key: str) -> None:
        super().__init__(f"Stored file not found: {key}")
        self.key = key


@runtime_checkable
class FileStorage(Protocol):
    """Blob storage for uploaded documents.

    Key format (Task 10): ``orgs/{org_id}/kbs/{kb_id}/documents/{document_id}/{sha256}``.
    """

    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes:
        """Raises StoredFileNotFoundError if the key does not exist."""
        ...

    async def stat_size(self, key: str) -> int:
        """The object's size in bytes. Raises StoredFileNotFoundError if the key does not exist."""
        ...

    async def get_range(self, key: str, start: int, end_inclusive: int) -> bytes:
        """Bytes ``start..end_inclusive`` (clamped to the object's end). Raises StoredFileNotFoundError if the key
        does not exist."""
        ...

    async def delete(self, key: str) -> None:
        """Idempotent: deleting a missing key is not an error."""
        ...

    async def delete_prefix(self, prefix: str) -> int:
        """Deletes every key starting with ``prefix``; returns how many were deleted."""
        ...
