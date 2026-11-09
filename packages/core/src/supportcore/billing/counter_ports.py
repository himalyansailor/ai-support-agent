"""Usage counter port: the month's running totals, a cache rebuilt from the ledger when absent."""

from typing import Protocol


class UsageCounter(Protocol):
    async def get(self, key: str) -> int | None: ...

    async def incr_if_present(self, key: str, amount: int) -> None:
        """Atomically adds `amount` when the key exists; does nothing when it is absent."""

    async def set_if_absent(self, key: str, value: int, ttl_seconds: int) -> None: ...

    async def set(self, key: str, value: int, ttl_seconds: int) -> None: ...
