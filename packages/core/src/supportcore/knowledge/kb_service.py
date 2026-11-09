"""Knowledge bases: embedding, chunking and reranker config, and the versioned vector collections.

The vector store and file storage are never touched inside the request transaction: work on them is
done by background jobs, enqueued with `after_commit` so a rolled-back request enqueues nothing.
Job payloads (all values JSON-safe, UUIDs as str):

- ensure_collection {kb_id, organisation_id, version}
- rebuild_kb        {kb_id, organisation_id, version, job_id}
- purge_kb          {organisation_id, kb_id, collections: [names], storage_prefix}

Versions: the `embedding_*` / `chunking` columns describe the served `active_version`. Changing the
embedding model or chunking while documents are indexed (or queued/processing) builds `building_version`
from the `building_*` columns (see `version_config`) while the active version keeps serving; the
rebuild job swaps it in. An openai_compatible model's vector size is probed before any row is locked.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from supportcore.ai.base_url import ensure_public_base_url
from supportcore.ai.catalog import RERANKER_MODELS, UnknownModelError, get_embedding_spec
from supportcore.ai.credentials import CredentialService
from supportcore.ai.settings_service import CredentialProviderMismatchError
from supportcore.billing.entitlements import EntitlementService
from supportcore.config import Settings
from supportcore.db.models import Document, IndexingJob, KnowledgeBase, Organisation, ProviderCredential, User
from supportcore.db.session import after_commit
from supportcore.db.tenancy import TenantScopedRepository
from supportcore.errors import DomainError, NotFoundError
from supportcore.events import EventBus
from supportcore.knowledge.chunking.ports import ChunkingParams
from supportcore.knowledge.events import KnowledgeBaseCreated, KnowledgeBaseDeleted, KnowledgeBaseUpdated
from supportcore.knowledge.vectorstore.ports import check_dimensions
from supportcore.registry import UnknownProviderError

if TYPE_CHECKING:
    from supportcore.jobs.ports import JobQueue
    from supportcore.wiring import Adapters

MAX_NAME_LENGTH = 100
MAX_DESCRIPTION_LENGTH = 2000
MAX_MODEL_NAME_LENGTH = 255
DIMENSION_PROBE_TEXT = "dimension probe"


class InvalidKnowledgeBaseError(DomainError):
    """Bad name/description, reranker, or a missing/unexpected embedding credential."""


class KnowledgeBaseNameTakenError(DomainError):
    def __init__(self) -> None:
        super().__init__("A knowledge base with this name already exists")


class RebuildInProgressError(DomainError):
    def __init__(self) -> None:
        super().__init__("A rebuild is already in progress; wait for it to finish before changing embedding or chunking")


class UnsupportedLanguageForModelError(DomainError):
    def __init__(self, model: str, language: str) -> None:
        self.model = model
        self.language = language
        super().__init__(f"Embedding model {model[:100]!r} does not support the organisation language {language!r}")


@dataclass(frozen=True)
class EmbeddingChoice:
    """An embedding model as picked by the user; `credential_id` is None for providers without keys."""

    provider: str
    model: str
    credential_id: UUID | None = None


@dataclass(frozen=True)
class VersionConfig:
    """What one KB version is (or will be) built with."""

    provider: str
    model: str
    dimensions: int
    credential_id: UUID | None
    chunking: dict[str, Any]


def collection_name(kb: "KnowledgeBase | UUID", version: int) -> str:
    kb_id = kb if isinstance(kb, UUID) else kb.id
    return f"kb_{kb_id.hex}_v{version}"


def kb_collection_names(kb: KnowledgeBase) -> list[str]:
    """Every collection the KB may own: versions 1..latest (dropping an absent one is a no-op)."""
    latest = max(kb.active_version, kb.building_version or 0)
    return [collection_name(kb.id, v) for v in range(1, latest + 1)]


def kb_storage_prefix(organisation_id: UUID, kb_id: UUID) -> str:
    return f"orgs/{organisation_id}/kbs/{kb_id}/"


def version_config(kb: KnowledgeBase, version: int) -> VersionConfig:
    """The config to embed/chunk `version` with: the building_* columns for building_version, the
    plain columns for active_version. Raises ValueError for any other version, and RuntimeError if
    building_version is set without its building_* config."""
    if kb.building_version is not None and version == kb.building_version:
        if (
            not kb.building_embedding_provider
            or not kb.building_embedding_model
            or kb.building_embedding_dimensions is None
            or kb.building_chunking is None
        ):
            raise RuntimeError(f"Knowledge base {kb.id} is building version {version} without its config")
        return VersionConfig(
            kb.building_embedding_provider,
            kb.building_embedding_model,
            kb.building_embedding_dimensions,
            kb.building_embedding_credential_id,
            dict(kb.building_chunking),
        )
    if version == kb.active_version:
        return VersionConfig(
            kb.embedding_provider,
            kb.embedding_model,
            kb.embedding_dimensions,
            kb.embedding_credential_id,
            dict(kb.chunking),
        )
    raise ValueError(f"Knowledge base {kb.id} has no version {version}")


def enqueue_after_commit(session: AsyncSession, queue: "JobQueue | None", name: str, payload: dict) -> None:
    """Enqueue `name` once the session's transaction commits (never on rollback)."""
    if queue is None:
        raise RuntimeError("No job queue is configured")
    after_commit(session, lambda: queue.enqueue(name, payload))


