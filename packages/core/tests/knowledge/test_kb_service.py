import json
from dataclasses import replace

import pytest
from sqlalchemy import select

from supportcore.ai.catalog import UnknownModelError
from supportcore.ai.credentials import CredentialInUseError, CredentialInvalidError, CredentialService
from supportcore.ai.ports import EmbedderFactory
from supportcore.ai.settings_service import CredentialProviderMismatchError
from supportcore.db.models import AuditLog, Document, IndexingJob, KnowledgeBase, ProviderCredential
from supportcore.errors import NotFoundError
from supportcore.knowledge.chunking.ports import ChunkingParams
from supportcore.knowledge.events import KnowledgeBaseCreated, KnowledgeBaseDeleted, KnowledgeBaseUpdated
from supportcore.knowledge.kb_service import (
    EmbeddingChoice,
    InvalidKnowledgeBaseError,
    KnowledgeBaseNameTakenError,
    KnowledgeBaseService,
    RebuildInProgressError,
    UnsupportedLanguageForModelError,
    collection_name,
    version_config,
)
from supportcore.registry import ProviderRegistry

KEY = "sk-test-SUPERSECRETKEYVALUE-abcd1234WXYZ"


async def _credential(session, org, adapters, provider: str, base_url: str | None = None) -> ProviderCredential:
    cred = ProviderCredential(
        organisation_id=org.id,
        provider=provider,
        label=f"{provider} key",
        encrypted_api_key=adapters.cipher.encrypt(KEY),
        base_url=base_url,
        last4=KEY[-4:],
    )
    session.add(cred)
    await session.flush()
    return cred


def _create_kwargs(owner, chunking, **overrides):
    kwargs = dict(
        name="Docs",
        description="",
        embedding_provider="fake",
        embedding_model="fake-embedding",
        embedding_credential_id=None,
        chunking=chunking,
        reranker="none",
        reranker_params={},
        actor=owner,
    )
    kwargs.update(overrides)
    return kwargs


async def _add_document(session, kb, name="a.md", sha="a" * 64, indexed_version: int | None = 1) -> Document:
    doc = Document(
        organisation_id=kb.organisation_id,
        knowledge_base_id=kb.id,
        filename=name,
        file_type="markdown",
        size_bytes=3,
        sha256=sha,
        storage_key=f"orgs/{kb.organisation_id}/kbs/{kb.id}/documents/x/{sha}",
        status="indexed" if indexed_version is not None else "uploaded",
        indexed_version=indexed_version,
    )
    session.add(doc)
    await session.flush()
    return doc


# --- create ---------------------------------------------------------------------------------------


async def test_create_kb_requires_matching_credential(session, kbs, org, owner, adapters, recursive):
    kwargs = _create_kwargs(owner, recursive, embedding_provider="openai", embedding_model="text-embedding-3-small")
    with pytest.raises(InvalidKnowledgeBaseError, match="API key"):
        await kbs.create(**kwargs)

    anthropic = await _credential(session, org, adapters, "anthropic")
    with pytest.raises(CredentialProviderMismatchError):
        await kbs.create(**{**kwargs, "embedding_credential_id": anthropic.id})

    openai = await _credential(session, org, adapters, "openai")
    kb = await kbs.create(**{**kwargs, "embedding_credential_id": openai.id})
    assert (kb.embedding_provider, kb.embedding_model, kb.embedding_dimensions) == (
        "openai",
        "text-embedding-3-small",
        1536,
    )
    assert kb.embedding_credential_id == openai.id


async def test_create_kb_credential_of_other_org_is_404(session, kbs, owner, adapters, recursive):
    from supportcore.db.models import Organisation

    other = Organisation(name="Other", slug="other-org")
    session.add(other)
    await session.flush()
    foreign = await _credential(session, other, adapters, "openai")
    kwargs = _create_kwargs(
        owner,
        recursive,
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
        embedding_credential_id=foreign.id,
    )
    with pytest.raises(NotFoundError):
        await kbs.create(**kwargs)


