"""In-memory InboundMailbox for tests (registered only when env == "test")."""

import uuid
from datetime import UTC, datetime

from supportcore.mail.ports import FetchedMessage, FetchResult, MailServer


class FakeInboundMailbox:
    def __init__(self) -> None:
        self._messages: list[tuple[int, datetime, bytes]] = []
        self._next_uid = 1
        self._uidvalidity = 1
        self._error: Exception | None = None
        self.calls = 0

    def deliver(self, raw: bytes, internal_date: datetime | None = None) -> int:
        uid = self._next_uid
        self._next_uid += 1
        self._messages.append((uid, internal_date or datetime.now(UTC), raw))
        return uid

    def reset_uidvalidity(self) -> None:
        """Like a server that rebuilt the folder: new UIDVALIDITY, UIDs start again."""
        self._uidvalidity = uuid.uuid4().int % (2**31) + 1000
        self._messages = []
        self._next_uid = 1

    def fail_with(self, exc: Exception | None) -> None:
        self._error = exc

    def _enter(self) -> None:
        self.calls += 1
        if self._error is not None:
            raise self._error

    async def check(self, server: MailServer, folder: str) -> None:
        self._enter()

    async def uidvalidity(self, server: MailServer, folder: str) -> int:
        self._enter()
        return self._uidvalidity

    async def fetch(
        self,
        server: MailServer,
        folder: str,
        *,
        after_uid: int | None,
        since: datetime | None,
        limit: int,
        max_bytes: int,
        max_batch_bytes: int | None = None,
    ) -> FetchResult:
        self._enter()
        matching = [
            FetchedMessage(uid=uid, internal_date=date, size=len(raw), raw=raw if len(raw) <= max_bytes else None)
            for uid, date, raw in self._messages
            if (after_uid is not None and uid > after_uid) or (after_uid is None and (since is None or date >= since))
        ]
        more = len(matching) > limit
        picked: list[FetchedMessage] = []
        total = 0
        for message in matching[:limit]:
            body = len(message.raw) if message.raw is not None else 0
            if picked and max_batch_bytes is not None and total + body > max_batch_bytes:
                more = True
                break
            picked.append(message)
            total += body
        return FetchResult(uidvalidity=self._uidvalidity, messages=picked, uidnext=self._next_uid, more=more)
