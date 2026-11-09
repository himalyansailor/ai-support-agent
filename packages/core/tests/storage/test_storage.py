import socket
import uuid

import pytest

from supportcore.config import Settings
from supportcore.storage.local_storage import LocalFileStorage
from supportcore.storage.ports import FileStorage, StoredFileNotFoundError
from supportcore.storage.s3_storage import S3FileStorage
from supportcore.wiring import build_adapters

MINIO = ("localhost", 9000)


def _minio_up() -> bool:
    try:
        with socket.create_connection(MINIO, timeout=0.5):
            return True
    except OSError:
        return False


def _s3() -> S3FileStorage:
    return S3FileStorage(
        endpoint_url="http://localhost:9000",
        bucket="support-documents",
        access_key="support",
        secret_key="supportsecret",
        region="us-east-1",
    )


@pytest.fixture(params=["local", pytest.param("s3", marks=pytest.mark.integration)])
async def storage(request, tmp_path):
    """Yields (storage, unique_prefix); everything written under the prefix is removed afterwards."""
    prefix = f"orgs/test-{uuid.uuid4().hex}/"
    if request.param == "local":
        store: FileStorage = LocalFileStorage(tmp_path / "root")
    else:
        if not _minio_up():
            pytest.skip("MinIO is not running on localhost:9000")
        store = _s3()
    try:
        yield store, prefix
    finally:
        await store.delete_prefix(prefix)


async def test_put_get_roundtrip(storage):
    store, p = storage
    await store.put(f"{p}a/doc", b"hello \x00 bytes", "application/octet-stream")
    assert await store.get(f"{p}a/doc") == b"hello \x00 bytes"
    await store.put(f"{p}a/doc", b"v2", "text/plain")
    assert await store.get(f"{p}a/doc") == b"v2"


async def test_get_missing_raises(storage):
    store, p = storage
    with pytest.raises(StoredFileNotFoundError):
        await store.get(f"{p}nope")


async def test_stat_size_and_get_range(storage):
    store, p = storage
    await store.put(f"{p}r", bytes(range(100)), "audio/mpeg")
    assert await store.stat_size(f"{p}r") == 100
    assert await store.get_range(f"{p}r", 10, 19) == bytes(range(10, 20))
    assert await store.get_range(f"{p}r", 0, 0) == b"\x00"
    assert await store.get_range(f"{p}r", 90, 99) == bytes(range(90, 100))


async def test_stat_size_and_get_range_missing_raise(storage):
    store, p = storage
    with pytest.raises(StoredFileNotFoundError):
        await store.stat_size(f"{p}nope")
    with pytest.raises(StoredFileNotFoundError):
        await store.get_range(f"{p}nope", 0, 9)


async def test_delete_idempotent(storage):
    store, p = storage
    await store.put(f"{p}x", b"1", "text/plain")
    await store.delete(f"{p}x")
    await store.delete(f"{p}x")
    with pytest.raises(StoredFileNotFoundError):
        await store.get(f"{p}x")


async def test_delete_prefix_counts(storage):
    store, p = storage
    for i in range(3):
        await store.put(f"{p}kb1/{i}", b"x", "text/plain")
    await store.put(f"{p}kb2/0", b"y", "text/plain")
    assert await store.delete_prefix(f"{p}kb1/") == 3
    assert await store.delete_prefix(f"{p}kb1/") == 0
    assert await store.get(f"{p}kb2/0") == b"y"


@pytest.mark.integration
async def test_s3_delete_prefix_paginates_beyond_1000():
    if not _minio_up():
        pytest.skip("MinIO is not running on localhost:9000")
    store = _s3()
    p = f"orgs/test-{uuid.uuid4().hex}/"
    try:
        import asyncio

        await asyncio.gather(*(store.put(f"{p}{i}", b"x", "text/plain") for i in range(1005)))
        assert await store.delete_prefix(p) == 1005
    finally:
        await store.delete_prefix(p)


@pytest.mark.parametrize("key", ["../evil", "a/../../evil", "/abs", "a/..", ""])
async def test_local_rejects_path_traversal(tmp_path, key):
    store = LocalFileStorage(tmp_path / "root")
    for call in (
        store.put(key, b"x", "text/plain"),
        store.get(key),
        store.delete(key),
    ):
        with pytest.raises(ValueError):
            await call
    assert not (tmp_path / "evil").exists()


async def test_local_rejects_symlink_escape(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    store = LocalFileStorage(root)
    with pytest.raises(ValueError):
        await store.put("link/f", b"x", "text/plain")
    assert list(outside.iterdir()) == []


async def test_local_put_is_atomic_and_prunes_empty_dirs(tmp_path):
    root = tmp_path / "root"
    store = LocalFileStorage(root)
    await store.put("orgs/o/kbs/k/f", b"x", "text/plain")
    assert [p.name for p in (root / "orgs/o/kbs/k").iterdir()] == ["f"]
    assert await store.delete_prefix("orgs/o/") == 1
    assert not (root / "orgs").exists() or not any((root / "orgs").rglob("*"))
    assert root.exists()


def test_build_adapters_selects_storage(tmp_path):
    def settings(kind: str) -> Settings:
        return Settings(
            _env_file=None,
            env="test",
            database_url="postgresql+asyncpg://u:p@h/db",
            redis_url="redis://h:6379/0",
            jwt_secret="x" * 32,
            smtp_from="noreply@example.com",
            file_storage=kind,
            local_storage_path=str(tmp_path / "up"),
        )

    local = build_adapters(settings("local")).storage
    assert isinstance(local, LocalFileStorage)
    s3 = build_adapters(settings("s3")).storage
    assert isinstance(s3, S3FileStorage)
