# Phase 1 — Foundation, Organisations, Auth, Roles & Team — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Plan 1 of 8.** Later plans: (2) Knowledge bases + RAG, (3) Chat widget + LangGraph bot + risk, (4) Inbox + realtime + assignment, (5) Observability surfaces, (6) Email, (7) Voice, (8) Billing.

**Goal:** A runnable monorepo where a business user can sign up, create an organisation, invite teammates, manage default and custom roles, and see an audit log — with ports/adapters, tenancy scoping and logs/metrics/traces wired in from day one.

**Architecture:** Hexagonal. Shared Python package `supportcore` holds domain services, ports (abstract interfaces) and adapters; adapters are registered in a `ProviderRegistry` and selected from config at a composition root in the FastAPI app. All tenant data goes through `TenantScopedRepository`, which cannot be constructed without an `organisation_id`. Domain services publish events on an `EventBus`; the audit log is a subscriber.

**Tech Stack:** Python ≥3.12, uv workspace, FastAPI, SQLAlchemy 2 (async, asyncpg), Alembic, pydantic-settings, argon2-cffi, PyJWT, structlog, OpenTelemetry SDK + OTLP exporter + FastAPI/SQLAlchemy instrumentations, prometheus-client, pytest + pytest-asyncio + httpx. Next.js (App Router, **JavaScript**), Tailwind, shadcn/ui, Vitest, Playwright. Docker Compose: `pgvector/pgvector:pg16`, `redis:7`, `minio/minio`, `axllent/mailpit`, `jaegertracing/all-in-one` (OTLP enabled), `prom/prometheus`, `grafana/grafana`.

**Spec:** `docs/superpowers/specs/2026-10-24-ai-support-platform-design.md`

## Global Constraints

- Frontend is JavaScript, not TypeScript (`.js`/`.jsx` only; no `tsconfig.json`).
- **No git operations** — the owner does not want them. There are no commit steps; skip any you'd normally add.
- No adapter is imported or instantiated inside domain services; only ports. Adapters are wired in `apps/api/src/supportapi/container.py`.
- Every tenant-owned table has a non-null `organisation_id`; every tenant query goes through `TenantScopedRepository`.
- Every log line carries `request_id`, `trace_id`, and `organisation_id` (when known); every HTTP span and metric carries `organisation_id` when known.
- All config from environment variables (12-factor), prefix `SUPPORT_`; `.env.example` lists every one.
- Cross-tenant access returns **404**, never 403 (do not reveal existence).
- Org-provided secrets are never returned to the frontend after saving (none in this phase, but no endpoint may echo a secret field).
- Permission keys (exact): `org.manage`, `kb.manage`, `bot.configure`, `queries.answer`, `calls.take`, `team.manage`, `analytics.view`, `billing.manage`, `channels.manage`.
- Default roles (my call on mapping): **Owner** = all; **Admin** = all except `org.manage`, `billing.manage`; **Agent** = `queries.answer`, `calls.take`; **Viewer** = `analytics.view`. Default roles are `is_system=True` and cannot be edited or deleted.
- Access token lifetime 15 min; refresh token 30 days, rotated on every use, stored hashed, sent as httpOnly `SameSite=Lax` cookie `support_refresh`; `Secure` when `SUPPORT_ENV != "dev"`.
- Emails are normalised with `.strip().lower()` everywhere they are stored or compared.

## Review Focus

1. **Cross-tenant IDs** — a member of org A requesting org B's members/roles/invites/audit by ID gets 404. Test: Task 7 `test_cross_tenant_org_access_is_404`, Task 9 `test_cannot_patch_member_of_other_org`.
2. **Last owner** — demoting or deactivating the only active Owner must fail with 409, or the org becomes unmanageable. Test: Task 9 `test_cannot_remove_last_owner`.
3. **Refresh token reuse** — replaying a rotated refresh token must fail and revoke the whole token family (stolen-cookie case). Test: Task 6 `test_refresh_reuse_revokes_family`.
4. **Invite edge cases** — expired, already-accepted, or for an email that's already a member. Test: Task 9 `test_invite_expired_is_410`, `test_invite_reuse_is_410`, `test_invite_existing_member_is_409`.
5. **Email casing/whitespace** — `" Bob@X.com "` signs up; `"bob@x.com"` logs in; a second signup with `"BOB@x.com"` is 409. Test: Task 6 `test_email_normalisation`.

---

## File Structure

