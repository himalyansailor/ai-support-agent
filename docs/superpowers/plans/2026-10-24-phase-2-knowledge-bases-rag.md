# Phase 2 — Knowledge Bases & RAG Pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Plan 2 of 8.** Builds on Phase 1 (`docs/superpowers/plans/2026-10-24-phase-1-foundation-orgs-auth-team.md`). Next: (3) Chat widget + LangGraph bot + risk.

**Goal:** A business can store its AI provider keys, create several knowledge bases (each with its own embedding model, chunking strategy and optional reranker), upload documents, click "start embedding", watch per-document indexing status and job logs, preview chunks, and test retrieval plus the answer the bot would give in a playground. Changing a knowledge base's embedding model rebuilds it in the background without breaking what is already served.

**Architecture:** New ports in `supportcore`: `SecretCipher`, `FileStorage`, `DocumentParser`, `Chunker`, `Embedder`, `VectorStore`, `Reranker`, `ChatModelFactory`, `JobQueue`. Adapters register in `ProviderRegistry` instances and are wired in one shared composition module, `supportcore/wiring.py`. Both the API container and the new Celery worker (`apps/worker`) use it. Upload and settings changes write rows and then enqueue jobs **after commit**. Workers run each job in `job_context(organisation_id)` with its own session and write a business-visible `IndexingJob` log. Chunk text lives in Postgres; vectors live in the vector store, in one collection per knowledge-base index version.

**Tech Stack (owner-decided):** pypdf, python-docx, beautifulsoup4 (parsing); langchain-text-splitters (chunking); langchain-openai (OpenAI and OpenAI-compatible embeddings and chat), langchain-anthropic (Claude chat); sentence-transformers (local embeddings, plus the cross-encoder reranker `BAAI/bge-reranker-base`); pgvector (default vector store) and chromadb (alternative adapter); MinIO via boto3 (S3 file storage) plus a local-disk adapter; `cryptography` Fernet (secrets); Celery + Redis (jobs) with opentelemetry-instrumentation-celery; python-multipart (uploads). Frontend: existing Next.js JS + shadcn.

**Spec:** `docs/superpowers/specs/2026-10-24-ai-support-platform-design.md` (§3 swappable components, §4.2 Knowledge bases, §4.4 LLM selection, §4.7 observability, §5 languages).

## Global Constraints

- All Phase 1 global constraints still apply (`.superpowers/sdd/2026-10-24-phase-1-foundation-orgs-auth-team/global-constraints.md`): JS-only frontend, **no git operations**, cross-tenant → 404, tenant queries through `TenantScopedRepository` (use `.select()` / `for_update`), every route takes `SessionDep`, and domain errors are registered in `DOMAIN_ERRORS` with a stable `code`.
- **Adapter wiring:** adapters are imported only by `packages/core/src/supportcore/wiring.py`, `apps/api/src/supportapi/container.py`, and `apps/worker/src/supportworker/`. Domain services depend only on ports. Extend `apps/api/tests/test_layering.py` to enforce this.
- **Secrets:** API keys are encrypted at rest with the `SecretCipher` port (Fernet). Responses only ever contain `last4`. Plaintext keys never appear in logs, spans, events, audit metadata or job logs.
- **Enqueueing:** jobs are only enqueued through `after_commit(session, fn)`, never inside an open transaction.
- **Job context:** every job runs inside `job_context(organisation_id, job_id)` with its own `UnitOfWork`.
- **Collections:** vector collection name is `kb_{kb_id.hex}_v{version}`. Every vector record carries `organisation_id`, `knowledge_base_id`, `document_id` and `chunk_id`, and every query filters on `organisation_id`.
- **Upload limits:** max upload `26214400` bytes (25 MB). Allowed extensions are `.pdf .docx .txt .md .markdown .html .htm`. Content is checked by sniffing as well as by extension:
  - PDF must start with `%PDF-`.
  - DOCX must be a zip containing `word/document.xml`.
  - Text, Markdown and HTML must decode as UTF-8, with a fallback to UTF-8-sig.
- **Chunking params:**
  - `strategy` is one of `fixed`, `recursive` (default) or `markdown`.
  - `chunk_size` is 100–8000 characters, default 1000.
  - `chunk_overlap` is 0 to `chunk_size // 2`, default 150.
- **Retrieval:** `top_k` 1–20, default 5. When the reranker is on, fetch `top_k * 4` candidates (capped at 80), then rerank down to `top_k`. Score = cosine similarity in [−1, 1], rounded to 4 decimal places.
- **Permissions:**
  - Credentials: `billing.manage` ("Manage billing & API keys").
  - AI settings writes: `bot.configure`.
  - KB and document writes, indexing and playground: `kb.manage`.
  - KB and document reads: `kb.manage` or `bot.configure`.
  - Changing org language: `org.manage`.
- **Model catalog:** the recommended default chat model is `anthropic` / `claude-sonnet-5-5`. The recommended local embedding model is `BAAI/bge-small-en-v1.5` (384 dims, English); the multilingual local option is `intfloat/multilingual-e5-small` (384 dims). OpenAI embeddings are `text-embedding-3-small` (1536) and `text-embedding-3-large` (3072). OpenAI and OpenAI-compatible chat model ids are free text (no invented model names in the catalog).
- **Test providers:** a `fake` embedding provider and a `fake` chat provider are registered **only when `settings.env == "test"`**. Tests never download models or call real APIs, except tests marked `@pytest.mark.models` (download local models) or `@pytest.mark.integration` (need Docker services). Both markers are skipped unless their env flag is set (`SUPPORT_RUN_MODEL_TESTS=1` / services reachable).

## Review Focus

1. **Duplicate, oversize or disguised uploads.** The same file uploaded twice to one KB gives 409 `duplicate_document`. More than 25 MB gives 413 `upload_too_large`. A `.pdf` that is really text gives 415 `unsupported_file_type`. Tests: Task 12 `test_upload_duplicate_409`, `test_upload_too_large_413`, `test_upload_disguised_type_415`.
2. **Changing the embedding model mid-flight.** The old index keeps serving until the rebuild completes, and the playground never mixes versions or dimensions. Test: Task 11 `test_rebuild_keeps_serving_old_version_until_swap`.
3. **Indexing failure and retry.** If the embedder raises mid-document, the document is `failed` with a readable error and the job is `failed` with steps recorded. Retrying produces exactly one set of chunks and vectors. Test: Task 11 `test_failed_job_retry_is_idempotent`.
4. **Cross-tenant ids.** KB, document, chunk, job or credential ids from another org give 404. A vector query is filtered by `organisation_id` even if the collection name were guessed. Tests: Task 12 `test_cross_tenant_kb_and_document_404`, Task 8 `test_query_filters_by_organisation`.
5. **Scanned or empty PDF.** A document with no extractable text ends `failed` with the message "No extractable text found (is this a scanned PDF?)", not `indexed` with 0 chunks. Test: Task 11 `test_empty_text_document_fails_clearly`.

---

## File Structure

