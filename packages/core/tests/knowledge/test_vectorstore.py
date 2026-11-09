"""VectorStore port: pgvector (default) and Chroma adapters behave the same.

The chroma cases are integration tests: they need `docker compose -f infra/docker-compose.yml
--profile chroma up -d chroma` (port 8001) and skip when its heartbeat fails.

DDL is not covered by the per-test rollback, so every test drops the collections it creates and,
for pgvector, the kb_vectors_* tables (they only ever exist in the test database for tests).
"""

import math
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from supportcore.config import Settings
from supportcore.knowledge.vectorstore.chroma_store import ChromaVectorStore
from supportcore.knowledge.vectorstore.pgvector_store import PgVectorStore
from supportcore.knowledge.vectorstore.ports import (
    VectorCollectionNotFoundError,
    VectorCollectionOwnershipError,
    VectorDimensionMismatchError,
    VectorHit,
    VectorRecord,
)
from supportcore.testing import TEST_DATABASE_URL
from supportcore.wiring import build_adapters

CHROMA_URL = "http://localhost:8001"
REPO_ROOT = Path(__file__).resolve().parents[4]
DIM = 4
Q = [1.0, 0.0, 0.0, 0.0]


def _chroma_up() -> bool:
    try:
        return httpx.get(f"{CHROMA_URL}/api/v2/heartbeat", timeout=1.0).status_code == 200
    except httpx.HTTPError:
        return False


async def _drop_vector_tables(engine) -> None:
    async with engine.begin() as conn:
        names = (
            await conn.execute(text("SELECT tablename FROM pg_tables WHERE tablename LIKE 'kb\\_vectors\\_%'"))
        ).scalars()
        for name in list(names):
            assert name.removeprefix("kb_vectors_").isdigit()
            await conn.execute(text(f'DROP TABLE IF EXISTS "{name}"'))
        await conn.execute(text("DELETE FROM vector_collections"))


@dataclass
class Coll:
    name: str
    org: uuid.UUID
    kb: uuid.UUID


@dataclass
class Ctx:
    store: object
    created: list[Coll] = field(default_factory=list)

    def new(self, *, org: uuid.UUID | None = None, kb: uuid.UUID | None = None, name: str | None = None) -> Coll:
        c = Coll(name or f"kb_{uuid.uuid4().hex}_v1", org or uuid.uuid4(), kb or uuid.uuid4())
        self.created.append(c)
        return c

    async def ensure(self, dims: int = DIM, **kw) -> Coll:
        c = self.new(**kw)
        await self.store.ensure_collection(c.name, dims, organisation_id=c.org, knowledge_base_id=c.kb)
        return c

    async def cleanup(self) -> None:
        for c in self.created:
            try:
                await self.store.drop_collection(c.name, organisation_id=c.org)
            except VectorCollectionOwnershipError:
                pass  # a test's deliberately foreign handle on someone else's collection


@pytest.fixture(params=["pgvector", pytest.param("chroma", marks=pytest.mark.integration)])
async def vs(request, db_engine):
    """Yields a Ctx; every collection created through it is dropped afterwards."""
    if request.param == "pgvector":
        ctx = Ctx(PgVectorStore(db_engine))
        try:
            yield ctx
        finally:
            await ctx.cleanup()
            await _drop_vector_tables(db_engine)
    else:
        if not _chroma_up():
            pytest.skip("Chroma is not running on localhost:8001")
        ctx = Ctx(ChromaVectorStore(CHROMA_URL))
        try:
            yield ctx
        finally:
            await ctx.cleanup()
            await ctx.store.close()


def _rec(embedding, c: Coll, *, doc=None, chunk=None, metadata=None, org=None, kb=None) -> VectorRecord:
    return VectorRecord(
        chunk_id=chunk or uuid.uuid4(),
        document_id=doc or uuid.uuid4(),
        organisation_id=org or c.org,
        knowledge_base_id=kb or c.kb,
        embedding=embedding,
        metadata=metadata if metadata is not None else {},
    )