```
pyproject.toml                         uv workspace root (members: packages/core, apps/api)
.env.example
Makefile                               up, down, migrate, test, dev-api, dev-web
infra/
  docker-compose.yml
  prometheus/prometheus.yml
  grafana/provisioning/datasources/datasources.yml
packages/core/
  pyproject.toml
  src/supportcore/
    config.py                          Settings
    registry.py                        ProviderRegistry
    events.py                          EventBus, DomainEvent
    observability/{logging.py,tracing.py,metrics.py,context.py}
    db/{base.py,session.py,uow.py,tenancy.py}
    db/models/{user.py,organisation.py,role.py,membership.py,invite.py,refresh_token.py,audit_log.py}
    rbac/{permissions.py,defaults.py,checker.py}
    identity/{ports.py,argon2_hasher.py,jwt_tokens.py,service.py}
    orgs/{service.py,events.py}
    roles/service.py
    team/{service.py,invites.py,events.py}
    notifications/{ports.py,console_sender.py,smtp_sender.py}
    audit/{service.py,subscribers.py}
  tests/...                            mirrors src
apps/api/
  pyproject.toml
  alembic.ini, migrations/
  src/supportapi/
    main.py                            create_app()
    container.py                       composition root
    deps.py                            db session, current_user, membership, require_permission
    middleware.py                      request context
    routers/{health.py,auth.py,me.py,orgs.py,roles.py,team.py,invites.py,audit.py,admin.py}
    schemas/...                        pydantic request/response models
  tests/...
apps/web/                              Next.js (JS)
  lib/api.js, lib/auth-context.jsx, lib/permissions.js
  app/(auth)/{login,signup,accept-invite}/page.jsx
  app/onboarding/page.jsx
  app/orgs/[orgId]/{layout.jsx,page.jsx,team/page.jsx,roles/page.jsx,audit/page.jsx}
  components/...                       shadcn + app components
  tests/unit/api.test.js
  tests/e2e/onboarding.spec.js
```

---

### Task 1: Workspace, infra and settings

**Files:**
- Create: `pyproject.toml`, `packages/core/pyproject.toml`, `apps/api/pyproject.toml`, `.env.example`, `Makefile`, `infra/docker-compose.yml`, `infra/prometheus/prometheus.yml`, `infra/grafana/provisioning/datasources/datasources.yml`
- Create: `packages/core/src/supportcore/config.py`
- Test: `packages/core/tests/test_config.py`

**Interfaces:**
- Produces: `Settings` (pydantic-settings, `env_prefix="SUPPORT_"`) with fields: `env: Literal["dev","test","prod"]="dev"`, `database_url: str`, `redis_url: str`, `jwt_secret: SecretStr`, `access_token_ttl_seconds: int=900`, `refresh_token_ttl_seconds: int=2592000`, `web_origin: str="http://localhost:3000"`, `email_sender: str="console"`, `smtp_host: str="localhost"`, `smtp_port: int=1025`, `smtp_from: str`, `otlp_endpoint: str="http://localhost:4317"`, `service_name: str="support-api"`, `invite_ttl_seconds: int=604800`. `get_settings() -> Settings` (lru_cached).

- [ ] **Step 1:** Write failing tests:

```python
def test_settings_reads_prefixed_env(monkeypatch):
    monkeypatch.setenv("SUPPORT_DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("SUPPORT_REDIS_URL", "redis://h:6379/0")
    monkeypatch.setenv("SUPPORT_JWT_SECRET", "x" * 32)
    monkeypatch.setenv("SUPPORT_SMTP_FROM", "noreply@example.com")
    s = Settings()
    assert s.database_url.endswith("/db")
    assert s.access_token_ttl_seconds == 900
    assert s.env == "dev"

def test_settings_rejects_short_jwt_secret_outside_dev(monkeypatch):
    # same env as above but SUPPORT_ENV=prod, SUPPORT_JWT_SECRET="short"
    with pytest.raises(ValidationError):
        Settings()
```

- [ ] **Step 2:** `uv run pytest packages/core/tests/test_config.py -v` → FAIL (module missing).
- [ ] **Step 3:** Create uv workspace and `Settings`. Validator: when `env != "dev"`, `jwt_secret` length ≥ 32. Compose file: Postgres on 5432 (db `support`, plus `support_test` created via init script), Redis 6379, MinIO 9000/9001, Mailpit 1025/8025, Jaeger 16686 + OTLP 4317, Prometheus 9090 scraping `host.docker.internal:8000/metrics`, Grafana 3001 with Prometheus + Jaeger datasources. Makefile targets: `up`, `down`, `migrate` (`uv run alembic -c apps/api/alembic.ini upgrade head`), `test`, `dev-api` (`uv run uvicorn supportapi.main:create_app --factory --reload`), `dev-web`.
- [ ] **Step 4:** Tests PASS; `make up` brings all containers healthy (`docker compose -f infra/docker-compose.yml ps` shows all `running`).

---

### Task 2: ProviderRegistry and EventBus

**Files:**
- Create: `packages/core/src/supportcore/registry.py`, `packages/core/src/supportcore/events.py`
- Test: `packages/core/tests/test_registry.py`, `packages/core/tests/test_events.py`

**Interfaces:**
- Produces:
  - `class ProviderRegistry(Generic[T])`: `__init__(self, port_name: str)`, `register(self, key: str) -> Callable[[Callable[..., T]], Callable[..., T]]` (decorator for a factory), `create(self, key: str, **kwargs) -> T`, `keys(self) -> list[str]`. Unknown key raises `UnknownProviderError(port_name, key, available)`.
  - `@dataclass(frozen=True) class DomainEvent`: `organisation_id: UUID | None`, `actor_user_id: UUID | None`, `occurred_at: datetime` (default now UTC). Subclasses add fields.
  - `class EventBus`: `subscribe(self, event_type: type[DomainEvent], handler: Callable[[DomainEvent], Awaitable[None]]) -> None`, `async publish(self, event: DomainEvent) -> None`. Handlers for a type and its base classes run in subscription order; a handler exception is logged with event type and does not stop other handlers (my call).