```
infra/docker-compose.yml               + chroma (profile "chroma"), + minio bucket init
infra/prometheus/prometheus.yml        + worker scrape target
Makefile                               + dev-worker, + gen-secrets-key
packages/core/src/supportcore/
  config.py                            + Phase 2 settings
  wiring.py                            shared composition: registries + build_adapters(settings)
  db/session.py                        + after_commit()
  db/models/{ai.py,knowledge.py}       ProviderCredential, OrganisationAISettings, KnowledgeBase, Document, Chunk, IndexingJob
  secrets/{ports.py,fernet_cipher.py}
  ai/{catalog.py,ports.py,prompts.py}
  ai/embeddings/{openai_embedder.py,local_embedder.py,fake_embedder.py}
  ai/chat/{langchain_factory.py,fake_chat.py}
  ai/rerank/{none_reranker.py,cross_encoder_reranker.py}
  ai/credentials.py                    CredentialService (+ validation)
  ai/settings_service.py               OrganisationAISettingsService
  storage/{ports.py,s3_storage.py,local_storage.py}
  knowledge/parsing/{ports.py,pdf.py,docx.py,text.py,html.py}
  knowledge/chunking/{ports.py,langchain_chunkers.py}
  knowledge/vectorstore/{ports.py,pgvector_store.py,chroma_store.py}
  knowledge/{events.py,kb_service.py,document_service.py,indexing.py,retrieval.py,playground.py,jobs.py}
  jobs/{ports.py,inline_queue.py,celery_queue.py}
apps/api/src/supportapi/
  routers/{ai.py,knowledge.py,documents.py,playground.py}
  schemas/{ai.py,knowledge.py}
apps/api/migrations/versions/0002_ai_credentials.py, 0003_knowledge.py
apps/worker/
  pyproject.toml
  src/supportworker/{__init__.py,celery_app.py,runtime.py,metrics.py}
  tests/...
apps/web/
  app/orgs/[orgId]/settings/ai/page.jsx
  app/orgs/[orgId]/knowledge/page.jsx
  app/orgs/[orgId]/knowledge/[kbId]/page.jsx       tabs: Documents · Settings · Playground · Activity
  components/knowledge/*.jsx, components/ai/*.jsx, components/onboarding/{language-step,ai-step}.jsx
  lib/onboarding-steps.js (append steps), lib/permissions.js (+ helpers)
  tests/e2e/knowledge.spec.js
```

---

### Task 1: Phase 2 settings, infra, after-commit hook, shared wiring module

**Files:**
- Modify: `packages/core/src/supportcore/config.py`, `.env.example`, `infra/docker-compose.yml`, `infra/prometheus/prometheus.yml`, `Makefile`, `packages/core/src/supportcore/db/session.py`, `apps/api/src/supportapi/container.py`, `apps/api/tests/test_layering.py`
- Create: `packages/core/src/supportcore/wiring.py`
- Test: `packages/core/tests/test_config.py`, `packages/core/tests/db/test_after_commit.py`

**Interfaces:**
- Produces:
  - Settings fields:
    - Secrets: `secrets_key: SecretStr | None = None`
    - File storage: `file_storage: Literal["s3","local"] = "s3"`, `s3_endpoint_url: str | None = "http://localhost:9000"`, `s3_bucket: str = "support-documents"`, `s3_access_key: str = "support"`, `s3_secret_key: SecretStr = "supportsecret"`, `s3_region: str = "us-east-1"`, `local_storage_path: str = ".data/uploads"`
    - Vector store: `vector_store: Literal["pgvector","chroma"] = "pgvector"`, `chroma_url: str = "http://localhost:8001"`
    - Jobs: `job_queue: Literal["inline","celery"] = "celery"`, `celery_broker_url: str | None = None` (falls back to `redis_url`), `worker_metrics_port: int = 9101`
    - Uploads and models: `max_upload_bytes: int = 26214400`, `model_cache_dir: str = ".data/models"`, `embed_batch_size: int = 64`
  - Validator: `secrets_key` is required when `env == "prod"`. In dev/test, `effective_secrets_key()` returns a fixed dev Fernet key and logs one warning.
  - `db/session.after_commit(session: AsyncSession, callback: Callable[[], None]) -> None`. It registers the callback to run once after the session's next successful commit, and never on rollback. It uses `session.sync_session` `after_commit` / `after_rollback` events with per-transaction bookkeeping in `session.info`.
  - `wiring.py`: this task creates the module docstring and a `build_adapters(settings) -> Adapters` stub with an `Adapters` dataclass. Later tasks add registries and fields to it. `container.Container` gains `adapters: Adapters`.
  - Compose:
    - Chroma service `chromadb/chroma` on host 8001 under `profiles: ["chroma"]`.
    - A one-shot `minio-init` service (image `minio/mc` or the chainguard mc equivalent; check it pulls) that creates the `support-documents` bucket.
    - A Prometheus job for `host.docker.internal:9101`.
  - Makefile:
    - `dev-worker`: `uv run celery -A supportworker.celery_app worker --loglevel=INFO --concurrency=2`
    - `gen-secrets-key`: prints a Fernet key.

- [ ] **Step 1:** Write failing tests:

```python
def test_secrets_key_required_in_prod(monkeypatch): ...      # env=prod, no SUPPORT_SECRETS_KEY → ValidationError
def test_dev_secrets_key_fallback_is_valid_fernet(): ...      # Fernet(settings.effective_secrets_key()) works
def test_phase2_defaults(): ...                                # max_upload_bytes == 26214400, vector_store == "pgvector", job_queue == "celery"

async def test_after_commit_runs_once_after_commit(session_factory):
    calls = []
    async with session_factory() as s:
        after_commit(s, lambda: calls.append(1))
        assert calls == []
        await s.commit()
    assert calls == [1]

async def test_after_commit_not_run_on_rollback(session_factory): ...
async def test_after_commit_callbacks_do_not_leak_to_next_transaction(session_factory): ...
```

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. Extend the layering test: a module under `supportcore/` other than `wiring.py`, the adapter modules themselves and `supportcore.testing` must not import adapter modules. The adapter modules are listed in the test as path globs: `*/fernet_cipher.py`, `*/s3_storage.py`, `*/local_storage.py`, `*_embedder.py`, `*/langchain_factory.py`, `*/fake_chat.py`, `*_reranker.py`, `*_store.py`, `*/inline_queue.py`, `*/celery_queue.py`, `knowledge/parsing/{pdf,docx,text,html}.py` and `knowledge/chunking/langchain_chunkers.py`.
- [ ] **Step 4:** PASS. `docker compose -f infra/docker-compose.yml up -d` creates the bucket (check with `docker compose ... logs minio-init`).

---

### Task 2: Secrets port, provider credentials, org AI settings (backend)

**Files:**
- Create: `packages/core/src/supportcore/secrets/{ports.py,fernet_cipher.py}`, `packages/core/src/supportcore/db/models/ai.py`, `apps/api/migrations/versions/0002_ai_credentials.py`, `packages/core/src/supportcore/ai/{catalog.py,credentials.py,settings_service.py,events.py}`
- Modify: `wiring.py` (cipher registry `"fernet"`), `audit/subscribers.py` (new actions), `errors.py`
- Test: `packages/core/tests/secrets/test_fernet.py`, `packages/core/tests/ai/test_catalog.py`, `packages/core/tests/ai/test_credentials.py`

