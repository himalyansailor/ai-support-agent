import json
import logging
from logging.handlers import RotatingFileHandler

from opentelemetry.sdk.trace import TracerProvider

from supportcore.config import Settings
from supportcore.observability.context import bind_organisation, request_context
from supportcore.observability.logging import configure_logging, get_logger


def _settings(**overrides) -> Settings:
    return Settings(
        env="test",
        database_url="x",
        redis_url="x",
        jwt_secret="test-secret-test-secret-test-secret-123",
        smtp_from="a@b.c",
        **overrides,
    )


def _file_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]


def _cleanup() -> None:
    for handler in _file_handlers():
        logging.getLogger().removeHandler(handler)
        handler.close()


def test_log_file_sink_writes_json_lines_with_trace_id(tmp_path):
    path = tmp_path / "logs" / "api.log"
    try:
        configure_logging(_settings(log_file=str(path), service_name="support-api"))
        tracer = TracerProvider().get_tracer("t")
        with request_context("r1"), tracer.start_as_current_span("s") as span:
            bind_organisation("11111111-1111-1111-1111-111111111111")
            get_logger("t").info("hello_file")
            expected = format(span.get_span_context().trace_id, "032x")
        logging.getLogger("stdlib.thing").warning("from stdlib")
        for handler in _file_handlers():
            handler.flush()
        lines = [json.loads(line) for line in path.read_text().splitlines()]
    finally:
        _cleanup()
    first = next(line for line in lines if line["event"] == "hello_file")
    assert first["trace_id"] == expected
    assert first["service"] == "support-api" and first["level"] == "info"
    assert first["organisation_id"] == "11111111-1111-1111-1111-111111111111"
    assert any(line["event"] == "from stdlib" for line in lines)


def test_log_file_sink_rotates_10mb_x5_and_is_idempotent(tmp_path):
    path = tmp_path / "api.log"
    try:
        configure_logging(_settings(log_file=str(path)))
        configure_logging(_settings(log_file=str(path)))
        handlers = _file_handlers()
        assert len(handlers) == 1
        assert handlers[0].maxBytes == 10 * 1024 * 1024 and handlers[0].backupCount == 5
    finally:
        _cleanup()


def test_no_file_sink_by_default():
    configure_logging(_settings())
    assert _file_handlers() == []


def test_each_process_gets_its_own_log_file(tmp_path):
    from supportcore.observability.logging import process_log_path

    base = str(tmp_path / "worker.log")
    assert process_log_path(base, None) == tmp_path / "worker.log"
    assert process_log_path(base, 2) == tmp_path / "worker-2.log"
    try:
        configure_logging(_settings(log_file=base), process_index=1)
        get_logger("t").info("child_line")
        for handler in _file_handlers():
            handler.flush()
        assert (tmp_path / "worker-1.log").exists() and not (tmp_path / "worker.log").exists()
        configure_logging(_settings(log_file=base), process_index=3)  # reconfigure: old handler is replaced
        assert [h.baseFilename for h in _file_handlers()] == [str(tmp_path / "worker-3.log")]
    finally:
        _cleanup()


def test_unwritable_log_directory_falls_back_to_console(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    try:
        configure_logging(_settings(log_file=str(blocker / "logs" / "api.log")))
        assert _file_handlers() == []
    finally:
        _cleanup()
    out = capsys.readouterr().out
    assert out.count("log_file_unavailable") == 1