def _clean_name(name: str) -> str:
    name = (name or "").strip()
    if not 1 <= len(name) <= MAX_NAME_LENGTH:
        raise InvalidKnowledgeBaseError(f"Name must be 1-{MAX_NAME_LENGTH} characters")
    return name


def _clean_description(description: str | None) -> str:
    description = (description or "").strip()
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise InvalidKnowledgeBaseError(f"Description must be at most {MAX_DESCRIPTION_LENGTH} characters")
    return description


def _embedding_view(provider: str, model: str, dimensions: int, credential_id: UUID | None) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": model,
        "dimensions": dimensions,
        "credential_id": str(credential_id) if credential_id else None,
    }


class KnowledgeBaseService:
    collection_name = staticmethod(collection_name)

    def __init__(
        self, session: AsyncSession, organisation_id: UUID, events: EventBus, adapters: "Adapters", settings: Settings
    ) -> None:
        self._session = session
        self._org_id = organisation_id
        self._events = events
        self._adapters = adapters
        self._settings = settings
        self._include_fake = settings.env == "test"  # the test-only `fake` embedding provider
        self._kbs = TenantScopedRepository(session, KnowledgeBase, organisation_id)
        self._documents = TenantScopedRepository(session, Document, organisation_id)
        self._credentials = TenantScopedRepository(session, ProviderCredential, organisation_id)
        self._jobs = TenantScopedRepository(session, IndexingJob, organisation_id)

    async def list(self) -> list[KnowledgeBase]:
        return await self._kbs.list(order_by=(KnowledgeBase.created_at, KnowledgeBase.id), limit=1000)

    async def get(self, kb_id: UUID, *, for_update: bool = False) -> KnowledgeBase:
        stmt = self._kbs.select(for_update=for_update).where(KnowledgeBase.id == kb_id)
        if for_update:
            stmt = stmt.execution_options(populate_existing=True)  # refresh after acquiring the lock
        kb = (await self._session.execute(stmt)).scalar_one_or_none()
        if kb is None:
            raise NotFoundError("Knowledge base not found")
        return kb

    async def create(
        self,
        name: str,
        description: str | None,
        embedding_provider: str,
        embedding_model: str,
        embedding_credential_id: UUID | None,
        chunking: ChunkingParams,
        reranker: str,
        reranker_params: dict[str, Any],
        actor: User,
    ) -> KnowledgeBase:
        name = _clean_name(name)
        description = _clean_description(description)
        await self._ensure_name_free(name)
        reranker, reranker_params = self._check_reranker(reranker, reranker_params)
        await EntitlementService(self._session, self._org_id, self._adapters).check_capacity("knowledge_bases")
        choice = EmbeddingChoice(embedding_provider, embedding_model, embedding_credential_id)
        probe = await self._probe_unlocked(choice)  # outbound, so before any row lock
        embedding = await self._resolve_embedding(choice, probe)
        kb = KnowledgeBase(
            name=name,
            description=description,
            embedding_provider=embedding.provider,
            embedding_model=embedding.model,
            embedding_dimensions=embedding.dimensions,
            embedding_credential_id=embedding.credential_id,
            chunking=chunking.model_dump(),
            reranker=reranker,
            reranker_params=reranker_params,
            active_version=1,
            created_by_user_id=actor.id,
        )
        await self._insert(kb)
        await self._events.publish(
            KnowledgeBaseCreated(
                organisation_id=self._org_id,
                actor_user_id=actor.id,
                knowledge_base_id=kb.id,
                name=kb.name,
                embedding_provider=kb.embedding_provider,
                embedding_model=kb.embedding_model,
            )
        )
        self._enqueue(
            "ensure_collection", {"kb_id": str(kb.id), "organisation_id": str(self._org_id), "version": 1}
        )
        return kb

    async def update(
        self,
        kb_id: UUID,
        *,
        name: str | None = None,
        description: str | None = None,
        embedding: EmbeddingChoice | None = None,
        chunking: ChunkingParams | dict[str, Any] | None = None,
        reranker: str | None = None,
        reranker_params: dict[str, Any] | None = None,
        actor: User,
    ) -> tuple[KnowledgeBase, bool]:
        """Applies the given changes (None = unchanged; a chunking dict is merged over the current chunking); returns (kb, rebuild_started).

        Name, description and reranker apply immediately. A new embedding model or chunking applies
        immediately when no document of the KB was ever indexed or is being indexed (queued,
        processing); otherwise it starts a rebuild into
        building_version (RebuildInProgressError if one is already pending). Switching only the
        credential applies immediately, except for openai_compatible, where another credential may
        mean another endpoint, so it rebuilds.
        """
        probe = None
        if embedding is not None and _embedding_differs(await self.get(kb_id), embedding):
            probe = await self._probe_unlocked(embedding)  # outbound, so before the KB lock
        kb = await self.get(kb_id, for_update=True)
        changes: dict[str, Any] = {}

        if name is not None:
            name = _clean_name(name)
            if name != kb.name:
                if name.lower() != kb.name.lower():
                    await self._ensure_name_free(name)
                changes["name"] = {"from": kb.name, "to": name}
                kb.name = name
                try:
                    await self._session.flush()
                except IntegrityError:  # lost a race on the unique name
                    raise KnowledgeBaseNameTakenError() from None
        if description is not None:
            description = _clean_description(description)
            if description != kb.description:
                changes["description"] = {"from": kb.description, "to": description}
                kb.description = description
        if reranker is not None or reranker_params is not None:
            new_reranker = reranker if reranker is not None else kb.reranker
            params = reranker_params if reranker_params is not None else ({} if reranker is not None else kb.reranker_params)
            new_reranker, params = self._check_reranker(new_reranker, params)
            if (new_reranker, params) != (kb.reranker, kb.reranker_params):
                changes["reranker"] = {
                    "from": {"name": kb.reranker, "params": kb.reranker_params},
                    "to": {"name": new_reranker, "params": params},
                }
                kb.reranker, kb.reranker_params = new_reranker, params

        embedding_requested = embedding is not None and _embedding_differs(kb, embedding)
        if isinstance(chunking, dict):  # partial: the fields sent, over the current (active) chunking
            chunking = ChunkingParams(**{**kb.chunking, **chunking})
        new_chunking = chunking.model_dump() if chunking is not None else None
        chunking_requested = new_chunking is not None and new_chunking != kb.chunking
        if (embedding_requested or chunking_requested) and kb.building_version is not None:
            raise RebuildInProgressError()

        rebuild = False
        if embedding_requested or chunking_requested:
            resolved = (
                await self._resolve_embedding(embedding, probe) if embedding is not None and embedding_requested else None
            )
            rebuild = await self._apply_index_config(
                kb, resolved, new_chunking if chunking_requested else None, changes
            )

        if changes:
            await self._session.flush()
            await self._events.publish(
                KnowledgeBaseUpdated(
                    organisation_id=self._org_id,
                    actor_user_id=actor.id,
                    knowledge_base_id=kb.id,
                    name=kb.name,
                    changes=changes,
                    rebuild=rebuild,
                )
            )
        return kb, rebuild

    async def delete(self, kb_id: UUID, actor: User) -> None:
        """Deletes the KB (documents, chunks and jobs cascade); its collections and stored files are
        purged by a job after commit. Allowed while a rebuild runs: the purge drops every version."""
        kb = await self.get(kb_id, for_update=True)
        payload = {
            "organisation_id": str(self._org_id),
            "kb_id": str(kb.id),
            "collections": kb_collection_names(kb),
            "storage_prefix": kb_storage_prefix(self._org_id, kb.id),
        }
        event = KnowledgeBaseDeleted(
            organisation_id=self._org_id, actor_user_id=actor.id, knowledge_base_id=kb.id, name=kb.name
        )
        await self._kbs.delete(kb)
        await self._events.publish(event)
        self._enqueue("purge_kb", payload)

    # --- internals ---------------------------------------------------------------------------------

    async def _apply_index_config(
        self,
        kb: KnowledgeBase,
        embedding: "_ResolvedEmbedding | None",
        chunking: dict[str, Any] | None,
        changes: dict[str, Any],
    ) -> bool:
        """Records the embedding/chunking change; returns True when it started a rebuild."""
        if embedding is not None:
            changes["embedding"] = {
                "from": _embedding_view(
                    kb.embedding_provider, kb.embedding_model, kb.embedding_dimensions, kb.embedding_credential_id
                ),
                "to": _embedding_view(embedding.provider, embedding.model, embedding.dimensions, embedding.credential_id),
            }
        if chunking is not None:
            changes["chunking"] = {"from": dict(kb.chunking), "to": chunking}

        vectors_change = chunking is not None or (
            embedding is not None
            and (
                (embedding.provider, embedding.model, embedding.dimensions)
                != (kb.embedding_provider, kb.embedding_model, kb.embedding_dimensions)
                or embedding.provider == "openai_compatible"
            )
        )
        if vectors_change and await self._has_indexed_documents(kb.id):
            target = embedding or _ResolvedEmbedding(
                kb.embedding_provider, kb.embedding_model, kb.embedding_dimensions, kb.embedding_credential_id
            )
            kb.building_version = kb.active_version + 1
            kb.building_embedding_provider = target.provider
            kb.building_embedding_model = target.model
            kb.building_embedding_dimensions = target.dimensions
            kb.building_embedding_credential_id = target.credential_id
            kb.building_chunking = chunking if chunking is not None else dict(kb.chunking)
            job = IndexingJob(
                knowledge_base_id=kb.id, document_id=None, kind="rebuild", kb_version=kb.building_version, status="queued"
            )
            await self._jobs.add(job)
            await self._session.flush()
            self._enqueue(
                "rebuild_kb",
                {
                    "kb_id": str(kb.id),
                    "organisation_id": str(self._org_id),
                    "version": kb.building_version,
                    "job_id": str(job.id),
                },
            )
            return True

        # Nothing indexed to keep serving: apply in place. New dimensions need a new collection.
        if embedding is not None:
            dims_changed = embedding.dimensions != kb.embedding_dimensions
            kb.embedding_provider = embedding.provider
            kb.embedding_model = embedding.model
            kb.embedding_dimensions = embedding.dimensions
            kb.embedding_credential_id = embedding.credential_id
            if dims_changed:
                kb.active_version += 1
                self._enqueue(
                    "ensure_collection",
                    {"kb_id": str(kb.id), "organisation_id": str(self._org_id), "version": kb.active_version},
                )
        if chunking is not None:
            kb.chunking = chunking
        return False

    async def _probe_unlocked(self, choice: EmbeddingChoice) -> "_Probe | None":
        """Probes the vector size of `choice` when the catalog does not know it (openai_compatible),
        without locking any row: the probe is an outbound call. None when no probe is needed or the
        choice is invalid (_resolve_embedding then reports why)."""
        provider, model = choice.provider.strip(), choice.model.strip()
        if not 1 <= len(model) <= MAX_MODEL_NAME_LENGTH:
            return None
        try:
            spec = get_embedding_spec(provider, model, include_fake=self._include_fake)
        except UnknownModelError:
            return None
        if spec.dimensions is not None or not spec.supports_language(await self._org_language()):
            return None
        credential: ProviderCredential | None = None
        if spec.requires_credential:
            if choice.credential_id is None:
                return None
            credential = await self._credentials.get(choice.credential_id)
            if credential is None or credential.provider != provider:
                return None
        elif choice.credential_id is not None:
            return None
        dimensions = await self._probe(provider, model, credential)
        return _Probe(provider, model, choice.credential_id, credential.base_url if credential else None, dimensions)

    async def _resolve_embedding(self, choice: EmbeddingChoice, probe: "_Probe | None") -> "_ResolvedEmbedding":
        """Validates `choice` and locks its credential; `probe` is _probe_unlocked()'s result for it."""
        provider, model = choice.provider.strip(), choice.model.strip()
        if not 1 <= len(model) <= MAX_MODEL_NAME_LENGTH:
            raise InvalidKnowledgeBaseError(f"Embedding model name must be 1-{MAX_MODEL_NAME_LENGTH} characters")
        spec = get_embedding_spec(provider, model, include_fake=self._include_fake)  # UnknownModelError
        language = await self._org_language()
        if not spec.supports_language(language):
            raise UnsupportedLanguageForModelError(model, language)

        credential: ProviderCredential | None = None
        if spec.requires_credential:
            if choice.credential_id is None:
                raise InvalidKnowledgeBaseError(f"An API key for provider {provider!r} is required")
            # FOR UPDATE pairs with CredentialService.delete's in-use check.
            credential = await self._credentials.get(choice.credential_id, for_update=True)
            if credential is None:
                raise NotFoundError("Credential not found")
            if credential.provider != provider:
                raise CredentialProviderMismatchError(credential.provider, provider)
        elif choice.credential_id is not None:
            raise InvalidKnowledgeBaseError(f"Provider {provider!r} does not use an API key")

        dimensions = spec.dimensions
        if dimensions is None:
            if probe is None or probe != _Probe(
                provider, model, choice.credential_id, credential.base_url if credential else None, probe.dimensions
            ):
                raise InvalidKnowledgeBaseError(
                    "The knowledge base or its API key changed while the embedding model was checked; try again"
                )
            dimensions = probe.dimensions
        return _ResolvedEmbedding(provider, model, dimensions, credential.id if credential else None)

    async def _probe(self, provider: str, model: str, credential: ProviderCredential | None) -> int:
        """Embeds DIMENSION_PROBE_TEXT once to learn the vector size (openai_compatible models)."""
        api_key = base_url = None
        if credential is not None:
            base_url = credential.base_url
            if base_url:  # SSRF guard: re-checked right before the outbound call
                await ensure_public_base_url(
                    base_url, allow_private=self._settings.effective_allow_private_base_urls()
                )
            api_key = CredentialService(
                self._session,
                self._org_id,
                self._adapters.cipher,
                self._adapters.credential_validator,
                self._events,
            ).decrypt_key(credential)
        embedder = self._adapters.embedders.create(
            provider, model, api_key=api_key, base_url=base_url, settings=self._settings
        )
        probe = getattr(embedder, "probe_dimensions", None)
        dims = await probe() if probe is not None else len(await embedder.embed_query(DIMENSION_PROBE_TEXT))
        try:
            return check_dimensions(dims)
        except ValueError as exc:
            raise InvalidKnowledgeBaseError(f"Embedding model returned unusable vectors: {exc}") from None

    def _check_reranker(self, name: str, params: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
        name = (name or "").strip()
        params = dict(params or {})
        model = params.get("model")
        if name == "cross_encoder" and isinstance(model, str) and model.strip() not in RERANKER_MODELS:
            raise UnknownModelError("cross_encoder", model[:100])  # only allow-listed models are loaded
        try:
            self._adapters.rerankers.create(name, params)  # validates name and params; loads nothing
        except UnknownProviderError:
            raise InvalidKnowledgeBaseError(f"Unknown reranker {name[:50]!r}") from None
        except ValueError as exc:
            raise InvalidKnowledgeBaseError(str(exc)) from None
        return name, params

    async def _org_language(self) -> str:
        stmt = select(Organisation.language).where(Organisation.id == self._org_id)
        language = (await self._session.execute(stmt)).scalar_one_or_none()
        if language is None:
            raise NotFoundError("Organisation not found")
        return language

    async def _has_indexed_documents(self, kb_id: UUID) -> bool:
        """Documents that were indexed at least once, or are on their way (queued, processing): a
        change in place would race their jobs. Merely uploaded ones have nothing to keep serving."""
        stmt = (
            self._documents.select(Document.id)
            .where(
                Document.knowledge_base_id == kb_id,
                or_(Document.indexed_version.is_not(None), Document.status.in_(("queued", "processing"))),
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None

    async def _ensure_name_free(self, name: str) -> None:
        stmt = self._kbs.select(KnowledgeBase.id).where(func.lower(KnowledgeBase.name) == name.lower())
        if (await self._session.execute(stmt)).first() is not None:
            raise KnowledgeBaseNameTakenError()

    async def _insert(self, kb: KnowledgeBase) -> None:
        """Flush inside a savepoint so a lost race on the unique name becomes KnowledgeBaseNameTakenError."""
        try:
            async with self._session.begin_nested():
                await self._kbs.add(kb)
                await self._session.flush()
        except IntegrityError:
            raise KnowledgeBaseNameTakenError() from None

    def _enqueue(self, name: str, payload: dict) -> None:
        enqueue_after_commit(self._session, self._adapters.job_queue, name, payload)


def _embedding_differs(kb: KnowledgeBase, embedding: EmbeddingChoice) -> bool:
    current = (kb.embedding_provider, kb.embedding_model, kb.embedding_credential_id)
    return (embedding.provider.strip(), embedding.model.strip(), embedding.credential_id) != current


@dataclass(frozen=True)
class _Probe:
    """A probed vector size, for the exact choice (and credential endpoint) it was probed with."""

    provider: str
    model: str
    credential_id: UUID | None
    base_url: str | None
    dimensions: int


@dataclass(frozen=True)
class _ResolvedEmbedding:
    provider: str
    model: str
    dimensions: int
    credential_id: UUID | None
