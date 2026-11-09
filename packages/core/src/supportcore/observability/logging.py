"""JSON logging for structlog AND the stdlib (uvicorn, SQLAlchemy, EventBus, Celery...).

Everything goes through one stdlib handler whose structlog ProcessorFormatter adds the same
context (request_id, job_id, trace_id, span_id, organisation_id) to every line.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog
from opentelemetry import trace

from supportcore.config import Settings
from supportcore.observability.context import job_id_var, organisation_id_var, request_id_var

# Loggers that ship their own handlers; they are re-routed through the root handler.
_THIRD_PARTY_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


# Libraries that log request URLs at INFO. Tool arguments travel in those URLs (and the Twilio SDK's carry recording
# resources and number-search params), so keep them at WARNING+.
_QUIET_LOGGERS = ("httpx", "httpcore", "twilio")


def _strip_query(value: object) -> object:
    if isinstance(value, str) and value.startswith("/") and "?" in value:
        return value.split("?", 1)[0]
    return value


class _RedactQueryStrings(logging.Filter):
    """Drops the query string from the request paths uvicorn logs (the access line and WebSocket lines):
    query strings carry search queries, which must never be logged."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_strip_query(arg) for arg in record.args)
        return True


_REDACT_QUERY_STRINGS = _RedactQueryStrings()


def _inject_context(_logger, _method, event_dict):
    event_dict["request_id"] = request_id_var()
    event_dict["organisation_id"] = organisation_id_var()
    if (job_id := job_id_var()) is not None:
        event_dict["job_id"] = job_id
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    else:
        event_dict["trace_id"] = None
        event_dict["span_id"] = None
    return event_dict


def _drop_color_message(_logger, _method, event_dict):
    event_dict.pop("color_message", None)  # uvicorn's ANSI-coloured duplicate of the message
    return event_dict


class _StdoutHandler(logging.StreamHandler):
    """Writes to whatever sys.stdout is at emit time (survives stream swaps, e.g. test capture)."""

    def __init__(self) -> None:
        super().__init__(sys.stdout)

    @property
    def stream(self):  # type: ignore[override]
        return sys.stdout

    @stream.setter
    def stream(self, _value) -> None:
        pass


class _FileHandler(RotatingFileHandler):
    """Marker type so configure_logging can replace its own file sink without touching others."""


LOG_FILE_MAX_BYTES = 10 * 1024 * 1024
LOG_FILE_BACKUPS = 5


def _add_service(service: str):
    def add_service(_logger, _method, event_dict):
        event_dict["service"] = service  # a Loki label (bounded: one value per deployable)
        return event_dict

    return add_service


def _shared_processors(service: str) -> list:
    return [
        _add_service(service),
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]


def process_log_path(log_file: str, process_index: int | str | None) -> Path:
    """One file per process: `worker.log` stays the main process's, pool child 2 writes `worker-2.log`.
    The index is the pool slot (stable across respawns), so the set of names stays bounded."""
    path = Path(log_file)
    if process_index is None:
        return path
    return path.with_name(f"{path.stem}-{process_index}{path.suffix}")


def configure_logging(settings: Settings, process_index: int | str | None = None) -> None:
    """`process_index`: set by processes that share a service name (Celery pool children) so that no two
    processes ever hold the same RotatingFileHandler file (a rollover renames the file under the others)."""
    level = logging.getLevelNamesMapping()[settings.log_level]

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *_shared_processors(settings.service_name),
            _inject_context,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[
            *_shared_processors(settings.service_name),
            structlog.stdlib.ExtraAdder(),
            _drop_color_message,
            _inject_context,
        ],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(default=str),
        ],
    )

    root = logging.getLogger()
    for handler in [h for h in root.handlers if isinstance(h, _StdoutHandler)]:
        root.removeHandler(handler)  # idempotent; leaves other handlers (e.g. pytest's) alone
    handler = _StdoutHandler()
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.setLevel(level)

    for old in [h for h in root.handlers if isinstance(h, _FileHandler)]:
        root.removeHandler(old)
        old.close()
    if settings.log_file:
        path = process_log_path(settings.log_file, process_index)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = _FileHandler(
                path, maxBytes=LOG_FILE_MAX_BYTES, backupCount=LOG_FILE_BACKUPS, encoding="utf-8"
            )
        except OSError as exc:  # unwritable or root-owned directory: keep logging to the console
            get_logger(__name__).warning("log_file_unavailable", path=str(path), error_type=type(exc).__name__)
        else:
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)

    for name in _THIRD_PARTY_LOGGERS:
        third_party = logging.getLogger(name)
        third_party.handlers.clear()
        third_party.propagate = True
        if _REDACT_QUERY_STRINGS not in third_party.filters:
            third_party.addFilter(_REDACT_QUERY_STRINGS)
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))


def get_logger(name: str):
    return structlog.get_logger(name)
