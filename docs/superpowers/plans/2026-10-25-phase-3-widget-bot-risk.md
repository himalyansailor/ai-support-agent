# Phase 3 — Chat Widget, LangGraph Bot & Risk Assessment — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Plan 3 of 8.** Builds on Phase 1 and Phase 2 (`docs/superpowers/plans/2026-10-24-phase-2-knowledge-bases-rag.md`). Next: (4) unified inbox + realtime + assignment.

**Goal:** A business creates a chat widget, configures its look, allowed domains, knowledge bases and (optionally) signed customer identity, and pastes a `<script>` snippet into its site. Customers chat with a LangGraph bot that:

- answers from the selected knowledge bases with citations, streaming its reply;
- can call the business's own read-only HTTP tools;
- runs every message through the business's risk rules, which escalate to a human, offer a human, or let the bot handle it.

Every decision is recorded with the rule that fired and why. The business sees each conversation's transcript and per-turn trace in the dashboard. Escalated conversations wait in `pending_human` and the customer can leave an email; Phase 4 adds the agents who answer them.

**Architecture:**

- **New ports in `supportcore`**, registered in `ProviderRegistry` instances in `supportcore/wiring.py`:
  - `RateLimiter`: redis, memory
  - `PubSub`: redis, memory
  - `Checkpointer`: postgres, memory
  - `CustomerTokenVerifier`: jwt_hs256
  - `RiskAssessor`: rules_llm
  - `ToolExecutor`: http
- **Bot:** a LangGraph `StateGraph` checkpointed per conversation (`thread_id = conversation id`).
- **Turns:**
  - A turn runs in the API process as a tracked asyncio task, started from the widget WebSocket handler.
  - Only one turn per conversation runs at a time, guarded by a lease column.
  - No DB session is held across a model call: nodes open short `UnitOfWork`s.
  - Turn output (token deltas, persisted messages, handoff) goes to the conversation's `PubSub` channel. Every socket of that conversation relays it, across tabs and API instances.
- **Widget:**
  - The widget is a small esbuild-bundled loader script. It adds a launcher button and an iframe pointing at the Next.js route `/embed/chat`.
  - The iframe page talks to public `/widget/v1/*` REST routes and one WebSocket.
  - Domain enforcement is the browser's `frame-ancestors` CSP, set per widget by `apps/web/proxy.js`. The `parent_origin` check at session start is defence in depth.

**Tech Stack (owner-decided):**

- Backend:
  - `langgraph`
  - `langgraph-checkpoint-postgres`, with `psycopg[binary]` and `psycopg-pool` alongside asyncpg
  - `redis` (redis-py asyncio, already present via `celery[redis]`), used by our own sliding-window limiter and the pub/sub
  - `pyjwt`, already present
  - SQLAlchemy pinned to `>=2.0.36,<2.1`, so the OTel SQLAlchemy instrumentor emits DB spans again
- Widget loader: esbuild, vanilla JS.
- Frontend: the existing Next.js JS app with shadcn.

**Spec:** `docs/superpowers/specs/2026-10-24-ai-support-platform-design.md`:

- §3: swappable components (risk assessment, real-time transport, checkpointer, cache/pub-sub, business-system tools)
- §4.2: each widget selects KBs
- §4.3: chat widget
- §4.4: the bot
- §4.5: risk assessment
- §4.7: per-conversation trace
- §5: languages and widget security

## Global Constraints

- **Still in force:** all Phase 1 and Phase 2 global constraints (`.superpowers/sdd/2026-10-24-phase-2-knowledge-bases-rag/global-constraints.md`):
  - JS-only frontend.
  - **No git operations.**
  - Cross-tenant access returns 404.
  - Queries go through `TenantScopedRepository`.
  - Routes use `SessionDep`.
  - Domain errors go in `DOMAIN_ERRORS` with a stable `code`.
  - Adapters are imported only by `wiring.py`, `container.py` and the worker; extend `test_layering.py`'s adapter globs for every new adapter module.
  - Secrets are encrypted with `SecretCipher`, and only `last4` is ever returned.
  - Jobs are enqueued only via `after_commit`.
  - The `fake` providers exist only when `env == "test"`.
- **Never logged or put in span attributes:** message text, customer email or name, tool arguments and tool responses. The same goes for every secret: the widget identity secret, tool auth header values, and customer and widget tokens. Logs and spans carry ids, counts, rule keys, provider/model, latencies and token counts only.
- **No session across provider calls:** no DB session or transaction is held open across an LLM, embedding, rerank or tool HTTP call. Graph nodes read and write in short `UnitOfWork(runtime.session_factory)` blocks.
- **Public widget API:**
  - Path prefix `/widget/v1`.
  - These routes never use dashboard auth.
  - Every route is rate limited through the `RateLimiter` port. Over the limit returns 429 `rate_limited` with a `Retry-After` header.
- **Rate limits** (constants in `supportcore/widget/limits.py`), as `(limit, window_seconds)`:
  - `SESSION_PER_IP = (20, 60)`
  - `CONFIG_PER_IP = (120, 60)`
  - `MESSAGE_PER_CONVERSATION = (20, 60)`
  - `MESSAGE_PER_ORG = (600, 60)`
  - `CONVERSATION_CREATE_PER_CUSTOMER = (10, 3600)`
- **Message limits:**
  - Customer message text is 1–4000 characters after stripping.
  - A WebSocket frame is at most 16384 bytes; a larger frame closes the socket with code 1009.
- **Widget session tokens:**
  - HS256 JWTs signed with `settings.jwt_secret`.
  - Claims: `type="widget"`, `sub=<customer_id>`, `org`, `wid` (widget id), `iat`, `exp`.
  - TTL is `settings.widget_session_ttl_seconds` (default 86400).
  - `JwtAccessTokenService.decode` already rejects `type != "access"`. A test must prove that a widget token is 401 on dashboard routes, and that an access token is rejected by the widget verifier.