async def test_create_kb_local_needs_no_credential(session, kbs, org, owner, recursive, job_queue, seen):
    kb = await kbs.create(
        **_create_kwargs(owner, recursive, embedding_provider="local", embedding_model="BAAI/bge-small-en-v1.5")
    )
    assert kb.organisation_id == org.id
    assert kb.embedding_credential_id is None
    assert kb.embedding_dimensions == 384
    assert kb.chunking == {"strategy": "recursive", "chunk_size": 1000, "chunk_overlap": 150}
    assert (kb.reranker, kb.reranker_params) == ("none", {})
    assert (kb.active_version, kb.building_version) == (1, None)
    assert kb.created_by_user_id == owner.id
    assert [e.knowledge_base_id for e in seen if isinstance(e, KnowledgeBaseCreated)] == [kb.id]

    # The vector collection is created by a job, enqueued only once the transaction commits.
    assert job_queue.jobs == []
    await session.commit()
    assert job_queue.jobs == [
        ("ensure_collection", {"kb_id": str(kb.id), "organisation_id": str(org.id), "version": 1})
    ]


async def test_reranker_model_must_be_allow_listed(kbs, fake_kb, owner, recursive):
    params = {"model": "attacker/huge-model"}
    with pytest.raises(UnknownModelError):
        await kbs.create(**_create_kwargs(owner, recursive, name="Other", reranker="cross_encoder", reranker_params=params))
    with pytest.raises(UnknownModelError):
        await kbs.update(fake_kb.id, reranker="cross_encoder", reranker_params=params, actor=owner)
    kb, _ = await kbs.update(
        fake_kb.id, reranker="cross_encoder", reranker_params={"model": "BAAI/bge-reranker-v2-m3"}, actor=owner
    )
    assert kb.reranker_params == {"model": "BAAI/bge-reranker-v2-m3"}


async def test_create_kb_rejects_unknown_model_and_bad_reranker(kbs, owner, recursive):
    with pytest.raises(UnknownModelError):
        await kbs.create(**_create_kwargs(owner, recursive, embedding_provider="local", embedding_model="nope"))
    with pytest.raises(InvalidKnowledgeBaseError):
        await kbs.create(**_create_kwargs(owner, recursive, reranker="magic"))
    with pytest.raises(InvalidKnowledgeBaseError):
        await kbs.create(**_create_kwargs(owner, recursive, reranker="none", reranker_params={"x": 1}))
    with pytest.raises(InvalidKnowledgeBaseError):
        await kbs.create(**_create_kwargs(owner, recursive, name="   "))


async def test_create_kb_unsupported_language_for_model(session, kbs, org, owner, recursive):
    org.language = "de"
    await session.flush()
    with pytest.raises(UnsupportedLanguageForModelError):
        await kbs.create(
            **_create_kwargs(owner, recursive, embedding_provider="local", embedding_model="BAAI/bge-small-en-v1.5")
        )
    kb = await kbs.create(
        **_create_kwargs(owner, recursive, embedding_provider="local", embedding_model="intfloat/multilingual-e5-small")
    )
    assert kb.embedding_dimensions == 384


class _ProbeEmbedder:
    def __init__(self, base_url: str | None) -> None:
        self.model_id = "my-embed"
        self.base_url = base_url
        self._dims: int | None = None

    @property
    def dimensions(self) -> int:
        assert self._dims is not None
        return self._dims

    async def probe_dimensions(self) -> int:
        self._dims = 12
        return 12


def _probe_adapters(adapters, created: list):
    registry = ProviderRegistry("embedder")

    @registry.register("openai_compatible")
    def _factory(*, model, api_key, base_url, settings):
        created.append((model, api_key, base_url))
        return _ProbeEmbedder(base_url)

    return replace(adapters, embedders=EmbedderFactory(registry))


async def test_create_kb_probes_dimensions_for_openai_compatible(session, org, bus, adapters, kb_settings, owner, recursive):
    created: list = []
    service = KnowledgeBaseService(session, org.id, bus, _probe_adapters(adapters, created), kb_settings)
    cred = await _credential(session, org, adapters, "openai_compatible", base_url="https://embed.example.com/v1")
    kb = await service.create(
        **_create_kwargs(
            owner,
            recursive,
            embedding_provider="openai_compatible",
            embedding_model="my-embed",
            embedding_credential_id=cred.id,
        )
    )
    assert kb.embedding_dimensions == 12
    assert created == [("my-embed", KEY, "https://embed.example.com/v1")]


