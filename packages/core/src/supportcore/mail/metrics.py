"""Prometheus metrics of the email sync. Labels are the organisation id and short outcome codes only (never an
address, subject or any other mail content)."""

from prometheus_client import Counter, Histogram

# outcome: ok | busy | auth_failed | error.
EMAIL_SYNC_RUNS = Counter(
    "support_email_sync_runs_total",
    "Mailbox sync runs, by outcome",
    ["organisation_id", "outcome"],
)
# outcome: ingested | duplicate | a skip reason (auto_submitted, bulk, autoreply_header, bounce, own_address,
# own_message, rate_limited, too_large, unparseable).
EMAIL_INBOUND = Counter(
    "support_email_inbound_total",
    "Inbound emails handled by the sync, by outcome",
    ["organisation_id", "outcome"],
)
EMAIL_SYNC_DURATION = Histogram(
    "support_email_sync_duration_seconds",
    "Mailbox sync run duration in seconds",
    ["organisation_id"],
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)


def record_sync_run(organisation_id: str, outcome: str, seconds: float) -> None:
    EMAIL_SYNC_RUNS.labels(organisation_id=organisation_id, outcome=outcome).inc()
    EMAIL_SYNC_DURATION.labels(organisation_id=organisation_id).observe(seconds)


def record_inbound(organisation_id: str, outcome: str) -> None:
    EMAIL_INBOUND.labels(organisation_id=organisation_id, outcome=outcome).inc()


# outcome: sent | retry | failed | ambiguous.
EMAIL_OUTBOUND = Counter(
    "support_email_outbound_total",
    "Outbound email send attempts, by outcome",
    ["organisation_id", "outcome"],
)


def record_outbound(organisation_id: str, outcome: str) -> None:
    EMAIL_OUTBOUND.labels(organisation_id=organisation_id, outcome=outcome).inc()