async def test_upsert_query_orders_by_similarity(vs):
    store = vs.store
    c = await vs.ensure()
    await store.ensure_collection(c.name, DIM, organisation_id=c.org, knowledge_base_id=c.kb)  # idempotent
    near = _rec([0.8, 0.6, 0.0, 0.0], c, metadata={"page": 2, "headings": ["Refunds", "EU"]})
    exact = _rec(Q, c)
    far = _rec([0.0, 1.0, 0.0, 0.0], c)
    opposite = _rec([-1.0, 0.0, 0.0, 0.0], c)
    await store.upsert(c.name, [far, near, opposite, exact], organisation_id=c.org)

    hits = await store.query(c.name, Q, organisation_id=c.org, k=10)
    assert [h.chunk_id for h in hits] == [exact.chunk_id, near.chunk_id, far.chunk_id, opposite.chunk_id]
    assert all(isinstance(h, VectorHit) for h in hits)
    for hit, expected in zip(hits, [1.0, 0.8, 0.0, -1.0], strict=True):
        assert math.isclose(hit.score, expected, abs_tol=1e-3), hits
    assert hits[1].document_id == near.document_id
    assert hits[1].metadata == {"page": 2, "headings": ["Refunds", "EU"]}

    top2 = await store.query(c.name, Q, organisation_id=c.org, k=2)
    assert [h.chunk_id for h in top2] == [exact.chunk_id, near.chunk_id]
    assert await store.count(c.name, organisation_id=c.org) == 4


async def test_upsert_idempotent(vs):
    store = vs.store
    c = await vs.ensure()
    rec = _rec(Q, c, metadata={"v": 1})
    await store.upsert(c.name, [rec], organisation_id=c.org)
    await store.upsert(c.name, [rec], organisation_id=c.org)
    assert await store.count(c.name, organisation_id=c.org) == 1

    changed = VectorRecord(**{**rec.__dict__, "embedding": [0.0, 1.0, 0.0, 0.0], "metadata": {"v": 2}})
    await store.upsert(c.name, [changed], organisation_id=c.org)
    assert await store.count(c.name, organisation_id=c.org) == 1
    [hit] = await store.query(c.name, [0.0, 1.0, 0.0, 0.0], organisation_id=c.org, k=5)
    assert hit.chunk_id == rec.chunk_id and hit.metadata == {"v": 2}
    assert math.isclose(hit.score, 1.0, abs_tol=1e-3)


async def test_query_filters_by_organisation(vs):
    """Org B's collection with an identical vector never shows up for org A, and vice versa."""
    store = vs.store
    a = await vs.ensure()
    b = await vs.ensure()
    a1 = _rec([0.9, 0.1, 0.0, 0.0], a)
    a2 = _rec([0.0, 0.0, 1.0, 0.0], a)
    b1 = _rec(Q, b)  # the best match overall, but org B's
    await store.upsert(a.name, [a1, a2], organisation_id=a.org)
    await store.upsert(b.name, [b1], organisation_id=b.org)

    hits = await store.query(a.name, Q, organisation_id=a.org, k=10)
    assert [h.chunk_id for h in hits] == [a1.chunk_id, a2.chunk_id]
    assert [h.chunk_id for h in await store.query(b.name, Q, organisation_id=b.org, k=10)] == [b1.chunk_id]

    # document_ids narrows further but never widens past the organisation
    narrowed = await store.query(
        a.name, Q, organisation_id=a.org, k=10, document_ids=[a2.document_id, b1.document_id]
    )
    assert [h.chunk_id for h in narrowed] == [a2.chunk_id]
    assert await store.query(a.name, Q, organisation_id=a.org, k=10, document_ids=[]) == []


async def test_wrong_organisation_is_rejected_everywhere(vs):
    store = vs.store
    c = await vs.ensure()
    rec = _rec(Q, c)
    await store.upsert(c.name, [rec], organisation_id=c.org)
    other_org = uuid.uuid4()

    with pytest.raises(VectorCollectionOwnershipError):
        await store.ensure_collection(c.name, DIM, organisation_id=other_org, knowledge_base_id=c.kb)
    with pytest.raises(VectorCollectionOwnershipError):  # same org, another knowledge base
        await store.ensure_collection(c.name, DIM, organisation_id=c.org, knowledge_base_id=uuid.uuid4())
    with pytest.raises(VectorCollectionOwnershipError):  # ownership is checked before dimensions
        await store.ensure_collection(c.name, DIM + 1, organisation_id=other_org, knowledge_base_id=c.kb)
    with pytest.raises(VectorCollectionOwnershipError):
        await store.upsert(c.name, [_rec(Q, c, org=other_org)], organisation_id=other_org)
    with pytest.raises(VectorCollectionOwnershipError):
        await store.query(c.name, Q, organisation_id=other_org, k=5)
    with pytest.raises(VectorCollectionOwnershipError):
        await store.delete_documents(c.name, [rec.document_id], organisation_id=other_org)
    with pytest.raises(VectorCollectionOwnershipError):
        await store.count(c.name, organisation_id=other_org)
    with pytest.raises(VectorCollectionOwnershipError):
        await store.drop_collection(c.name, organisation_id=other_org)

    # nothing changed for the owner
    assert await store.count(c.name, organisation_id=c.org) == 1
    [hit] = await store.query(c.name, Q, organisation_id=c.org, k=5)
    assert hit.chunk_id == rec.chunk_id