async def test_create_kb_probe_refuses_private_base_url(session, org, bus, adapters, kb_settings, owner, recursive):
    created: list = []
    settings = kb_settings.model_copy(update={"allow_private_base_urls": False})
    service = KnowledgeBaseService(session, org.id, bus, _probe_adapters(adapters, created), settings)
    cred = await _credential(session, org, adapters, "openai_compatible", base_url="http://127.0.0.1:11434/v1")
    with pytest.raises(CredentialInvalidError):
        await service.create(
            **_create_kwargs(
                owner,
                recursive,
                embedding_provider="openai_compatible",
                embedding_model="my-embed",
                embedding_credential_id=cred.id,
            )
        )
    assert created == []  # no outbound call was even prepared


def _lock_spy(session, created: list):
    """Records, in order, "lock" for every SELECT ... FOR UPDATE the session runs and "probe" when
    the embedder factory builds the probing embedder."""
    from sqlalchemy.dialects import postgresql

    order: list[str] = []
    original = session.execute

    async def execute(statement, *args, **kwargs):
        try:
            sql = str(statement.compile(dialect=postgresql.dialect()))
        except Exception:
            sql = str(statement)
        if "FOR UPDATE" in sql or "FOR SHARE" in sql:
            order.append("lock")
        return await original(statement, *args, **kwargs)

    session.execute = execute

    class _Created(list):
        def append(self, item):
            order.append("probe")
            super().append(item)

    return order, _Created(created)


async def test_create_kb_probes_before_taking_row_locks(session, org, bus, adapters, kb_settings, owner, recursive):
    cred = await _credential(session, org, adapters, "openai_compatible", base_url="https://embed.example.com/v1")
    order, created = _lock_spy(session, [])
    service = KnowledgeBaseService(session, org.id, bus, _probe_adapters(adapters, created), kb_settings)
    try:
        kb = await service.create(
            **_create_kwargs(
                owner, recursive, embedding_provider="openai_compatible", embedding_model="my-embed",
                embedding_credential_id=cred.id,
            )
        )
    finally:
        del session.execute
    assert kb.embedding_dimensions == 12
    assert "probe" in order and "lock" in order  # the credential is still locked, but only afterwards
    assert "lock" not in order[: order.index("probe")]


async def test_update_kb_probes_before_taking_row_locks(session, kbs, fake_kb, org, bus, adapters, kb_settings, owner):
    cred = await _credential(session, org, adapters, "openai_compatible", base_url="https://embed.example.com/v1")
    order, created = _lock_spy(session, [])
    service = KnowledgeBaseService(session, org.id, bus, _probe_adapters(adapters, created), kb_settings)
    try:
        kb, rebuild = await service.update(
            fake_kb.id, embedding=EmbeddingChoice("openai_compatible", "my-embed", cred.id), actor=owner
        )
    finally:
        del session.execute
    assert rebuild is False and kb.embedding_dimensions == 12
    assert "lock" in order and "lock" not in order[: order.index("probe")]


async def test_update_unchanged_compatible_embedding_does_not_probe(session, org, bus, adapters, kb_settings, owner, recursive):
    cred = await _credential(session, org, adapters, "openai_compatible", base_url="https://embed.example.com/v1")
    created: list = []
    service = KnowledgeBaseService(session, org.id, bus, _probe_adapters(adapters, created), kb_settings)
    kb = await service.create(
        **_create_kwargs(
            owner, recursive, embedding_provider="openai_compatible", embedding_model="my-embed",
            embedding_credential_id=cred.id,
        )
    )
    created.clear()
    await service.update(kb.id, name="Renamed", embedding=EmbeddingChoice("openai_compatible", "my-embed", cred.id), actor=owner)
    assert created == []