- **Signed customer identity:**
  - HS256 JWTs signed with the widget's identity secret.
  - `sub` (the business's user id, 1–255 characters) and `exp` are required. `exp` may be at most 86400 s after `iat` (or after now, when `iat` is missing).
  - `email` and `name` are optional.
  - Every other algorithm, including `none`, is rejected.
  - Secret format: `wsec_` followed by 43 urlsafe-base64 characters (32 random bytes).
  - The secret is encrypted at rest and returned in plaintext exactly once, on create or rotate.
- **Widget public key:** `wk_` followed by 24 urlsafe characters, random, unique, and not secret.
- **Allowed domains:**
  - Up to 20 entries.
  - Each entry is a host with an optional port and an optional `*.` wildcard prefix for subdomains, e.g. `shop.example.com`, `*.example.com`, `localhost:5500`.
  - Entries are stored lowercased, without a scheme.
  - `frame-ancestors` lists `https://<entry>`, plus `http://<entry>` when the entry's host is `localhost` or `127.0.0.1`. `settings.web_origin` is always added, for the dashboard preview.
  - An empty list means only the preview works.
- **Permissions:**
  - Widgets: `channels.manage`.
  - Risk rules, business tools and the bot test console: `bot.configure`.
  - Conversations (read): any of `queries.answer`, `analytics.view` or `bot.configure`.
- **Conversation statuses:** `bot` (the bot is answering), `pending_human` (escalated, waiting for a person), `resolved` (ended by the customer or closed idle), `closed`. Phase 4 adds assignment states.
- **Risk actions:** `escalate`, `offer_human`, `bot_handles`. When several rules fire, the strongest action wins (escalate > offer_human > bot_handles). Ties go to the lowest `position`.
- **Default risk rules**, seeded lazily on the first `RiskRuleService.list()`:

  | Kind | Key or name | Action | Notes |
  |---|---|---|---|
  | topic | "Refunds" | escalate | |
  | topic | "Cancel subscription" | escalate | |
  | topic | "Payment changes" | escalate | Creating, updating or deleting payments or payment methods |
  | builtin | `asks_for_human` | escalate | |
  | builtin | `low_confidence` | offer_human | |
  | builtin | `frustration` | offer_human | |
  | builtin | `repeated_failure` | offer_human | `params {"failures": 2}` |
  | builtin | `write_action` | escalate | Locked: cannot be disabled, deleted or have its action changed |
- **Bot limits:**
  - At most 4 tool calls per turn; a 5th tool call request is answered without tools.
  - Tool HTTP timeout is 10 s.
  - The tool response is capped at 102400 bytes, and the text given to the model is cut to 8000 characters.
  - Chat timeout is 60 s. The classifier timeout is 20 s, at temperature 0.
  - The prompt history is the last 20 messages.
  - Turn lease: 120 s.
- **Idle close:** a conversation in `bot` status with no message for 30 min becomes `resolved`. The periodic job runs every 300 s: on Celery beat, or as an API task with inline jobs.
- **Widget UI text:**
  - Every string comes from `supportcore/widget/strings.py`, keyed by language (only `en` exists), and is sent to the iframe in the config response. The iframe page has no hard-coded English customer-facing strings.
  - The business may override `greeting`.

## Review Focus

1. **Escalation keywords must win even when the classifier is down.**
   - Expected: a message matching an escalate rule's keyword ("I want a refund") escalates without calling the LLM. When the classifier call fails, keyword rules still apply. When nothing matches, the bot answers, and the decision is recorded with source `classifier_unavailable`.
   - Tests: Task 7 `test_keyword_escalates_without_classifier_call`, `test_classifier_failure_falls_back_to_keywords`.
2. **Two messages sent quickly, or two tabs open.**
   - Expected: exactly one turn runs at a time. The second message is answered in a follow-up turn that includes it. Both sockets receive every event once.
   - Tests: Task 10 `test_concurrent_messages_single_turn_then_followup`, `test_two_sockets_receive_events_once`.
3. **A widget embedded on a site not in allowed domains, or a session forged from curl.**
   - Expected: `/embed/chat` is served with `frame-ancestors` listing only the allowed origins and the dashboard. A `parent_origin` outside the list is 403 `origin_not_allowed`. Session creation is rate limited per IP.
   - Tests: Task 6 `test_session_rejects_unlisted_parent_origin`, `test_session_rate_limited_429`; Task 13 `proxy sets frame-ancestors from frame-policy`.
4. **The business's chat key is missing or invalid, or the provider is down.**
   - Expected: when the bot is not ready, a new customer message escalates with reason `bot_unavailable`. A provider error mid-turn sends the customer the localised "sorry" string and then offers a human. No stack trace and no key ever reaches the widget.
   - Tests: Task 9 `test_not_ready_escalates_bot_unavailable`, `test_provider_error_offers_human`.
5. **A business tool that writes, or that points at an internal address.**
   - Expected: a model call to a tool marked `read_only=false` is never executed; the turn escalates via `write_action`. A tool URL on a private address is refused at save time in prod and at call time (hardened client), and the bot is told the tool failed.
   - Tests: Task 8 `test_private_url_refused`; Task 9 `test_write_tool_escalates_without_http_call`.

---

## File Structure

```
pyproject.toml / packages/core/pyproject.toml   + langgraph, langgraph-checkpoint-postgres, psycopg[binary], psycopg-pool; sqlalchemy<2.1
Makefile                                         migrate also runs `supportapi.cli setup-checkpointer`; + build-widget
.env.example                                     + Phase 3 settings
packages/core/src/supportcore/
  config.py                                      + checkpointer, pubsub, rate_limiter, trusted_proxy_hops, widget_session_ttl_seconds
  wiring.py                                      + rate limiter, pubsub, checkpointer, customer token verifier, risk assessor, tool executor
  ratelimit/{ports.py,redis_limiter.py,memory_limiter.py}
  pubsub/{ports.py,redis_pubsub.py,memory_pubsub.py}
  ai/chat_setup.py                               load_chat_setup() + build_chat_model() (shared by playground, bot, classifier)
  ai/chat/scripted_chat.py                       test-only scripted chat model with tool calls + streaming
  widget/{limits.py,strings.py,domains.py,service.py,events.py,session.py,tokens.py}
  widget/identity/{ports.py,jwt_verifier.py}
  conversations/{service.py,events.py}
  risk/{defaults.py,service.py,ports.py,rules_llm.py,prompts.py,events.py}
  tools/{schema.py,service.py,ports.py,http_executor.py,events.py}
  bot/{state.py,graph.py,nodes.py,checkpointer.py,postgres_checkpointer.py,memory_checkpointer.py,runtime.py,turns.py,prompts.py,metrics.py}
  db/models/{widget.py,conversation.py,risk.py,tools.py}
apps/api/src/supportapi/
  routers/{widgets.py,widget_public.py,widget_ws.py,risk.py,tools.py,conversations.py}
  schemas/{widgets.py,widget_public.py,risk.py,tools.py,conversations.py}
  client_ip.py
  cli.py                                         + setup-checkpointer
apps/api/migrations/versions/0005_widgets.py, 0006_conversations.py, 0007_risk_rules.py, 0008_business_tools.py
apps/worker/src/supportworker/celery_app.py      + close-idle-conversations beat entry
apps/widget/                                     package.json, build.mjs (esbuild), src/{loader.js,protocol.js}, tests/, demo/index.html
apps/web/
  proxy.js                                       frame-ancestors for /embed/*
  app/embed/chat/page.jsx                        iframe chat UI
  lib/{widget-client.js,widget-socket.js}
  components/embed/{chat-window.jsx,message-list.jsx,composer.jsx,handoff-panel.jsx,csat-form.jsx}
  app/orgs/[orgId]/{widgets,widgets/[widgetId],settings/risk,settings/tools,conversations,conversations/[conversationId]}/page.jsx
  components/{widgets,risk,tools,conversations}/*
  tests/e2e/{stub-llm.mjs,widget.spec.js}
```

---

### Task 1: Dependencies, Phase 3 settings, checkpointer port and setup command

**Files:**
- Modify:
  - `packages/core/pyproject.toml`
  - `packages/core/src/supportcore/config.py`
  - `.env.example`
  - `Makefile`
  - `apps/api/src/supportapi/cli.py`
  - `apps/api/migrations/env.py`
  - `packages/core/src/supportcore/wiring.py`
  - `packages/core/tests/test_layering.py`
- Create: `packages/core/src/supportcore/bot/{checkpointer.py,postgres_checkpointer.py,memory_checkpointer.py}`
- Test:
  - `packages/core/tests/bot/test_checkpointer.py`
  - `packages/core/tests/test_config.py`

**Interfaces:**
- Produces:
  - Settings fields:
    - `checkpointer: Literal["postgres","memory"] = "postgres"`
    - `pubsub: Literal["redis","memory"] = "redis"`
    - `rate_limiter: Literal["redis","memory"] = "redis"`
    - `trusted_proxy_hops: int = 0`
    - `widget_session_ttl_seconds: int = 86400`
  - `bot/checkpointer.py`:
    - `class CheckpointerProvider(Protocol)`, with `async def open(self) -> BaseCheckpointSaver`, `async def setup(self) -> None` and `async def aclose(self) -> None`
    - `def psycopg_conninfo(database_url: str) -> str`: strips the `+asyncpg` driver suffix
  - `Adapters.checkpointer: CheckpointerProvider`
  - CLI `setup-checkpointer`: calls `setup()`.

- [ ] **Step 1: Write failing tests**
  - `test_config.py::test_phase3_defaults`: asserts the five new defaults.
  - `test_checkpointer.py`:
    - `test_psycopg_conninfo_strips_driver`: `"postgresql+asyncpg://u:p@h:5432/d"` becomes `"postgresql://u:p@h:5432/d"`.
    - `test_memory_checkpointer_roundtrip`: compile a one-node graph with the memory saver, invoke it twice on the same `thread_id`, and assert the state accumulated.
    - `@pytest.mark.integration test_postgres_checkpointer_setup_idempotent`: `setup()` twice, then the same round trip on `TEST_DATABASE_URL`.
- [ ] **Step 2: Run them.** `uv run pytest packages/core/tests/bot packages/core/tests/test_config.py -v`. Expected: FAIL (import errors).
- [ ] **Step 3: Implement.**
  - **Dependencies:** `uv add --package supportcore langgraph langgraph-checkpoint-postgres "psycopg[binary]" psycopg-pool "sqlalchemy[asyncio]>=2.0.36,<2.1"`.
  - **Checkpointers:**
    - The Postgres provider lazily builds one `psycopg_pool.AsyncConnectionPool(conninfo, max_size=10, kwargs={"autocommit": True, "prepare_threshold": 0})` and an `AsyncPostgresSaver(pool)`.
    - The memory provider returns one `InMemorySaver`.
  - **Wiring:** register both in a `checkpointer_registry` in `wiring.py` and close them in `Adapters.aclose()`.
  - **Alembic:** `env.py` `include_name` and `include_object` skip the tables `checkpoints`, `checkpoint_blobs`, `checkpoint_writes` and `checkpoint_migrations`.
  - **Makefile:** `migrate:` runs `uv run python -m supportapi.cli setup-checkpointer` after alembic.
  - **Layering:** add `*_checkpointer.py` to the adapter globs, except `bot/checkpointer.py`, which is the port.
- [ ] **Step 4: Run the tests.** Expected: PASS. Run `make test` (`alembic check` included). Expected: PASS. Then start Jaeger and send one API request: a `SELECT` span now appears under the HTTP span.

---

### Task 2: RateLimiter port (Redis sliding window, memory) and client IP

**Files:**
- Create:
  - `packages/core/src/supportcore/ratelimit/{ports.py,redis_limiter.py,memory_limiter.py}`
  - `apps/api/src/supportapi/client_ip.py`
- Modify:
  - `wiring.py`
  - `apps/api/src/supportapi/errors.py`
- Test:
  - `packages/core/tests/ratelimit/test_limiters.py`
  - `apps/api/tests/test_client_ip.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) RateLimitResult(allowed: bool, remaining: int, retry_after_seconds: int)`
  - `class RateLimiter(Protocol): async def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult`
  - `class RateLimitedError(DomainError)`, with attribute `retry_after_seconds`, registered as `(429, "rate_limited")`. The handler adds a `Retry-After` header.
  - `async def enforce(limiter: RateLimiter, key: str, limit_window: tuple[int, int]) -> None`: raises `RateLimitedError`.
  - `Adapters.rate_limiter`
  - `client_ip(scope_or_request, trusted_proxy_hops: int) -> str`: with hops `n > 0`, uses the n-th address from the right of `X-Forwarded-For`, else the socket peer.

- [ ] **Step 1: Write failing tests.** Run every limiter test against both adapters (parametrised; the Redis one is `integration`):
  - `test_allows_up_to_limit_then_blocks`: limit 3, window 60. Three hits are allowed; the 4th is refused with `retry_after_seconds` in 1–60 and `remaining == 0`.
  - `test_window_slides`: inject a clock into the memory adapter (`now=` callable). After the window passes, hits are allowed again.
  - `test_keys_are_independent`.
  - `test_client_ip_ignores_forwarded_without_trusted_hops`.
  - `test_client_ip_uses_nth_from_right`: XFF `"1.1.1.1, 2.2.2.2"` with hops 1 gives `"2.2.2.2"`.
  - `test_rate_limited_error_sets_retry_after_header`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - **Redis adapter:** one sorted set per key. In a `MULTI` pipeline, `ZREMRANGEBYSCORE` drops entries older than the window and `ZCARD` counts what is left. When the count is below the limit, `ZADD now:uuid` and set `PEXPIRE` to the window. Otherwise compute `retry_after` from the oldest entry.
  - **Keys:** prefixed `rl:`.
  - **Memory adapter:** a dict of deques, guarded by an `asyncio.Lock`.
  - **Wiring:** use `redis.asyncio.from_url(settings.redis_url)`, and close the client in `aclose()`.
- [ ] **Step 4: Run the tests.** Expected: PASS.

---

### Task 3: PubSub port (Redis, memory)

**Files:**
- Create: `packages/core/src/supportcore/pubsub/{ports.py,redis_pubsub.py,memory_pubsub.py}`
- Modify: `wiring.py`
- Test: `packages/core/tests/pubsub/test_pubsub.py`

**Interfaces:**
- Produces:
  - `class PubSub(Protocol)`:
    - `async def publish(self, channel: str, message: dict) -> None`: JSON-serialisable.
    - `def subscribe(self, channel: str) -> AsyncContextManager[AsyncIterator[dict]]`
  - `def conversation_channel(conversation_id: UUID) -> str`: returns `"conv:" + hex`.
  - `Adapters.pubsub`

- [ ] **Step 1: Write failing tests.** Parametrise over both adapters; Redis is `integration`.
  - `test_subscriber_receives_published_in_order`: three messages arrive in order.
  - `test_two_subscribers_each_receive_once`.
  - `test_unsubscribe_on_exit_stops_delivery`: after the context exits, the memory adapter has no listeners left for the channel.
  - `test_other_channel_not_delivered`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - **Redis adapter:** one `redis.asyncio` client. Each `subscribe` opens its own `pubsub()`, subscribes, yields an async iterator of JSON-decoded `message` payloads, and unsubscribes and closes on exit.
  - **Memory adapter:** a set of `asyncio.Queue` per channel.
- [ ] **Step 4: Run the tests.** Expected: PASS.

---

### Task 4: Retrieval hardening and a shared chat model setup

**Files:**
- Create: `packages/core/src/supportcore/ai/chat_setup.py`
- Modify:
  - `knowledge/retrieval.py`
  - `knowledge/playground.py`
  - `ai/catalog.py`
  - `apps/api/src/supportapi/schemas/playground.py`
  - `apps/web/components/knowledge/playground-panel.jsx` (shows warnings)
- Test:
  - `packages/core/tests/knowledge/test_retrieval.py`
  - `packages/core/tests/ai/test_chat_setup.py`
  - existing playground tests

**Interfaces:**
- Produces:
  - `EmbeddingModelSpec.confidence_threshold: float`. Values:

    | Model | Threshold |
    |---|---|
    | `BAAI/bge-small-en-v1.5` | 0.60 |
    | `intfloat/multilingual-e5-small` | 0.80 |
    | openai (both) | 0.35 |
    | openai_compatible | 0.35 |
    | fake | 0.0 |

  - `RetrievedChunk.confidence: float`: cosine score minus the KB model's threshold, rounded to 4 places.
  - `RetrievalResult.warnings: list[str]`.
  - `RetrievalResult.best_confidence -> float | None` (property).
  - `ai/chat_setup.py`:
    - `@dataclass ChatSetup(provider, model, temperature, api_key, base_url, organisation_name, language)`
    - `async def load_chat_setup(session, organisation_id, adapters, settings, events) -> ChatSetup | None`: returns `None` when the bot is not ready.
    - `async def build_chat_model(adapters, settings, setup: ChatSetup, *, temperature: float | None = None, timeout: float) -> BaseChatModel`: runs `ensure_public_base_url` first when `base_url` is set.
- Consumes: `RetrievalService`, `PlaygroundService` and `OrganisationAISettingsService` (Phase 2).

- [ ] **Step 1: Write failing tests.**
  - `test_failing_kb_is_skipped_with_warning`: two KBs, and the second's vector query raises `RuntimeError`. The first KB's chunks are returned, and `warnings == ["kb_unavailable:<kb2 id>"]`.
  - `test_unlisted_reranker_skips_rerank_with_warning`:
    - Setup: a KB row with `reranker="cross_encoder"` and `reranker_params={"model":"evil/model"}`.
    - Expected: chunks come back in cosine order, `rerank_score is None`, and the warning reads `reranker_unavailable:<kb id>`.
  - `test_merge_orders_by_confidence_when_not_all_reranked`: KB A has a threshold of 0.0 (fake) and score 0.5; KB B has a threshold of 0.35 (patched spec) and score 0.6. A ranks first.
  - `test_merge_orders_by_rerank_when_all_reranked`.
  - `test_best_confidence_none_when_no_chunks`.
  - `test_load_chat_setup_none_when_not_ready`.
  - `test_build_chat_model_runs_ssrf_guard`: an `openai_compatible` setup whose `base_url` is `http://169.254.169.254` with `allow_private=False` raises `CredentialInvalidError`.
  - Existing playground tests still pass.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - **Failing KB:** wrap each KB's search and rerank in try/except, logging `retrieval_kb_failed` with the kb id and error type.
  - **Bad reranker config:** catch `ValueError` and `UnknownProviderError` from `rerankers.create`.
  - **Merge order:** sort by `rank_score` when every target reranked, else by `confidence`.
  - **Playground:** use `load_chat_setup` and `build_chat_model` and delete its private copies. `PlaygroundOut` gains `warnings`, which already exists, and now merges retrieval warnings.
- [ ] **Step 4: Run** `uv run pytest packages/core/tests/knowledge packages/core/tests/ai apps/api/tests/test_playground_routes.py -v`. Expected: PASS.

---

### Task 5: Widgets — model, service, admin routes

**Files:**
- Create:
  - `db/models/widget.py`
  - `migrations/versions/0005_widgets.py`
  - `widget/{domains.py,service.py,events.py,strings.py}`
  - `routers/widgets.py`
  - `schemas/widgets.py`
- Modify:
  - `db/models/__init__.py`
  - `audit/subscribers.py`
  - `errors.py`
  - `main.py`
- Test:
  - `packages/core/tests/widget/test_domains.py`
  - `packages/core/tests/widget/test_service.py`
  - `apps/api/tests/test_widget_routes.py`

**Interfaces:**
- Produces:
  - **`Widget` model** (Tenant, Timestamp):
    - `id`, `name` (1–100 characters)
    - `public_key` (unique, indexed)
    - `enabled: bool = True`
    - `allowed_domains: list[str]` (JSONB)
    - `appearance: dict` (JSONB), with keys:
      - `primary_color` (`#rrggbb`)
      - `position` (`bottom-right` | `bottom-left`)
      - `launcher` (`bubble` | `custom`)
      - `logo_url` (https or null)
      - `greeting` (≤ 500 characters or null)
    - `csat_enabled: bool = True`
    - `encrypted_identity_secret: str | None`, `identity_secret_last4: str | None`
    - `identity_required: bool = False`
    - `created_by_user_id`
  - **`WidgetKnowledgeBase`:** `widget_id` + `knowledge_base_id`, as a composite PK, with cascade on both foreign keys.
  - **`widget/domains.py`:**
    - `normalize_domain(entry: str) -> str`: raises `InvalidDomainError`.
    - `origin_allowed(origin: str, domains: list[str], *, web_origin: str) -> bool`
    - `frame_ancestors(domains, web_origin) -> str`
  - **`WidgetService(session, organisation_id, events, cipher)`:**
    - `create(name, actor)`
    - `get(id)`
    - `list()`
    - `update(id, *, name, enabled, allowed_domains, appearance, csat_enabled, identity_required, knowledge_base_ids, actor)`
    - `delete(id, actor)`
    - `rotate_identity_secret(id, actor) -> tuple[Widget, str]`
    - `get_by_public_key(key) -> Widget | None` (static, not tenant-scoped; returns the widget only when it is enabled)
    - `knowledge_base_ids(widget_id) -> list[UUID]`
  - **Errors:**
    - `InvalidDomainError` → 422 `invalid_domain`
    - `InvalidAppearanceError` → 422 `invalid_appearance`
    - `TooManyDomainsError` → 422 `too_many_domains`
    - `IdentitySecretRequiredError` → 422 `identity_secret_required`, raised when `identity_required` is set with no secret
  - **Events:** `WidgetCreated`, `WidgetUpdated` (with a `changes` diff; secret values are never in it), `WidgetDeleted`, `WidgetIdentitySecretRotated`. All are audited.
  - **Routes** (`channels.manage`):
    - `GET` and `POST /orgs/{org}/widgets`
    - `GET`, `PATCH` and `DELETE /orgs/{org}/widgets/{id}`
    - `POST /orgs/{org}/widgets/{id}/identity-secret`, which returns `{"secret": "wsec_…"}` once
  - **`WidgetOut`:** includes `snippet`, built server-side: `<script src="{web_origin}/widget/v1.js" data-key="{public_key}" async></script>`. It never includes the secret, only `identity_secret_last4`.
  - **`widget/strings.py`:**
    - `STRINGS: dict[str, dict[str, str]]`, with `en` keys: `greeting`, `placeholder`, `send`, `connecting_human`, `human_offer`, `talk_to_human`, `no_agent_email_prompt`, `email_saved`, `bot_error`, `end_chat`, `csat_prompt`, `csat_thanks`, `new_conversation`, `powered_by`.
    - `strings_for(language) -> dict[str, str]`: falls back to `en`.

- [ ] **Step 1: Write failing tests.**
  - **Domains:**
    - `test_normalize_domain`: `"HTTPS://Shop.Example.com/"` becomes `"shop.example.com"`; `"*.Example.com"` becomes `"*.example.com"`; `"localhost:5500"` is kept.
    - `test_normalize_rejects`: `"*"`, `"exa mple.com"`, `"http://"`, `"a.com/path?x"` (a path counts as a reject only when it is not a bare `/`), `"*.*.com"`.
    - `test_origin_allowed_wildcard_matches_subdomain_not_apex`.
    - `test_origin_allowed_requires_https_except_localhost`.
    - `test_frame_ancestors_contains_web_origin_and_entries`.
  - **Service:**
    - `test_create_generates_unique_public_key_prefix_wk`.
    - `test_update_rejects_kb_from_other_org_404`.
    - `test_rotate_secret_returns_plaintext_once_and_stores_ciphertext`: the column is not equal to the plaintext, `cipher.decrypt` round-trips, and the audit metadata has no secret.
    - `test_identity_required_without_secret_422`.
  - **Routes:**
    - `test_widget_crud_requires_channels_manage`: an Agent role gets 403.
    - `test_cross_tenant_widget_404`.
    - `test_widget_out_has_snippet_and_no_secret`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - The model, the migration `0005_widgets` and the service, following `KnowledgeBaseService` patterns.
  - Appearance validation lives in the service: unknown keys are rejected.
  - A KB link to another org's KB raises `NotFoundError`.
- [ ] **Step 4: Run the tests and `alembic check`.** Expected: PASS.

---

### Task 6: Customers, conversations and the public widget session API

**Files:**
- Create:
  - `db/models/conversation.py`
  - `migrations/versions/0006_conversations.py`
  - `widget/identity/{ports.py,jwt_verifier.py}`
  - `widget/{tokens.py,session.py,limits.py}`
  - `conversations/{service.py,events.py}`
  - `routers/widget_public.py`
  - `schemas/widget_public.py`
- Modify: `wiring.py`, `container.py`, `deps.py`, `errors.py`, `main.py`
- Test:
  - `packages/core/tests/widget/test_identity.py`
  - `packages/core/tests/widget/test_tokens.py`
  - `packages/core/tests/conversations/test_service.py`
  - `apps/api/tests/test_widget_public_routes.py`

**Interfaces:**
- Produces:
  - **Models** (all Tenant):
    - **`Customer`:**
      - `id`
      - `external_id: str | None`, unique per org when set (partial unique index)
      - `visitor_id: str | None`, unique per org when set
      - `email`, `name`
      - `last_seen_at`
    - **`Conversation`:**
      - `id`, `widget_id` (FK, SET NULL), `customer_id` (FK, CASCADE)
      - `channel="chat"`
      - `status` (CHECK one of the four statuses)
      - `language`
      - `is_test: bool` (dashboard preview)
      - `escalation: dict | None` (JSONB `{action, rule_key, rule_name, reason, source}`)
      - `contact_email: str | None`
      - `csat_score: int | None` (1–5), `csat_comment: str | None` (≤ 1000 characters)
      - `turn_lease_until: datetime | None`
      - `last_message_at`
      - Index `(organisation_id, status, last_message_at)`.
    - **`Message`:**
      - `id`, `conversation_id` (CASCADE)
      - `role` (`customer` | `bot` | `agent` | `system`)
      - `text`
      - `citations: list` (JSONB `[{chunk_id, document_id, document_name}]`)
      - `client_id: str | None`, unique per conversation when set (idempotent resend)
      - `created_at`
      - Index `(conversation_id, created_at)`.
    - **`BotTurn`:**
      - `id`, `conversation_id`, `message_id` (the customer message that started the turn), `reply_message_id: UUID | None`
      - `outcome` (`answered` | `offered_human` | `escalated` | `error`)
      - `no_answer: bool` (the model replied `[NO_ANSWER]`; feeds the Phase 5 knowledge-gaps report)
      - `risk: dict` (decision)
      - `retrieval: dict` (`{chunks: [{chunk_id, kb_id, score, confidence, rerank_score}], warnings, timings_ms}`)
      - `tool_calls: list` (`[{tool_id, name, status, latency_ms}]`)
      - `model: dict` (`{provider, model, input_tokens, output_tokens}`)
      - `latency_ms`, `trace_id`, `created_at`
  - **Customer identity:**
    - `class CustomerTokenVerifier(Protocol): def verify(self, token: str, secret: str) -> VerifiedCustomer`
    - `VerifiedCustomer(external_id, email, name)`
    - `InvalidCustomerTokenError` → 401 `invalid_customer_token`
  - **`widget/tokens.py`:**
    - `WidgetTokenService(secret, ttl)`
    - `issue(customer_id, organisation_id, widget_id) -> str`
    - `decode(token) -> WidgetClaims(customer_id, organisation_id, widget_id)`: raises `InvalidWidgetTokenError` → 401 `invalid_widget_token`
  - **`widget/session.py` `WidgetSessionService(session, adapters, settings, events)`:**
    - `public_config(public_key) -> PublicWidgetConfig` (`appearance`, `strings`, `language`, `csat_enabled`, `organisation_name`): raises `NotFoundError` for an unknown or disabled key.
    - `frame_policy(public_key) -> str`
    - `start(public_key, *, parent_origin, visitor_id, customer_token, is_preview) -> SessionStart(token, customer_id, visitor_id, conversation: Conversation | None)`
    - Errors:
      - `OriginNotAllowedError` → 403 `origin_not_allowed`
      - `IdentityRequiredError` → 401 `identity_required`
  - **`ConversationService(session, organisation_id, events)`:**
    - `create(customer_id, widget_id, language, is_test)`
    - `list_for_customer(customer_id, limit=20)`
    - `get_for_customer(id, customer_id)`: a foreign id gives `NotFoundError`.
    - `messages(conversation_id, *, after: datetime | None, limit=100)`
    - `add_customer_message(conversation, text, client_id) -> tuple[Message, bool created]`
    - `add_message(conversation, role, text, citations=())`
    - `end(conversation)`: sets `resolved`.
    - `rate(conversation, score, comment)`: only when `resolved` and `csat_enabled`, else `InvalidConversationStateError` → 409 `invalid_conversation_state`.
    - `set_contact_email(conversation, email)`: only when `pending_human`.
    - `escalate(conversation, decision)`
    - `acquire_turn_lease(conversation_id, seconds=120) -> bool`: an atomic `UPDATE … WHERE lease IS NULL OR lease < now()`.
    - `release_turn_lease(conversation_id)`
  - **Events:** `ConversationStarted`, `ConversationEscalated`, `ConversationResolved`, `CsatSubmitted`. Logged with ids only; not audited, since they are not settings changes.
  - **Public routes** (no auth unless noted; every one rate limited):
    - `GET /widget/v1/config?key=`: `CONFIG_PER_IP`.
    - `GET /widget/v1/frame-policy?key=`: returns `{"frame_ancestors": "..."}`; unknown keys give `"'none'"`; `CONFIG_PER_IP`.
    - `POST /widget/v1/sessions` with body `{key, parent_origin, visitor_id?, customer_token?, preview?: bool}`. `preview=true` is honoured only when `parent_origin == settings.web_origin`. Limited by `SESSION_PER_IP`.
    - Bearer widget token required:
      - `GET /widget/v1/conversations` (the customer's own)
      - `POST /widget/v1/conversations`: limited by `CONVERSATION_CREATE_PER_CUSTOMER`.
      - `GET /widget/v1/conversations/{id}/messages?after=`
      - `POST /widget/v1/conversations/{id}/end`
      - `POST /widget/v1/conversations/{id}/csat`
      - `POST /widget/v1/conversations/{id}/contact`
  - **Container:** gains `widget_tokens: WidgetTokenService`.
  - **deps:** `get_widget_claims` reads the Bearer token, decodes it with `WidgetTokenService`, and binds the organisation.

- [ ] **Step 1: Write failing tests.**
  - **Identity:**
    - `test_verify_valid_token`.
    - `test_rejects_alg_none`.
    - `test_rejects_rs256_header`.
    - `test_rejects_missing_exp`.
    - `test_rejects_exp_more_than_24h_ahead`.
    - `test_rejects_wrong_secret`.
    - `test_rejects_sub_too_long`.
  - **Tokens:**
    - `test_widget_token_roundtrip`.
    - `test_access_token_rejected_by_widget_service`.
    - `test_widget_token_rejected_on_dashboard_route`: `GET /me` with a widget token gives 401.
  - **Service:**
    - `test_start_anonymous_creates_customer_with_new_visitor_id`.
    - `test_start_same_visitor_reuses_customer`.
    - `test_start_signed_identifies_by_external_id_across_visitors`.
    - `test_identity_required_without_token_401`.
    - `test_add_customer_message_idempotent_by_client_id`.
    - `test_turn_lease_exclusive_until_expiry`.
    - `test_csat_only_after_resolved`.
  - **Routes:**
    - `test_session_rejects_unlisted_parent_origin`: 403 `origin_not_allowed`.
    - `test_session_rate_limited_429`: memory limiter; the 21st call gives 429 with `Retry-After`.
    - `test_config_unknown_key_404`.
    - `test_disabled_widget_404`.
    - `test_customer_cannot_read_other_customers_conversation_404`.
    - `test_message_text_never_logged`: capture structlog output while posting a conversation's messages and assert the text is absent.
    - `test_preview_only_from_web_origin`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - **Customer verifier:** `jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["sub", "exp"]})`, then the 24 h `exp` check.
  - **Wiring:** register `jwt_hs256` in a `customer_token_registry`.
  - **Rate-limit keys:**
    - `ip:{route}:{client_ip}`
    - `conv:{id}`
    - `org:{id}:msg`
    - `cust:{id}:conv`
  - **CORS:** the public routes are called from the iframe, which is on `web_origin`, so the existing CORS config is enough.
- [ ] **Step 4: Run the tests and `alembic check`.** Expected: PASS.

---

### Task 7: Risk rules and the rules_llm risk assessor

**Files:**
- Create:
  - `db/models/risk.py`
  - `migrations/versions/0007_risk_rules.py`
  - `risk/{defaults.py,service.py,ports.py,rules_llm.py,prompts.py,events.py}`
  - `routers/risk.py`
  - `schemas/risk.py`
- Modify: `wiring.py`, `errors.py`, `audit/subscribers.py`, `main.py`
- Test:
  - `packages/core/tests/risk/test_service.py`
  - `packages/core/tests/risk/test_rules_llm.py`
  - `apps/api/tests/test_risk_routes.py`

**Interfaces:**
- Produces:
  - **`RiskRule` model** (Tenant, Timestamp):
    - `id`
    - `kind` (`topic` | `builtin`)
    - `builtin_key: str | None`, unique per org when set
    - `name` (1–100 characters)
    - `description` (≤ 1000 characters; the classifier sees it)
    - `keywords: list[str]` (JSONB, ≤ 30, each 2–100 characters)
    - `action`
    - `enabled`
    - `locked`
    - `position: int`
    - `params: dict`
  - **`risk/defaults.py`:**
    - `DEFAULT_RULES` (table above, with descriptions and English keywords). Refunds: `["refund", "money back", "return for a refund"]`. Cancel: `["cancel my subscription", "cancel subscription", "unsubscribe", "close my account"]`. Payment changes: `["update my card", "change my card", "payment method", "remove my card", "charge my card", "change billing"]`.
    - `HUMAN_REQUEST_KEYWORDS = {"en": ["human", "real person", "agent", "representative", "speak to someone", "talk to someone"]}`
    - `BUILTIN_KEYS`
  - **`RiskRuleService(session, organisation_id, events)`:**
    - `list()`: seeds the defaults on first call, with `ON CONFLICT DO NOTHING` on `(organisation_id, builtin_key)`.
    - `create_topic(name, description, keywords, action, actor)`
    - `update(id, …, actor)`
    - `delete(id, actor)`: builtins can't be deleted (409 `builtin_rule`) and locked rules can't be changed (409 `rule_locked`).
    - `reorder(ids, actor)`
    - `enabled_rules() -> list[RiskRuleSnapshot]`: plain dataclasses, usable after the session closes.
  - **Ports:**
    - `RiskInput(message, recent_messages: list[tuple[role, text]], language, rules: list[RiskRuleSnapshot], failed_answers: int)`
    - `RiskDecision(action, rule_key: str | None, rule_name: str | None, reason: str, source: Literal["keyword", "classifier", "classifier_unavailable", "none", "system", "customer"])`
    - `class RiskAssessor(Protocol)`:
      - `async def assess(self, inp: RiskInput, classify: Classify | None) -> RiskDecision`
      - `Classify = Callable[[list[BaseMessage]], Awaitable[str]]`: built by the bot from `build_chat_model` with temperature 0 and a 20 s timeout. `None` means no chat model.
    - `def post_retrieval_decision(rules, best_confidence: float | None, failed_answers: int) -> RiskDecision | None`: covers `low_confidence` (best confidence `< params.get("min_confidence", 0.0)`, or no chunks) and `repeated_failure` (`failed_answers >= params["failures"]`).
    - `rule_key` for topics is `f"topic:{rule_id}"`.
  - **`risk/prompts.py`:**
    - `build_classifier_messages(inp) -> list[BaseMessage]`. The system prompt starts with the marker line `SUPPORT_RISK_CLASSIFIER v1`, so the e2e stub can recognise it.
    - It asks for JSON: `{"topic_ids": [..], "asks_for_human": bool, "frustrated": bool, "reason": str}`.
    - Rules are listed as `id: name — description`.
  - **`RulesLlmAssessor` order:**
    1. Keyword match on enabled topics, and `asks_for_human` against `HUMAN_REQUEST_KEYWORDS[language]`: case-insensitive whole-word or phrase match (`\b…\b`). A hit for an `escalate` rule returns immediately, without calling `classify`.
    2. The classifier, with the JSON parsed out of the first `{…}` in the text. On parse failure, timeout or provider error, use the keyword hits only and set `source="classifier_unavailable"`.
    3. Combine all fired rules and pick the strongest action, then the lowest position.
    4. If nothing fired, return `bot_handles` with `source="none"`.
  - **Routes:**
    - `GET /orgs/{org}/risk-rules` (`bot.configure` or `analytics.view`)
    - `POST /orgs/{org}/risk-rules`
    - `PATCH` and `DELETE /orgs/{org}/risk-rules/{id}`
    - `PUT /orgs/{org}/risk-rules/order`
    - Writes need `bot.configure`.
  - **Events:** `RiskRuleCreated`, `RiskRuleUpdated` and `RiskRuleDeleted`, all audited.

- [ ] **Step 1: Write failing tests.**
  - **Service:**
    - `test_list_seeds_defaults_once`: concurrent first lists give 8 rules, with no duplicates.
    - `test_builtin_cannot_be_deleted_409`.
    - `test_write_action_locked`.
    - `test_keywords_validated`.
  - **Assessor** (use a recording `classify` stub):
    - `test_keyword_escalates_without_classifier_call`: "I want a refund please" escalates on Refunds with `source="keyword"`, and `classify` is not called.
    - `test_human_request_keyword_escalates`.
    - `test_classifier_topic_escalates`: the stub returns `{"topic_ids": ["<cancel id>"], ...}` for "how do I stop paying monthly".
    - `test_classifier_frustration_offers_human`.
    - `test_strongest_action_wins`: frustration (offer) plus refund (escalate) gives escalate.
    - `test_disabled_rule_ignored`.
    - `test_classifier_failure_falls_back_to_keywords`: `classify` raises; "refund" still escalates; with no keyword, the result is `bot_handles` with `source="classifier_unavailable"`.
    - `test_classifier_garbage_json_is_unavailable`.
    - `test_unknown_topic_id_from_classifier_ignored`.
  - **Post-retrieval decision:**
    - `test_post_retrieval_low_confidence_offer`.
    - `test_post_retrieval_repeated_failure_offer`.
  - **Routes:**
    - `test_risk_rules_permissions`: Agent gets 403; an analytics viewer can read.
    - `test_cross_tenant_rule_404`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** as specified. Register `rules_llm` in `risk_assessor_registry` and add `Adapters.risk_assessor`.
- [ ] **Step 4: Run the tests and `alembic check`.** Expected: PASS.

---

### Task 8: Business tools — "call my API"

**Files:**
- Create:
  - `db/models/tools.py`
  - `migrations/versions/0008_business_tools.py`
  - `tools/{schema.py,service.py,ports.py,http_executor.py,events.py}`
  - `routers/tools.py`
  - `schemas/tools.py`
- Modify: `wiring.py`, `errors.py`, `audit/subscribers.py`, `main.py`, `test_layering.py`
- Test:
  - `packages/core/tests/tools/test_schema.py`
  - `packages/core/tests/tools/test_service.py`
  - `packages/core/tests/tools/test_http_executor.py`
  - `apps/api/tests/test_tool_routes.py`

**Interfaces:**
- Produces:
  - **`BusinessTool` model** (Tenant, Timestamp):
    - `id`
    - `name`: matches `^[a-z][a-z0-9_]{1,63}$` and is unique per org.
    - `description`: 1–1000 characters; the model sees it.
    - `method` (`GET` | `POST`)
    - `url_template`: https; may contain `{param}` placeholders that name input properties.
    - `auth_header_name: str | None`, `encrypted_auth_header_value: str | None`, `auth_header_last4`
    - `input_schema: dict`
    - `read_only: bool = True`
    - `enabled: bool = True`
  - **`tools/schema.py`:**
    - `validate_input_schema(schema) -> dict`: `type: object`, at most 20 `properties`, each of type `string`, `number`, `integer` or `boolean`, with optional `description` and `enum`, and `required` a subset of the properties. Raises `InvalidToolSchemaError` → 422 `invalid_tool_schema`.
    - `validate_arguments(schema, args) -> dict`: raises `InvalidToolArgumentsError`.
    - `tool_definition(tool) -> dict`: `{"name", "description", "parameters"}`, ready for `bind_tools`.
  - **`ToolExecutor` port:** `async def call(self, tool: ToolSnapshot, args: dict, auth_value: str | None) -> ToolResult(ok: bool, status: int | None, text: str, latency_ms: int, error: str | None)`.
  - **`HttpToolExecutor(settings)`:**
    - Placeholders are substituted with URL-encoded values.
    - `GET` sends the remaining args as query params; `POST` sends them as a JSON body.
    - It uses the guarded client from `supportcore.ai.http` with `allow_private=settings.effective_allow_private_base_urls()`, `max_response_bytes=102400` and a 10 s timeout.
    - It never raises: errors become `ok=False`, with `error` set to `timeout`, `blocked_address`, `http_<status>`, `response_too_large` or `network_error`.
  - **`BusinessToolService(session, organisation_id, events, cipher, settings)`:**
    - `list()`, `create(...)`, `update(...)`, `delete(...)`
    - `enabled_snapshots() -> list[ToolSnapshot]`: plain data, including the decrypted auth value, held only for one turn.
    - `test(id, args) -> ToolResult`: commits before the HTTP call.
  - **Routes** (`bot.configure`):
    - CRUD on `/orgs/{org}/tools`
    - `POST /orgs/{org}/tools/{id}/test` with body `{arguments}`
  - **Events** are audited. The auth value is never in a diff; the diff says `"auth_header": "changed"`.

- [ ] **Step 1: Write failing tests.**
  - **Schema:**
    - `test_validate_schema_accepts_simple_object`.
    - `test_rejects_nested_object`.
    - `test_rejects_placeholder_not_in_properties`.
    - `test_validate_arguments_types_and_required`.
  - **Executor** (an `httpx.MockTransport` injected through a test seam, `transport=`):
    - `test_get_substitutes_and_encodes_placeholder`: `order_id="a/b"` becomes `.../orders/a%2Fb`.
    - `test_post_sends_json`.
    - `test_auth_header_sent_not_logged`.
    - `test_timeout_returns_error_result`.
    - `test_response_capped`.
    - `test_private_url_refused`: `allow_private=False` and `http://10.0.0.5/x` give `ok=False` with `error="blocked_address"`.
  - **Service and routes:**
    - `test_create_requires_https_in_prod`.
    - `test_auth_value_encrypted_and_last4_only`.
    - `test_tool_routes_require_bot_configure`.
    - `test_cross_tenant_tool_404`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - Add an optional `transport` parameter to `guarded_async_client`, for tests only.
  - Register `http` in `tool_executor_registry` and add `Adapters.tool_executor`.
  - Add `tools/http_executor.py` to the layering globs.
- [ ] **Step 4: Run the tests and `alembic check`.** Expected: PASS.

---

### Task 9: The LangGraph bot

**Files:**
- Create:
  - `bot/{state.py,graph.py,nodes.py,prompts.py,runtime.py,metrics.py}`
  - `ai/chat/scripted_chat.py`
- Modify:
  - `wiring.py`: register a `scripted` chat provider for tests only.
  - `observability/metrics.py`
  - `ai/prompts.py`
- Test: `packages/core/tests/bot/test_graph.py`

**Interfaces:**
- Consumes:
  - `RetrievalService` (Task 4)
  - `load_chat_setup` and `build_chat_model` (Task 4)
  - `RiskAssessor` and `post_retrieval_decision` (Task 7)
  - `BusinessToolService.enabled_snapshots` and `ToolExecutor` (Task 8)
  - `ConversationService` and `BotTurn` (Task 6)
  - `WidgetService.knowledge_base_ids` (Task 5)
  - `Adapters.checkpointer` (Task 1)
- Produces:
  - `bot/runtime.py`: `@dataclass BotRuntime(settings, session_factory, adapters, events)`.
  - `bot/state.py`:
    - `class BotState(TypedDict)`:
      - `messages: Annotated[list[BaseMessage], add_messages]`
      - `failed_answers: int`
      - `decision: dict | None`
      - `retrieval: dict | None`
      - `tool_calls: list[dict]`
      - `tool_rounds: int`
      - `outcome: str | None`
      - `reply: dict | None` (`{text, citations}`)
    - The per-turn fields are reset by the `prepare` node.
  - `bot/graph.py`:
    - `def build_bot_graph(runtime: BotRuntime) -> StateGraph` (uncompiled).
    - `async def compile_bot_graph(runtime) -> CompiledStateGraph`: uses the checkpointer.
  - Turn context: `@dataclass TurnInput(organisation_id, conversation_id, widget_id, message_id, text, language)`. It is passed through `config["configurable"]`, with `thread_id = str(conversation_id)`.
  - Streaming: `TurnSink = Protocol` with `async token(delta: str)`. It is passed as `config["configurable"]["sink"]`.
  - Graph nodes, in order:
    1. `prepare`: in one `UnitOfWork`, reads the chat setup (or `None`), enabled risk rules, tool snapshots and the widget's KB ids into a per-turn context kept in `config` (never in checkpointed state). It appends `HumanMessage(text)` and resets the turn fields.
    2. `assess_risk`:
       - If the chat setup is `None`, the decision is escalate, with `rule_key="bot_unavailable"` and `source="system"`.
       - Otherwise it calls the assessor with `classify` built from the chat setup.
       - Routes: `escalate` → `handoff`; else → `retrieve`.
    3. `retrieve`: runs `RetrievalService` over the widget's KBs with `top_k=5` (prepare, release, run), storing chunk ids, scores, confidence and warnings in `retrieval`. With no KBs, it is skipped and confidence is `None`.
    4. `generate`:
       - Builds the messages: system prompt, the last 20 history messages, and a context block from retrieval.
       - Binds `tool_definition`s when there are any tools and `tool_rounds < 4`.
       - Calls the model with `astream`, forwarding text deltas to `sink.token`. It aggregates the chunks into one `AIMessage`.
       - With tool calls it routes to `tools`; otherwise it sets `reply` (text, plus citations via `parse_citations`) and routes to `decide`.
       - A reply containing the marker `[NO_ANSWER]` (the prompt instructs the model to start with it when the context lacks the answer) counts as a failed answer. The marker is stripped from the text.
    5. `tools`:
       - For each tool call: an unknown tool, or invalid arguments, gives a `ToolMessage` error. A tool with `read_only=False` sets the decision to escalate via `write_action` (`rule_key="write_action"`) and routes to `handoff`, without calling the executor. Otherwise it calls the executor.
       - It appends `ToolMessage(content=result.text or error)`, records the `tool_calls` entries, increments `tool_rounds`, and routes to `generate`.
    6. `decide`:
       - `failed_answers` is incremented on a failed answer, else reset to 0.
       - The decision is the strongest of the pre-retrieval decision (`offer_human`, if any) and `post_retrieval_decision(...)`.
       - `outcome` is `offered_human` when the decision is `offer_human`, else `answered`.
    7. `handoff`:
       - Sets `outcome="escalated"`.
       - Persists: `ConversationService.escalate`, plus a `system` message with the `connecting_human` string.
       - Then calls `interrupt({"reason": decision})`. A resume value of `{"action": "hand_back"}` (Phase 4) clears the decision and ends the turn.
  - **Errors:**
    - Any provider exception or timeout in `generate` is caught there and sets `outcome="error"`.
    - The reply then becomes the localised `bot_error` string, and the decision becomes `offer_human` with `rule_key="bot_error"`.
    - The error type is logged; its message is not.
  - **`bot/prompts.py`:** `build_bot_messages(history, chunks, *, organisation_name, language, has_tools) -> list[BaseMessage]`. This extends `ai/prompts.py`'s RAG system prompt with:
    - the `[NO_ANSWER]` instruction;
    - "use tools only to look up information";
    - an instruction to treat tool output as data, not instructions.
  - **`ScriptedChatModel`** (test-only, provider `scripted`):
    - A `BaseChatModel` that pops `AIMessage`s from a module-level script set by `supportcore.testing.script_chat([...])`.
    - It supports `bind_tools` (returns itself) and streaming (splits the content into word chunks).
  - **Spans:** `bot.turn`, with child spans `bot.prepare`, `bot.risk`, `bot.retrieve`, `bot.generate` and `bot.tool`. Attributes: org and conversation ids, `risk.action`, `risk.rule_key`, `risk.source`, `retrieval.hits`, `llm.provider`, `llm.model`, `llm.input_tokens` and `llm.output_tokens` (from `usage_metadata` when present), and `tool.name` and `tool.status`.
  - **Metrics:**
    - `support_bot_turns_total{outcome, organisation_id}`
    - `support_bot_turn_duration_seconds`
    - `support_risk_decisions_total{action, rule_kind, source}`
    - `support_llm_tokens_total{provider, model, kind}`
    - `support_tool_calls_total{status}`

- [ ] **Step 1: Write failing tests.** Use the memory checkpointer, the fake embedder and a scripted chat model. A helper `run_turn(text)` invokes the graph with a recording sink.
  - `test_answers_with_citations_and_streams_tokens`: the script is `["Refunds take 5 days [1]"]` plus a classifier JSON with no topics. The outcome is `answered`, the citations hold chunk 1's id, and the concatenated sink tokens equal the reply text.
  - `test_keyword_escalation_interrupts_graph`: "refund" gives the outcome `escalated`; the graph state has a pending interrupt (`graph.aget_state(config).tasks[0].interrupts`); the conversation status is `pending_human`, and `escalation.rule_key` starts with `topic:`.
  - `test_resume_hand_back_clears_interrupt`.
  - `test_not_ready_escalates_bot_unavailable`: no chat model is configured.
  - `test_provider_error_offers_human`: the scripted model raises; the outcome is `error`; the reply is `bot_error`; the decision is `offer_human`.
  - `test_low_confidence_offers_human`: the rule's `min_confidence` is 1.0.
  - `test_two_failed_answers_offer_human`: two `[NO_ANSWER] ...` replies; the second turn is `offered_human` and the marker is not in the reply.
  - `test_read_tool_called_and_result_used`: the script is a tool call `get_order{order_id:"42"}`, then text. The executor stub was called once and `tool_calls[0].status == "ok"`.
  - `test_write_tool_escalates_without_http_call`.
  - `test_tool_round_limit`: the script returns tool calls 5 times; the executor is called 4 times; the 5th generate is unbound and answers.
  - `test_history_persists_across_turns_via_checkpointer`.
  - `test_no_db_session_open_during_model_call`: the scripted model's hook asserts `engine.pool.checkedout() == 0` while the model runs.
  - `test_message_text_not_in_span_attributes`: an in-memory span exporter; no attribute value contains the message text.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** as specified. The graph shape: START → prepare → assess_risk → (handoff | retrieve → generate ⇄ tools → decide) → END. `tools` may route to handoff.
- [ ] **Step 4: Run** `uv run pytest packages/core/tests/bot -v`. Expected: PASS.

---

### Task 10: Turn runner and the widget WebSocket

**Files:**
- Create:
  - `bot/turns.py`
  - `routers/widget_ws.py`
- Modify:
  - `container.py`: builds `BotRuntime`, the compiled graph and `TurnRunner`.
  - `main.py`: lifespan starts and stops the runner, plus an idle-close periodic task with inline jobs.
  - `knowledge/jobs.py` or a new `conversations/jobs.py`: the `close_idle_conversations` job.
  - `apps/worker/src/supportworker/celery_app.py`: beat entry.
- Test:
  - `packages/core/tests/bot/test_turns.py`
  - `apps/api/tests/test_widget_ws.py`

**Interfaces:**
- Produces:
  - **`TurnRunner(runtime, graph)`:**
    - `def submit(organisation_id, conversation_id) -> None`: starts a tracked task.
    - `async def aclose(timeout=10)`: cancels outstanding tasks.
  - **One task:**
    1. `acquire_turn_lease`. If it is taken, return; the running turn picks up the new message.
    2. Find the oldest customer message with no `BotTurn` and no later bot or system reply, then run the graph for it. All customer messages newer than the last bot reply are passed together, joined with blank lines.
    3. Persist the `BotTurn` and the reply `Message`.
    4. Publish the events below.
    5. Release the lease, then loop while unanswered messages remain. Skip all of this when the conversation is not in `bot` status.
  - **Pub/sub events** on `conversation_channel(id)`:
    - `{"type": "message", "message": MessageOut}`
    - `{"type": "bot_token", "delta"}`
    - `{"type": "bot_done", "outcome", "offer_human": bool}`
    - `{"type": "handoff", "status": "pending_human"}`
    - `{"type": "status", "status"}`
  - **`WS /widget/v1/conversations/{id}/ws` protocol:**
    1. The `Origin` header must equal `settings.web_origin`, else close 4403.
    2. The first frame must arrive within 5 s and be `{"type": "auth", "token"}`, else close 4401.
    3. The token's customer must own the conversation, else close 4404.
    4. The server replies `{"type": "ready", "status"}`.
    5. Client frames:
       - `{"type": "message", "text", "client_id"}`: rate limited by conversation and org. Over the limit, the server replies `{"type": "error", "code": "rate_limited", "retry_after"}` and keeps the socket open. Otherwise the server persists the message, publishes it, and calls `submit` when the status is `bot`.
       - `{"type": "talk_to_human"}`: escalates with `rule_key="customer_request"`, `source="customer"`.
       - `{"type": "ping"}`: answered with `pong`.
    6. The server relays every pub/sub event to the socket.
  - **`close_idle_conversations(runtime)`:** a job that sets `resolved` and publishes `status`.

- [ ] **Step 1: Write failing tests.**
  - **Runner** (memory pubsub, scripted chat, inline):
    - `test_concurrent_messages_single_turn_then_followup`:
      - Setup: two customer messages are added and `submit` is called twice, while the scripted model blocks on an event. Then release it.
      - Expected: exactly one turn runs first, then a second turn, whose input includes the second message. Two `BotTurn`s; the lease is released.
    - `test_pending_human_no_turn`.
    - `test_events_published_in_order`: `message` (customer), then `bot_token`…, then `message` (bot), then `bot_done`.
  - **WS** (Starlette `TestClient` websocket on the app with memory adapters and inline jobs):
    - `test_ws_rejects_foreign_origin_4403`.
    - `test_ws_requires_auth_frame_4401`.
    - `test_ws_other_customers_conversation_4404`.
    - `test_ws_message_roundtrip_bot_reply`.
    - `test_two_sockets_receive_events_once`.
    - `test_ws_rate_limited_error_frame`.
    - `test_ws_oversize_frame_closes_1009`.
    - `test_talk_to_human_escalates`.
    - `test_idle_conversation_closed`: `last_message_at` is set 31 minutes back; run the job; the status is `resolved`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - **WebSocket handler:** runs `receive_loop` and `relay_loop` with `asyncio.TaskGroup`, and unsubscribes on disconnect.
  - **Runner:** keeps a set of tasks and logs failures with ids only.
  - **Beat:** `close-idle-conversations` every 300 s.
- [ ] **Step 4: Run the tests.** Expected: PASS.

---

### Task 11: Conversations admin API (read)

**Files:**
- Create:
  - `routers/conversations.py`
  - `schemas/conversations.py`
- Modify: `conversations/service.py` (admin queries)
- Test: `apps/api/tests/test_conversation_routes.py`

**Interfaces:**
- Produces:
  - `GET /orgs/{org}/conversations?status=&widget_id=&include_test=false&cursor=&limit=`: reuses the `pagination.py` cursor. Each item: `id`, `status`, `channel`, `customer {id, name, email, identified}`, `last_message_preview` (the first 120 characters), `escalation`, `csat_score`, `last_message_at`, `is_test`.
  - `GET /orgs/{org}/conversations/{id}`: the conversation, its messages, and its `turns`. Each turn holds `BotTurn` fields, with retrieved chunks hydrated to `{chunk_id, document_name, ordinal, score, confidence, rerank_score}`; chunks since deleted show `document_name: null`.
  - Permission: any of `queries.answer`, `analytics.view` or `bot.configure`.

- [ ] **Step 1: Write failing tests.**
  - `test_list_filters_status_and_excludes_test_by_default`.
  - `test_detail_includes_turn_trace_with_rule`.
  - `test_cross_tenant_conversation_404`.
  - `test_viewer_can_read_agent_can_read_no_perm_403`: a custom role with only `kb.manage` gets 403.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests.** Expected: PASS.

---

### Task 12: Widget loader (esbuild)

**Files:**
- Create:
  - `apps/widget/package.json` (scripts `build`, `test`; devDeps `esbuild`, `vitest`, `jsdom`)
  - `apps/widget/build.mjs`
  - `apps/widget/src/{loader.js,protocol.js}`
  - `apps/widget/tests/loader.test.js`
  - `apps/widget/demo/index.html`
- Modify:
  - `Makefile`: `build-widget`
  - `apps/web/.gitignore` or `README`: note that `public/widget/` is a build output

**Interfaces:**
- Produces:
  - `apps/web/public/widget/v1.js`: built, minified IIFE, at most 15 KB.
  - The global `window.SupportWidget`:
    - `init({key, customerToken?, launcher?})`: auto-called from the script tag's `data-key`.
    - `open()`, `close()`, `toggle()`
    - `identify(customerToken)`
    - `shutdown()`
    - `on(event, cb)`, with events `open`, `close`, `unread`
  - `protocol.js` message types, each `{source: "support-widget", type, payload}`:
    - **Parent → frame:** `init {key, customerToken, parentOrigin}`, `identify`, `open`, `close`.
    - **Frame → parent:** `ready`, `config {appearance}` (for the launcher's colour and position), `request_close`, `unread {count}`.
  - **Behaviour:**
    - The iframe `src` is `{webOrigin}/embed/chat?key=…`, where `webOrigin` is derived from the script's own `src`.
    - The parent accepts messages only when `event.origin === webOrigin` and `event.source === iframe.contentWindow`, and posts with `targetOrigin = webOrigin`.
    - The customer token is sent by `postMessage`, never in the URL.
    - With `launcher:"custom"` there is no bubble; the site calls `open()`.
    - Accessibility: the bubble is a `<button aria-label>` and the iframe has a `title`. `Esc` closes the widget while it is focused.

- [ ] **Step 1: Write failing tests** (vitest with jsdom).
  - `creates bubble and hidden iframe from data-key`.
  - `custom launcher has no bubble and open() shows iframe`.
  - `ignores messages from other origins`.
  - `ignores messages from same origin but other window`.
  - `sends init with token via postMessage to webOrigin only`.
  - `identify posts new token`.
  - `shutdown removes DOM`.
- [ ] **Step 2: Run them.** `cd apps/widget && npm test`. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - **`build.mjs`:** runs `esbuild.build({entryPoints: ["src/loader.js"], bundle: true, minify: true, format: "iife", target: "es2018", outfile: "../web/public/widget/v1.js"})` and fails when the output exceeds 15360 bytes.
  - **`demo/index.html`:** a plain page that includes the snippet with a key from `?key=` and has a custom "Help" button calling `SupportWidget.open()`. It is served by `npx serve -l 5500 apps/widget/demo` in e2e.
- [ ] **Step 4: Run** `npm test && npm run build`. Expected: PASS, and the bundle is under 15 KB.

---

### Task 13: Web — the iframe chat page and the frame policy

**Files:**
- Create:
  - `apps/web/proxy.js`
  - `apps/web/app/embed/chat/page.jsx`
  - `apps/web/app/embed/layout.jsx` (no app shell, transparent background)
  - `apps/web/lib/{widget-client.js,widget-socket.js}`
  - `apps/web/components/embed/{chat-window.jsx,message-list.jsx,composer.jsx,handoff-panel.jsx,csat-form.jsx}`
- Test:
  - `apps/web/tests/unit/widget-socket.test.js`
  - `apps/web/tests/unit/proxy.test.js`

**Interfaces:**
- Consumes: the public routes and WebSocket protocol (Tasks 6 and 10), and the postMessage protocol (Task 12).
- Produces:
  - **`proxy.js`:**
    - Matcher: `/embed/:path*`.
    - Fetches `GET {API}/widget/v1/frame-policy?key=` and caches it in memory per key for 60 s.
    - Sets `Content-Security-Policy: frame-ancestors <value>`.
    - On fetch failure, uses `frame-ancestors 'none'`.
    - Read `node_modules/next/dist/docs/01-app/01-getting-started/16-proxy.md` first.
  - **`widget-client.js`:**
    - `createWidgetClient({apiUrl, key})`, with methods:
      - `config()`
      - `startSession({parentOrigin, customerToken, preview})`
      - `listConversations()`
      - `createConversation()`
      - `messages(id, after)`
      - `end(id)`
      - `rate(id, score, comment)`
      - `contact(id, email)`
    - Keeps the token in memory and the visitor id in `localStorage` under `support_widget_visitor:{key}`, with try/catch: when storage is unavailable, the visitor id lives in memory.
  - **`widget-socket.js`:**
    - `connectConversation({apiUrl, conversationId, token, onEvent})`, returning `{send(text) -> client_id, talkToHuman(), close()}`.
    - Reconnects with backoff of 1, 2, 4 and 8 s, capped at 30 s.
    - After a reconnect it calls `messages(id, after=lastSeen)` to fill the gap, de-duplicating by message id.
    - It retries unsent messages with the same `client_id`.
  - **Page behaviour:**
    - The page waits for the parent's `init`, or, in preview, reads `?preview=1`. It then calls `config` and `startSession`, and resumes the latest `bot`/`pending_human` conversation or shows the greeting.
    - **Messages:** bot tokens stream into a live bubble. Citations render as document-name chips. The list is announced through `aria-live="polite"`.
    - **Offer:** `offer_human` shows the `talk_to_human` button.
    - **Pending human:** `pending_human` shows the `handoff-panel` with the email form.
    - **Ending:** "End chat" leads to the CSAT form when `csat_enabled`.
    - **Strings:** every string comes from `config.strings`.
    - **Theme:** `appearance.primary_color` is applied as a CSS variable.

- [ ] **Step 1: Write failing tests.**
  - `widget-socket`, with a fake WebSocket class:
    - `sends auth first`.
    - `reconnects with backoff and refetches gap`.
    - `dedupes messages by id`.
    - `resends unsent with same client_id`.
  - `proxy`: `proxy sets frame-ancestors from frame-policy` and `falls back to none on fetch error`, calling the exported `proxy` function with a mocked `fetch`.
- [ ] **Step 2: Run them.** `npm test`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `npm test && npm run build`. Expected: PASS.

---

### Task 14: Web — the Widgets, Risk rules, Tools and Conversations pages

**Files:**
- Create:
  - `app/orgs/[orgId]/widgets/page.jsx`
  - `app/orgs/[orgId]/widgets/[widgetId]/page.jsx`
  - `app/orgs/[orgId]/settings/risk/page.jsx`
  - `app/orgs/[orgId]/settings/tools/page.jsx`
  - `app/orgs/[orgId]/conversations/page.jsx`
  - `app/orgs/[orgId]/conversations/[conversationId]/page.jsx`
  - `components/widgets/{widget-form.jsx,domain-list.jsx,snippet-card.jsx,identity-secret-card.jsx,widget-preview.jsx}`
  - `components/risk/{rule-list.jsx,rule-dialog.jsx}`
  - `components/tools/{tool-list.jsx,tool-dialog.jsx,schema-editor.jsx,tool-test-panel.jsx}`
  - `components/conversations/{conversation-table.jsx,transcript.jsx,turn-trace.jsx}`
  - `lib/{widgets.js,risk.js,tools.js}` (form validation helpers)
- Modify:
  - `components/app-shell.jsx` (`NAV_ITEMS`)
  - `lib/permissions.js` (if any keys are missing)
- Test: `apps/web/tests/unit/{widgets.test.js,risk.test.js,tools.test.js}`

**Interfaces:**
- **Navigation (`NAV_ITEMS`):**
  - "Conversations": any of `queries.answer`, `analytics.view` or `bot.configure`.
  - "Widgets": `channels.manage`.
  - "Risk rules" (`/settings/risk`): `bot.configure`.
  - "Tools" (`/settings/tools`): `bot.configure`.
- **Widget detail page:**
  - General: name and enabled.
  - Appearance: colour, position, launcher, logo, greeting.
  - Allowed domains: an editable list, validated client-side with the same rules as `normalize_domain`.
  - Knowledge bases: a multi-select.
  - Snippet: copy button, plus a JS API example for the custom launcher.
  - Identity:
    - Generate or rotate the secret, shown once in a dialog with copy and an "I've saved it" confirmation.
    - An "identity required" switch.
    - A server-side signing example in Node and Python.
  - CSAT switch.
  - A live preview: an iframe of `/embed/chat?key=…&preview=1`, labelled "Test conversation (not counted)".
- **Risk rules page:**
  - Ordered list: name, kind badge, action select, enabled switch, keywords chips, up/down reorder buttons.
  - Locked rules show a lock icon and disabled controls.
  - "Add topic" dialog.
  - Low-confidence and repeated-failure params are editable.
- **Tools page:**
  - List of tools, plus a dialog with name, description, method, URL template, auth header and an `input_schema` editor (a rows UI generating the JSON).
  - A "Read-only" switch. Turning it off shows a warning: "Calls to this tool will always hand the conversation to a person."
  - A test panel shows status, latency and the truncated response.
- **Conversations:**
  - Table with status, channel, customer, preview, escalation reason and CSAT, plus status/widget filters and an "include test" toggle.
  - Detail page: the transcript, with a turn-trace panel per bot reply showing the risk decision (rule name, action, source, reason), retrieved chunks with scores, tool calls, model and tokens, latency, and the trace id (copyable).

- [ ] **Step 1: Write failing unit tests.**
  - `normalizeDomain mirrors backend cases`.
  - `validateToolName`.
  - `schemaRowsToJson and back`.
  - `ruleActionsLockedForWriteAction`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement the pages**, following the Phase 2 knowledge pages' patterns (`usePolling` where status changes, `InlineError`, shadcn components, empty and no-access states).
- [ ] **Step 4: Run** `npm test && npm run build && npm run lint`. Expected: PASS.

---

### Task 15: End-to-end — a widget on a third-party page, with a stub LLM

**Files:**
- Create:
  - `apps/web/tests/e2e/stub-llm.mjs`
  - `apps/web/tests/e2e/widget.spec.js`
- Modify:
  - `apps/web/tests/e2e/helpers.js`
  - `Makefile` (`e2e` notes)
  - `README` run notes

**Interfaces:**
- Consumes: everything above.
- **`stub-llm.mjs`:** a Node HTTP server on `:5555` implementing:
  - `GET /v1/models`.
  - `POST /v1/chat/completions`, both streaming (SSE `chat.completion.chunk`) and non-streaming.
  - Response selection:
    - When the system message contains `SUPPORT_RISK_CLASSIFIER v1`, it returns `{"topic_ids":[],"asks_for_human":false,"frustrated":false,"reason":"none"}`.
    - Otherwise it returns `"Our return window is 30 days [1]."`.
  - It records requests so the spec can assert that it was never sent the `refund` message.

- [ ] **Step 1: Write the spec** `widget.spec.js`:
  1. **Setup:**
     - Sign up and create an org (helpers).
     - Add an `openai_compatible` credential with `base_url` `http://localhost:5555/v1` (dev allows private addresses) and select it as the chat model.
     - Create a KB with the local embedder and upload `returns.md` ("Returns are accepted within 30 days."). Wait until it is `indexed`.
  2. **Widget:** create one with allowed domain `localhost:5500` and that KB, then copy the key from the snippet card.
  3. **Answer:**
     - Open `http://localhost:5500/?key=…` and click the bubble.
     - Type "How long do I have to return an item?". The streamed bot message contains "30 days", and a citation chip "returns.md" is visible.
  4. **Escalation:** type "I want a refund". The system message `connecting_human` text appears and the email form shows. Submit `c@x.com`; the "email saved" text appears. The stub recorded no completion request containing "refund".
  5. **Dashboard:** Conversations lists the conversation as `pending_human`, with the reason "Refunds". The detail page shows the turn trace with rule "Refunds" and source "keyword".
  6. **Unlisted origin:** open the demo page served from `http://127.0.0.1:5500` (not allowed). The iframe is blocked by CSP: assert that the frame's content never shows the greeting.
- [ ] **Step 2: Run it.** Needs, in addition to the Phase 2 e2e servers:
  - `node apps/web/tests/e2e/stub-llm.mjs`
  - `npx serve -l 5500 apps/widget/demo`
  - `make build-widget`

  Run `make e2e`. Expected: before the fixes in this task, it fails at the first missing piece; after them, `widget.spec.js` and the existing specs all PASS.
- [ ] **Step 3: Fix whatever the e2e uncovers**, in the owning task's files, and re-run until three consecutive passes.
