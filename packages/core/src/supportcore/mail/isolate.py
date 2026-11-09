"""The hard bound around mail parsing: parse, sanitise and select attachments in a killable child process.

The pre-scans in `parse.py` and `sanitize.py` are the fast path, but the libraries underneath (the standard-library
email parser, nh3/html5ever) have produced super-linear shapes more than once, so a scan can never be the only
defence. `parse_and_sanitize_bounded` runs `parse_email`, `sanitize_html` (when there is html) and
`select_attachments` in a long-lived child process and kills it when a message takes longer than `timeout`.

**Task 6 (and any other caller) must call only this entry point for raw mail from the network.** Never call
`parse_email` or `sanitize_html` directly on untrusted bytes in the worker or API process.

How it works:
- The child is a plain subprocess, `python -I -m supportcore.mail.isolate_child` (see that module), started lazily,
  one per process. It is not a `multiprocessing` child, so it starts inside a daemonic Celery prefork worker, and
  nothing of the parent (DB connections, threads, the event loop, `__main__`) is forked or re-imported into it.
- Framing over its stdin/stdout: every frame is an 8-byte big-endian length and then the payload. A request is two
  frames, the operation name and the raw bytes; the reply is one frame of JSON (`encode_result`; attachment bytes
  are base64, sent once: a kept attachment refers to the parsed attachment that holds its bytes). Nothing is
  pickled in either direction. The child sends a `ready` frame once it has imported.
- On a timeout, a crash, a malformed reply or any unexpected error, the child is killed and restarted lazily on the
  next call, and the result is a skip: `ok=False`, `error_type="too_complex"`. A message with no headers at all
  gives `ok=False`, `error_type="unparseable"`. Both are the `unparseable` skip reason; the `error_type` is for the
  integration-error row (Task 6 must add `too_complex` to the email error types).
- When the child cannot be started (or does not report ready within 60 s), `MailIsolationUnavailable` is raised.
  That is transient and says nothing about the message: the caller must not skip it or advance past it.
- The startup of the child does not count against `timeout`; writing the request and reading the reply do.
- The child limits its address space to 2 GiB where the OS supports it; a `MemoryError` is `too_complex`.
- Orphans: the child exits when its stdin closes (this process died) and a watchdog thread in the child exits when
  its parent pid changes, even while it is busy inside a parser. The child is also killed at interpreter exit.
- After a `fork()` (a Celery prefork worker), the inherited child belongs to the parent: the forked process forgets
  it and starts its own on first use.
- Calls are serialised on one child; the timeout starts when the call reaches it. The async entry point first
  queues on an `asyncio.Lock` (one per event loop), then waits in `asyncio.to_thread`, so queued callers hold no
  thread and never block the event loop.
"""

import asyncio
import atexit
import base64
import json
import os
import selectors
import struct
import subprocess
import sys
import threading
import time
import weakref
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Any

from supportcore.mail import MailIsolationUnavailable
from supportcore.mail.parse import ParsedEmail, RawAttachment
from supportcore.mail.sanitize import SanitizedHtml

DEFAULT_TIMEOUT = 10.0
STARTUP_TIMEOUT = 60.0
TOO_COMPLEX = "too_complex"
UNPARSEABLE = "unparseable"
CHILD_MODULE = "supportcore.mail.isolate_child"
PARSE = "parse"
# The operations a caller may ask for. The child runs "slow" and "crash" only when its settings env is "test".
OPERATIONS = frozenset({PARSE, "slow", "crash"})
READY = b"ready"
FRAME_LENGTH = struct.Struct(">Q")
MAX_REPLY_BYTES = 1 << 30
_CHUNK = 1 << 20

__all__ = [
    "DEFAULT_TIMEOUT",
    "BoundedResult",
    "MailIsolationUnavailable",
    "parse_and_sanitize_bounded",
    "parse_and_sanitize_bounded_blocking",
    "shutdown_workers",
]


@dataclass(frozen=True)
class BoundedResult:
    ok: bool
    error_type: str | None = None  # None when ok; "too_complex" (timeout, crash, bug) or "unparseable" (no headers)
    parsed: ParsedEmail | None = None
    sanitized: SanitizedHtml | None = None  # None when the message has no html
    kept: list[RawAttachment] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


# --- the JSON codec (the child encodes, this process decodes) ---


def _encode_attachment(att: RawAttachment, shared: dict[int, int] | None = None) -> dict[str, Any]:
    """`shared` maps id(bytes) of the parsed attachments to their index; such bytes are sent as a reference."""
    doc: dict[str, Any] = {
        "filename": att.filename,
        "content_type": att.content_type,
        "content_id": att.content_id,
        "inline": att.inline,
    }
    if shared is not None and id(att.data) in shared:
        doc["data_of"] = shared[id(att.data)]
    else:
        doc["data"] = base64.b64encode(att.data).decode("ascii")
    return doc