**Interfaces:**
- Produces:
  - `SecretCipher(Protocol)`: `encrypt(plaintext: str) -> str` and `decrypt(token: str) -> str`. `decrypt` raises `SecretDecryptError`. `FernetCipher(key: str)`.
  - Models:
    - `ProviderCredential(TenantMixin; id, provider: str, label: str, encrypted_api_key: Text, base_url: str | None, last4: str, created_by_user_id, last_validated_at)`, unique `(organisation_id, label)`.
    - `OrganisationAISettings(organisation_id PK/FK CASCADE, chat_provider: str | None, chat_model: str | None, chat_credential_id FK ProviderCredential SET NULL, temperature: float = 0.2)`.
  - `catalog.py`:
    - `EmbeddingModelSpec(provider, model, dimensions: int | None, languages: frozenset[str], requires_credential: bool, recommended: bool, label: str)`. `"*"` in `languages` means any language. `dimensions=None` means probed at KB creation.
    - `ChatModelSpec(provider, model, label, recommended, requires_credential=True)`.
    - Free-text providers `openai` (chat) and `openai_compatible` (chat and embeddings) get a spec with `model="*"`.
    - `LANGUAGES = [("en", "English")]`.
    - Functions: `embedding_models_for(language: str) -> list[EmbeddingModelSpec]`, `chat_models() -> list[ChatModelSpec]`, `get_embedding_spec(provider, model) -> EmbeddingModelSpec` (raises `UnknownModelError`).
    - The `fake` provider specs appear only when `include_fake=True`.
    - Exact entries are per Global Constraints.
  - `CredentialService(session, organisation_id, cipher, validator, events)`:
    - `create(provider, label, api_key, base_url, actor) -> ProviderCredential`. It strips the key, rejects an empty key with `CredentialInvalidError`, requires `base_url` for `openai_compatible` (`http(s)` URL), validates through `validator`, then stores the encrypted key and `last4`.
    - `list()`.
    - `delete(credential_id, actor)`. Raises `CredentialInUseError` (409, `credential_in_use`) if a KB or the AI settings reference it. The KB check is added in Task 10, through a hook list `in_use_checks`.
    - `decrypt_key(credential) -> str`, for adapters only.
  - `CredentialValidator(Protocol)`: `async validate(provider, api_key, base_url) -> None`. It raises `CredentialInvalidError(message)` with a sanitized provider message: no key echoed, at most 200 chars. Task 4 supplies the real implementation. In this task, tests use a stub.
  - `OrganisationAISettingsService(session, organisation_id, events)`: `get() -> OrganisationAISettings` (creates defaults lazily, recommended provider/model with credential None) and `update(chat_provider, chat_model, chat_credential_id, temperature, actor)`. It validates:
    - the provider and model against the catalog (free-text allowed where `model="*"`);
    - that the credential belongs to the org and has the matching provider (`CredentialProviderMismatchError` 422);
    - temperature between 0 and 1.
  - Events: `CredentialCreated(credential_id, provider, label)`, `CredentialDeleted(...)`, `AISettingsUpdated(changes)`. Audit actions: `credential.created`, `credential.deleted` (target `"credential"`) and `ai_settings.updated` (target `"organisation"`). Metadata never includes keys.
  - Errors in `DOMAIN_ERRORS`: `CredentialInvalidError` (422, `credential_invalid`), `CredentialInUseError` (409, `credential_in_use`), `CredentialLabelTakenError` (409, `credential_label_taken`), `CredentialProviderMismatchError` (422, `credential_provider_mismatch`), `UnknownModelError` (422, `unknown_model`).

- [ ] **Step 1:** Failing tests:

```python
def test_fernet_roundtrip_and_tamper_detection(): ...
def test_catalog_recommended_chat_model(): assert [m for m in chat_models() if m.recommended][0].model == "claude-sonnet-5-5"
def test_embedding_models_for_en_includes_bge_small_and_openai(): ...
def test_fake_provider_hidden_by_default(): ...
async def test_create_credential_stores_ciphertext_and_last4(session, org_a, owner): ...
    # row.encrypted_api_key != key; row.last4 == key[-4:]; decrypt_key(row) == key
async def test_create_credential_runs_validator_and_maps_error(...): ...
async def test_openai_compatible_requires_base_url(...): ...
async def test_delete_credential_in_use_by_ai_settings_409(...): ...
async def test_audit_metadata_never_contains_key(...): ...
async def test_ai_settings_rejects_credential_of_other_provider(...): ...
```

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement, including migration `0002` (hand-checked). `alembic check` passes.
- [ ] **Step 4:** PASS.

---

### Task 3: Embedder port + adapters (OpenAI, OpenAI-compatible, local, fake)

**Files:**
- Create: `packages/core/src/supportcore/ai/ports.py`, `packages/core/src/supportcore/ai/embeddings/{openai_embedder.py,local_embedder.py,fake_embedder.py}`
- Modify: `wiring.py` (`embedder_registry`)
- Test: `packages/core/tests/ai/test_embedders.py`

**Interfaces:**
- Produces:
  - `Embedder(Protocol)`: `model_id: str`, `dimensions: int`, `async embed_documents(texts: list[str]) -> list[list[float]]`, `async embed_query(text: str) -> list[float]`.
  - `EmbedderFactory`, built from the registry: `create(provider: str, model: str, *, api_key: str | None, base_url: str | None, settings: Settings) -> Embedder`. Registry keys: `openai`, `openai_compatible`, `local`, and `fake` (test only).
  - `OpenAIEmbedder`: wraps `langchain_openai.OpenAIEmbeddings(model, api_key, base_url)` using `aembed_documents` and `aembed_query`. It batches by `settings.embed_batch_size`. Dimensions come from the catalog spec, or from a probe call when the spec's `dimensions` is None.
  - `LocalEmbedder`:
    - Loads a `sentence_transformers.SentenceTransformer(model, cache_folder=settings.model_cache_dir)` once per process per model, through a module-level lock and cache.
    - `encode` runs in `asyncio.to_thread` with `normalize_embeddings=True`.
    - The `intfloat/multilingual-e5-*` models need the `"passage: "` prefix on documents and `"query: "` on queries.
  - `FakeEmbedder(dimensions=8)`: deterministic. A SHA-256 hash of the lowercased token bag becomes a normalized vector, so identical texts give identical vectors and texts sharing words are closer. It records `calls`. Setting `fail_on: str | None` makes it raise `EmbeddingError` for any text containing that substring.
  - `EmbeddingError(DomainError)` carries a readable message.

