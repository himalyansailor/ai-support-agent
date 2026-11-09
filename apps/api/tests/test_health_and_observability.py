import json
import logging
from uuid import uuid4

from fastapi import APIRouter, Depends
from supportcore.observability.context import bind_organisation
from supportcore.observability.tracing import get_test_span_exporter


async def test_healthz_ok(client):
    r = await client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


async def test_readyz_ok(client):
    r = await client.get("/readyz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


async def test_request_id_echoed_and_generated(client):
    r = await client.get("/healthz", headers={"X-Request-ID": "abc"})
    assert r.headers["X-Request-ID"] == "abc"
    r2 = await client.get("/healthz")
    assert len(r2.headers["X-Request-ID"]) >= 16


async def test_metrics_exposes_http_counter(client):
    await client.get("/healthz")
    body = (await client.get("/metrics")).text
    assert 'support_http_requests_total{method="GET",organisation_id="",route="/healthz",status="200"}' in body
    assert "support_http_request_duration_seconds_bucket" in body


async def test_span_recorded_for_request(client):
    await client.get("/healthz")
    spans = get_test_span_exporter().get_finished_spans()
    assert any(s.name.startswith("GET /healthz") for s in spans)


async def test_org_bound_in_dependency_reaches_metrics_and_span(app, client):
    org_id = uuid4()

    async def dep():
        bind_organisation(org_id)

    router = APIRouter()

    @router.get("/orgs/{org_id}/probe", dependencies=[Depends(dep)])
    async def probe(org_id: str):
        return {"ok": True}

    app.include_router(router)
    r = await client.get(f"/orgs/{org_id}/probe")
    assert r.status_code == 200
    body = (await client.get("/metrics")).text
    assert (
        f'support_http_requests_total{{method="GET",organisation_id="{org_id}",'
        'route="/orgs/{org_id}/probe",status="200"}' in body
    )
    spans = get_test_span_exporter().get_finished_spans()
    assert any(s.attributes.get("organisation.id") == str(org_id) for s in spans)


async def test_unmatched_path_uses_fixed_route_label(client):
    r = await client.get("/no/such/path/12345")
    assert r.status_code == 404
    body = (await client.get("/metrics")).text
    assert 'route="unmatched"' in body
    assert "/no/such/path/12345" not in body


async def test_stdlib_log_inside_request_is_json_with_request_id(app, client, capsys):
    @app.get("/_t/log")
    async def log_route():
        logging.getLogger("supportapi.test").warning("from stdlib")
        return {"ok": True}

    capsys.readouterr()
    r = await client.get("/_t/log", headers={"X-Request-ID": "req-log-1"})
    assert r.status_code == 200
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    line = next(x for x in lines if x["event"] == "from stdlib")
    assert line["request_id"] == "req-log-1"
    assert line["logger"] == "supportapi.test"
    assert line["trace_id"] is not None and line["span_id"] is not None


# --- search queries never reach logs or spans (final review I2) ----------------------------------------------


async def test_query_string_not_in_server_span(client):
    get_test_span_exporter().clear()
    await client.get(f"/orgs/{uuid4()}/inbox/conversations?q=SECRET-QUERY-7781&limit=5")
    spans = [s for s in get_test_span_exporter().get_finished_spans() if "/inbox/conversations" in s.name]
    assert spans
    for span in get_test_span_exporter().get_finished_spans():
        assert "SECRET-QUERY-7781" not in json.dumps(dict(span.attributes or {}), default=str)
        assert "SECRET-QUERY-7781" not in span.name


def test_uvicorn_access_log_has_no_query_string(app, capsys):
    """The uvicorn access line (its own format and args) is logged without the query string."""
    capsys.readouterr()
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", "/orgs/x/inbox/conversations?q=SECRET-QUERY-7782", "1.1", 200
    )
    logging.getLogger("uvicorn.error").info(
        '%s - "WebSocket %s" %s', "127.0.0.1:5000", "/ws?q=SECRET-QUERY-7783", "[accepted]"
    )
    out = capsys.readouterr().out
    assert "/orgs/x/inbox/conversations" in out and "/ws" in out
    assert "SECRET-QUERY" not in out