async def test_upsert_rejects_foreign_records(vs):
    store = vs.store
    c = await vs.ensure()
    good = _rec(Q, c)
    for bad in (_rec(Q, c, org=uuid.uuid4()), _rec(Q, c, kb=uuid.uuid4())):
        with pytest.raises(VectorCollectionOwnershipError):
            await store.upsert(c.name, [good, bad], organisation_id=c.org)
    assert await store.count(c.name, organisation_id=c.org) == 0  # whole batch rejected
    # a foreign org cannot take over an existing chunk id either
    await store.upsert(c.name, [good], organisation_id=c.org)
    takeover = VectorRecord(**{**good.__dict__, "organisation_id": uuid.uuid4()})
    with pytest.raises(VectorCollectionOwnershipError):
        await store.upsert(c.name, [takeover], organisation_id=c.org)
    [hit] = await store.query(c.name, Q, organisation_id=c.org, k=5)
    assert hit.chunk_id == good.chunk_id


async def test_ownership_error_does_not_reveal_existence(vs):
    c = await vs.ensure()
    with pytest.raises(VectorCollectionNotFoundError) as foreign:
        await vs.store.query(c.name, Q, organisation_id=uuid.uuid4(), k=5)
    with pytest.raises(VectorCollectionNotFoundError) as missing:
        await vs.store.upsert(vs.new().name, [], organisation_id=c.org)
    assert str(foreign.value) == str(missing.value)


async def test_query_isolated_per_collection(vs):
    store = vs.store
    one = await vs.ensure()
    two = await vs.ensure(org=one.org, kb=one.kb)
    r1, r2 = _rec(Q, one), _rec(Q, two)
    await store.upsert(one.name, [r1], organisation_id=one.org)
    await store.upsert(two.name, [r2], organisation_id=two.org)
    assert [h.chunk_id for h in await store.query(one.name, Q, organisation_id=one.org, k=5)] == [r1.chunk_id]
    assert await store.count(two.name, organisation_id=two.org) == 1


async def test_delete_documents(vs):
    store = vs.store
    c = await vs.ensure()
    doc1, doc2, doc3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await store.upsert(
        c.name,
        [
            _rec(Q, c, doc=doc1),
            _rec([0.9, 0.1, 0.0, 0.0], c, doc=doc1),
            _rec([0.0, 1.0, 0.0, 0.0], c, doc=doc2),
            _rec([0.0, 0.0, 1.0, 0.0], c, doc=doc3),
        ],
        organisation_id=c.org,
    )
    await store.delete_documents(c.name, [doc1, doc3], organisation_id=c.org)
    assert await store.count(c.name, organisation_id=c.org) == 1
    hits = await store.query(c.name, Q, organisation_id=c.org, k=10)
    assert [h.document_id for h in hits] == [doc2]
    await store.delete_documents(c.name, [doc1], organisation_id=c.org)  # already gone: no-op
    await store.delete_documents(c.name, [], organisation_id=c.org)
    assert await store.count(c.name, organisation_id=c.org) == 1


async def test_drop_collection_idempotent(vs):
    store = vs.store
    c = await vs.ensure()
    await store.upsert(c.name, [_rec(Q, c)], organisation_id=c.org)
    await store.drop_collection(c.name, organisation_id=c.org)
    await store.drop_collection(c.name, organisation_id=c.org)
    await store.drop_collection(vs.new().name, organisation_id=c.org)  # never created
    assert await store.count(c.name, organisation_id=c.org) == 0
    assert await store.query(c.name, Q, organisation_id=c.org, k=5) == []
    # the old dimensions and owner are forgotten with the collection
    other = vs.new(name=c.name)
    await store.ensure_collection(c.name, 3, organisation_id=other.org, knowledge_base_id=other.kb)
    assert await store.count(c.name, organisation_id=other.org) == 0