async def test_kb_name_unique_case_insensitive(kbs, fake_kb, owner, recursive):
    with pytest.raises(KnowledgeBaseNameTakenError):
        await kbs.create(**_create_kwargs(owner, recursive, name="  dOCS "))
    other = await kbs.create(**_create_kwargs(owner, recursive, name="Billing"))
    with pytest.raises(KnowledgeBaseNameTakenError):
        await kbs.update(other.id, name="DOCS", actor=owner)
    await kbs.update(fake_kb.id, name="docs", actor=owner)  # renaming itself (case change) is fine
    assert sorted(kb.name for kb in await kbs.list()) == ["Billing", "docs"]


async def test_get_kb_of_other_org_is_404(session, bus, adapters, kb_settings, fake_kb):
    from supportcore.db.models import Organisation

    other = Organisation(name="Other", slug="other-org")
    session.add(other)
    await session.flush()
    service = KnowledgeBaseService(session, other.id, bus, adapters, kb_settings)
    with pytest.raises(NotFoundError):
        await service.get(fake_kb.id)
    assert await service.list() == []


# --- update ---------------------------------------------------------------------------------------


async def test_update_embedding_starts_rebuild_and_enqueues_after_commit(
    session, kbs, fake_kb, org, owner, job_queue, seen
):
    await _add_document(session, fake_kb)
    await session.commit()
    job_queue.jobs.clear()

    kb, rebuild = await kbs.update(
        fake_kb.id,
        embedding=EmbeddingChoice("local", "BAAI/bge-small-en-v1.5", None),
        actor=owner,
    )
    assert rebuild is True
    # The served (active) config is untouched until the rebuild swaps versions.
    assert (kb.active_version, kb.building_version) == (1, 2)
    assert (kb.embedding_provider, kb.embedding_dimensions) == ("fake", 8)
    building = version_config(kb, 2)
    assert (building.provider, building.model, building.dimensions) == ("local", "BAAI/bge-small-en-v1.5", 384)
    assert version_config(kb, 1).dimensions == 8

    [job] = (await session.execute(select(IndexingJob).where(IndexingJob.knowledge_base_id == kb.id))).scalars()
    assert (job.kind, job.kb_version, job.status, job.document_id) == ("rebuild", 2, "queued", None)
    [event] = [e for e in seen if isinstance(e, KnowledgeBaseUpdated)]
    assert event.rebuild is True and "embedding" in event.changes

    assert job_queue.jobs == []
    await session.commit()
    assert job_queue.jobs == [
        (
            "rebuild_kb",
            {"kb_id": str(kb.id), "organisation_id": str(org.id), "version": 2, "job_id": str(job.id)},
        )
    ]


async def test_update_chunking_starts_rebuild(session, kbs, fake_kb, owner):
    await _add_document(session, fake_kb)
    kb, rebuild = await kbs.update(
        fake_kb.id, chunking=ChunkingParams(strategy="fixed", chunk_size=500, chunk_overlap=0), actor=owner
    )
    assert rebuild is True
    assert kb.chunking["strategy"] == "recursive"
    assert version_config(kb, 2).chunking == {"strategy": "fixed", "chunk_size": 500, "chunk_overlap": 0}


async def test_update_embedding_without_documents_applies_immediately(session, kbs, fake_kb, org, owner, job_queue):
    await session.commit()
    job_queue.jobs.clear()
    kb, rebuild = await kbs.update(
        fake_kb.id, embedding=EmbeddingChoice("local", "BAAI/bge-small-en-v1.5", None), actor=owner
    )
    assert rebuild is False
    assert (kb.embedding_provider, kb.embedding_dimensions, kb.building_version) == ("local", 384, None)
    # New dimensions need a new (empty) collection.
    assert kb.active_version == 2
    await session.commit()
    assert job_queue.jobs == [
        ("ensure_collection", {"kb_id": str(kb.id), "organisation_id": str(org.id), "version": 2})
    ]


