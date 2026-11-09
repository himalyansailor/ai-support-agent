"""Background job table: name -> handler. Queues (inline, Celery) call `dispatch`.

Payloads (JSON, UUIDs as str), as enqueued by kb_service / document_service:

- ensure_collection {kb_id, organisation_id, version}
- index_document    {job_id, organisation_id}
- rebuild_kb        {kb_id, organisation_id, version, job_id}
- purge_document    {organisation_id, kb_id, document_id, storage_key, collections}
- purge_kb          {organisation_id, kb_id, collections, storage_prefix}
- close_idle_conversations {}  (periodic, see supportcore.conversations.jobs)
- check_sla {}  (periodic, see supportcore.inbox.jobs)
- rollup_analytics {}  (periodic, see supportcore.analytics.jobs)
- sync_mailboxes {}  (periodic, see supportcore.mail.sync): enqueues sync_mailbox per due mailbox, then runs
  the outbound sweep (supportcore.mail.outbound.sweep_outbound)
- sync_mailbox {organisation_id, mailbox_id}  (also "Check now")
- send_email {organisation_id, email_message_id}  (see supportcore.mail.outbound): one send attempt of an agent's
  email reply; re-enqueues itself with a countdown after a transient failure
- finalise_stale_calls {}  (periodic, see supportcore.voice.jobs): finalises voice calls whose stream end or
  status callback never came
- start_call_recording {organisation_id, call_id, status_callback?}  (see supportcore.voice.recordings): starts Twilio's recording
- fetch_call_recording {organisation_id, call_id, recording_sid, attempt?, recording_duration?}: copies the finished recording into
  storage, then deletes it on Twilio; re-enqueues itself with a countdown after a failed download
- purge_call_recordings {}  (periodic, daily): deletes the expired recordings
- sync_billing_plan {plan_id}  (see supportcore.billing.plans): pushes a plan's prices to the billing provider
- report_usage {}  (periodic, see supportcore.billing.reporting): sends overage usage to the billing provider
- reconcile_usage_counters {}  (periodic, hourly, see supportcore.billing.usage): resets the usage counters to the ledger
- billing_alerts {}  (periodic, see supportcore.billing.alerts): emails usage, trial and payment warnings

Every job runs inside `job_context(organisation_id, job_id)` (jobs without an IndexingJob row get a
fresh id for log correlation) and opens its own sessions. index_document and rebuild_kb record
failures on the job and never raise; the others raise so the queue reports them.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from opentelemetry import trace

from supportcore.analytics.jobs import rollup_analytics
from supportcore.billing.alerts import run_alerts
from supportcore.billing.plans import sync_plan_job
from supportcore.billing.reporting import report_usage
from supportcore.billing.usage import reconcile_counters
from supportcore.conversations.jobs import close_idle_conversations
from supportcore.db.models import KnowledgeBase
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.inbox.jobs import check_sla
from supportcore.jobs.ports import JobRuntime, UnknownJobError
from supportcore.knowledge.indexing import IndexingService
from supportcore.knowledge.kb_service import (
    collection_name,
    kb_storage_prefix,
    version_config,
)
from supportcore.mail.outbound import send_email, sweep_outbound
from supportcore.mail.sync import sync_mailbox, sync_mailboxes
from supportcore.observability.context import job_context
from supportcore.observability.logging import get_logger
from supportcore.voice import jobs as voice_jobs
from supportcore.voice import recordings as voice_recordings

logger = get_logger(__name__)
tracer = trace.get_tracer("supportcore.jobs")

Handler = Callable[[JobRuntime, dict], Awaitable[None]]

# How often stale-job recovery runs: celery beat, each worker pool process, the API with inline jobs.
RECOVERY_INTERVAL_SECONDS = 300


class InvalidJobPayloadError(ValueError):
    """A payload names resources outside its organisation / knowledge base."""


async def _ensure_collection(rt: JobRuntime, payload: dict) -> None:
    org, kb_id, version = UUID(payload["organisation_id"]), UUID(payload["kb_id"]), int(payload["version"])
    async with rt.session_factory() as s:
        kb = await TenantScopedRepository(s, KnowledgeBase, org).get(kb_id)
        if kb is None:
            logger.info("ensure_collection_kb_gone", knowledge_base_id=str(kb_id))
            return
        try:
            dims = version_config(kb, version).dimensions
        except ValueError:  # superseded meanwhile (e.g. dimensions changed again)
            logger.info("ensure_collection_version_gone", knowledge_base_id=str(kb_id), kb_version=version)
            return
    await rt.adapters.vector_store.ensure_collection(
        collection_name(kb_id, version), dims, organisation_id=org, knowledge_base_id=kb_id
    )


async def _index_document(rt: JobRuntime, payload: dict) -> None:
    await IndexingService(rt).index_document(UUID(payload["job_id"]), UUID(payload["organisation_id"]))


async def _rebuild_kb(rt: JobRuntime, payload: dict) -> None:
    await IndexingService(rt).rebuild_kb(
        UUID(payload["kb_id"]), int(payload["version"]), UUID(payload["organisation_id"]), UUID(payload["job_id"])
    )


def _check_collections(kb_id: UUID, names: list[str]) -> list[str]:
    prefix = f"kb_{kb_id.hex}_v"
    for name in names:
        if not (name.startswith(prefix) and name.removeprefix(prefix).isdigit()):
            raise InvalidJobPayloadError("Collection does not belong to the knowledge base")
    return names


async def _purge_document(rt: JobRuntime, payload: dict) -> None:
    org, kb_id, document_id = UUID(payload["organisation_id"]), UUID(payload["kb_id"]), UUID(payload["document_id"])
    key = payload["storage_key"]
    if not key.startswith(kb_storage_prefix(org, kb_id) + "documents/"):
        raise InvalidJobPayloadError("Storage key does not belong to the knowledge base")
    for name in _check_collections(kb_id, payload["collections"]):
        await rt.adapters.vector_store.delete_documents(name, [document_id], organisation_id=org)
    await rt.adapters.storage.delete(key)


async def _purge_kb(rt: JobRuntime, payload: dict) -> None:
    org, kb_id = UUID(payload["organisation_id"]), UUID(payload["kb_id"])
    prefix = payload["storage_prefix"]
    if prefix != kb_storage_prefix(org, kb_id):
        raise InvalidJobPayloadError("Storage prefix does not belong to the knowledge base")
    for name in _check_collections(kb_id, payload["collections"]):
        await rt.adapters.vector_store.drop_collection(name, organisation_id=org)
    deleted = await rt.adapters.storage.delete_prefix(prefix)
    logger.info("knowledge_base_purged", knowledge_base_id=str(kb_id), files_deleted=deleted)


async def _close_idle_conversations(rt: JobRuntime, payload: dict) -> None:
    await close_idle_conversations(rt)


async def _check_sla(rt: JobRuntime, payload: dict) -> None:
    await check_sla(rt, datetime.now(UTC))


async def _rollup_analytics(rt: JobRuntime, payload: dict) -> None:
    await rollup_analytics(rt, datetime.now(UTC))


async def _sync_mailboxes(rt: JobRuntime, payload: dict) -> None:
    now = datetime.now(UTC)
    try:
        await sync_mailboxes(rt, now)
    finally:  # the outbound sweep runs on the same tick, even when the fan-out failed
        await sweep_outbound(rt, now)


async def _finalise_stale_calls(rt: JobRuntime, payload: dict) -> None:
    await voice_jobs.finalise_stale_calls(rt)


async def _start_call_recording(rt: JobRuntime, payload: dict) -> None:
    await voice_recordings.start_call_recording(
        rt, UUID(payload["organisation_id"]), UUID(payload["call_id"]), payload.get("status_callback")
    )


async def _fetch_call_recording(rt: JobRuntime, payload: dict) -> None:
    await voice_recordings.fetch_call_recording(
        rt,
        UUID(payload["organisation_id"]),
        UUID(payload["call_id"]),
        payload["recording_sid"],
        int(payload.get("attempt", 1)),
        int(payload["recording_duration"]) if payload.get("recording_duration") is not None else None,
    )


async def _purge_call_recordings(rt: JobRuntime, payload: dict) -> None:
    await voice_recordings.purge_call_recordings(rt)


async def _sync_mailbox(rt: JobRuntime, payload: dict) -> None:
    await sync_mailbox(rt, UUID(payload["organisation_id"]), UUID(payload["mailbox_id"]), datetime.now(UTC))


async def _send_email(rt: JobRuntime, payload: dict) -> None:
    await send_email(rt, UUID(payload["organisation_id"]), UUID(payload["email_message_id"]), datetime.now(UTC))


async def _sync_billing_plan(rt: JobRuntime, payload: dict) -> None:
    await sync_plan_job(rt, UUID(payload["plan_id"]))


async def _report_usage(rt: JobRuntime, payload: dict) -> None:
    await report_usage(rt, datetime.now(UTC))


async def _reconcile_usage_counters(rt: JobRuntime, payload: dict) -> None:
    await reconcile_counters(rt, datetime.now(UTC))


async def _billing_alerts(rt: JobRuntime, payload: dict) -> None:
    await run_alerts(rt, datetime.now(UTC))


JOB_HANDLERS: dict[str, Handler] = {
    "ensure_collection": _ensure_collection,
    "index_document": _index_document,
    "rebuild_kb": _rebuild_kb,
    "purge_document": _purge_document,
    "purge_kb": _purge_kb,
    "close_idle_conversations": _close_idle_conversations,
    "check_sla": _check_sla,
    "rollup_analytics": _rollup_analytics,
    "sync_mailboxes": _sync_mailboxes,
    "sync_mailbox": _sync_mailbox,
    "send_email": _send_email,
    "finalise_stale_calls": _finalise_stale_calls,
    "start_call_recording": _start_call_recording,
    "fetch_call_recording": _fetch_call_recording,
    "purge_call_recordings": _purge_call_recordings,
    "sync_billing_plan": _sync_billing_plan,
    "report_usage": _report_usage,
    "reconcile_usage_counters": _reconcile_usage_counters,
    "billing_alerts": _billing_alerts,
}


async def fail_job(runtime: JobRuntime, name: str, payload: dict, message: str) -> None:
    """Records that job `name` could not finish (e.g. the queue's time limit hit it outside the job's
    own error handling). Only index_document and rebuild_kb have a job row to record it on."""
    if name not in ("index_document", "rebuild_kb"):
        return
    org = UUID(payload["organisation_id"])
    with job_context(org, str(payload["job_id"])):
        await IndexingService(runtime).fail_interrupted(UUID(payload["job_id"]), org, message)


async def recover_stale_jobs(runtime: JobRuntime) -> int:
    """Fails jobs left `running` by a worker that died and sends again jobs whose message was lost
    (see IndexingService.recover_stale_jobs / requeue_lost_jobs). Returns how many jobs it touched."""
    with job_context(None, "recover_stale_jobs"):
        service = IndexingService(runtime)
        recovered = await service.recover_stale_jobs()
        if recovered:
            logger.warning("indexing_stale_jobs_recovered", jobs=recovered)
        return recovered + await service.requeue_lost_jobs()


async def dispatch(runtime: JobRuntime, name: str, payload: dict) -> None:
    """Runs job `name` in its organisation's job context. Raises UnknownJobError for unknown names."""
    handler = JOB_HANDLERS.get(name)
    if handler is None:
        raise UnknownJobError(name)
    org = payload.get("organisation_id")
    organisation_id = UUID(org) if org else None
    job_id = str(payload.get("job_id") or uuid4())
    with job_context(organisation_id, job_id):
        with tracer.start_as_current_span(f"job {name}", record_exception=False) as span:
            span.set_attribute("job.name", name)
            span.set_attribute("job.id", job_id)
            if organisation_id is not None:
                span.set_attribute("organisation.id", str(organisation_id))
            try:
                await handler(runtime, payload)
            except Exception as exc:
                span.set_status(trace.Status(trace.StatusCode.ERROR, type(exc).__name__))
                logger.warning("job_failed", job_name=name, error_type=type(exc).__name__)
                raise