async def test_dimension_mismatch(vs):
    store = vs.store
    c = await vs.ensure()
    with pytest.raises(VectorDimensionMismatchError):
        await store.ensure_collection(c.name, DIM + 1, organisation_id=c.org, knowledge_base_id=c.kb)
    with pytest.raises(VectorDimensionMismatchError):
        await store.upsert(c.name, [_rec([1.0, 0.0, 0.0], c)], organisation_id=c.org)
    with pytest.raises(VectorDimensionMismatchError):
        await store.query(c.name, [1.0, 0.0, 0.0], organisation_id=c.org, k=5)
    assert await store.count(c.name, organisation_id=c.org) == 0


async def test_unknown_collection(vs):
    store = vs.store
    c = vs.new()
    with pytest.raises(VectorCollectionNotFoundError):
        await store.upsert(c.name, [_rec(Q, c)], organisation_id=c.org)
    assert await store.query(c.name, Q, organisation_id=c.org, k=5) == []
    assert await store.count(c.name, organisation_id=c.org) == 0
    await store.delete_documents(c.name, [uuid.uuid4()], organisation_id=c.org)


@pytest.mark.parametrize("k", [0, 201, -1])
async def test_query_k_bounds(vs, k):
    c = await vs.ensure()
    with pytest.raises(ValueError):
        await vs.store.query(c.name, Q, organisation_id=c.org, k=k)


@pytest.mark.parametrize("dims", [0, -3, 16001, 2.5, True, "384"])
async def test_invalid_dimensions(vs, dims):
    c = vs.new()
    with pytest.raises(ValueError):
        await vs.store.ensure_collection(c.name, dims, organisation_id=c.org, knowledge_base_id=c.kb)


async def test_pgvector_hostile_collection_name_is_data(db_engine):
    store = PgVectorStore(db_engine)
    org, kb = uuid.uuid4(), uuid.uuid4()
    name = "kb'; DROP TABLE vector_collections; --\"ünïçødé 🚀 $1 :name"
    try:
        await store.ensure_collection(name, DIM, organisation_id=org, knowledge_base_id=kb)
        rec = VectorRecord(uuid.uuid4(), uuid.uuid4(), org, kb, Q, {"q": "'; --"})
        await store.upsert(name, [rec], organisation_id=org)
        [hit] = await store.query(name, Q, organisation_id=org, k=1)
        assert hit.chunk_id == rec.chunk_id and hit.metadata == {"q": "'; --"}
        assert await store.count(name, organisation_id=org) == 1
        async with db_engine.connect() as conn:
            stored = (await conn.execute(text("SELECT name FROM vector_collections"))).scalars().all()
        assert stored == [name]
        await store.drop_collection(name, organisation_id=org)
        assert await store.count(name, organisation_id=org) == 0
    finally:
        await _drop_vector_tables(db_engine)


async def test_pgvector_halfvec_for_3072_dims(db_engine):
    store = PgVectorStore(db_engine)
    org, kb = uuid.uuid4(), uuid.uuid4()
    name = f"kb_{uuid.uuid4().hex}_v1"
    try:
        await store.ensure_collection(name, 3072, organisation_id=org, knowledge_base_id=kb)
        await store.ensure_collection(f"kb_{uuid.uuid4().hex}_v1", 2000, organisation_id=org, knowledge_base_id=kb)
        async with db_engine.connect() as conn:
            col_types = dict(
                (
                    await conn.execute(
                        text(
                            "SELECT c.relname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
                            "JOIN pg_class c ON c.oid = a.attrelid "
                            "WHERE c.relname IN ('kb_vectors_3072', 'kb_vectors_2000') AND a.attname = 'embedding'"
                        )
                    )
                ).all()
            )
            index_defs = {
                t: (await conn.execute(text("SELECT indexdef FROM pg_indexes WHERE tablename = :t"), {"t": t}))
                .scalars()
                .all()
                for t in ("kb_vectors_3072", "kb_vectors_2000")
            }
        assert col_types == {"kb_vectors_3072": "halfvec(3072)", "kb_vectors_2000": "vector(2000)"}
        big, small = index_defs["kb_vectors_3072"], index_defs["kb_vectors_2000"]
        assert any("USING hnsw (embedding halfvec_cosine_ops)" in d for d in big), big
        assert any("USING hnsw (embedding vector_cosine_ops)" in d for d in small), small
        for defs in (big, small):
            assert any("(collection, organisation_id)" in d for d in defs), defs
            assert any("(collection, document_id)" in d for d in defs), defs

        target = [0.0] * 3072
        target[0] = 1.0
        other = [0.0] * 3072
        other[1] = 1.0
        c = Coll(name, org, kb)
        best, worst = _rec(target, c), _rec(other, c)
        await store.upsert(name, [worst, best], organisation_id=org)
        hits = await store.query(name, target, organisation_id=org, k=2)
        assert [h.chunk_id for h in hits] == [best.chunk_id, worst.chunk_id]
        assert math.isclose(hits[0].score, 1.0, abs_tol=1e-3)
    finally:
        await _drop_vector_tables(db_engine)


