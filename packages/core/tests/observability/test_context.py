from uuid import uuid4

from structlog.testing import capture_logs

from supportcore.observability.context import (
    bind_organisation,
    job_context,
    job_id_var,
    organisation_id_var,
    request_context,
    request_id_var,
)


def test_bind_outside_any_context_does_not_leak():
    with capture_logs() as logs:
        bind_organisation(uuid4())
    assert organisation_id_var() is None
    assert request_id_var() is None
    assert any(e["log_level"] == "warning" and "no active" in e["event"] for e in logs)


def test_request_context_sets_and_resets():
    org_id = uuid4()
    with request_context("r-1"):
        assert request_id_var() == "r-1" and organisation_id_var() is None
        bind_organisation(org_id)
        assert organisation_id_var() == str(org_id)
    assert request_id_var() is None and organisation_id_var() is None


def test_job_context_sets_and_resets():
    org_id, other = uuid4(), uuid4()
    with job_context(org_id, job_id="job-7"):
        assert organisation_id_var() == str(org_id)
        assert job_id_var() == "job-7"
        assert request_id_var() is None
        bind_organisation(other)
        assert organisation_id_var() == str(other)
    assert organisation_id_var() is None and job_id_var() is None


def test_job_context_without_org():
    with job_context(None):
        assert organisation_id_var() is None
    assert organisation_id_var() is None


def test_contexts_reset_on_error():
    try:
        with job_context(uuid4()):
            raise ValueError("boom")
    except ValueError:
        pass
    assert organisation_id_var() is None