def _decode_attachment(doc: dict[str, Any], parsed: ParsedEmail | None = None) -> RawAttachment:
    if "data_of" in doc:
        index = doc["data_of"]
        if parsed is None or type(index) is not int or not 0 <= index < len(parsed.attachments):
            raise ValueError("malformed reply")
        data = parsed.attachments[index].data  # the same bytes object, not a copy
    else:
        data = base64.b64decode(doc["data"], validate=True)
    return RawAttachment(
        filename=doc["filename"],
        content_type=doc["content_type"],
        data=data,
        content_id=doc["content_id"],
        inline=doc["inline"],
    )


def encode_result(result: BoundedResult, *, restart: bool = False) -> bytes:
    """JSON (ASCII; lone surrogates survive as escapes). Raises when the result is not a well-formed BoundedResult.

    `restart` tells the parent that the child exits after this reply (after a MemoryError), so the next message
    goes to a fresh child instead of racing the exit.
    """
    parsed: dict[str, Any] | None = None
    shared: dict[int, int] = {}
    if result.parsed is not None:
        shared = {id(a.data): i for i, a in enumerate(result.parsed.attachments)}
        parsed = {f.name: getattr(result.parsed, f.name) for f in fields(ParsedEmail)}
        parsed["date"] = result.parsed.date.isoformat() if result.parsed.date is not None else None
        parsed["attachments"] = [_encode_attachment(a) for a in result.parsed.attachments]
    sanitized = result.sanitized
    doc = {
        "ok": result.ok,
        "error_type": result.error_type,
        "parsed": parsed,
        "sanitized": None
        if sanitized is None
        else {"html": sanitized.html, "has_remote_images": sanitized.has_remote_images},
        "kept": [_encode_attachment(a, shared) for a in result.kept],  # kept bytes are the parsed ones
        "skipped": result.skipped,
        "restart": restart,
    }
    return json.dumps(doc, allow_nan=False, separators=(",", ":")).encode("ascii")


def decode_result(data: bytes) -> BoundedResult:
    """The inverse of `encode_result`; raises on anything malformed."""
    return _decode_reply(data)[0]


def _decode_reply(data: bytes) -> tuple[BoundedResult, bool]:
    doc = json.loads(data)
    parsed: ParsedEmail | None = None
    if doc["parsed"] is not None:
        values = dict(doc["parsed"])
        for name in ("references", "to", "cc"):
            values[name] = tuple(values[name])
        values["date"] = datetime.fromisoformat(values["date"]) if values["date"] is not None else None
        values["attachments"] = [_decode_attachment(a) for a in values["attachments"]]
        parsed = ParsedEmail(**values)
    sanitized = None if doc["sanitized"] is None else SanitizedHtml(**doc["sanitized"])
    if not isinstance(doc["ok"], bool) or doc["error_type"] not in (None, TOO_COMPLEX, UNPARSEABLE):
        raise ValueError("malformed reply")
    result = BoundedResult(
        ok=doc["ok"],
        error_type=doc["error_type"],
        parsed=parsed,
        sanitized=sanitized,
        kept=[_decode_attachment(a, parsed) for a in doc["kept"]],
        skipped=[dict(s) for s in doc["skipped"]],
    )
    return result, doc.get("restart") is True


# --- pipe I/O with a deadline (the pipes are non-blocking on this side) ---


class _Timeout(Exception):
    pass


