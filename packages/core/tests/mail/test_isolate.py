import asyncio
import base64
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import billiard
import pytest

from supportcore.mail import MailIsolationUnavailable, isolate
from supportcore.mail.attachments import select_attachments
from supportcore.mail.isolate import (
    BoundedResult,
    decode_result,
    encode_result,
    parse_and_sanitize_bounded,
    parse_and_sanitize_bounded_blocking as bounded,
    shutdown_workers,
)
from supportcore.mail.parse import ParsedEmail, RawAttachment, parse_email
from supportcore.mail.sanitize import SanitizedHtml, html_to_text, sanitize_html

FIX = Path(__file__).parent / "fixtures"
H = b"From: a@example.test\nMessage-ID: <x@example.test>\n"
PLAIN = (FIX / "plain_html.eml").read_bytes()


def _set_child_env(value: str | None) -> None:
    """The parent passes its settings env to the child when it starts; restart it so the change applies."""
    from supportcore.config import get_settings

    shutdown_workers()
    if value is None:
        os.environ.pop("SUPPORT_ENV", None)
    else:
        os.environ["SUPPORT_ENV"] = value
    get_settings.cache_clear()


@pytest.fixture(scope="module", autouse=True)
def _child_in_test_env():
    previous = os.environ.get("SUPPORT_ENV")
    _set_child_env("test")
    yield
    _set_child_env(previous)


def _child_pid() -> int:
    assert isolate._worker.process is not None
    return isolate._worker.process.pid


def _assert_same_as_direct(raw: bytes) -> BoundedResult:
    result = bounded(raw)
    parsed = parse_email(raw)
    kept, skipped = select_attachments(parsed)
    assert result.ok and result.error_type is None
    assert result.parsed == parsed
    assert result.sanitized == (sanitize_html(parsed.html_raw) if parsed.html_raw else None)
    assert (result.kept, result.skipped) == (kept, skipped)
    return result


@pytest.mark.parametrize("name", sorted(p.name for p in FIX.glob("*.eml")))
def test_results_identical_to_calling_the_functions_directly(name):
    _assert_same_as_direct((FIX / name).read_bytes())


def test_attachment_bytes_and_awkward_text_survive_the_json_round_trip():
    pdf = bytes(range(256)) * 50
    raw = (
        H + b"Date: Mon, 26 Oct 2026 08:00:00 +0200\nSubject: =?utf-8?q?caf=C3=A9_=F0=9F=98=80?=\n"
        b"Content-Type: multipart/mixed; boundary=B\n\n"
        b"--B\nContent-Type: text/plain; charset=utf-8\n\nna\xc3\xafve \xff\xfe text\n"
        b'--B\nContent-Type: text/html\n\n<p>hi<img src="https://t.test/x.png"></p>\n'
        b'--B\nContent-Type: application/pdf\nContent-Disposition: attachment; filename="r.pdf"\n'
        b"Content-Transfer-Encoding: base64\n\n" + base64.encodebytes(pdf) + b"--B\n"
        b'Content-Type: application/x-msdownload\nContent-Disposition: attachment; filename="x.exe"\n\nMZ\n--B--\n'
    )
    result = _assert_same_as_direct(raw)
    assert result.kept[0].data == pdf and result.parsed.attachments[0].data == pdf
    assert result.skipped == [{"filename": "x.exe", "size": 2, "reason": "type_not_allowed"}]
    assert result.sanitized.has_remote_images and result.parsed.date.tzinfo is not None


def test_thirty_mb_message_with_three_large_attachments_keeps_them_all():
    """Three 7 MB PDFs: 28.4 MB raw (under the 30 MB fetch cap) and 21 MB kept (under the 25 MB message total).

    Three 9 MB files cannot pass both limits (36.5 MB as base64, 27 MB kept), so 7 MB is the largest such case.
    """
    files = [bytes([i]) * (7 * 1024 * 1024) for i in range(3)]
    raw = H + b"Content-Type: multipart/mixed; boundary=B\n\n--B\nContent-Type: text/plain\n\nsee attached\n"
    for i, data in enumerate(files):
        raw += (
            b'--B\nContent-Type: application/pdf\nContent-Disposition: attachment; filename="f%d.pdf"\n' % i
            + b"Content-Transfer-Encoding: base64\n\n"
            + base64.encodebytes(data)
        )
    raw += b"--B--\n"
    assert 28 * 2**20 < len(raw) <= 30 * 2**20
    bounded(PLAIN)  # the start is not timed
    t = time.perf_counter()
    result = bounded(raw, timeout=10.0)
    assert time.perf_counter() - t < 10.0
    assert result.ok and result.skipped == [] and result.parsed.text == "see attached"
    assert [a.data for a in result.kept] == files
    assert all(k.data is p.data for k, p in zip(result.kept, result.parsed.attachments, strict=True))  # one copy


