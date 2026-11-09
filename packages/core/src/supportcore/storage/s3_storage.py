import asyncio
from typing import Any

from supportcore.storage.ports import StoredFileNotFoundError

_BATCH = 1000


class S3FileStorage:
    """S3 / MinIO storage. boto3 is blocking, so every call runs in a worker thread."""

    def __init__(self, endpoint_url: str | None, bucket: str, access_key: str, secret_key: str, region: str) -> None:
        # boto3 is imported lazily so importing supportcore.wiring stays cheap.
        import boto3
        from botocore.client import Config

        self._bucket = bucket
        # One client per instance; boto3 clients are thread-safe.
        self._client: Any = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=5,
                read_timeout=30,
            ),
        )

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        await asyncio.to_thread(
            self._client.put_object, Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
        )

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._get, key)

    def _get(self, key: str) -> bytes:
        from botocore.exceptions import ClientError

        try:
            return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise StoredFileNotFoundError(key) from exc
            raise

    async def stat_size(self, key: str) -> int:
        return await asyncio.to_thread(self._stat_size, key)

    def _stat_size(self, key: str) -> int:
        from botocore.exceptions import ClientError

        try:
            return int(self._client.head_object(Bucket=self._bucket, Key=key)["ContentLength"])
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
                raise StoredFileNotFoundError(key) from exc
            raise

    async def get_range(self, key: str, start: int, end_inclusive: int) -> bytes:
        return await asyncio.to_thread(self._get_range, key, start, end_inclusive)

    def _get_range(self, key: str, start: int, end_inclusive: int) -> bytes:
        from botocore.exceptions import ClientError

        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key, Range=f"bytes={start}-{end_inclusive}")
            return response["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise StoredFileNotFoundError(key) from exc
            raise

    async def delete(self, key: str) -> None:
        # S3 delete_object succeeds for missing keys, which gives idempotency.
        await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=key)

    async def delete_prefix(self, prefix: str) -> int:
        return await asyncio.to_thread(self._delete_prefix, prefix)

    def _delete_prefix(self, prefix: str) -> int:
        deleted = 0
        pages = self._client.get_paginator("list_objects_v2").paginate(
            Bucket=self._bucket, Prefix=prefix, PaginationConfig={"PageSize": _BATCH}
        )
        for page in pages:
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            for i in range(0, len(keys), _BATCH):
                batch = keys[i : i + _BATCH]
                resp = self._client.delete_objects(Bucket=self._bucket, Delete={"Objects": batch, "Quiet": True})
                if resp.get("Errors"):
                    raise RuntimeError(f"Failed to delete {len(resp['Errors'])} objects under prefix {prefix!r}")
                deleted += len(batch)
        return deleted
