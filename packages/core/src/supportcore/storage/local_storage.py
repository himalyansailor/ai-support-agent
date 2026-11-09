import asyncio
import os
import tempfile
from pathlib import Path

from supportcore.storage.ports import StoredFileNotFoundError


class LocalFileStorage:
    """Filesystem storage under ``root``; writes are atomic (temp file + rename)."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).expanduser().resolve()

    def _path(self, key: str) -> Path:
        if not key or key.startswith("/") or ".." in key.split("/") or "\\" in key or "\x00" in key:
            raise ValueError(f"Invalid storage key: {key!r}")
        path = (self._root / key).resolve()  # resolves symlinks
        if not path.is_relative_to(self._root) or path == self._root:
            raise ValueError(f"Invalid storage key: {key!r}")
        return path

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        await asyncio.to_thread(self._put, path, data)

    def _put(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
            raise StoredFileNotFoundError(key) from exc

    async def stat_size(self, key: str) -> int:
        path = self._path(key)
        try:
            if not path.is_file():
                raise FileNotFoundError(key)
            return (await asyncio.to_thread(path.stat)).st_size
        except FileNotFoundError as exc:
            raise StoredFileNotFoundError(key) from exc

    async def get_range(self, key: str, start: int, end_inclusive: int) -> bytes:
        path = self._path(key)
        try:
            return await asyncio.to_thread(self._read_range, path, start, end_inclusive)
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
            raise StoredFileNotFoundError(key) from exc

    @staticmethod
    def _read_range(path: Path, start: int, end_inclusive: int) -> bytes:
        with path.open("rb") as f:
            f.seek(start)
            return f.read(max(end_inclusive - start + 1, 0))

    async def delete(self, key: str) -> None:
        path = self._path(key)
        await asyncio.to_thread(self._delete, path)

    def _delete(self, path: Path) -> None:
        try:
            path.unlink()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            return
        self._prune_empty_dirs(path.parent)

    async def delete_prefix(self, prefix: str) -> int:
        if prefix.startswith("/") or ".." in prefix.split("/") or "\x00" in prefix:
            raise ValueError(f"Invalid storage prefix: {prefix!r}")
        return await asyncio.to_thread(self._delete_prefix, prefix)

    def _delete_prefix(self, prefix: str) -> int:
        # Prefixes are string prefixes (like S3): walk the directory that could contain matches.
        base = (self._root / prefix.rpartition("/")[0]).resolve()
        if not base.is_relative_to(self._root) or not base.is_dir():
            return 0
        deleted = 0
        for dirpath, _dirs, files in os.walk(base):
            for name in files:
                full = Path(dirpath) / name
                rel = full.relative_to(self._root).as_posix()
                if rel.startswith(prefix):
                    full.unlink(missing_ok=True)
                    deleted += 1
        for dirpath, _dirs, _files in os.walk(base, topdown=False):
            self._prune_empty_dirs(Path(dirpath))
        return deleted

    def _prune_empty_dirs(self, directory: Path) -> None:
        while directory != self._root and directory.is_relative_to(self._root):
            try:
                directory.rmdir()
            except OSError:
                return
            directory = directory.parent
