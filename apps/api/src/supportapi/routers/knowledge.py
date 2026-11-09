"""Knowledge bases and their indexing job log (read: kb.manage or bot.configure, write: kb.manage)."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from supportapi.container import Container
from supportapi.deps import (
    OrgContext,
    SessionDep,
    build_kb_service,
    get_container,
    require_any_permission,
    require_permission,
)
from supportapi.pagination import decode_cursor, encode_cursor
from supportapi.schemas.knowledge import (
    ChunkingOut,
    CreateKnowledgeBaseRequest,
    DocumentCountsOut,
    EmbeddingOut,
    JobOut,
    JobPage,
    KnowledgeBaseOut,
    KnowledgeBaseUpdatedOut,
    RerankerOut,
    UpdateKnowledgeBaseRequest,
)
from supportcore.db.models import Document, IndexingJob, KnowledgeBase
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.knowledge.chunking.ports import ChunkingParams
from supportcore.knowledge.kb_service import EmbeddingChoice
from supportcore.rbac.permissions import Permission

router = APIRouter(tags=["knowledge"])

READ = Permission.KB_MANAGE, Permission.BOT_CONFIGURE
DEFAULT_CHUNKING = {"strategy": "recursive", "chunk_size": 1000, "chunk_overlap": 150}


async def document_counts(
    session: AsyncSession, organisation_id: UUID, kb_ids: list[UUID]
) -> dict[UUID, DocumentCountsOut]:
    """Per-KB document counts by status from one grouped query."""
    counts = {kb_id: DocumentCountsOut() for kb_id in kb_ids}
    if not kb_ids:
        return counts
    stmt = (
        TenantScopedRepository(session, Document, organisation_id)
        .select(Document.knowledge_base_id, Document.status, func.count())
        .where(Document.knowledge_base_id.in_(kb_ids))
        .group_by(Document.knowledge_base_id, Document.status)
    )
    for kb_id, status, n in (await session.execute(stmt)).all():
        setattr(counts[kb_id], status, n)
    return counts


def kb_out(kb: KnowledgeBase, counts: DocumentCountsOut) -> KnowledgeBaseOut:
    return KnowledgeBaseOut(
        id=kb.id,
        name=kb.name,
        description=kb.description,
        embedding=EmbeddingOut(
            provider=kb.embedding_provider,
            model=kb.embedding_model,
            dimensions=kb.embedding_dimensions,
            credential_id=kb.embedding_credential_id,
        ),
        chunking=ChunkingOut(**kb.chunking),
        reranker=RerankerOut(name=kb.reranker, params=kb.reranker_params),
        active_version=kb.active_version,
        building_version=kb.building_version,
        document_counts=counts,
        created_at=kb.created_at,
    )


async def _single_out(session: AsyncSession, kb: KnowledgeBase) -> KnowledgeBaseOut:
    counts = await document_counts(session, kb.organisation_id, [kb.id])
    return kb_out(kb, counts[kb.id])


@router.get("/orgs/{org_id}/kbs", response_model=list[KnowledgeBaseOut])
async def list_kbs(
    session: SessionDep,
    ctx: OrgContext = Depends(require_any_permission(*READ)),
    container: Container = Depends(get_container),
) -> list[KnowledgeBaseOut]:
    kbs = await build_kb_service(session, ctx.organisation.id, container).list()
    counts = await document_counts(session, ctx.organisation.id, [kb.id for kb in kbs])
    return [kb_out(kb, counts[kb.id]) for kb in kbs]


@router.post("/orgs/{org_id}/kbs", response_model=KnowledgeBaseOut, status_code=201)
async def create_kb(
    session: SessionDep,
    body: CreateKnowledgeBaseRequest,
    ctx: OrgContext = Depends(require_permission(Permission.KB_MANAGE)),
    container: Container = Depends(get_container),
) -> KnowledgeBaseOut:
    chunking = ChunkingParams(
        **{**DEFAULT_CHUNKING, **body.chunking.model_dump(exclude_none=True)}
    )  # InvalidChunkingParamsError -> 422
    kb = await build_kb_service(session, ctx.organisation.id, container).create(
        body.name,
        body.description,
        body.embedding.provider,
        body.embedding.model,
        body.embedding.credential_id,
        chunking,
        body.reranker.name or "none",
        body.reranker.params or {},
        ctx.user,
    )
    await session.refresh(kb)  # server-generated timestamps
    return kb_out(kb, DocumentCountsOut())


@router.get("/orgs/{org_id}/kbs/{kb_id}", response_model=KnowledgeBaseOut)
async def get_kb(
    session: SessionDep,
    kb_id: UUID,
    ctx: OrgContext = Depends(require_any_permission(*READ)),
    container: Container = Depends(get_container),
) -> KnowledgeBaseOut:
    kb = await build_kb_service(session, ctx.organisation.id, container).get(kb_id)
    return await _single_out(session, kb)


@router.patch("/orgs/{org_id}/kbs/{kb_id}", response_model=KnowledgeBaseUpdatedOut)
async def update_kb(
    session: SessionDep,
    kb_id: UUID,
    body: UpdateKnowledgeBaseRequest,
    ctx: OrgContext = Depends(require_permission(Permission.KB_MANAGE)),
    container: Container = Depends(get_container),
) -> KnowledgeBaseUpdatedOut:
    """Only fields present in the body are passed on, so re-sending unchanged values never rebuilds
    and a pending rebuild does not block a name/reranker change."""
    service = build_kb_service(session, ctx.organisation.id, container)
    sent = body.model_fields_set
    changes: dict = {}
    if "name" in sent:
        changes["name"] = body.name
    if "description" in sent:
        changes["description"] = body.description
    if body.embedding is not None:
        e = body.embedding
        changes["embedding"] = EmbeddingChoice(e.provider, e.model, e.credential_id)
    if body.chunking is not None:
        changes["chunking"] = body.chunking.model_dump(exclude_none=True)  # merged by the service, under the KB lock
    if body.reranker is not None:
        if "name" in body.reranker.model_fields_set and body.reranker.name is not None:
            changes["reranker"] = body.reranker.name
        if "params" in body.reranker.model_fields_set and body.reranker.params is not None:
            changes["reranker_params"] = body.reranker.params
    kb, rebuild_started = await service.update(kb_id, actor=ctx.user, **changes)
    out = await _single_out(session, kb)
    return KnowledgeBaseUpdatedOut(**out.model_dump(), rebuild_started=rebuild_started)


@router.delete("/orgs/{org_id}/kbs/{kb_id}", status_code=204)
async def delete_kb(
    session: SessionDep,
    kb_id: UUID,
    ctx: OrgContext = Depends(require_permission(Permission.KB_MANAGE)),
    container: Container = Depends(get_container),
) -> Response:
    await build_kb_service(session, ctx.organisation.id, container).delete(kb_id, ctx.user)
    return Response(status_code=204)


@router.get("/orgs/{org_id}/kbs/{kb_id}/jobs", response_model=JobPage)
async def list_jobs(
    session: SessionDep,
    kb_id: UUID,
    limit: int = Query(50, ge=1, le=100),
    before: str | None = Query(None, max_length=200, description="next_before from the previous page"),
    ctx: OrgContext = Depends(require_any_permission(*READ)),
    container: Container = Depends(get_container),
) -> JobPage:
    await build_kb_service(session, ctx.organisation.id, container).get(kb_id)  # 404 for a foreign KB
    stmt = (
        TenantScopedRepository(session, IndexingJob, ctx.organisation.id)
        .select(IndexingJob, Document.filename)
        .outerjoin(Document, Document.id == IndexingJob.document_id)
        .where(IndexingJob.knowledge_base_id == kb_id)
        .order_by(IndexingJob.queued_at.desc(), IndexingJob.id.desc())
        .limit(limit + 1)
    )
    if before is not None:
        queued_at, id = decode_cursor(before)
        stmt = stmt.where(tuple_(IndexingJob.queued_at, IndexingJob.id) < tuple_(queued_at, id))
    rows = (await session.execute(stmt)).all()
    items = [
        JobOut(
            id=job.id,
            kind=job.kind,
            document_id=job.document_id,
            document_name=filename,
            status=job.status,
            kb_version=job.kb_version,
            attempts=job.attempts,
            error=job.error,
            steps=job.steps,
            queued_at=job.queued_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )
        for job, filename in rows[:limit]
    ]
    next_before = encode_cursor(items[-1].queued_at, items[-1].id) if len(rows) > limit else None
    return JobPage(items=items, next_before=next_before)