- [ ] **Step 1:** Failing tests:

```python
def test_registry_creates_registered_provider():
    reg = ProviderRegistry[str]("greeter")
    @reg.register("hello")
    def make(name): return f"hello {name}"
    assert reg.create("hello", name="x") == "hello x"
    assert reg.keys() == ["hello"]

def test_registry_unknown_key_lists_available():
    reg = ProviderRegistry[str]("greeter")
    reg.register("a")(lambda: "a")
    with pytest.raises(UnknownProviderError) as e:
        reg.create("b")
    assert "a" in str(e.value) and "greeter" in str(e.value)

def test_registry_duplicate_key_raises(): ...  # ValueError

async def test_event_bus_dispatches_to_subscribers_in_order(): ...
async def test_event_bus_handler_error_does_not_block_others(): ...
async def test_event_bus_base_class_subscriber_receives_subclass(): ...
```

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `uv run pytest packages/core/tests/test_registry.py packages/core/tests/test_events.py -v` → PASS.

---

### Task 3: Observability foundation + app factory + health

**Files:**
- Create: `packages/core/src/supportcore/observability/{context.py,logging.py,tracing.py,metrics.py}`
- Create: `apps/api/src/supportapi/{main.py,middleware.py,container.py}`, `apps/api/src/supportapi/routers/health.py`
- Test: `apps/api/tests/test_health_and_observability.py`, `packages/core/tests/observability/test_logging.py`

**Interfaces:**
- Produces:
  - `context.py`: contextvars `request_id_var`, `organisation_id_var`; `bind_organisation(org_id: UUID) -> None` (sets contextvar **and** current span attribute `organisation.id`).
  - `logging.configure_logging(settings: Settings) -> None` — structlog JSON to stdout; processor injects `request_id`, `organisation_id`, `trace_id`, `span_id` from contextvars/current span. `get_logger(name: str)`.
  - `tracing.configure_tracing(settings: Settings, app: FastAPI | None = None, engine: AsyncEngine | None = None) -> None` — OTLP gRPC exporter to `settings.otlp_endpoint`, resource `service.name=settings.service_name`; instruments FastAPI and SQLAlchemy. In `env == "test"`, uses an in-memory exporter retrievable via `get_test_span_exporter()`.
  - `metrics.py`: `HTTP_REQUESTS = Counter("support_http_requests_total", ..., ["method","route","status","organisation_id"])`, `HTTP_LATENCY = Histogram("support_http_request_duration_seconds", ..., ["method","route"])`; `metrics_app()` ASGI app for `/metrics`.
  - `middleware.RequestContextMiddleware` — reads/creates `X-Request-ID`, sets contextvar, echoes header, records metrics using the matched route template (not raw path) and `organisation_id_var` value or `""`.
  - `main.create_app(settings: Settings | None = None) -> FastAPI` — configures logging/tracing, mounts `/metrics`, includes routers, CORS for `settings.web_origin` with credentials.
  - `routers/health.py`: `GET /healthz` → `{"status":"ok"}`; `GET /readyz` → runs `SELECT 1` (after Task 4; in this task returns ok) → `{"status":"ok"}` or 503.

- [ ] **Step 1:** Failing tests:

```python
async def test_healthz_ok(client):
    r = await client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}

async def test_request_id_echoed_and_generated(client):
    r = await client.get("/healthz", headers={"X-Request-ID": "abc"})
    assert r.headers["X-Request-ID"] == "abc"
    r2 = await client.get("/healthz")
    assert len(r2.headers["X-Request-ID"]) >= 16

async def test_metrics_exposes_http_counter(client):
    await client.get("/healthz")
    body = (await client.get("/metrics")).text
    assert 'support_http_requests_total{method="GET",organisation_id="",route="/healthz",status="200"}' in body

async def test_span_recorded_for_request(client):
    await client.get("/healthz")
    spans = get_test_span_exporter().get_finished_spans()
    assert any(s.name.startswith("GET /healthz") for s in spans)

def test_log_line_includes_context(capsys):
    # configure_logging, set request_id_var="r1", bind_organisation(uuid), log "hi"
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["request_id"] == "r1" and line["organisation_id"] == str(org_id)
```

`client` fixture (in `apps/api/tests/conftest.py`): `create_app(Settings(env="test", ...))` wrapped in `httpx.AsyncClient(transport=ASGITransport(app))`.

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `uv run pytest apps/api/tests/test_health_and_observability.py packages/core/tests/observability -v` → PASS. Manual: `make dev-api`, hit `/healthz`, see the trace in Jaeger at `localhost:16686` under `support-api`.

---

### Task 4: Database layer, models, migrations, tenant-scoped repository

**Files:**
- Create: `packages/core/src/supportcore/db/{base.py,session.py,uow.py,tenancy.py}`, `packages/core/src/supportcore/db/models/*.py`
- Create: `apps/api/alembic.ini`, `apps/api/migrations/env.py`, first revision `0001_identity_orgs_team.py`
- Modify: `apps/api/src/supportapi/routers/health.py` (`/readyz` does `SELECT 1`)
- Test: `packages/core/tests/db/test_tenancy.py`, `packages/core/tests/conftest.py`