- [ ] **Step 1:** Failing tests: `test_fake_embedder_deterministic_and_normalised`, `test_fake_embedder_similar_texts_closer`, `test_fake_embedder_fail_on`, `test_factory_unknown_provider_lists_available`, `test_openai_embedder_batches_requests` (patch `OpenAIEmbeddings.aembed_documents`; 130 texts with batch 64 → 3 calls), `test_e5_prefixes_applied` (patch the model's `encode`), and `@pytest.mark.models test_local_bge_small_real_embedding` (dimension 384).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. Register the `models` marker in the root pytest config, skipped unless `SUPPORT_RUN_MODEL_TESTS=1`.
- [ ] **Step 4:** PASS.

---

### Task 4: Chat model factory (LangChain) + real credential validator + AI routes

**Files:**
- Create: `packages/core/src/supportcore/ai/chat/{langchain_factory.py,fake_chat.py}`, `packages/core/src/supportcore/ai/validation.py`, `apps/api/src/supportapi/routers/ai.py`, `apps/api/src/supportapi/schemas/ai.py`
- Modify: `wiring.py` (`chat_registry`), `container.py`, `main.py`, `routers/orgs.py` (`PATCH /orgs/{id}` language)
- Test: `packages/core/tests/ai/test_chat_factory.py`, `apps/api/tests/test_ai_routes.py`

**Interfaces:**
- Produces:
  - `ChatModelFactory`: `create(provider, model, *, api_key, base_url, temperature) -> langchain_core.language_models.BaseChatModel`.
    - `anthropic` → `ChatAnthropic`.
    - `openai` → `ChatOpenAI`.
    - `openai_compatible` → `ChatOpenAI(base_url=...)`.
    - `fake` (test only) → `FakeListChatModel(responses=["Fake answer [1]"])`.
  - `ProviderCredentialValidator(embedder_factory, chat_factory)` implements Task 2's `CredentialValidator`:
    - `anthropic`: one `ainvoke("ping")` with `max_tokens=1` against the recommended model.
    - `openai`: an `embed_query("ping")` with `text-embedding-3-small`.
    - `openai_compatible`: GET `{base_url}/models` with `Authorization: Bearer`; HTTP 2xx is valid.
    - Each call has a 10 s timeout. Any exception becomes `CredentialInvalidError(sanitized)`.
  - Routes:
    - `GET /ai/catalog?language=en` (auth): `{"chat_models":[...], "embedding_models":[...], "languages":[{"code","label"}]}`.
    - `GET/POST /orgs/{org_id}/ai/credentials` and `DELETE /orgs/{org_id}/ai/credentials/{id}` (`billing.manage`). The credential view is `{"id","provider","label","base_url","last4","created_at","last_validated_at"}`.
    - `GET /orgs/{org_id}/ai/settings` (any member) and `PUT /orgs/{org_id}/ai/settings` (`bot.configure`). The view is `{"chat_provider","chat_model","chat_credential_id","temperature","ready": bool}`, where `ready` = a credential is set and the provider matches.
    - `PATCH /orgs/{org_id}` `{"language"}` (`org.manage`). The language must be in `LANGUAGES`; otherwise 422 `unsupported_language`.

- [ ] **Step 1:** Failing tests: `test_factory_builds_anthropic_with_key` (assert type and model; no network), `test_validator_sanitises_provider_error` (the provider error contains the key string, and the raised message must not), `test_credentials_crud_never_returns_key`, `test_agent_cannot_list_credentials_403`, `test_ai_settings_ready_flag`, `test_catalog_filters_by_language`, `test_patch_org_language`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. Tests use `fake` providers through the test-only registration.
- [ ] **Step 4:** PASS.

---

### Task 5: FileStorage port (S3/MinIO + local disk)

**Files:**
- Create: `packages/core/src/supportcore/storage/{ports.py,s3_storage.py,local_storage.py}`
- Modify: `wiring.py` (`storage_registry`)
- Test: `packages/core/tests/storage/test_storage.py`

**Interfaces:**
- Produces:
  - `FileStorage(Protocol)`: `async put(key: str, data: bytes, content_type: str) -> None`, `async get(key: str) -> bytes` (raises `StoredFileNotFoundError`), `async delete(key: str) -> None` (idempotent), `async delete_prefix(prefix: str) -> int`.
  - `S3FileStorage(endpoint_url, bucket, access_key, secret_key, region)` uses boto3 in `asyncio.to_thread`.
  - `LocalFileStorage(root)` rejects keys containing `..` or starting with `/` (`ValueError`) and writes atomically (temp file + rename).
  - Key format, used by Task 10: `orgs/{org_id}/kbs/{kb_id}/documents/{document_id}/{sha256}`.

- [ ] **Step 1:** Failing tests, parametrised over local (tmp_path) and S3 (`@pytest.mark.integration`, MinIO): `test_put_get_roundtrip`, `test_get_missing_raises`, `test_delete_idempotent`, `test_delete_prefix_counts`, and `test_local_rejects_path_traversal`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** PASS (integration cases run against the compose MinIO).

---

### Task 6: Document parsers

**Files:**
- Create: `packages/core/src/supportcore/knowledge/parsing/{ports.py,pdf.py,docx.py,text.py,html.py}`
- Modify: `wiring.py` (`parser_registry` keyed by normalized type: `pdf`, `docx`, `text`, `markdown`, `html`)
- Test: `packages/core/tests/knowledge/test_parsing.py`. Fixtures are generated at test time in `packages/core/tests/knowledge/conftest.py`: DOCX with python-docx; a 2-page text PDF as a minimal hand-written PDF byte string (pypdf can't add text to pages, so build the content streams by hand); an image-only/blank PDF for the empty-text case.

**Interfaces:**
- Produces:
  - `ParsedDocument(text: str, pages: list[PageSpan])`, where `PageSpan(page: int, char_start: int, char_end: int)` (PDF only; empty for other types), and `title: str | None`.
  - `DocumentParser(Protocol)`: `parse(data: bytes, filename: str) -> ParsedDocument`. It is synchronous; callers use `to_thread`.
  - `detect_file_type(filename: str, data: bytes) -> str`. It returns one of `pdf docx text markdown html`. It raises `UnsupportedFileTypeError` (415, `unsupported_file_type`) when the extension is not allowed or the content sniff fails (rules in Global Constraints).
  - Parse failures and empty text: `ParseError(message)`. For empty or whitespace-only extracted text the message is exactly "No extractable text found (is this a scanned PDF?)".
  - HTML parser: drop `script`, `style`, `nav`, `footer` and `header`; keep the heading text; collapse whitespace; `title` from `<title>`.
  - DOCX parser: paragraphs and table cells in document order, joined with `\n`; headings become Markdown `#` lines, so the markdown chunker can use them.

- [ ] **Step 1:** Failing tests: `test_detect_types_by_extension_and_sniff`, `test_disguised_pdf_rejected` (`.pdf` holding text), `test_pdf_pages_spans` (2-page PDF), `test_docx_headings_become_markdown`, `test_html_strips_scripts_and_nav`, `test_empty_text_raises_parse_error_message`, `test_text_utf8_sig_ok_and_binary_rejected`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** PASS.

---

### Task 7: Chunking strategies

**Files:**
- Create: `packages/core/src/supportcore/knowledge/chunking/{ports.py,langchain_chunkers.py}`
- Modify: `wiring.py` (`chunker_registry`: `fixed`, `recursive`, `markdown`)
- Test: `packages/core/tests/knowledge/test_chunking.py`

**Interfaces:**
- Produces:
  - `ChunkingParams(strategy: str, chunk_size: int = 1000, chunk_overlap: int = 150)` (pydantic). It validates the ranges in Global Constraints and raises `InvalidChunkingParamsError` (422, `invalid_chunking_params`).
  - `ChunkDraft(ordinal: int, text: str, char_start: int, char_end: int, metadata: dict)`. `metadata` may hold `page` (or `pages: [int]`) and `headings: [str]`.
  - `Chunker(Protocol)`: `chunk(doc: ParsedDocument, params: ChunkingParams) -> list[ChunkDraft]`.
    - `fixed` uses `CharacterTextSplitter(separator="", chunk_size, chunk_overlap, add_start_index=True)`.
    - `recursive` uses `RecursiveCharacterTextSplitter(..., add_start_index=True)`.
    - `markdown` uses `MarkdownHeaderTextSplitter(headers_to_split_on=[("#","h1"),("##","h2"),("###","h3")], strip_headers=False)`, then recursive splitting within each section. It computes `char_start` by locating each section in the original text.
  - Every chunker drops whitespace-only chunks, numbers ordinals from 0, and fills `page`/`pages` from `doc.pages` by overlapping char spans.

- [ ] **Step 1:** Failing tests: `test_params_validation` (overlap > size/2 → error; size 50 → error), `test_fixed_respects_size_and_overlap`, `test_recursive_prefers_paragraph_boundaries`, `test_markdown_carries_headings_metadata`, `test_char_spans_match_source` (`doc.text[c.char_start:c.char_end] == c.text` for all strategies), `test_pages_attached_from_pdf_spans`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** PASS.

---

### Task 8: VectorStore port (pgvector default, Chroma)

**Files:**
- Create: `packages/core/src/supportcore/knowledge/vectorstore/{ports.py,pgvector_store.py,chroma_store.py}`
- Modify: `wiring.py` (`vector_store_registry`), `apps/api/migrations/env.py` (`include_object` ignores tables named `kb_vectors_%`)
- Test: `packages/core/tests/knowledge/test_vectorstore.py`

**Interfaces:**
- Produces:
  - `VectorRecord(chunk_id: UUID, document_id: UUID, organisation_id: UUID, knowledge_base_id: UUID, embedding: list[float], metadata: dict)` and `VectorHit(chunk_id, document_id, score: float, metadata: dict)`.
  - `VectorStore(Protocol)`, all methods async:
    - `ensure_collection(name: str, dimensions: int) -> None`
    - `upsert(name: str, records: list[VectorRecord]) -> None` (idempotent per `chunk_id`)
    - `query(name: str, embedding: list[float], *, organisation_id: UUID, k: int, document_ids: list[UUID] | None = None) -> list[VectorHit]`, ordered by score descending, cosine.
    - `delete_documents(name: str, document_ids: list[UUID]) -> None`
    - `drop_collection(name: str) -> None` (idempotent)
    - `count(name: str) -> int`
  - `PgVectorStore(engine)`:
    - One physical table per dimension, `kb_vectors_{dim}` (`id uuid pk = chunk_id`, `collection text`, `organisation_id uuid`, `knowledge_base_id uuid`, `document_id uuid`, `embedding vector(dim)` — or `halfvec(dim)` when dim > 2000, `metadata jsonb`).
    - Indexes: HNSW on `embedding` with cosine ops (`vector_cosine_ops` / `halfvec_cosine_ops`), plus btree on `(collection, organisation_id)` and `(collection, document_id)`.
    - Tables are created lazily in `ensure_collection` with `CREATE TABLE/INDEX IF NOT EXISTS`, holding a Postgres advisory lock keyed on the table name.
    - A collection→dimension registry lives in a small regular table `vector_collections(name pk, dimensions, created_at)`, which is part of migration `0003`. `drop_collection` deletes rows with `collection = name` and the registry row.
    - Score is `1 - (embedding <=> query)`.
  - `ChromaVectorStore(url)` uses `chromadb.HttpClient`, one Chroma collection per name with `hnsw:space=cosine`, and a metadata `where` on `organisation_id`. Score is `1 - distance`.

- [ ] **Step 1:** Failing tests, parametrised `[pgvector, chroma]`. The chroma case is `@pytest.mark.integration` and needs `docker compose --profile chroma up -d chroma`:
  - `test_upsert_query_orders_by_similarity`
  - `test_upsert_idempotent`
  - `test_query_filters_by_organisation` (records for org A and org B in the same collection; querying as A returns only A)
  - `test_delete_documents`
  - `test_drop_collection_idempotent`
  - `test_pgvector_halfvec_for_3072_dims`
  - `test_alembic_check_ignores_vector_tables` (run `alembic check` through `subprocess` after `ensure_collection`)
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** PASS.

---

### Task 9: Reranker port (none + local cross-encoder)

**Files:**
- Create: `packages/core/src/supportcore/ai/rerank/{none_reranker.py,cross_encoder_reranker.py}`; extend `ai/ports.py`
- Modify: `wiring.py` (`reranker_registry`: `none`, `cross_encoder`, `fake` test-only)
- Test: `packages/core/tests/ai/test_rerankers.py`

**Interfaces:**
- Produces:
  - `RerankCandidate(chunk_id: UUID, text: str, score: float)` and `RerankResult(chunk_id, score: float, rerank_score: float | None)`.
  - `Reranker(Protocol)`: `async rerank(query: str, candidates: list[RerankCandidate], top_n: int) -> list[RerankResult]`.
    - `NoneReranker` returns the first `top_n` with `rerank_score=None`.
    - `CrossEncoderReranker(model="BAAI/bge-reranker-base", cache_dir)` loads once per process, runs `predict` in a thread, and sorts by `rerank_score` descending.
    - `FakeReranker` reverses the order (so tests can see it ran).
  - Reranker params on a KB: `{"model": str}`. Only `cross_encoder` uses it; the default is the model above.

- [ ] **Step 1:** Failing tests: `test_none_reranker_truncates`, `test_fake_reranker_reorders`, `test_cross_encoder_sorts_by_score` (patch the `CrossEncoder.predict` return), and `@pytest.mark.models test_cross_encoder_real_model_prefers_relevant`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** PASS.

---

### Task 10: Knowledge base + document domain (models, services, migration)

**Files:**
- Create: `packages/core/src/supportcore/db/models/knowledge.py`, `apps/api/migrations/versions/0003_knowledge.py`, `packages/core/src/supportcore/knowledge/{events.py,kb_service.py,document_service.py}`
- Modify: `ai/credentials.py` (register a KB in-use check), `audit/subscribers.py`, `errors.py`
- Test: `packages/core/tests/knowledge/test_kb_service.py`, `packages/core/tests/knowledge/test_document_service.py`

**Interfaces:**
- Produces:
  - Models (all `TenantMixin`):
    - `KnowledgeBase`:
      - `id`, `name` (unique per org, case-insensitive check in the service), `description`.
      - Embedding: `embedding_provider`, `embedding_model`, `embedding_dimensions: int`, `embedding_credential_id` (FK SET NULL; nullable for `local`).
      - Chunking and reranking: `chunking: JSONB` (ChunkingParams dump), `reranker: str = "none"`, `reranker_params: JSONB = {}`.
      - Versions: `active_version: int = 1`, `building_version: int | None`.
      - `created_by_user_id`.
    - `Document`: `id`, `knowledge_base_id` (FK CASCADE), `filename`, `file_type`, `size_bytes`, `sha256`, `storage_key`, `status: "uploaded"|"queued"|"processing"|"indexed"|"failed"`, `error: str | None`, `chunk_count: int = 0`, `indexed_version: int | None`, `uploaded_by_user_id`. Unique `(knowledge_base_id, sha256)`.
    - `Chunk`: `id`, `knowledge_base_id`, `document_id` (FK CASCADE), `kb_version: int`, `ordinal`, `text`, `char_start`, `char_end`, `metadata: JSONB`. Index `(document_id, kb_version, ordinal)`.
    - `IndexingJob`: `id`, `knowledge_base_id`, `document_id` (nullable, FK CASCADE), `kind: "document"|"rebuild"`, `kb_version`, `status: "queued"|"running"|"succeeded"|"failed"`, `attempts: int`, `error`, `steps: JSONB list`, `queued_at`, `started_at`, `finished_at`. Index `(knowledge_base_id, queued_at desc)`.
    - `vector_collections` (Task 8's registry table).
  - `KnowledgeBaseService(session, organisation_id, events, adapters)`:
    - `create(name, description, embedding_provider, embedding_model, embedding_credential_id, chunking: ChunkingParams, reranker, reranker_params, actor) -> KnowledgeBase`.
      - It resolves the catalog spec, requires a credential of the matching provider when `requires_credential`, and checks the model supports the org language (`unsupported_language_for_model` 422).
      - When the spec's dimensions are None, it probes the dimensions by embedding `"dimension probe"`.
      - It calls `ensure_collection(collection_name(kb, 1), dims)` after commit, via `after_commit` enqueueing an `ensure_collection` job. That keeps the vector store out of the request transaction.
    - `list()`, `get(kb_id)`.
    - `update(kb_id, *, name=None, description=None, embedding=..., chunking=None, reranker=None, reranker_params=None, actor)`:
      - Changing `embedding_*` or `chunking` while documents exist sets `building_version = active_version + 1` and enqueues a `rebuild_kb` job after commit.
      - If a rebuild is already running, it fails with `RebuildInProgressError` (409, `rebuild_in_progress`).
      - Reranker and name changes apply immediately, with no rebuild.
    - `delete(kb_id, actor)`: deletes rows, then after commit enqueues a `purge_kb` job (drop both collections, `storage.delete_prefix`).
    - `collection_name(kb, version) -> str` per Global Constraints.
  - `DocumentService(session, organisation_id, events, adapters, settings)`:
    - `upload(kb_id, filename, data: bytes, actor) -> Document`.
      - It checks the size first: `UploadTooLargeError` (413, `upload_too_large`).
      - Then `detect_file_type`, then sha256 dedupe: `DuplicateDocumentError` (409, `duplicate_document`, message includes the existing filename).
      - Then `storage.put`, then the row with status `uploaded`.
      - If the DB insert fails after the put, it deletes the stored object.
    - `start_indexing(kb_id, document_ids: list[UUID] | None, actor) -> list[IndexingJob]`. It takes the given documents, or by default all `uploaded` and `failed` documents. Each becomes `queued` with one `IndexingJob(kind="document", kb_version=kb.building_version or kb.active_version)`, enqueued after commit. Already `queued` or `processing` documents are skipped.
    - `reindex_document(document_id, actor)`.
    - `delete(document_id, actor)`: deletes the row (chunks cascade), then after commit enqueues `purge_document` (vectors in active and building collections, and the stored file).
    - `list(kb_id, status=None)`, `get(document_id)`.
    - `chunks(document_id, *, version=None, limit=50, offset=0) -> (items, total)`, defaulting to the version that is served.
  - Events: `KnowledgeBaseCreated`, `KnowledgeBaseUpdated(changes, rebuild: bool)`, `KnowledgeBaseDeleted`, `DocumentUploaded`, `DocumentDeleted`, `IndexingStarted(document_ids)`. Audit actions: `kb.created`, `kb.updated`, `kb.deleted`, `document.uploaded`, `document.deleted`, `kb.indexing_started`.
  - `JobQueue` usage: services receive `adapters.job_queue` (Task 11 defines the port; this task depends on the port signature `enqueue(name: str, payload: dict) -> None` and uses an in-test recording fake).

- [ ] **Step 1:** Failing tests: `test_create_kb_requires_matching_credential`, `test_create_kb_local_needs_no_credential`, `test_kb_name_unique_case_insensitive`, `test_update_embedding_starts_rebuild_and_enqueues_after_commit`, `test_update_while_rebuilding_409`, `test_reranker_change_no_rebuild`, `test_upload_dedupes_by_sha256`, `test_upload_too_large`, `test_upload_cleans_storage_if_db_insert_fails`, `test_start_indexing_skips_in_flight_documents`, `test_delete_document_enqueues_purge_after_commit`, `test_credential_in_use_by_kb_409`, `test_audit_actions_written`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement, including migration `0003` (hand-checked). `alembic check` passes.
- [ ] **Step 4:** PASS.

---

### Task 11: Job queue port, worker app, indexing pipeline

**Files:**
- Create: `packages/core/src/supportcore/jobs/{ports.py,inline_queue.py,celery_queue.py}`, `packages/core/src/supportcore/knowledge/{indexing.py,jobs.py}`, `apps/worker/pyproject.toml`, `apps/worker/src/supportworker/{__init__.py,celery_app.py,runtime.py,metrics.py}`
- Modify: root `pyproject.toml` (workspace member `apps/worker`), `wiring.py` (`job_queue_registry`), `observability/tracing.py` (optional Celery instrumentation hook called by the worker)
- Test: `packages/core/tests/knowledge/test_indexing.py`, `apps/worker/tests/test_celery_app.py`

**Interfaces:**
- Produces:
  - `JobQueue(Protocol)`: `enqueue(name: str, payload: dict) -> None` (sync; called from `after_commit`).
  - `InlineJobQueue(runtime_factory)`: it schedules `dispatch(name, payload)` as an asyncio task on the running loop and keeps those tasks. `async drain() -> None` awaits all pending tasks, including ones they enqueue; tests use it.
  - `CeleryJobQueue(celery_app)`: `send_task("supportworker.run_job", args=[name, payload])`, with OTel context injected by the Celery instrumentation.
  - `jobs.py`: `JOB_HANDLERS: dict[str, Callable[[JobRuntime, dict], Awaitable[None]]]` with `ensure_collection`, `index_document`, `rebuild_kb`, `purge_document` and `purge_kb`. `async dispatch(runtime: JobRuntime, name: str, payload: dict) -> None` raises `UnknownJobError`.
  - `JobRuntime(settings, session_factory, adapters, events)`: built by `supportworker.runtime.build_runtime()` and by the API for inline mode.
  - `IndexingService(runtime)`:
    - `async index_document(job_id: UUID) -> None`.
      - Load the job (and its org) inside `job_context`. Mark the job `running` (`attempts += 1`) and the document `processing`, then commit.
      - Steps, each timed and appended to `job.steps` as `{"name","status","duration_ms","detail"}`: `download`, then `parse` (in a thread), then `chunk`, then `embed` (batched), then `upsert`.
      - Idempotency: before inserting, delete existing `Chunk` rows and vectors for `(document_id, kb_version)`.
      - On success: the document becomes `indexed`, with `chunk_count`, `indexed_version` and `error = None`; the job becomes `succeeded`.
      - On `ParseError`, `EmbeddingError` or any exception: the document becomes `failed` with `error` = a readable message (an unexpected exception gives "Indexing failed: <ExceptionType>"; never a stack trace). The job becomes `failed` with the error, and the failing step has status `failed`. The job never re-raises out of the handler; this is logged and counted.
      - If the KB or document was deleted meanwhile, the job is marked `failed` with "Document no longer exists" and exits cleanly.
    - `async rebuild_kb(kb_id, version)`:
      - `ensure_collection` for the new version with the new dims.
      - One `index_document`-equivalent run per non-deleted document into `version`, recorded on one `IndexingJob(kind="rebuild")`, with per-document results in the steps.
      - When every document succeeded or failed, it swaps `active_version = version` and `building_version = None`, drops the old collection, and deletes old-version chunks.
      - A rebuild where every document failed does **not** swap; it records failure and clears `building_version`.
  - Metrics (prometheus_client, labels include `organisation_id`):
    - `support_indexing_jobs_total{kind,status,organisation_id}`
    - `support_indexing_job_duration_seconds{kind}`
    - `support_indexed_chunks_total{organisation_id}`
    - `support_embedding_requests_total{provider,status}`
  - Worker:
    - `celery_app.py` builds `Celery("supportworker", broker=settings.celery_broker_url or settings.redis_url)` with the task `supportworker.run_job(name, payload)`, which calls `asyncio.run(dispatch(...))` on a per-process runtime.
    - It sets `task_acks_late=True`, `worker_prefetch_multiplier=1` and `task_time_limit=1800`.
    - `metrics.py` starts `prometheus_client.start_http_server(settings.worker_metrics_port)` on `worker_process_init`.
    - Logging and tracing are configured with `service_name="support-worker"`.

- [ ] **Step 1:** Failing tests (core, inline queue, fake embedder, local storage):

```python
async def test_index_document_happy_path(kb_with_doc, runtime):
    # upload markdown → start_indexing → drain → document.status == "indexed", chunk_count > 0,
    # vector_store.count(collection) == chunk_count, job.status == "succeeded", step names in order
async def test_empty_text_document_fails_clearly(...):
    # error == "No extractable text found (is this a scanned PDF?)"; job failed; no chunks
async def test_failed_job_retry_is_idempotent(...):
    # fake embedder fail_on="BOOM" → failed; set fail_on=None; reindex → indexed;
    # Chunk rows for doc/version == chunk_count and vector count == chunk_count (no duplicates)
async def test_rebuild_keeps_serving_old_version_until_swap(...):
    # index v1; change embedding (fake dims 8 → fake dims 16) → during rebuild active_version == 1 and
    # retrieval (Task 13 service, or direct vector query) uses v1 collection; after drain active_version == 2,
    # old collection dropped, old chunks gone
async def test_rebuild_all_failed_does_not_swap(...): ...
async def test_deleted_document_job_exits_cleanly(...): ...
async def test_job_logs_carry_org_context(caplog_json): ...   # log lines emitted during the job include organisation_id
def test_worker_registers_run_job_task(): ...                  # apps/worker
```

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** PASS. Manual check: `make dev-worker`, with the API using `SUPPORT_JOB_QUEUE=celery`. Upload and index one file through curl; the worker log shows JSON lines with `organisation_id` and `job_id`; Jaeger shows the `support-worker` service; `curl localhost:9101/metrics` shows `support_indexing_jobs_total`.

---

### Task 12: Knowledge base + document API routes

**Files:**
- Create: `apps/api/src/supportapi/routers/{knowledge.py,documents.py}`, `apps/api/src/supportapi/schemas/knowledge.py`
- Modify: `container.py` (inline runtime when `job_queue == "inline"`), `main.py`, `errors.py`
- Test: `apps/api/tests/test_knowledge_routes.py`, `apps/api/tests/test_document_routes.py`

**Interfaces:**
- Produces (all under `/orgs/{org_id}`):
  - KB routes:
    - `GET /kbs` → list.
    - `POST /kbs` → 201 KB view.
    - `GET /kbs/{kb_id}` and `PATCH /kbs/{kb_id}` (the response includes `"rebuild_started": bool`).
    - `DELETE /kbs/{kb_id}` → 204.
  - KB view: `{"id","name","description","embedding":{"provider","model","dimensions","credential_id"},"chunking":{...},"reranker":{"name","params"},"active_version","building_version","document_counts":{"uploaded","queued","processing","indexed","failed"},"created_at"}`.
  - Document routes:
    - `POST /kbs/{kb_id}/documents`: multipart `file`, one file per request. It streams into memory with a hard cap of `max_upload_bytes + 1`, rejecting early with 413. 201 document view.
    - `GET /kbs/{kb_id}/documents?status=`.
    - `GET /kbs/{kb_id}/documents/{doc_id}` and `DELETE /kbs/{kb_id}/documents/{doc_id}` → 204.
    - `POST /kbs/{kb_id}/documents/index` `{"document_ids": [..] | null}` → 202 `{"jobs":[{"id","document_id"}]}` ("start embedding").
    - `POST /kbs/{kb_id}/documents/{doc_id}/reindex` → 202.
    - `GET /kbs/{kb_id}/documents/{doc_id}/chunks?limit=50&offset=0` → `{"items":[{"id","ordinal","text","char_start","char_end","metadata"}],"total","version"}`.
  - `GET /kbs/{kb_id}/jobs?limit=50&before=<cursor>`: same opaque keyset cursor style as audit logs, giving `{"items":[{"id","kind","document_id","document_name","status","kb_version","attempts","error","steps","queued_at","started_at","finished_at"}],"next_before"}`.
  - Document view: `{"id","filename","file_type","size_bytes","status","error","chunk_count","indexed_version","uploaded_at"}`. Never `storage_key`.
  - Permissions per Global Constraints.

- [ ] **Step 1:** Failing tests (inline queue plus fake providers; tests `await` the queue's `drain()` through an app-state handle):
  - `test_create_kb_and_list`
  - `test_upload_index_and_chunks_flow` (upload `.md`, then `index`, then drain; status `indexed`; chunks non-empty)
  - `test_upload_duplicate_409`
  - `test_upload_too_large_413` (set `max_upload_bytes` small in `test_settings`)
  - `test_upload_disguised_type_415`
  - `test_cross_tenant_kb_and_document_404` (KB, doc, chunks and jobs ids)
  - `test_agent_cannot_upload_403`
  - `test_viewer_with_bot_configure_can_read_not_write` (custom role)
  - `test_jobs_log_shows_steps`
  - `test_patch_embedding_returns_rebuild_started`
  - `test_document_view_never_exposes_storage_key`
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** PASS.

---

### Task 13: Retrieval + playground

**Files:**
- Create: `packages/core/src/supportcore/knowledge/{retrieval.py,playground.py}`, `packages/core/src/supportcore/ai/prompts.py`, `apps/api/src/supportapi/routers/playground.py`
- Test: `packages/core/tests/knowledge/test_retrieval.py`, `apps/api/tests/test_playground_routes.py`

**Interfaces:**
- Produces:
  - `RetrievalService(session, organisation_id, adapters)`:
    - `async retrieve(kb_ids: list[UUID], query: str, top_k: int) -> RetrievalResult`. Phase 3 calls it with several KBs.
    - Per KB: embed the query with that KB's embedder (cache the embedder per KB within the call), query its **active** collection with `k = top_k*4 if reranker else top_k`, and hydrate `Chunk` rows and document filenames from Postgres. Hits whose chunk row is missing are dropped.
    - Across KBs: merge by score, apply each KB's reranker to its own candidates, then take the global `top_k` by (`rerank_score` if present else `score`).
    - `RetrievalResult(chunks: list[RetrievedChunk], timings_ms: dict[str, int])`, where `RetrievedChunk(chunk_id, knowledge_base_id, document_id, document_name, ordinal, text, score, rerank_score, metadata)`.
  - `prompts.build_rag_messages(question: str, chunks: list[RetrievedChunk], *, organisation_name: str, language: str) -> list[BaseMessage]`.
    - The system prompt instructs: answer only from the numbered context; cite as `[n]`; if the answer is not in the context, say you don't know and offer a human; answer in the language with code `language`.
    - The context block numbers chunks from 1, each with its document name.
  - `PlaygroundService(session, organisation_id, adapters, cipher)`: `async run(kb_id, question: str, top_k: int, with_answer: bool) -> PlaygroundResult`.
    - It runs retrieval.
    - If `with_answer`: it loads the org AI settings; if not `ready`, it returns `answer=None` with `warnings=["no_chat_model_configured"]`. Otherwise it builds the chat model and calls `ainvoke` with a 60 s timeout. Citations are parsed with `\[(\d+)\]` and mapped to chunk ids, ignoring out-of-range numbers. A provider error becomes `answer=None` with `warnings=["chat_model_error: <sanitized>"]`.
    - `PlaygroundResult(chunks, answer: {"text","citations":[chunk_id],"provider","model"} | None, warnings: list[str], timings_ms: {"embed","search","rerank","generate"}, trace_id: str | None)`, where `trace_id` is the current span's trace id hex.
    - The run is wrapped in a span `playground.run`, with attributes `kb.id`, `retrieval.top_k`, `retrieval.hits` and `llm.provider`/`llm.model`. The question text and the context are never span attributes.
  - Route: `POST /orgs/{org_id}/kbs/{kb_id}/playground` `{"question": 1..2000 chars, "top_k": 1..20 = 5, "with_answer": true}` (`kb.manage`) → `PlaygroundResult` JSON.

- [ ] **Step 1:** Failing tests: `test_retrieve_orders_and_hydrates`, `test_retrieve_uses_active_version_only`, `test_reranker_applied_when_configured` (fake reranker reverses), `test_merge_across_two_kbs`, `test_prompt_numbers_context_and_sets_language`, `test_playground_without_chat_settings_warns`, `test_playground_answer_with_citations` (the fake chat answer "Fake answer [1]" gives the first chunk id), `test_playground_provider_error_is_sanitised_warning`, `test_playground_cross_tenant_404`, `test_playground_span_has_no_question_text`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** PASS.

---

### Task 14: Web — AI settings page + onboarding language & AI steps

**Files:**
- Create: `apps/web/app/orgs/[orgId]/settings/ai/page.jsx`, `apps/web/components/ai/{credential-list.jsx,add-credential-dialog.jsx,chat-model-form.jsx}`, `apps/web/components/onboarding/{language-step.jsx,ai-step.jsx}`
- Modify: `apps/web/lib/onboarding-steps.js` (append `language`, `ai`), `apps/web/components/app-shell.jsx` (nav: "Knowledge" needs `kb.manage` or `bot.configure`; "AI settings" needs `billing.manage` or `bot.configure`), `apps/web/lib/permissions.js` (`canAny(membership, keys)`)
- Test: `apps/web/tests/unit/permissions.test.js` (+ `canAny`), `apps/web/tests/unit/onboarding-steps.test.js`

**Interfaces:**
- Consumes: Task 4 routes.
- Produces:
  - AI settings page, two cards.
    - **API keys** (`billing.manage`): a list of label, provider, `••••last4`, created date, and delete, with a 409 `credential_in_use` message. The add dialog has provider select, label, key (password input, never re-shown), and base URL only for `openai_compatible`. Validation errors show inline from `ApiError.detail`.
    - **Chat model** (`bot.configure`): provider and model select from `/ai/catalog`, with free-text when the model is `"*"`; a credential select filtered by provider; a temperature slider (0–1, step 0.1); a "Ready" or "Not ready" badge from `ready`.
  - Onboarding steps, in order `organisation`, `language`, `ai`:
    - `language` (select from catalog languages; English only) sets `PATCH /orgs/{id}`.
    - `ai` adds a credential and picks the chat model. It is skippable with "I'll do this later". A dashboard banner "Your bot can't answer customers until an AI key is added." shows while `GET /ai/settings` returns `ready=false`, and the banner links to AI settings.

- [ ] **Step 1:** Failing Vitest: `canAny true if any`, `canAny false for null`, `onboarding steps order is organisation → language → ai`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Vitest PASS; `npm run lint` and `npm run build` clean. Manual check (headless): add an `openai_compatible` credential pointed at a throwaway local stub that answers `GET /v1/models` with 200 (a ~10-line FastAPI app in the scratchpad, not in the repo; `fake` providers are test-only). Then pick a chat model and see the "Ready" badge.

---

### Task 15: Web — knowledge bases, documents, chunk preview, activity, playground

**Files:**
- Create: `apps/web/app/orgs/[orgId]/knowledge/page.jsx`, `apps/web/app/orgs/[orgId]/knowledge/[kbId]/page.jsx`, `apps/web/components/knowledge/{kb-create-dialog.jsx,kb-settings-form.jsx,documents-table.jsx,upload-dropzone.jsx,chunk-preview-sheet.jsx,activity-log.jsx,playground-panel.jsx,status-badge.jsx}`, `apps/web/lib/use-polling.js`
- Test: `apps/web/tests/unit/use-polling.test.js`, `apps/web/tests/unit/kb-settings.test.js`

**Interfaces:**
- Consumes: Tasks 12–13 routes, `/ai/catalog`, `/orgs/{id}/ai/credentials`.
- Produces:
  - **Knowledge list:** cards with name, embedding model, document counts by status, and a "Rebuilding…" badge when `building_version` is set. The "New knowledge base" dialog has:
    - name and description;
    - embedding model select from the catalog for the org language, grouped by provider, with `recommended` marked and local models labelled "Free · runs on your server";
    - a credential select when required (with a link to AI settings if none);
    - a chunking strategy radio, chunk size and overlap number inputs with live validation matching Global Constraints;
    - a reranker select (`none`, `cross_encoder`).
  - **KB detail tabs:**
    - **Documents:**
      - Upload: a dropzone plus file picker, multiple files uploaded one request each with per-file progress and error rows (409/413/415 messages from `detail`).
      - Table columns: filename, type, size, status badge, chunks, error tooltip, actions (preview chunks, reindex, delete with confirm).
      - A primary "Start embedding (N)" button (N = uploaded + failed) that calls `POST .../documents/index`.
      - Polls every 2 s while any document is `queued` or `processing`, and stops otherwise.
    - **Settings:** the same form as create. A save that changes embedding or chunking first shows a confirm dialog: "This rebuilds the knowledge base in the background. Current answers keep working until it finishes."
    - **Playground:**
      - Question textarea, `top_k` (1–20), a "Generate answer" toggle and a Run button.
      - Results: ranked chunks (score, rerank score, document name, ordinal, expandable text), the answer with clickable `[n]` citations that scroll to the chunk, warnings (`no_chat_model_configured` links to AI settings), and timings plus `trace_id` (copy button).
    - **Activity:** the jobs log table with expandable steps (name, status, duration) and "Load more" via `next_before`.
  - `usePolling(fn, { intervalMs, enabled })`: no overlapping calls; cleans up on unmount.
  - `validateChunking({strategy, chunk_size, chunk_overlap}) -> {ok, errors}`, exported from `kb-settings-form.jsx` or `lib/knowledge.js`, mirroring Global Constraints.

- [ ] **Step 1:** Failing Vitest: `usePolling does not overlap calls`, `usePolling stops when disabled`, `validateChunking rejects overlap > size/2`, `validateChunking accepts defaults`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Vitest PASS; lint and build clean.

---

### Task 16: End-to-end — knowledge base flow with the local embedder

**Files:**
- Create: `apps/web/tests/e2e/knowledge.spec.js`, `apps/web/tests/e2e/fixtures/returns-policy.md`
- Modify: `Makefile` (`e2e` target notes: requires worker or `SUPPORT_JOB_QUEUE=inline`; first run downloads `BAAI/bge-small-en-v1.5` into `.data/models`)

**Interfaces:**
- Consumes: everything above. The API runs with `SUPPORT_JOB_QUEUE=celery` plus `make dev-worker` (preferred, exercises the real worker), or `inline`; note which one in the report.

- [ ] **Step 1:** Write spec `owner creates KB with local embeddings, uploads, embeds, previews chunks and tests retrieval`:
  1. Sign up and create an org (reuse helpers from `onboarding.spec.js` and extract them to `tests/e2e/helpers.js`). Skip the AI step.
  2. Knowledge → New knowledge base: "Help Center", local `BAAI/bge-small-en-v1.5`, recursive 500/100. It appears in the list.
  3. Upload `returns-policy.md` (it contains "Refunds are issued within 14 days"). The row shows `Uploaded`.
  4. Click "Start embedding (1)". Within 120 s the row shows `Indexed` with chunks > 0.
  5. Preview chunks: the first chunk's text is visible.
  6. Playground: ask "How long do refunds take?" with "Generate answer" off. The top chunk contains "14 days". Then turn "Generate answer" on: the warning links to AI settings (no chat model configured).
  7. Activity: one succeeded job with steps `download, parse, chunk, embed, upsert`.
  8. Upload the same file again: the row error shows the duplicate message.
- [ ] **Step 2:** `npm run e2e` → PASS twice in a row (the first run may be slow while the model downloads).
- [ ] **Step 3:** Manual check via HTTP APIs, with evidence in the report:
  - Prometheus has `support_indexing_jobs_total{status="succeeded",organisation_id="<id>"}`.
  - Jaeger has `support-worker` traces linked to the API upload/index trace (same trace id across services, via Celery context propagation).
  - The worker's JSON logs carry `organisation_id` and `job_id`.
