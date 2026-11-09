"""Per-request / per-job context shared by logging, metrics and tracing.

A single mutable dict is stored in a contextvar by request_context() (HTTP middleware) or
job_context() (workers). Because the dict is shared (not re-assigned), values bound deep inside
FastAPI dependencies are visible to the middleware when the response ends. Both context managers
reset the contextvar on exit, so nothing outlives the request or job.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import UUID

import structlog
from opentelemetry import trace

_request_state: ContextVar[dict[str, Any] | None] = ContextVar("request_state", default=None)


@contextmanager
def _state(state: dict[str, Any]) -> Iterator[None]:
    token = _request_state.set(state)
    try:
        yield
    finally:
        _request_state.reset(token)


@contextmanager
def request_context(request_id: str) -> Iterator[None]:
    """Context for one HTTP request; the tenant is bound later with bind_organisation()."""
    with _state({"request_id": request_id, "job_id": None, "organisation_id": None}):
        yield


@contextmanager
def job_context(organisation_id: UUID | None, job_id: str | None = None) -> Iterator[None]:
    """Context for one background job (e.g. a Celery task) working for organisation_id."""
    org = str(organisation_id) if organisation_id is not None else None
    with _state({"request_id": None, "job_id": job_id, "organisation_id": org}):
        yield


def _get(key: str) -> str | None:
    state = _request_state.get()
    return state.get(key) if state else None


def request_id_var() -> str | None:
    return _get("request_id")


def job_id_var() -> str | None:
    return _get("job_id")


def organisation_id_var() -> str | None:
    return _get("organisation_id")


def bind_organisation(org_id: UUID) -> None:
    """Record the tenant for the active request/job in the context and on the current span.

    Outside request_context()/job_context() this is a no-op (plus a warning): creating state here
    would outlive the caller and tag unrelated work with this tenant.
    """
    state = _request_state.get()
    if state is None:
        structlog.get_logger(__name__).warning(
            "bind_organisation called with no active request or job context; ignored",
            bound_organisation_id=str(org_id),
        )
        return
    state["organisation_id"] = str(org_id)
    span = trace.get_current_span()
    if span.get_span_context().is_valid:
        span.set_attribute("organisation.id", str(org_id))