async def test_update_with_only_unindexed_documents_applies_in_place(session, kbs, fake_kb, owner, job_queue):
    await _add_document(session, fake_kb, indexed_version=None)  # uploaded, never indexed
    kb, rebuild = await kbs.update(
        fake_kb.id,
        embedding=EmbeddingChoice("local", "BAAI/bge-small-en-v1.5", None),
        chunking=ChunkingParams(strategy="markdown"),
        actor=owner,
    )
    assert rebuild is False
    assert (kb.embedding_provider, kb.embedding_dimensions, kb.chunking["strategy"]) == ("local", 384, "markdown")
    assert (kb.active_version, kb.building_version) == (2, None)
    assert (await session.execute(select(IndexingJob))).first() is None


@pytest.mark.parametrize("status", ["queued", "processing"])
async def test_update_with_documents_in_flight_rebuilds(session, kbs, fake_kb, owner, status):
    doc = await _add_document(session, fake_kb, indexed_version=None)
    doc.status = status  # never indexed, but a job is on its way
    await session.flush()
    kb, rebuild = await kbs.update(fake_kb.id, chunking=ChunkingParams(strategy="markdown"), actor=owner)
    assert rebuild is True
    assert (kb.active_version, kb.building_version, kb.chunking["strategy"]) == (1, 2, "recursive")


async def test_update_while_rebuilding_409(session, kbs, fake_kb, owner):
    await _add_document(session, fake_kb)
    await kbs.update(fake_kb.id, embedding=EmbeddingChoice("local", "BAAI/bge-small-en-v1.5", None), actor=owner)
    with pytest.raises(RebuildInProgressError):
        await kbs.update(fake_kb.id, chunking=ChunkingParams(strategy="markdown"), actor=owner)
    # Changes that need no rebuild still apply.
    kb, rebuild = await kbs.update(fake_kb.id, description="New", actor=owner)
    assert (kb.description, rebuild) == ("New", False)


async def test_reranker_change_no_rebuild(session, kbs, fake_kb, owner, job_queue, seen):
    await _add_document(session, fake_kb)
    await session.commit()
    job_queue.jobs.clear()
    kb, rebuild = await kbs.update(
        fake_kb.id, name="Docs 2", reranker="cross_encoder", reranker_params={"model": "BAAI/bge-reranker-base"}, actor=owner
    )
    assert rebuild is False
    assert (kb.name, kb.reranker, kb.reranker_params) == ("Docs 2", "cross_encoder", {"model": "BAAI/bge-reranker-base"})
    assert (kb.active_version, kb.building_version) == (1, None)
    [event] = [e for e in seen if isinstance(e, KnowledgeBaseUpdated)]
    assert event.rebuild is False
    assert event.changes["name"] == {"from": "Docs", "to": "Docs 2"}
    await session.commit()
    assert job_queue.jobs == []


async def test_update_same_values_is_noop(session, kbs, fake_kb, owner, seen, recursive):
    await _add_document(session, fake_kb)
    _kb, rebuild = await kbs.update(
        fake_kb.id,
        name="Docs",
        embedding=EmbeddingChoice("fake", "fake-embedding", None),
        chunking=recursive,
        actor=owner,
    )
    assert rebuild is False
    assert [e for e in seen if isinstance(e, KnowledgeBaseUpdated)] == []


# --- delete ---------------------------------------------------------------------------------------


async def test_delete_kb_enqueues_purge_after_commit(session, kbs, fake_kb, org, owner, job_queue, seen):
    await _add_document(session, fake_kb)
    await kbs.update(fake_kb.id, embedding=EmbeddingChoice("local", "BAAI/bge-small-en-v1.5", None), actor=owner)
    await session.commit()
    job_queue.jobs.clear()

    await kbs.delete(fake_kb.id, owner)
    assert (await session.execute(select(KnowledgeBase))).first() is None
    assert (await session.execute(select(Document))).first() is None  # cascaded
    assert [e.knowledge_base_id for e in seen if isinstance(e, KnowledgeBaseDeleted)] == [fake_kb.id]
    assert job_queue.jobs == []
    await session.commit()
    assert job_queue.jobs == [
        (
            "purge_kb",
            {
                "organisation_id": str(org.id),
                "kb_id": str(fake_kb.id),
                "collections": [collection_name(fake_kb.id, 1), collection_name(fake_kb.id, 2)],
                "storage_prefix": f"orgs/{org.id}/kbs/{fake_kb.id}/",
            },
        )
    ]
    with pytest.raises(NotFoundError):
        await kbs.get(fake_kb.id)