async def test_pgvector_ensure_collection_records_registry_row(db_engine):
    store = PgVectorStore(db_engine)
    name, org, kb = f"kb_{uuid.uuid4().hex}_v1", uuid.uuid4(), uuid.uuid4()
    sql = text("SELECT dimensions, organisation_id, knowledge_base_id FROM vector_collections WHERE name = :n")
    try:
        await store.ensure_collection(name, DIM, organisation_id=org, knowledge_base_id=kb)
        async with db_engine.connect() as conn:
            row = (await conn.execute(sql, {"n": name})).one()
        assert tuple(row) == (DIM, org, kb)
        await store.drop_collection(name, organisation_id=org)
        async with db_engine.connect() as conn:
            assert (await conn.execute(sql, {"n": name})).one_or_none() is None
    finally:
        await _drop_vector_tables(db_engine)


async def test_alembic_check_ignores_vector_tables(db_engine):
    """`alembic check` (run by `make test`) must not report the lazily created kb_vectors_* tables."""
    store = PgVectorStore(db_engine)
    org, kb = uuid.uuid4(), uuid.uuid4()
    async with db_engine.connect() as conn:
        had_version_table = (await conn.execute(text("SELECT to_regclass('alembic_version')"))).scalar() is not None
    try:
        await store.ensure_collection(f"kb_{uuid.uuid4().hex}_v1", DIM, organisation_id=org, knowledge_base_id=kb)
        await store.ensure_collection(f"kb_{uuid.uuid4().hex}_v1", 3072, organisation_id=org, knowledge_base_id=kb)
        env = {**os.environ, "SUPPORT_DATABASE_URL": TEST_DATABASE_URL}
        alembic = ["uv", "run", "alembic", "-c", "apps/api/alembic.ini"]
        # The test schema comes from Base.metadata.create_all, so mark it as being at head first.
        if not had_version_table:
            stamp = subprocess.run([*alembic, "stamp", "head"], cwd=REPO_ROOT, env=env, capture_output=True, text=True)
            assert stamp.returncode == 0, stamp.stdout + stamp.stderr
        check = subprocess.run([*alembic, "check"], cwd=REPO_ROOT, env=env, capture_output=True, text=True)
        assert check.returncode == 0, check.stdout + check.stderr
    finally:
        await _drop_vector_tables(db_engine)
        if not had_version_table:
            async with db_engine.begin() as conn:
                await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


def _settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        env="test",
        database_url="postgresql+asyncpg://u:p@h/db",
        redis_url="redis://h:6379/0",
        jwt_secret="x" * 32,
        smtp_from="noreply@example.com",
        **overrides,
    )


async def test_build_adapters_selects_vector_store(db_engine):
    shared = build_adapters(_settings(), engine=db_engine)
    assert isinstance(shared.vector_store, PgVectorStore)
    assert shared.vector_store.engine is db_engine and shared.owned_engine is None
    await shared.aclose()  # leaves the caller's engine alone
    async with db_engine.connect() as conn:
        assert (await conn.execute(text("SELECT 1"))).scalar() == 1

    own = build_adapters(_settings())
    assert isinstance(own.vector_store, PgVectorStore)
    assert own.owned_engine is not None and own.vector_store.engine is own.owned_engine
    await own.aclose()
    assert own.owned_engine is None

    chroma = build_adapters(_settings(vector_store="chroma", chroma_url="http://chroma:8000"))
    assert isinstance(chroma.vector_store, ChromaVectorStore) and chroma.owned_engine is None
    await chroma.aclose()