**Interfaces:**
- Produces:
  - `Base` (DeclarativeBase) with `TimestampMixin` (`created_at`, `updated_at`, server defaults UTC) and `TenantMixin` (`organisation_id: Mapped[UUID]`, FK `organisations.id`, `ondelete="CASCADE"`, indexed, non-null). All PKs are UUID v4.
  - Models:
    - `User(id, email unique, password_hash, name, is_platform_admin bool=False, is_active bool=True)`
    - `Organisation(id, name, slug unique, language="en", created_by_user_id)`
    - `Role(TenantMixin; id, name, permissions: ARRAY(Text), is_system bool)` unique `(organisation_id, name)`
    - `Membership(TenantMixin; id, user_id, role_id, status: "active"|"deactivated", phone_number nullable, agent_status: "online"|"away"|"offline"="offline")` unique `(organisation_id, user_id)`
    - `Invite(TenantMixin; id, email, role_id, token_hash unique, invited_by_user_id, expires_at, accepted_at nullable, revoked_at nullable)`
    - `RefreshToken(id, user_id, family_id, token_hash unique, expires_at, revoked_at nullable, replaced_by_id nullable)`
    - `AuditLog(TenantMixin; id, actor_user_id nullable, action str, target_type str, target_id str nullable, metadata JSONB)` index `(organisation_id, created_at desc)`
  - `session.create_engine(settings) -> AsyncEngine`, `session.session_factory(engine) -> async_sessionmaker`.
  - `uow.UnitOfWork`: async context manager; `.session`; `commit()`; rolls back on exception.
  - `tenancy.TenantScopedRepository(Generic[M])`: `__init__(self, session: AsyncSession, model: type[M], organisation_id: UUID)` — raises `TypeError` if `model` lacks `TenantMixin` and `ValueError` if `organisation_id` is None. Methods: `async get(self, id: UUID) -> M | None` (filters by org), `async list(self, *where, order_by=None, limit: int=50, offset: int=0) -> list[M]`, `async add(self, obj: M) -> M` (forces `obj.organisation_id = self.organisation_id`), `async delete(self, obj: M) -> None` (asserts same org).
- Test DB: `SUPPORT_TEST_DATABASE_URL` (default `postgresql+asyncpg://support:support@localhost:5432/support_test`). Fixture creates schema via `Base.metadata.create_all` once per session and wraps each test in a transaction rolled back at end.

- [ ] **Step 1:** Failing tests:

```python
async def test_repo_get_hides_other_org_rows(session, org_a, org_b):
    role_b = Role(name="X", permissions=[], is_system=False, organisation_id=org_b.id)
    session.add(role_b); await session.flush()
    repo = TenantScopedRepository(session, Role, org_a.id)
    assert await repo.get(role_b.id) is None

async def test_repo_add_forces_org(session, org_a, org_b):
    repo = TenantScopedRepository(session, Role, org_a.id)
    r = await repo.add(Role(name="Y", permissions=[], is_system=False, organisation_id=org_b.id))
    assert r.organisation_id == org_a.id

def test_repo_rejects_non_tenant_model(session):
    with pytest.raises(TypeError):
        TenantScopedRepository(session, User, uuid4())

def test_repo_rejects_missing_org(session):
    with pytest.raises(ValueError):
        TenantScopedRepository(session, Role, None)
```

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement models, repo, Alembic (async `env.py` importing `Base.metadata`), generate and hand-check revision `0001`.
- [ ] **Step 4:** Tests PASS; `make migrate` succeeds against the dev DB; `GET /readyz` → 200 with DB up, 503 with DB stopped.

---

### Task 5: RBAC — permissions, default roles, checker

**Files:**
- Create: `packages/core/src/supportcore/rbac/{permissions.py,defaults.py,checker.py}`
- Test: `packages/core/tests/rbac/test_rbac.py`

**Interfaces:**
- Produces:
  - `class Permission(StrEnum)` with the nine keys from Global Constraints. `ALL_PERMISSIONS: frozenset[Permission]`. `PERMISSION_LABELS: dict[Permission, str]` (human labels for UI).
  - `DEFAULT_ROLES: dict[str, frozenset[Permission]]` — keys `"Owner","Admin","Agent","Viewer"` per Global Constraints.
  - `def has_permission(granted: Iterable[str], required: Permission) -> bool`.
  - `def validate_permissions(values: Iterable[str]) -> list[Permission]` — raises `InvalidPermissionError(unknown: list[str])`.

- [ ] **Step 1:** Failing tests: `test_owner_has_all`, `test_admin_lacks_billing_and_org_manage`, `test_agent_permissions_exact` (`== {QUERIES_ANSWER, CALLS_TAKE}`), `test_viewer_permissions_exact`, `test_validate_permissions_rejects_unknown` (`["kb.manage","nope"]` → error listing `["nope"]`), `test_has_permission`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** PASS.

---

### Task 6: Identity — hashing, tokens, auth service, auth routes