def test_codec_round_trips_every_field_type():
    att = RawAttachment("a.png", "image/png", b"\x00\xff", "cid@x", True)
    parsed = ParsedEmail(
        message_id="m", in_reply_to=None, references=("a", "b"), subject="s\ud800", from_address="a@example.test",
        from_name="", reply_to=None, to=("b@example.test",), cc=(), date=datetime(2026, 1, 1, tzinfo=UTC), text="t",
        html_raw=None, attachments=[att], auto_submitted=None, precedence="bulk", has_autoreply_header=True,
        return_path_empty=False, is_delivery_report=False, bounced_message_id=None, x_support_mailbox=None,
        truncated=True,
    )  # fmt: skip
    result = BoundedResult(
        True, None, parsed, SanitizedHtml(None, False), [att], [{"filename": "", "size": 0, "reason": "x"}]
    )
    assert decode_result(encode_result(result)) == result
    assert decode_result(encode_result(BoundedResult(False, "too_complex"))) == BoundedResult(False, "too_complex")
    with pytest.raises(ValueError):
        decode_result(
            b'{"ok": true, "error_type": "nope", "parsed": null, "sanitized": null, "kept": [], "skipped": []}'
        )


def test_no_headers_is_unparseable_not_a_raise():
    result = bounded(b"\x00\x01 no headers here\n\nbody")
    assert not result.ok and result.error_type == "unparseable" and result.parsed is None


def test_slow_child_is_killed_within_timeout_and_next_call_succeeds():
    assert bounded(PLAIN, _operation="slow").ok  # warm-up: the start is not timed
    before = _child_pid()
    t = time.perf_counter()
    result = bounded(b"SLEEP", timeout=1.0, _operation="slow")
    assert time.perf_counter() - t < 2.0
    assert not result.ok and result.error_type == "too_complex"
    again = bounded(PLAIN, timeout=5.0, _operation="slow")
    assert again.ok and again.parsed.message_id == "m1@example.test"
    assert _child_pid() != before  # killed and restarted


@pytest.mark.parametrize("payload", [b"CRASH", b"RAISE", b"MEMORY", b"UNENCODABLE"])
def test_child_failures_are_skips_not_raises(payload):
    result = bounded(payload, timeout=5.0, _operation="crash")
    assert not result.ok and result.error_type == "too_complex"
    assert bounded(PLAIN, timeout=5.0, _operation="crash").ok  # restarted (or still alive) and working


def test_memory_error_restarts_the_child():
    bounded(PLAIN)
    before = _child_pid()
    assert bounded(b"MEMORY", timeout=5.0, _operation="crash").error_type == "too_complex"
    assert bounded(PLAIN).ok and _child_pid() != before


def test_test_only_operations_are_refused_outside_the_test_env():
    _set_child_env("dev")
    try:
        assert bounded(PLAIN).ok
        before = _child_pid()
        t = time.perf_counter()
        for operation, payload in (("slow", b"SLEEP"), ("crash", b"CRASH")):
            result = bounded(payload, timeout=5.0, _operation=operation)
            assert not result.ok and result.error_type == "too_complex"
        assert time.perf_counter() - t < 2.0 and _child_pid() == before  # answered, not slept or crashed
        assert not bounded(PLAIN, _operation="slow").ok  # refused even for a valid message
    finally:
        _set_child_env("test")


def test_unknown_operation_is_rejected_before_reaching_the_child():
    with pytest.raises(ValueError):
        bounded(PLAIN, _operation="supportcore.mail.parse:parse_email")


def test_start_failure_raises_isolation_unavailable(monkeypatch):
    shutdown_workers()
    monkeypatch.setattr(isolate, "CHILD_MODULE", "supportcore.mail.no_such_module")
    with pytest.raises(MailIsolationUnavailable):
        bounded(PLAIN)
    monkeypatch.setattr(isolate.sys, "executable", "/nonexistent/python")
    with pytest.raises(MailIsolationUnavailable):
        bounded(PLAIN)
    with pytest.raises(MailIsolationUnavailable):
        asyncio.run(parse_and_sanitize_bounded(PLAIN))
    monkeypatch.undo()
    assert bounded(PLAIN).ok  # a later call starts it again
    assert isolate.MailIsolationUnavailable is MailIsolationUnavailable


def _pool_job(raw: bytes) -> tuple[bool, str | None, str | None]:
    result = bounded(raw)
    return result.ok, result.error_type, result.parsed.message_id if result.parsed else None


