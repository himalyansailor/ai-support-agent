import json
import logging
from uuid import uuid4

from supportcore.config import Settings
from supportcore.observability.context import bind_organisation, job_context, request_context
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


def _lines(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.strip().splitlines() if line.startswith("{")]


def test_log_line_includes_context(capsys):
    configure_logging(_settings())
    org_id = uuid4()
    with request_context("r1"):
        bind_organisation(org_id)
        get_logger("t").info("hi")
    line = _lines(capsys)[-1]
    assert line["event"] == "hi"
    assert line["logger"] == "t"
    assert line["request_id"] == "r1" and line["organisation_id"] == str(org_id)
    assert "trace_id" in line and "span_id" in line


def test_stdlib_logging_is_json_with_context(capsys):
    configure_logging(_settings())
    org_id = uuid4()
    with request_context("r-std"):
        bind_organisation(org_id)
        logging.getLogger("x").warning("hello %s", "world")
    line = _lines(capsys)[-1]
    assert line["event"] == "hello world"
    assert line["logger"] == "x"
    assert line["level"] == "warning"
    assert line["request_id"] == "r-std" and line["organisation_id"] == str(org_id)
    assert "trace_id" in line and "span_id" in line and "timestamp" in line


def test_stdlib_exception_and_extras_rendered(capsys):
    configure_logging(_settings())
    with job_context(uuid4(), job_id="j1"):
        try:
            raise ValueError("boom")
        except ValueError:
            logging.getLogger("y").exception("failed", extra={"handler": "h"})
    line = _lines(capsys)[-1]
    assert line["event"] == "failed" and line["job_id"] == "j1" and line["handler"] == "h"
    assert "ValueError: boom" in line["exception"]


def test_log_level_setting_is_honoured(capsys):
    configure_logging(_settings(log_level="ERROR"))
    logging.getLogger("x").warning("quiet")
    get_logger("t").warning("quiet too")
    logging.getLogger("x").error("loud")
    assert [line["event"] for line in _lines(capsys)] == ["loud"]
    configure_logging(_settings())


def test_configure_logging_is_idempotent(capsys):
    configure_logging(_settings())
    configure_logging(_settings())
    logging.getLogger("x").warning("once")
    assert [line["event"] for line in _lines(capsys)] == ["once"]


def test_uvicorn_color_message_extra_is_dropped(capsys):
    configure_logging(_settings())
    logging.getLogger("uvicorn.error").info("up %s", "x", extra={"color_message": "\x1b[1mup\x1b[0m"})
    line = _lines(capsys)[-1]
    assert line["event"] == "up x" and "color_message" not in line


def test_twilio_sdk_request_logs_are_kept_quiet(capsys):
    """The Twilio SDK logs each REST request's URL (recording resources, search params) at INFO."""
    configure_logging(_settings(log_level="INFO"))
    logging.getLogger("twilio.http_client").info("-- BEGIN Twilio API Request --")
    logging.getLogger("twilio.http_client").warning("kept")
    assert [line["event"] for line in _lines(capsys)] == ["kept"]
    configure_logging(_settings())
