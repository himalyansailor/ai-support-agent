"""Links from the platform admin pages to the observability tools. Every variable part is URL-encoded."""

from urllib.parse import quote, urlencode
from uuid import UUID

GRAFANA_PLATFORM_DASHBOARD = "support-platform"  # infra/grafana/provisioning/dashboards/platform-overview.json


def _base(url: str) -> str:
    return url.rstrip("/")


def grafana_url(base: str) -> str:
    return f"{_base(base)}/d/{GRAFANA_PLATFORM_DASHBOARD}"


def grafana_org_url(base: str, organisation_id: UUID) -> str:
    """The platform dashboard with its `org` variable (var-org) set to the organisation."""
    return f"{grafana_url(base)}?{urlencode({'var-org': str(organisation_id)})}"


def jaeger_url(base: str, service_name: str) -> str:
    return f"{_base(base)}/search?{urlencode({'service': service_name})}"


def langfuse_url(host: str | None, project_id: str | None) -> str | None:
    if not host:
        return None
    return f"{_base(host)}/project/{quote(project_id, safe='')}/traces" if project_id else _base(host)


def langfuse_org_url(host: str | None, project_id: str | None, organisation_id: UUID) -> str | None:
    """The project's traces filtered to the tag `organisation:<id>` the tracer puts on every trace
    (supportcore.llmtrace.langfuse_tracer). Needs the project id: Langfuse's UI routes are per project."""
    if not host or not project_id:
        return None
    flt = f"tags;arrayOptions;;any of;organisation:{organisation_id}"
    return f"{langfuse_url(host, project_id)}?{urlencode({'filter': flt})}"