# Celery forks its pool the same way; other tests may have left threads in this process.
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_works_inside_a_daemonic_billiard_pool_worker():
    """R3-1: a Celery prefork worker is a daemonic billiard process, which multiprocessing refused to start from."""
    assert bounded(PLAIN).ok  # this process already has a child when the pool forks
    parent_child = _child_pid()
    with billiard.get_context("fork").Pool(1) as pool:
        assert pool.apply(_pool_job, (PLAIN,)) == (True, None, "m1@example.test")
        assert pool.apply(_pool_job, (PLAIN,)) == (True, None, "m1@example.test")
    assert bounded(PLAIN).ok and _child_pid() == parent_child  # the fork never touched this process's child


def test_async_entry_point_does_not_block_the_loop():
    async def main():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        task = asyncio.create_task(ticker())
        slow = await parse_and_sanitize_bounded(b"SLEEP", timeout=1.0, _operation="slow")
        task.cancel()
        return slow, ticks, await parse_and_sanitize_bounded(PLAIN)

    slow, ticks, ok = asyncio.run(main())
    assert slow.error_type == "too_complex" and ticks > 20 and ok.ok


def test_queued_async_callers_do_not_hold_threads():
    """R3-6: callers wait on an asyncio.Lock, so a 2-thread executor still has a free thread for others."""
    bounded(PLAIN)  # started outside the timed part

    async def main():
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=2))
        calls = [
            asyncio.create_task(parse_and_sanitize_bounded(b"SLEEP", timeout=1.0, _operation="slow")) for _ in range(3)
        ]
        await asyncio.sleep(0.2)
        t = time.perf_counter()
        await asyncio.to_thread(lambda: None)
        waited = time.perf_counter() - t
        results = await asyncio.gather(*calls)
        return waited, results

    waited, results = asyncio.run(main())
    assert waited < 0.5
    assert [r.error_type for r in results] == ["too_complex"] * 3


def test_memory_limit_is_best_effort():
    code = (
        "from supportcore.mail.isolate_child import limit_memory\n"
        "limit_memory()\n"
        "import resource\n"
        "print(resource.getrlimit(resource.RLIMIT_AS)[0])\n"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True, timeout=60)
    if sys.platform.startswith("linux"):
        assert int(out.stdout) == 2 << 30
    # elsewhere (macOS refuses RLIMIT_AS) it must just not fail


def test_orphaned_child_exits_when_the_parent_is_killed():
    code = (
        "from supportcore.mail import isolate\n"
        "isolate.parse_and_sanitize_bounded_blocking(b'From: a@example.test\\n\\nhi')\n"
        "print(isolate._worker.process.pid, flush=True)\n"
        "import time; time.sleep(60)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        child_pid = int(parent.stdout.readline())
        os.kill(child_pid, 0)  # alive
        parent.send_signal(signal.SIGKILL)
        parent.wait(10)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.2)
        pytest.fail("the child outlived its parent")
    finally:
        parent.kill()


# The three round-2 Critical payloads, through the direct functions and through the bounded entry point.
def _r21():
    return "".join(f"<div><b title={i}></div><!--</b>-->" for i in range(8000)) + "x"


def _r22():
    return "<p " + " ".join(f"a{i}" for i in range(100_000)) + ">x</p>"


def _r23():
    return H + b'Content-Type: multipart/mixed; boundary="a b"\n\n' + b"--a b\n\n" * 400_000


@pytest.mark.parametrize("html_factory", [_r21, _r22])
def test_critical_html_payloads_direct_and_bounded(html_factory):
    html = html_factory()
    for fn in (sanitize_html, html_to_text):
        t = time.perf_counter()
        fn(html)
        assert time.perf_counter() - t < 5.0
    raw = H + b"Content-Type: text/html\n\n" + html.encode()
    bounded(PLAIN)  # the start is not timed
    t = time.perf_counter()
    result = bounded(raw, timeout=10.0)
    assert time.perf_counter() - t < 5.0
    assert result.ok and result.sanitized.html is None and result.parsed.text.endswith("x")


def test_critical_spaced_boundary_payload_direct_and_bounded():
    raw = _r23()
    t = time.perf_counter()
    direct = parse_email(raw)
    assert time.perf_counter() - t < 5.0 and direct.truncated
    bounded(PLAIN)
    t = time.perf_counter()
    result = bounded(raw, timeout=10.0)
    assert time.perf_counter() - t < 5.0 and result.ok and result.parsed.truncated


def test_child_environment_carries_no_parent_secrets():
    env = isolate._child_env()
    assert set(env) <= {"PATH", "LANG", "SUPPORT_ENV"} and env["SUPPORT_ENV"] == "test"