**Files:**
- Create: `packages/core/src/supportcore/identity/{ports.py,argon2_hasher.py,jwt_tokens.py,service.py}`
- Create: `apps/api/src/supportapi/routers/{auth.py,me.py}`, `apps/api/src/supportapi/schemas/auth.py`
- Modify: `apps/api/src/supportapi/container.py` (register `"argon2"` hasher, `"jwt"` token service in `hasher_registry`, `token_registry`), `apps/api/src/supportapi/deps.py` (`get_current_user`)
- Test: `packages/core/tests/identity/test_service.py`, `apps/api/tests/test_auth_routes.py`

**Interfaces:**
- Produces:
  - `ports.PasswordHasher(Protocol)`: `hash(self, password: str) -> str`, `verify(self, hash: str, password: str) -> bool`.
  - `ports.AccessTokenService(Protocol)`: `issue(self, user_id: UUID) -> str`, `decode(self, token: str) -> UUID` (raises `InvalidTokenError`).
  - `service.AuthService(session, hasher, tokens, settings, events)`:
    - `async signup(self, email: str, password: str, name: str) -> AuthResult`
    - `async login(self, email: str, password: str) -> AuthResult`
    - `async refresh(self, raw_refresh_token: str) -> AuthResult`
    - `async logout(self, raw_refresh_token: str) -> None`
    - `AuthResult(user: User, access_token: str, refresh_token: str, refresh_expires_at: datetime)`
    - Errors: `EmailTakenError`, `InvalidCredentialsError`, `InvalidRefreshTokenError`. Password minimum 10 chars → `WeakPasswordError`.
    - Refresh tokens: 32 random bytes, urlsafe; store SHA-256 hash; rotation marks old `revoked_at`, sets `replaced_by_id`; presenting a revoked token revokes every token with the same `family_id`.
  - Routes: `POST /auth/signup` (201), `POST /auth/login` (200), `POST /auth/refresh` (reads cookie `support_refresh`), `POST /auth/logout` (204, clears cookie). Body responses: `{"access_token": str, "user": {"id","email","name","is_platform_admin"}}`. Errors: 409 email taken, 401 bad creds / bad refresh, 422 weak password.
  - `deps.get_current_user(...) -> User` — `Authorization: Bearer`; 401 otherwise or if user inactive.
  - `GET /me` → `{"user": {...}, "memberships": [{"organisation_id","organisation_name","role_name","permissions":[...]}]}`.

- [ ] **Step 1:** Failing tests (service-level and route-level):

```python
async def test_email_normalisation(auth):
    await auth.signup(" Bob@X.com ", "correct-horse-1", "Bob")
    res = await auth.login("bob@x.com", "correct-horse-1")
    assert res.user.email == "bob@x.com"
    with pytest.raises(EmailTakenError):
        await auth.signup("BOB@x.com", "correct-horse-1", "Bob2")

async def test_login_wrong_password(auth): ...           # InvalidCredentialsError
async def test_login_unknown_email_same_error(auth): ...  # InvalidCredentialsError (no enumeration)
async def test_weak_password_rejected(auth): ...          # "short" → WeakPasswordError

async def test_refresh_rotates(auth):
    r1 = await auth.signup("a@x.com", "correct-horse-1", "A")
    r2 = await auth.refresh(r1.refresh_token)
    assert r2.refresh_token != r1.refresh_token

async def test_refresh_reuse_revokes_family(auth):
    r1 = await auth.signup("a@x.com", "correct-horse-1", "A")
    r2 = await auth.refresh(r1.refresh_token)
    with pytest.raises(InvalidRefreshTokenError):
        await auth.refresh(r1.refresh_token)          # replay
    with pytest.raises(InvalidRefreshTokenError):
        await auth.refresh(r2.refresh_token)          # family revoked

async def test_signup_route_sets_httponly_cookie(client):
    r = await client.post("/auth/signup", json={"email":"a@x.com","password":"correct-horse-1","name":"A"})
    assert r.status_code == 201
    cookie = r.headers["set-cookie"]
    assert "support_refresh=" in cookie
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()
    assert "refresh_token" not in r.json()

async def test_me_requires_auth(client): ...               # 401 without bearer
async def test_me_returns_user_and_empty_memberships(client, auth_headers): ...
```

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. Routes map domain errors to HTTP codes in one exception handler module (`apps/api/src/supportapi/errors.py`), reused by later tasks.
- [ ] **Step 4:** `uv run pytest packages/core/tests/identity apps/api/tests/test_auth_routes.py -v` → PASS.

---

### Task 7: Organisations, membership deps and permission guard

**Files:**
- Create: `packages/core/src/supportcore/orgs/{service.py,events.py}`
- Create: `apps/api/src/supportapi/routers/orgs.py`, `apps/api/src/supportapi/schemas/orgs.py`
- Modify: `apps/api/src/supportapi/deps.py`
- Test: `packages/core/tests/orgs/test_service.py`, `apps/api/tests/test_orgs_routes.py`

