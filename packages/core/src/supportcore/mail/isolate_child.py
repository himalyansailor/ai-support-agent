"""The mail parser child process: `python -I -m supportcore.mail.isolate_child`, started and owned by `isolate.py`.

It reads requests from stdin (two frames: the operation name, then the raw message bytes) and answers each with one
JSON frame on stdout (see `isolate.py` for the framing). It runs only the operations named in `_operations()`:
`parse` always; the test-only `slow` and `crash` only when `SUPPORT_ENV` in the child's environment is `test`
(the parent passes only that, `PATH` and `LANG`, resolved from its own settings, so no secret reaches the child). An unknown or refused operation is answered `too_complex`. Nothing is imported by
name from the request.

It never writes anything but frames: file descriptor 1 is moved to a private descriptor and replaced with
/dev/null, and stderr is /dev/null (set by the parent), so a stray print or traceback can neither corrupt the
stream nor put mail content into a log.
"""

import os
import sys
import threading
import time
from collections.abc import Callable
from typing import BinaryIO

from supportcore.mail.attachments import select_attachments
from supportcore.mail.isolate import FRAME_LENGTH, PARSE, READY, TOO_COMPLEX, UNPARSEABLE, BoundedResult, encode_result
from supportcore.mail.parse import UnparseableEmailError, parse_email
from supportcore.mail.sanitize import sanitize_html

MEMORY_LIMIT = 2 << 30  # address space, bytes

Operation = Callable[[bytes], BoundedResult]


def parse_and_sanitize(raw: bytes) -> BoundedResult:
    """The `parse` operation: the three pure functions, in order."""
    parsed = parse_email(raw)
    sanitized = sanitize_html(parsed.html_raw) if parsed.html_raw else None
    kept, skipped = select_attachments(parsed)
    return BoundedResult(True, None, parsed, sanitized, kept, skipped)


def _slow(raw: bytes) -> BoundedResult:
    """Test only: `SLEEP` hangs (to exercise the timeout); anything else is parsed."""
    if raw.startswith(b"SLEEP"):
        time.sleep(120)
    return parse_and_sanitize(raw)


def _crash(raw: bytes) -> BoundedResult:
    """Test only: the failure modes of the child; anything else is parsed."""
    if raw.startswith(b"CRASH"):
        os._exit(7)
    if raw.startswith(b"RAISE"):
        raise ValueError("boom")
    if raw.startswith(b"MEMORY"):
        raise MemoryError
    if raw.startswith(b"UNENCODABLE"):
        return BoundedResult(True, None, parsed=object())  # type: ignore[arg-type]
    return parse_and_sanitize(raw)


def _is_test_env() -> bool:
    return os.environ.get("SUPPORT_ENV") == "test"  # set by the parent from its settings


def _operations() -> dict[str, Operation]:
    operations: dict[str, Operation] = {PARSE: parse_and_sanitize}
    if _is_test_env():
        operations.update(slow=_slow, crash=_crash)
    return operations


def limit_memory() -> None:
    """Best effort: caps the address space at 2 GiB. Enforced on Linux; macOS refuses it and Windows lacks it."""
    try:
        import resource  # POSIX only

        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        limit = MEMORY_LIMIT if hard == resource.RLIM_INFINITY else min(MEMORY_LIMIT, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
    except (ImportError, AttributeError, ValueError, OSError):
        pass  # unsupported here; the parent's timeout still bounds the child


def _watch_parent(parent_pid: int) -> None:
    while True:
        time.sleep(1.0)
        if os.getppid() != parent_pid:
            os._exit(0)


def _receive(stream: BinaryIO) -> bytes | None:
    header = stream.read(FRAME_LENGTH.size)
    if len(header) < FRAME_LENGTH.size:
        return None
    (size,) = FRAME_LENGTH.unpack(header)
    payload = stream.read(size)
    return payload if len(payload) == size else None


def _send(stream: BinaryIO, payload: bytes) -> None:
    stream.write(FRAME_LENGTH.pack(len(payload)))
    stream.write(payload)
    stream.flush()


def handle(operations: dict[str, Operation], name: bytes, raw: bytes) -> tuple[bytes, bool]:
    """The reply frame for one request, and whether the child must exit after sending it (after a MemoryError)."""
    fatal = False
    work = operations.get(name.decode("ascii", "replace"))
    try:
        result = work(raw) if work is not None else BoundedResult(False, TOO_COMPLEX)
    except UnparseableEmailError:
        result = BoundedResult(False, UNPARSEABLE)
    except MemoryError:
        result, fatal = BoundedResult(False, TOO_COMPLEX), True
    except BaseException:  # noqa: BLE001 - a bug or RecursionError in a parser is a skip, not a crash
        result = BoundedResult(False, TOO_COMPLEX)
    try:
        return encode_result(result, restart=fatal), fatal
    except MemoryError:
        return encode_result(BoundedResult(False, TOO_COMPLEX), restart=True), True
    except BaseException:  # noqa: BLE001 - a result that does not encode is a skip
        return encode_result(BoundedResult(False, TOO_COMPLEX), restart=fatal), fatal


def main() -> None:
    threading.Thread(target=_watch_parent, args=(os.getppid(),), daemon=True).start()
    requests = sys.stdin.buffer
    replies = os.fdopen(os.dup(1), "wb")
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.close(devnull)
    operations = _operations()
    limit_memory()
    _send(replies, READY)
    while True:
        name = _receive(requests)
        raw = _receive(requests) if name is not None else None
        if name is None or raw is None:
            return  # the parent closed the pipe, or died
        reply, fatal = handle(operations, name, raw)
        del raw
        _send(replies, reply)
        if fatal:
            return  # a fresh child is started on the next call


if __name__ == "__main__":
    main()
