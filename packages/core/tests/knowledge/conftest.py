import io

import pytest
from docx import Document


def build_pdf(page_texts: list[str | None]) -> bytes:
    """Minimal valid PDF; one page per entry. None means a page with no content stream."""
    objs: dict[int, bytes] = {}
    n = len(page_texts)
    font_id = 3 + 2 * n
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[2] = f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode()
    for i, text in enumerate(page_texts):
        page_id, content_id = 3 + 2 * i, 4 + 2 * i
        contents = f" /Contents {content_id} 0 R" if text is not None else ""
        objs[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]{contents} "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>"
        ).encode()
        stream = f"BT /F1 12 Tf 72 720 Td ({text or ''}) Tj ET".encode()
        objs[content_id] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        if text is None:
            del objs[content_id]
    objs[font_id] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for oid in sorted(objs):
        offsets[oid] = len(out)
        out += b"%d 0 obj\n" % oid + objs[oid] + b"\nendobj\n"
    xref = len(out)
    size = max(objs) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for oid in range(1, size):
        if oid in offsets:
            out += b"%010d 00000 n \n" % offsets[oid]
        else:
            out += b"0000000000 65535 f \n"
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref)
    return bytes(out)


@pytest.fixture
def two_page_pdf() -> bytes:
    return build_pdf(["Hello first page", "Second page here"])


@pytest.fixture
def blank_pdf() -> bytes:
    return build_pdf([None])


@pytest.fixture
def sample_docx() -> bytes:
    doc = Document()
    doc.add_heading("Refund Policy", level=1)
    doc.add_paragraph("We refund within 30 days.")
    doc.add_heading("Shipping", level=2)
    doc.add_paragraph("Ships in 3 days.")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Region"
    table.rows[0].cells[1].text = "EU"
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


@pytest.fixture
def pdf_builder():
    return build_pdf


# --- knowledge base / document service fixtures -------------------------------------------------


class RecordingJobQueue:
    """JobQueue fake: records (name, payload) pairs instead of running anything."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, dict]] = []

    def enqueue(self, name: str, payload: dict, *, countdown: float | None = None) -> None:
        self.jobs.append((name, payload))

    def names(self) -> list[str]:
        return [name for name, _payload in self.jobs]


class MemoryStorage:
    """FileStorage fake kept in a dict."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data

    async def get(self, key: str) -> bytes:
        return self.objects[key]

    async def stat_size(self, key: str) -> int:
        return len(self.objects[key])

    async def get_range(self, key: str, start: int, end_inclusive: int) -> bytes:
        return self.objects[key][start : end_inclusive + 1]

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)

    async def delete_prefix(self, prefix: str) -> int:
        keys = [k for k in self.objects if k.startswith(prefix)]
        for k in keys:
            del self.objects[k]
        return len(keys)


@pytest.fixture
def kb_settings(tmp_path):
    from cryptography.fernet import Fernet

    from supportcore.config import Settings
    from supportcore.testing import TEST_DATABASE_URL

    return Settings(
        _env_file=None,
        env="test",
        database_url=TEST_DATABASE_URL,
        redis_url="redis://localhost:6379/0",
        jwt_secret="test-secret-test-secret-test-secret-123",
        smtp_from="noreply@test.local",
        secrets_key=Fernet.generate_key().decode(),
        file_storage="local",
        local_storage_path=str(tmp_path / "uploads"),
        vector_store="chroma",
        max_upload_bytes=1000,
    )


@pytest.fixture
def job_queue() -> RecordingJobQueue:
    return RecordingJobQueue()


@pytest.fixture
def storage() -> MemoryStorage:
    return MemoryStorage()


@pytest.fixture
def adapters(kb_settings, job_queue, storage):
    from dataclasses import replace

    from supportcore.wiring import build_adapters

    return replace(build_adapters(kb_settings), storage=storage, job_queue=job_queue)


@pytest.fixture
async def owner(session):
    from supportcore.db.models import User

    u = User(email="owner@x.com", password_hash="x", name="Owner")
    session.add(u)
    await session.flush()
    return u


@pytest.fixture
async def org(session, owner):
    from supportcore.events import EventBus
    from supportcore.orgs.service import OrganisationService

    return await OrganisationService(session, EventBus()).create(owner, "Acme")


@pytest.fixture
def seen() -> list:
    return []


@pytest.fixture
def bus(session, seen):
    from supportcore.audit.subscribers import register_audit_subscribers
    from supportcore.events import DomainEvent, EventBus

    b = EventBus()
    register_audit_subscribers(b, lambda: session)

    async def record(e: DomainEvent) -> None:
        seen.append(e)

    b.subscribe(DomainEvent, record)
    return b


@pytest.fixture
def kbs(session, org, bus, adapters, kb_settings):
    from supportcore.knowledge.kb_service import KnowledgeBaseService

    return KnowledgeBaseService(session, org.id, bus, adapters, kb_settings)


@pytest.fixture
def documents(session, org, bus, adapters, kb_settings):
    from supportcore.knowledge.document_service import DocumentService

    return DocumentService(session, org.id, bus, adapters, kb_settings)


@pytest.fixture
def recursive():
    from supportcore.knowledge.chunking.ports import ChunkingParams

    return ChunkingParams(strategy="recursive")


@pytest.fixture
async def fake_kb(kbs, owner, recursive):
    """A KB on the test-only fake embedder (8 dims, no credential)."""
    return await kbs.create(
        name="Docs",
        description="Help centre",
        embedding_provider="fake",
        embedding_model="fake-embedding",
        embedding_credential_id=None,
        chunking=recursive,
        reranker="none",
        reranker_params={},
        actor=owner,
    )