**Interfaces:**
- Consumes: `DEFAULT_ROLES`, `TenantScopedRepository`, `EventBus`, `get_current_user`.
- Produces:
  - `events.OrganisationCreated(DomainEvent)`: `organisation_name: str`.
  - `OrganisationService(session, events)`: `async create(self, creator: User, name: str) -> Organisation` — creates org with unique slug (slugified name, `-2`, `-3`… on collision), the four system roles, and an active Owner membership for the creator; publishes `OrganisationCreated`. `async get_for_member(self, user: User, org_id: UUID) -> tuple[Organisation, Membership, Role]` — raises `NotFoundError` if no active membership (platform admins: see Task 10).
  - `deps.get_org_context(org_id: UUID, user=Depends(get_current_user)) -> OrgContext` with `OrgContext(organisation, membership, role, user)`; calls `bind_organisation(org_id)`.
  - `deps.require_permission(perm: Permission) -> Callable` — dependency returning `OrgContext`; 403 if the role lacks `perm` (the user *is* a member, so 403 is correct here).
  - Routes: `POST /orgs` `{name}` → 201 `{"id","name","slug","language"}`; `GET /orgs/{org_id}` → same (any member).

- [ ] **Step 1:** Failing tests: `test_create_org_makes_owner_and_system_roles` (4 roles, `is_system` all True, creator membership role name `"Owner"`), `test_slug_collision_suffixes` (`"Acme"` twice → `acme`, `acme-2`), `test_create_publishes_event`, `test_cross_tenant_org_access_is_404` (user A GET org B → 404), `test_require_permission_403_for_viewer` (use a throwaway route registered in the test app requiring `team.manage`), `test_log_and_metric_tagged_with_org` (after `GET /orgs/{id}`, `/metrics` contains `organisation_id="<id>"`).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** PASS.

---

### Task 8: Roles — custom role management

**Files:**
- Create: `packages/core/src/supportcore/roles/service.py`, `apps/api/src/supportapi/routers/roles.py`, `apps/api/src/supportapi/schemas/roles.py`
- Test: `packages/core/tests/roles/test_service.py`, `apps/api/tests/test_roles_routes.py`

**Interfaces:**
- Consumes: `TenantScopedRepository`, `validate_permissions`, `require_permission(Permission.TEAM_MANAGE)`.
- Produces:
  - Events: `RoleCreated`, `RoleUpdated`, `RoleDeleted` (fields: `role_id: UUID`, `role_name: str`, `permissions: list[str]`).
  - `RoleService(session, organisation_id, events)`: `list()`, `create(name, permissions, actor) -> Role`, `update(role_id, name, permissions, actor) -> Role`, `delete(role_id, actor) -> None`. Errors: `SystemRoleImmutableError` (409), `RoleInUseError` (409, includes member count), `RoleNameTakenError` (409), `InvalidPermissionError` (422), `NotFoundError` (404).
  - Routes (all require `team.manage`): `GET /orgs/{org_id}/roles`, `POST /orgs/{org_id}/roles`, `PATCH /orgs/{org_id}/roles/{role_id}`, `DELETE /orgs/{org_id}/roles/{role_id}` (204). `GET /permissions` (authenticated) → `[{"key","label"}]`.

- [ ] **Step 1:** Failing tests: `test_create_custom_role`, `test_cannot_edit_system_role`, `test_cannot_delete_role_in_use`, `test_duplicate_role_name_409`, `test_unknown_permission_422`, `test_agent_cannot_create_role_403`, `test_role_of_other_org_404`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** PASS.

---

### Task 9: Team — members, invites, email sender port

**Files:**
- Create: `packages/core/src/supportcore/notifications/{ports.py,console_sender.py,smtp_sender.py}`
- Create: `packages/core/src/supportcore/team/{service.py,invites.py,events.py}`
- Create: `apps/api/src/supportapi/routers/{team.py,invites.py}`, `apps/api/src/supportapi/schemas/team.py`
- Modify: `apps/api/src/supportapi/container.py` (register `"console"`, `"smtp"` in `email_sender_registry`; select via `settings.email_sender`)
- Test: `packages/core/tests/team/test_members.py`, `packages/core/tests/team/test_invites.py`, `packages/core/tests/notifications/test_senders.py`, `apps/api/tests/test_team_routes.py`

**Interfaces:**
- Produces:
  - `ports.EmailSender(Protocol)`: `async send(self, message: EmailMessage) -> None` with `EmailMessage(to: str, subject: str, text: str, html: str | None = None)`. `ConsoleEmailSender` logs the message (dev; also keeps `sent: list[EmailMessage]` for tests). `SmtpEmailSender(host, port, from_addr)` uses `aiosmtplib` (Mailpit locally).
  - Events: `MemberInvited(email, role_id)`, `InviteAccepted(user_id, membership_id)`, `InviteRevoked(invite_id)`, `MemberUpdated(membership_id, changes: dict)`.
  - `MemberService(session, organisation_id, events)`: `list() -> list[MemberView]` (`MemberView(membership_id, user_id, name, email, role_id, role_name, status, phone_number, agent_status)`), `update(membership_id, *, role_id=None, status=None, phone_number=None, actor) -> MemberView`. Raises `LastOwnerError` (409) if the change leaves zero active memberships with the Owner system role.
  - `InviteService(session, organisation_id, events, email_sender, settings)`: `create(email, role_id, actor) -> Invite` (raises `AlreadyMemberError` 409; re-inviting a pending email revokes the old invite and issues a new one); `list_pending()`; `revoke(invite_id, actor)`. Invite link: `{settings.web_origin}/accept-invite?token=<raw>`; raw token 32 urlsafe bytes, stored hashed.
  - `accept_invite(session, events, auth_service, raw_token: str, *, current_user: User | None, name: str | None, password: str | None) -> AcceptResult` — if `current_user` is given, its email must equal the invite email (else `InviteEmailMismatchError` 403); otherwise creates the user via `AuthService.signup` and returns tokens. Raises `InviteInvalidError` (410) if expired, accepted, or revoked.
  - Routes: `GET /orgs/{org_id}/members` (`team.manage` or `queries.answer` — agents need the list for reassignment; my call), `PATCH /orgs/{org_id}/members/{membership_id}` (`team.manage`; additionally a member may PATCH their own `phone_number`), `POST/GET /orgs/{org_id}/invites`, `DELETE /orgs/{org_id}/invites/{invite_id}` (`team.manage`), `GET /invites/{token}` (public → `{"organisation_name","email","role_name"}` or 410), `POST /invites/accept` (public or authenticated).