async def test_rolled_back_delete_enqueues_nothing(session, kbs, fake_kb, owner, job_queue):
    await session.commit()
    job_queue.jobs.clear()
    await kbs.delete(fake_kb.id, owner)
    await session.rollback()
    await session.commit()
    assert job_queue.jobs == []


def test_collection_name_format():
    from uuid import UUID

    kb_id = UUID("12345678-1234-5678-1234-567812345678")
    assert collection_name(kb_id, 3) == "kb_12345678123456781234567812345678_v3"


# --- credentials ----------------------------------------------------------------------------------


async def test_credential_in_use_by_kb_409(session, kbs, org, owner, adapters, bus, recursive):
    cred = await _credential(session, org, adapters, "openai")
    await kbs.create(
        **_create_kwargs(
            owner,
            recursive,
            name="Support articles",
            embedding_provider="openai",
            embedding_model="text-embedding-3-small",
            embedding_credential_id=cred.id,
        )
    )
    credentials = CredentialService(session, org.id, adapters.cipher, adapters.credential_validator, bus)
    with pytest.raises(CredentialInUseError) as exc:
        await credentials.delete(cred.id, owner)
    assert str(exc.value) == "Credential is in use by knowledge base 'Support articles'"


async def test_credential_in_use_by_building_version_409(session, kbs, fake_kb, org, owner, adapters, bus):
    await _add_document(session, fake_kb)
    cred = await _credential(session, org, adapters, "openai")
    await kbs.update(
        fake_kb.id, embedding=EmbeddingChoice("openai", "text-embedding-3-small", cred.id), actor=owner
    )
    credentials = CredentialService(session, org.id, adapters.cipher, adapters.credential_validator, bus)
    with pytest.raises(CredentialInUseError):
        await credentials.delete(cred.id, owner)


# --- audit ----------------------------------------------------------------------------------------


async def test_audit_actions_written(session, kbs, fake_kb, org, owner):
    await kbs.update(fake_kb.id, description="Changed", actor=owner)
    await kbs.delete(fake_kb.id, owner)
    rows = list(
        (
            await session.execute(
                select(AuditLog).where(AuditLog.organisation_id == org.id, AuditLog.action.like("kb.%"))
            )
        ).scalars()
    )
    by_action = {r.action: r for r in rows}
    assert set(by_action) == {"kb.created", "kb.updated", "kb.deleted"}
    for row in rows:
        assert (row.target_type, row.target_id, row.actor_user_id) == ("knowledge_base", str(fake_kb.id), owner.id)
        json.dumps(row.metadata_)
    assert by_action["kb.created"].metadata_["name"] == "Docs"
    assert by_action["kb.updated"].metadata_["rebuild"] is False
    assert by_action["kb.updated"].metadata_["changes"]["description"] == {"from": "Help centre", "to": "Changed"}


async def test_kb_create_refused_402(session, org, bus, adapters, kb_settings, owner, recursive):
    from supportcore.billing.errors import PlanLimitError
    from supportcore.billing.fake_billing import FakeBilling
    from supportcore.testing import start_trial_plan

    await start_trial_plan(session, org.id, knowledge_bases=1)
    created: list = []
    service = KnowledgeBaseService(
        session, org.id, bus, replace(_probe_adapters(adapters, created), billing=FakeBilling()), kb_settings
    )
    await service.create(**_create_kwargs(owner, recursive))
    cred = await _credential(session, org, adapters, "openai_compatible", base_url="https://embed.example.com/v1")
    with pytest.raises(PlanLimitError) as raised:
        await service.create(
            **_create_kwargs(
                owner,
                recursive,
                name="Second",
                embedding_provider="openai_compatible",
                embedding_model="my-embed",
                embedding_credential_id=cred.id,
            )
        )
    assert (raised.value.resource, raised.value.limit) == ("knowledge_bases", 1)
    assert created == []  # refused before the embedding probe
    assert len((await session.execute(select(KnowledgeBase).where(KnowledgeBase.organisation_id == org.id))).all()) == 1
