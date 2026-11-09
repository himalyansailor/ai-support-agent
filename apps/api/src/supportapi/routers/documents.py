"""Documents of a knowledge base (read: kb.manage or bot.configure, write: kb.manage)."""

from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile

from supportapi.container import Container
from supportapi.deps import (
    OrgContext,
    SessionDep,
    build_document_service,
    get_container,
    require_any_permission,
    require_permission,
)
from supportapi.schemas.knowledge import (
    ChunkOut,
    ChunkPage,
    DocumentOut,
    IndexDocumentsRequest,
    JobRefOut,
    JobsStartedOut,
)
from supportcore.db.models import Document, IndexingJob
from supportcore.errors import NotFoundError
from supportcore.knowledge.document_service import DocumentService, UploadTooLargeError
from supportcore.rbac.permissions import Permission

router = APIRouter(tags=["documents"])

READ = Permission.KB_MANAGE, Permission.BOT_CONFIGURE
READ_CHUNK_BYTES = 1024 * 1024


def document_out(d: Document) -> DocumentOut:
    return DocumentOut(
        id=d.id,
        filename=d.filename,
        file_type=d.file_type,
        size_bytes=d.size_bytes,
        status=d.status,
        error=d.error,
        chunk_count=d.chunk_count,
        indexed_version=d.indexed_version,
        uploaded_at=d.created_at,
        updated_at=d.updated_at,
    )


def _jobs_out(jobs: list[IndexingJob]) -> JobsStartedOut:
    return JobsStartedOut(jobs=[JobRefOut(id=j.id, document_id=j.document_id) for j in jobs])


async def _document_of_kb(service: DocumentService, kb_id: UUID, document_id: UUID) -> Document:
    """The document, 404 unless it belongs to the knowledge base named in the URL."""
    document = await service.get(document_id)
    if document.knowledge_base_id != kb_id:
        raise NotFoundError("Document not found")
    return document


async def _read_capped(file: UploadFile, max_bytes: int) -> bytes:
    """Reads the upload in chunks, giving up as soon as it is larger than max_bytes (never buffers more
    than max_bytes + 1)."""
    data = bytearray()
    while len(data) <= max_bytes:
        chunk = await file.read(min(READ_CHUNK_BYTES, max_bytes + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    if len(data) > max_bytes:
        raise UploadTooLargeError(max_bytes)
    return bytes(data)


@router.post("/orgs/{org_id}/kbs/{kb_id}/documents", response_model=DocumentOut, status_code=201)
async def upload_document(
    session: SessionDep,
    kb_id: UUID,
    file: UploadFile = File(...),
    ctx: OrgContext = Depends(require_permission(Permission.KB_MANAGE)),
    container: Container = Depends(get_container),
) -> DocumentOut:
    data = await _read_capped(file, container.settings.max_upload_bytes)
    if not data:
        raise HTTPException(status_code=422, detail="The uploaded file is empty")
    service = build_document_service(session, ctx.organisation.id, container)
    document = await service.upload(kb_id, file.filename or "", data, ctx.user)
    await session.refresh(document)  # server-generated timestamps
    return document_out(document)


@router.get("/orgs/{org_id}/kbs/{kb_id}/documents", response_model=list[DocumentOut])
async def list_documents(
    session: SessionDep,
    kb_id: UUID,
    status: str | None = Query(None, max_length=32),
    ctx: OrgContext = Depends(require_any_permission(*READ)),
    container: Container = Depends(get_container),
) -> list[DocumentOut]:
    documents = await build_document_service(session, ctx.organisation.id, container).list(kb_id, status)
    return [document_out(d) for d in documents]


@router.post("/orgs/{org_id}/kbs/{kb_id}/documents/index", response_model=JobsStartedOut, status_code=202)
async def start_indexing(
    session: SessionDep,
    kb_id: UUID,
    body: IndexDocumentsRequest,
    ctx: OrgContext = Depends(require_permission(Permission.KB_MANAGE)),
    container: Container = Depends(get_container),
) -> JobsStartedOut:
    service = build_document_service(session, ctx.organisation.id, container)
    return _jobs_out(await service.start_indexing(kb_id, body.document_ids, ctx.user))


@router.get("/orgs/{org_id}/kbs/{kb_id}/documents/{doc_id}", response_model=DocumentOut)
async def get_document(
    session: SessionDep,
    kb_id: UUID,
    doc_id: UUID,
    ctx: OrgContext = Depends(require_any_permission(*READ)),
    container: Container = Depends(get_container),
) -> DocumentOut:
    service = build_document_service(session, ctx.organisation.id, container)
    return document_out(await _document_of_kb(service, kb_id, doc_id))


@router.delete("/orgs/{org_id}/kbs/{kb_id}/documents/{doc_id}", status_code=204)
async def delete_document(
    session: SessionDep,
    kb_id: UUID,
    doc_id: UUID,
    ctx: OrgContext = Depends(require_permission(Permission.KB_MANAGE)),
    container: Container = Depends(get_container),
) -> Response:
    service = build_document_service(session, ctx.organisation.id, container)
    await _document_of_kb(service, kb_id, doc_id)
    await service.delete(doc_id, ctx.user)
    return Response(status_code=204)


@router.post("/orgs/{org_id}/kbs/{kb_id}/documents/{doc_id}/reindex", response_model=JobsStartedOut, status_code=202)
async def reindex_document(
    session: SessionDep,
    kb_id: UUID,
    doc_id: UUID,
    ctx: OrgContext = Depends(require_permission(Permission.KB_MANAGE)),
    container: Container = Depends(get_container),
) -> JobsStartedOut:
    """`jobs` is empty when the document is already queued or processing, unless it has been stuck
    in the queue for over 10 minutes (no job running): then it is queued again."""
    service = build_document_service(session, ctx.organisation.id, container)
    await _document_of_kb(service, kb_id, doc_id)
    job = await service.reindex_document(doc_id, ctx.user)
    return _jobs_out([job] if job is not None else [])


@router.get("/orgs/{org_id}/kbs/{kb_id}/documents/{doc_id}/chunks", response_model=ChunkPage)
async def list_chunks(
    session: SessionDep,
    kb_id: UUID,
    doc_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    ctx: OrgContext = Depends(require_any_permission(*READ)),
    container: Container = Depends(get_container),
) -> ChunkPage:
    service = build_document_service(session, ctx.organisation.id, container)
    await _document_of_kb(service, kb_id, doc_id)
    chunks, total, version = await service.chunks(doc_id, limit=limit, offset=offset)
    items = [
        ChunkOut(
            id=c.id, ordinal=c.ordinal, text=c.text, char_start=c.char_start, char_end=c.char_end, metadata=c.metadata_
        )
        for c in chunks
    ]
    return ChunkPage(items=items, total=total, version=version)