- [ ] **Step 1:** Failing tests:

```python
async def test_cannot_remove_last_owner(members, owner_membership, owner_user):
    with pytest.raises(LastOwnerError):
        await members.update(owner_membership.id, status="deactivated", actor=owner_user)
    viewer = await role_by_name("Viewer")
    with pytest.raises(LastOwnerError):
        await members.update(owner_membership.id, role_id=viewer.id, actor=owner_user)

async def test_invite_sends_email_with_link(invites, console_sender): ...
    # one message; to == invitee; "accept-invite?token=" in text
async def test_invite_existing_member_is_409(invites, owner_user): ...
async def test_invite_expired_is_410(...): ...      # freeze time past expires_at
async def test_invite_reuse_is_410(...): ...        # accept twice
async def test_accept_creates_user_and_membership(...): ...
async def test_accept_email_mismatch_403(...): ...
async def test_cannot_patch_member_of_other_org(client, ...): ...   # 404
async def test_member_can_set_own_phone_without_team_manage(client, ...): ...
async def test_smtp_sender_delivers_to_mailpit(): ...   # marked @pytest.mark.integration; skips if Mailpit is not up; checks Mailpit API /api/v1/messages
```

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `uv run pytest packages/core/tests/team packages/core/tests/notifications apps/api/tests/test_team_routes.py -v` → PASS.

---

### Task 10: Audit log and platform admin

**Files:**
- Create: `packages/core/src/supportcore/audit/{service.py,subscribers.py}`, `apps/api/src/supportapi/routers/{audit.py,admin.py}`
- Modify: `apps/api/src/supportapi/container.py` (subscribe audit handlers at startup), `deps.py` (`require_platform_admin`; `get_org_context` lets platform admins read any org with a synthetic read-only context — permissions = `{analytics.view}` (my call))
- Test: `packages/core/tests/audit/test_subscribers.py`, `apps/api/tests/test_audit_admin_routes.py`