def _wait(fd: int, event: int, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _Timeout
    with selectors.DefaultSelector() as selector:
        selector.register(fd, event)
        if not selector.select(remaining):
            raise _Timeout


def _write_all(fd: int, data: bytes, deadline: float) -> None:
    view = memoryview(data)
    while view:
        _wait(fd, selectors.EVENT_WRITE, deadline)
        try:
            written = os.write(fd, view[:_CHUNK])
        except BlockingIOError:
            continue
        view = view[written:]


def _read_exact(fd: int, size: int, deadline: float) -> bytes:
    buffer = bytearray()
    while len(buffer) < size:
        _wait(fd, selectors.EVENT_READ, deadline)
        try:
            chunk = os.read(fd, min(size - len(buffer), _CHUNK))
        except BlockingIOError:
            continue
        if not chunk:
            raise EOFError
        buffer += chunk
    return bytes(buffer)


def _read_frame(fd: int, deadline: float) -> bytes:
    (size,) = FRAME_LENGTH.unpack(_read_exact(fd, FRAME_LENGTH.size, deadline))
    if size > MAX_REPLY_BYTES:
        raise ValueError("reply too large")
    return _read_exact(fd, size, deadline)


def _write_frame(fd: int, payload: bytes, deadline: float) -> None:
    _write_all(fd, FRAME_LENGTH.pack(len(payload)), deadline)
    _write_all(fd, payload, deadline)


# --- the child process ---

_inherited: list[subprocess.Popen[bytes]] = []  # children of the pre-fork parent; kept so __del__ never runs here


def _child_env() -> dict[str, str]:
    try:
        from supportcore.config import get_settings

        env = get_settings().env
    except Exception:  # noqa: BLE001 - settings that do not load are not the test env
        env = "prod"
    keep = {k: os.environ[k] for k in ("PATH", "LANG") if k in os.environ}
    return {**keep, "SUPPORT_ENV": env}


class _Worker:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.process: subprocess.Popen[bytes] | None = None

    def _start(self) -> None:
        try:
            process = subprocess.Popen(  # fixed argv: this interpreter and a module of this package
                [sys.executable, "-I", "-m", CHILD_MODULE],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,  # a traceback could quote mail content
                close_fds=True,
                env=_child_env(),  # never the parent's secrets
            )
        except (OSError, ValueError) as exc:
            raise MailIsolationUnavailable("mail parser process could not be started") from exc
        self.process = process
        try:
            assert process.stdin is not None and process.stdout is not None
            os.set_blocking(process.stdin.fileno(), False)
            os.set_blocking(process.stdout.fileno(), False)
            ready = _read_frame(process.stdout.fileno(), time.monotonic() + STARTUP_TIMEOUT)
        except (_Timeout, EOFError, OSError, ValueError, AssertionError):
            ready = b""
        if ready != READY:
            self.kill()
            raise MailIsolationUnavailable("mail parser process did not start")

    def kill(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        for stream in (process.stdin, process.stdout):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass
        try:
            process.kill()
            process.wait(5)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def forget_after_fork(self) -> None:
        """In a forked child: drop (without killing) the parent's process and close this copy of its pipes."""
        self.lock = threading.Lock()
        process, self.process = self.process, None
        if process is None:
            return
        _inherited.append(process)
        for stream in (process.stdin, process.stdout):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass

    def run(self, operation: str, raw: bytes, timeout: float) -> BoundedResult:
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                self.kill()
                self._start()  # raises MailIsolationUnavailable
            process = self.process
            assert process is not None and process.stdin is not None and process.stdout is not None
            deadline = time.monotonic() + timeout
            try:
                _write_frame(process.stdin.fileno(), operation.encode("ascii"), deadline)
                _write_frame(process.stdin.fileno(), raw, deadline)
                result, restart = _decode_reply(_read_frame(process.stdout.fileno(), deadline))
            except Exception:  # noqa: BLE001 - timeout, dead child, broken pipe, malformed reply: kill and skip
                self.kill()
                return BoundedResult(False, TOO_COMPLEX)
            if restart:
                self.kill()
            return result


_worker = _Worker()
_async_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()
_async_locks_guard = threading.Lock()


def _after_fork_in_child() -> None:
    global _async_locks, _async_locks_guard
    _worker.forget_after_fork()
    _async_locks = weakref.WeakKeyDictionary()
    _async_locks_guard = threading.Lock()


os.register_at_fork(after_in_child=_after_fork_in_child)


def shutdown_workers() -> None:
    """Kills the child (it restarts lazily on the next call). Registered at interpreter exit."""
    with _worker.lock:
        _worker.kill()


atexit.register(shutdown_workers)


def parse_and_sanitize_bounded_blocking(
    raw: bytes, *, timeout: float = DEFAULT_TIMEOUT, _operation: str = PARSE
) -> BoundedResult:
    """Blocking form (sync code, Celery tasks, tests). Raises `MailIsolationUnavailable` when the child cannot start.

    `_operation` is a test hook: one of `OPERATIONS`; the child refuses the test-only ones outside the test env.
    """
    if _operation not in OPERATIONS:
        raise ValueError("unknown operation")
    return _worker.run(_operation, raw, timeout)


def _async_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    with _async_locks_guard:
        lock = _async_locks.get(loop)
        if lock is None:
            lock = _async_locks[loop] = asyncio.Lock()
        return lock


async def parse_and_sanitize_bounded(
    raw: bytes, *, timeout: float = DEFAULT_TIMEOUT, _operation: str = PARSE
) -> BoundedResult:
    """The entry point for async callers: queue on the loop's lock, then wait for the child in a thread."""
    async with _async_lock():
        return await asyncio.to_thread(parse_and_sanitize_bounded_blocking, raw, timeout=timeout, _operation=_operation)