**Interfaces:**
- Consumes: all events from Tasks 7–9.
- Produces:
  - `AuditService(session)`: `async record(self, event: DomainEvent, action: str, target_type: str, target_id: str | None, metadata: dict) -> None`.
  - `register_audit_subscribers(bus: EventBus, session_provider) -> None`. Action names (exact): `organisation.created`, `role.created`, `role.updated`, `role.deleted`, `member.invited`, `invite.accepted`, `invite.revoked`, `member.updated`. Metadata never contains tokens or password hashes.
  - Audit writes happen in the same unit of work as the change (handler gets the request's session), so a rolled-back change leaves no audit row.
  - Routes: `GET /orgs/{org_id}/audit-logs?limit=50&before=<iso>` (`analytics.view`) → `{"items":[{"id","action","actor":{"id","name"},"target_type","target_id","metadata","created_at"}], "next_before": str|null}`. `GET /admin/orgs` (platform admin) → `[{"id","name","slug","member_count","created_at"}]`.

- [ ] **Step 1:** Failing tests: `test_role_created_writes_audit_row`, `test_member_updated_metadata_has_changes_not_secrets`, `test_audit_rolled_back_with_change`, `test_audit_list_paginates_desc`, `test_audit_other_org_404`, `test_admin_orgs_requires_platform_admin` (403 for normal user), `test_platform_admin_can_read_any_org_audit`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** PASS. Full backend suite: `make test` → all PASS.

---

### Task 11: Web scaffold, API client and auth pages

**Files:**
- Create: `apps/web` via `npx create-next-app@latest apps/web --js --tailwind --eslint --app --no-src-dir --import-alias "@/*" --use-npm --yes`; then `npx shadcn@latest init -y` (JS) and add `button input label card form table dialog dropdown-menu select badge sonner checkbox avatar separator`.
- Create: `apps/web/lib/api.js`, `apps/web/lib/auth-context.jsx`, `apps/web/app/(auth)/login/page.jsx`, `apps/web/app/(auth)/signup/page.jsx`, `apps/web/.env.example` (`NEXT_PUBLIC_API_URL=http://localhost:8000`)
- Test: `apps/web/tests/unit/api.test.js` (Vitest + jsdom; `vitest.config.mjs`)

**Interfaces:**
- Consumes: Task 6 routes.
- Produces:
  - `lib/api.js`: `createApiClient({ baseUrl, getAccessToken, setAccessToken, onAuthFailure })` returning `{ get(path), post(path, body), patch(path, body), del(path) }`. All requests use `credentials: "include"`. On 401 it calls `POST /auth/refresh` once (concurrent 401s share one in-flight refresh), retries the original request, and calls `onAuthFailure()` if refresh fails. Non-2xx responses throw `ApiError { status, detail }`.
  - `lib/auth-context.jsx`: `<AuthProvider>` holding the access token **in memory only** (never localStorage), bootstrapping by calling `/auth/refresh` on mount; `useAuth()` → `{ user, memberships, login(email,pw), signup(email,pw,name), logout(), api, loading }`.
  - Pages: `/login`, `/signup` (redirect to `/onboarding` if the user has no memberships, else `/orgs/{firstOrgId}`).

- [ ] **Step 1:** Failing Vitest tests: `refreshes once and retries on 401`, `concurrent 401s trigger a single refresh`, `calls onAuthFailure when refresh fails`, `throws ApiError with status and detail`.
- [ ] **Step 2:** `cd apps/web && npx vitest run` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Vitest PASS; `npm run build` succeeds; manual: sign up at `localhost:3000/signup` against the running API, reload the page, and stay logged in (refresh cookie works).

---

### Task 12: Web — onboarding, app shell, team, roles, audit, accept invite

**Files:**
- Create: `apps/web/app/onboarding/page.jsx`, `apps/web/app/orgs/[orgId]/{layout.jsx,page.jsx,team/page.jsx,roles/page.jsx,audit/page.jsx}`, `apps/web/app/(auth)/accept-invite/page.jsx`, `apps/web/lib/permissions.js`, `apps/web/lib/onboarding-steps.js`, `apps/web/components/{app-shell.jsx,org-switcher.jsx,invite-dialog.jsx,role-editor-dialog.jsx,member-row-actions.jsx}`
- Test: `apps/web/tests/unit/permissions.test.js`

**Interfaces:**
- Consumes: Tasks 7–10 routes, `useAuth()`.
- Produces:
  - `lib/permissions.js`: `can(membership, permissionKey) -> boolean`; `PERMISSIONS` constant mirroring backend keys.
  - `lib/onboarding-steps.js`: exported array `ONBOARDING_STEPS = [{ id: "organisation", title, Component }]`. Later phases append steps (language, LLM key, plan & limit behaviour, phone, email) — the wizard renders whatever is in the array with next/back and progress. In this phase only the `organisation` step exists.
  - App shell: sidebar nav entries filtered by `can()` (Team and Roles need `team.manage`; Audit needs `analytics.view`); org switcher from `/me` memberships; placeholder dashboard page.
  - Team page: members table (name, email, role select, status, phone, agent status), invite dialog (email + role), pending invites with revoke, inline errors from `ApiError.detail` (e.g. last-owner 409 shows "An organisation needs at least one active Owner.").
  - Roles page: system roles shown read-only with a lock badge; custom roles create/edit with a permission checkbox list from `GET /permissions`; delete disabled with a tooltip when in use.
  - Audit page: table with "Load more" using `next_before`.
  - Accept-invite page: `GET /invites/{token}`; logged-in → "Join {org}" button; logged-out → name + password form; 410 → "This invite has expired or was already used."

- [ ] **Step 1:** Failing Vitest: `can() true for granted`, `can() false for missing`, `can() false for null membership`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement the pages and components.
- [ ] **Step 4:** Vitest PASS; `npm run build` succeeds with no ESLint errors.

---

### Task 13: End-to-end smoke test

**Files:**
- Create: `apps/web/playwright.config.mjs`, `apps/web/tests/e2e/onboarding.spec.js`
- Modify: `Makefile` (`e2e` target: assumes `make up`, `make migrate`, API and web running; API with `SUPPORT_EMAIL_SENDER=smtp` to Mailpit)

**Interfaces:**
- Consumes: everything above; Mailpit HTTP API `GET http://localhost:8025/api/v1/messages` to read the invite link.

- [ ] **Step 1:** Write the spec `owner signs up, creates org, invites agent, agent accepts and sees no Team nav`:
  1. Sign up `owner+<rand>@example.com`; land on `/onboarding`; create org "Acme <rand>"; land on `/orgs/{id}`.
  2. Team → invite `agent+<rand>@example.com` as Agent; invite shows as pending.
  3. Fetch the invite link from Mailpit; open it in a new browser context; set name + password; land in the org.
  4. Assert the sidebar has no "Team" or "Roles" entry; navigating to `/orgs/{id}/team` shows a "You don't have access" state.
  5. Back as owner: Audit page lists `member.invited` and `invite.accepted`.
- [ ] **Step 2:** `cd apps/web && npx playwright test` → PASS.
- [ ] **Step 3:** Manual check: Jaeger shows traces for the flow; Prometheus `support_http_requests_total` has series labelled with the new org's ID.
