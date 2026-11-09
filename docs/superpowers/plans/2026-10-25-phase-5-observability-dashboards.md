# Phase 5 — Observability Surfaces: Dashboards, Logs, Langfuse — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Plan 5 of 8.** Builds on Phases 1–4 (`docs/superpowers/plans/2026-10-25-phase-4-inbox-realtime-assignment.md`). Next: (6) email channel.

**Goal:** Businesses and the platform team can see how support is going.

- **Business dashboard.** A business sees, for a chosen date range:
  - conversation volume by channel
  - AI resolution rate
  - escalation rate and reasons
  - first-response and resolution time
  - open queue size
  - SLA breaches
  - CSAT
  - per-agent performance
  - top topics
  - knowledge gaps (questions the bot could not answer, linked to the knowledge base)
  - usage: messages, tokens and LLM cost
- **Business logs.** A business sees its integration errors next to the existing audit and indexing logs.
- **Platform admin view.** The platform super-admin gets an in-app view with:
  - active organisations
  - usage and LLM cost per organisation
  - API error rate and p50/p95 latency
  - job queue depth
  - provider failure rates
  - links into Grafana (metrics and Loki logs), Jaeger (traces) and Langfuse (LLM traces), filtered by organisation or trace.
- **LLM traces.** Every LLM call (bot answer, risk classifier, playground) is traced in a self-hosted Langfuse, tagged with organisation, conversation and turn.

**Architecture:**

- **Analytics.** Services write an append-only `conversation_events` fact log in the same transaction as the change.
  - A Celery-beat job rolls finished hours into small rollup tables per organisation.
  - Dashboard queries read the rollups and compute the current hour live, with the same aggregation code.
- **New ports**, registered in `wiring.py`:
  - `LlmTracer` (none, langfuse)
  - `MetricsQuery` (prometheus), which the admin API uses to read Prometheus
- **Logs.** Logs go to stdout and, optionally, a JSON file. Grafana Alloy ships them to Loki, and Grafana reads Loki, Prometheus and Jaeger.
- **Optional services.** Langfuse v3 (web, worker and ClickHouse) runs under an opt-in compose profile and reuses our Postgres, Redis and MinIO.

**Tech Stack (owner-decided):**

- Langfuse self-hosted v3, with the Langfuse Python SDK and its LangChain callback.
- Recharts through shadcn chart components.
- Grafana Loki plus Grafana Alloy.
- Hourly rollups plus a live current hour.
- The existing Prometheus, Grafana and Jaeger.

**Spec:** `docs/superpowers/specs/2026-10-24-ai-support-platform-design.md`:

- §3: logs sink, metrics, traces
- §4.7: observability, business-facing and platform-facing
- §4.8: usage metering always runs; plans come in Phase 8
- §5

## Global Constraints

- **Still in force:** all Phase 1–4 global constraints (`.superpowers/sdd/2026-10-25-phase-4-inbox-realtime-assignment/global-constraints.md`):
  - **No git operations.**
  - Cross-tenant access returns 404.
  - Queries go through `TenantScopedRepository`.
  - Routes use `SessionDep`.
  - `DOMAIN_ERRORS` codes.
  - The adapter layering rule.
  - Secrets are encrypted.
  - Message text, emails, names, search queries and secrets are never put in logs, spans or metrics.
  - No DB session is held across provider calls.
  - Publishing happens after commit only.
- **Permissions:**
  - The business dashboard, analytics routes and the integration-error log need `analytics.view`.
  - The knowledge-gaps list shows customer question text, so it needs `analytics.view` and one of `bot.configure` or `kb.manage`.
  - Every `/admin/*` route and page needs `is_platform_admin`.
  - Model prices: `is_platform_admin`.
- **Date ranges:**
  - `from`/`to` are dates in the organisation's support timezone (Phase 4 `SupportSettings.timezone`).
  - The range is at most 92 days; the default is the last 7 days including today.
  - Buckets are `hour` (ranges of 2 days or less) or `day`.
  - Day boundaries use the organisation's timezone, built from UTC hour buckets. An organisation on a :30 or :45 offset gets that offset's half hour counted in the neighbouring day. Document this in the API.
- **Metric definitions** (one module, `supportcore/analytics/definitions.py`, documented there):

  | Metric | Definition |
  |---|---|
  | Conversation started | A `conversation_created` event; test conversations (`is_test`) are always excluded. |
  | AI-resolved | The conversation was resolved without ever entering the team: a `resolved` event with `by="bot"`, or idle-closed while in `bot`. |
  | AI resolution rate | AI-resolved ÷ conversations resolved in the range. |
  | Escalation rate | Conversations with ≥1 `entered_team` event ÷ conversations started. |
  | First response time | `first_agent_response` timestamp minus the `entered_team` timestamp of the same cycle, in wall seconds. Business-hours SLA stays the Phase 4 SLA columns. |
  | Resolution time | `resolved` timestamp minus `conversation_created` timestamp, in seconds. |
  | CSAT | Average of 1–5 scores plus the count; also the share of scores ≥ 4. |
  | Agent handled | `resolved` events by that membership. |
  | Handle time | Resolved timestamp minus the agent's first `assigned` timestamp in that cycle. |
  | Knowledge gap | A bot turn with `no_answer=true` or outcome `offered_human` with `rule_key="low_confidence"`. |
  | Top topics | Risk decisions whose `rule_key` starts with `topic:`, plus classifier topics, counted by rule name. |
  | Tokens and cost | Summed from bot turns and classifier calls. Cost uses platform model prices, in integer micro-units of USD; it is null when no price is set and is never invented. |

- **Model prices:**
  - A platform table `model_prices(provider, model, input_per_million_micros, output_per_million_micros, currency="USD", updated_by, updated_at)`, unique on (provider, model).
  - No values are seeded. The super-admin enters them. A missing price shows "—".
- **Rollups:**
  - A rollup recomputes a whole hour idempotently (delete and insert, or upsert) from events.
  - The job reprocesses the last 3 closed hours every run, plus any hour marked dirty by a late event (`event.occurred_at` older than 3 hours).
  - It runs every 300 s through beat, or as an inline periodic task.
  - Backfill: `uv run python -m supportapi.cli rollup-backfill --org <id|all> --from <date> --to <date>`.
- **Integration errors:**
  - `integration_events` rows are deduplicated per (org, kind, provider, error_type, UTC hour), with a `count` and `last_seen_at`.
  - Kinds: `llm`, `embedding`, `rerank`, `tool`, `classifier`. Email and telephony come later.
  - Rows hold no message text and no secrets.
- **Langfuse:**
  - The `llm_tracer` setting is `none` (default in tests) or `langfuse`.
  - Prod refuses `langfuse` without keys.
  - Traces carry these tags and metadata: `organisation_id`, `conversation_id`, `turn` / `message_id`, `kind` (`answer` | `classifier` | `playground`), and provider/model.
  - Content capture (prompts and completions) is controlled by `langfuse_capture_content` (default `true`). Langfuse is the platform's own infrastructure.
  - API keys are never sent. Tool auth values never appear in prompts; they never did.
- **Loki:**
  - `log_file` setting (optional): when set, the JSON logs also go to a rotating file (10 MB × 5).
  - Alloy tails `.data/logs/*.log`.
  - Every log line already carries `organisation_id`, `request_id`/`job_id` and `trace_id`.

## Review Focus

1. **A business with an agent in another timezone, a DST day, or a half-hour offset.**
   - Expected: daily buckets follow the organisation's timezone. A DST day has 23 or 25 hourly buckets and still sums correctly. The half-hour offset is documented and applied consistently.
   - Tests: Task 4 `test_day_buckets_follow_org_timezone_dst`, `test_half_hour_offset_documented_bucketing`.
2. **Late or replayed events.**
   - Expected: a CSAT submitted two days after resolution lands in the hour it was submitted, and that hour is rolled up again. Running the rollup twice gives identical numbers.
   - Tests: Task 4 `test_late_event_marks_hour_dirty`, `test_rollup_idempotent`.
3. **Cross-tenant leakage in analytics or admin data.**
   - Expected: org B's numbers never include org A's events. Business routes never return another org's knowledge gaps. Admin routes are 403 for non-admins, even owners.
   - Tests: Task 5 `test_analytics_org_isolated`, Task 9 `test_admin_routes_require_platform_admin`.
4. **Langfuse down or misconfigured.**
   - Expected: a bot turn still answers, tracing failures are logged once with ids only, and there are no request timeouts. With `llm_tracer=none` nothing is sent.
   - Test: Task 7 `test_tracer_failure_does_not_break_turn`.
5. **Prices not set, or a free-text model id.**
   - Expected: tokens are counted, cost is null and shows "—", and cost is never estimated. After the admin sets a price, new turns get cost. Old turns stay null; costs are not recomputed historically.
   - Tests: Task 2 `test_cost_null_without_price`, `test_price_applies_to_new_turns_only`.

---

## File Structure

```
infra/docker-compose.yml           + loki, alloy (default), clickhouse + langfuse-web + langfuse-worker (profile "langfuse")
infra/loki/loki.yml, infra/alloy/config.alloy
infra/grafana/provisioning/datasources/datasources.yml  + Loki (derived field trace_id → Jaeger)
infra/grafana/provisioning/dashboards/{dashboards.yml,platform-overview.json,bot-and-llm.json,jobs.json}
infra/postgres/init.sql            + langfuse database (idempotent)
packages/core/src/supportcore/
  analytics/{events.py,definitions.py,rollup.py,queries.py,jobs.py,integration_errors.py,pricing.py}
  db/models/analytics.py           ConversationEvent, OrgHourlyStats, AgentHourlyStats, ReasonHourlyStats, RollupDirtyHour, IntegrationEvent, ModelPrice
  llmtrace/{ports.py,none_tracer.py,langfuse_tracer.py}
  metricsquery/{ports.py,prometheus_query.py}
  observability/{logging.py (+file sink), metrics.py (+LLM requests, queue depth)}
apps/api/src/supportapi/routers/{analytics.py,integration_errors.py,admin.py (+overview, prices)}, cli.py (+rollup-backfill)
apps/api/migrations/versions/0016_analytics.py
apps/worker/src/supportworker/{celery_app.py (+rollup beat), metrics.py (+queue depth collector)}
apps/web/
  components/ui/chart.jsx (shadcn chart, Recharts)
  app/(dashboard)/orgs/[orgId]/page.jsx (dashboard), logs/{integration-errors}/page.jsx
  app/(dashboard)/admin/{page.jsx,orgs/page.jsx,prices/page.jsx}
  components/analytics/*, components/admin/*
  lib/analytics.js
  tests/e2e/analytics.spec.js
```

---

### Task 1: The analytics event log

**Files:**
- Create:
  - `db/models/analytics.py` (`ConversationEvent` only in this task)
  - `migrations/versions/0016_analytics.py` (this migration grows in Tasks 2–4; keep one file, since none of it has been applied outside dev and test)
  - `analytics/events.py`
- Modify (record calls), in the same transaction as each change:
  - `conversations/service.py`
  - `assignment/service.py`
  - `bot/turns.py`
  - `conversations/jobs.py`
- Test:
  - `packages/core/tests/analytics/test_events.py`

**Interfaces:**
- **`ConversationEvent`** (Tenant):
  - `id`
  - `conversation_id` (FK CASCADE)
  - `kind`
  - `occurred_at` (DB `clock_timestamp()`)
  - `channel`
  - `membership_id` (nullable)
  - `data` (JSONB, ids, numbers and rule keys only, never text)
  - Index `(organisation_id, occurred_at)`.
- **Kinds:**

  | Kind | `data` |
  |---|---|
  | `conversation_created` | `{widget_id, is_test}` |
  | `entered_team` | `{rule_key, rule_name, action, source}` |
  | `assigned` | `{membership_id}` |
  | `first_agent_response` | `{membership_id}` |
  | `resolved` | `{by: "bot" | "agent" | "customer" | "idle", membership_id}` |
  | `reopened` | |
  | `closed` | |
  | `handed_back` | |
  | `csat` | `{score}` |
  | `bot_turn` | `{outcome, no_answer, rule_key, rule_name, input_tokens, output_tokens, provider, model, cost_micros}` |
  | `message` | `{role}` |
  | `sla_breached` | `{kind}` (recorded by Phase 4 `check_sla` when it flags a breach) |

- **`analytics/events.py`:** `record(session, organisation_id, conversation_id, kind, *, channel, membership_id=None, data=None) -> None`. It is a plain insert, with no commit.
  - It never raises out of the caller's flow for unknown `data` keys, but validates `kind` against the list.
  - `is_test` conversations still record (filtered at query time).

- [ ] **Step 1: Write failing tests**, one per path:
  - `test_created_and_message_events`.
  - `test_escalation_records_entered_team_with_rule`.
  - `test_assignment_records_assigned`.
  - `test_first_agent_reply_records_once_per_cycle`.
  - `test_resolve_by_agent_vs_customer_vs_idle_vs_bot`.
  - `test_reopen_close_handback_events`.
  - `test_csat_event`.
  - `test_bot_turn_event_has_tokens_and_no_text`: a sentinel customer message never appears in any event row.
  - `test_events_rolled_back_with_transaction`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Recording calls go right after the state change they describe, in the service method that makes the change.
- [ ] **Step 4: Run them, plus `make test` and `alembic check`.** Expected: PASS.

---

### Task 2: Model prices and LLM cost

**Files:**
- Create: `analytics/pricing.py`
- Modify:
  - `db/models/analytics.py` (`ModelPrice`)
  - migration 0016
  - `bot/turns.py` and `bot/nodes.py` (cost on the `bot_turn` event and on `BotTurn.model`)
  - `apps/api/src/supportapi/routers/admin.py` (prices CRUD)
  - `schemas/`
- Test:
  - `packages/core/tests/analytics/test_pricing.py`
  - `apps/api/tests/test_admin_prices.py`

**Interfaces:**
- **`ModelPrice`:** not a tenant table. Columns are as in the Global Constraints.
- **`pricing.py`:**
  - `async def price_for(session, provider, model) -> ModelPrice | None`, cached per process for 60 s.
  - `def cost_micros(price, input_tokens, output_tokens) -> int | None`: integer arithmetic, rounded half-up. Returns None when there is no price or tokens are unknown.
- **Routes** (platform admin):
  - `GET /admin/model-prices`
  - `PUT /admin/model-prices` with `{provider, model, input_per_million_micros, output_per_million_micros}` (upsert; values are 0–10^12)
  - `DELETE /admin/model-prices/{id}`
  - All of them are audited as platform actions (log event with ids only).
- **Recording:**
  - The classifier's token usage goes into the turn totals.
  - `BotTurn.model` gains `cost_micros` (nullable).

- [ ] **Step 1: Write failing tests.**
  - `test_cost_micros_rounding`.
  - `test_cost_null_without_price`.
  - `test_price_applies_to_new_turns_only`.
  - `test_admin_prices_crud_and_403_for_owner`.
  - `test_price_cache_refreshes`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them, plus `make test`.** Expected: PASS.

---

### Task 3: Integration error log

**Files:**
- Create:
  - `analytics/integration_errors.py`
  - `routers/integration_errors.py` and its schema
- Modify:
  - `db/models/analytics.py` (`IntegrationEvent`)
  - migration 0016
  - Recording call sites:
    - the `bot/nodes.py` generate error and classifier unavailable
    - `knowledge/indexing.py` embedding failures
    - `knowledge/retrieval.py` `kb_unavailable` / `reranker_unavailable`
    - `bot/nodes.py` tool errors (`http_*` / `timeout` / `blocked_address` / `network_error`)
- Test:
  - `packages/core/tests/analytics/test_integration_errors.py`
  - `apps/api/tests/test_integration_error_routes.py`

**Interfaces:**
- **`IntegrationEvent`** (Tenant):
  - `id`
  - `kind`
  - `provider` (string, e.g. `anthropic`, `openai_compatible`, `local`, or a tool name)
  - `error_type` (short code)
  - `hour` (UTC hour start)
  - `count`
  - `first_seen_at`, `last_seen_at`
  - Unique on (org, kind, provider, error_type, hour).
- **`record_integration_error(session_factory, organisation_id, kind, provider, error_type)`:**
  - It runs in its own short `UnitOfWork`, using `INSERT … ON CONFLICT DO UPDATE count = count + 1, last_seen_at = now()`, so a failing turn's rollback does not lose it.
  - It never raises; failures are logged with ids only.
- **Route:** `GET /orgs/{org}/integration-errors?from&to&kind` (`analytics.view`), with cursor paging and newest `last_seen_at` first.

- [ ] **Step 1: Write failing tests.**
  - `test_dedup_per_hour_increments_count`.
  - `test_recorded_even_when_turn_rolls_back`.
  - `test_no_text_or_secret_in_rows`.
  - `test_route_permissions_and_cross_tenant_404`.
  - `test_each_call_site_records`: a parametrised fault injection per kind.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them, plus `make test`.** Expected: PASS.

---

### Task 4: Hourly rollups

**Files:**
- Create:
  - `analytics/definitions.py`
  - `analytics/rollup.py`
  - `analytics/jobs.py`
- Modify:
  - `db/models/analytics.py` (`OrgHourlyStats`, `AgentHourlyStats`, `ReasonHourlyStats`, `RollupDirtyHour`)
  - migration 0016
  - `knowledge/jobs.py` (dispatch)
  - `celery_app.py` (beat `rollup-analytics`, 300 s)
  - `main.py` (inline periodic)
  - `cli.py` (`rollup-backfill`)
- Test: `packages/core/tests/analytics/test_rollup.py`

**Interfaces:**
- **`OrgHourlyStats`** (org, hour, channel), with integer columns:
  - `conversations_started`
  - `entered_team`
  - `resolved`
  - `ai_resolved`
  - `first_response_count`, `first_response_seconds_sum`
  - `resolution_count`, `resolution_seconds_sum`
  - `csat_count`, `csat_sum`, `csat_positive`
  - `sla_breaches`
  - `messages_customer`, `messages_bot`, `messages_agent`
  - `bot_turns`, `knowledge_gaps`
  - `input_tokens`, `output_tokens`
  - `cost_micros`, plus `cost_unpriced_turns`
- **`AgentHourlyStats`** (org, hour, membership_id):
  - `assigned`
  - `handled`
  - `handle_seconds_sum`
  - `first_responses`, `first_response_seconds_sum`
  - `csat_count`, `csat_sum`
- **`ReasonHourlyStats`** (org, hour, kind ∈ {`escalation`, `topic`, `knowledge_gap_kb`}, key, label):
  - `count`
  - The label is a rule name or KB name, never customer text.
- **`RollupDirtyHour`** (org, hour): written by `events.record` when `occurred_at` is older than 3 hours.
- **`rollup.py`:**
  - `async def compute_hour(session, organisation_id, hour) -> HourStats`: pure aggregation over events in `[hour, hour + 1 h)`. The same code is used live for the current hour.
  - `async def store_hour(session, organisation_id, hour, stats)`: deletes and inserts in one transaction.
  - Time-based metrics are attributed to the hour of the closing event: first response to the hour of `first_agent_response`, and resolution to the hour of `resolved`.
- **`jobs.rollup_analytics(runtime, now)`:**
  - For every org with events in the window, roll up the last 3 closed hours plus its dirty hours, then clear the dirty marks.
  - Each org runs in its own try/except.
  - `sla_breaches` counts `sla_breached` events.

- [ ] **Step 1: Write failing tests.** Use fixed event fixtures and an injected `now`.
  - `test_compute_hour_definitions`: covers every metric in the definitions table.
  - `test_rollup_idempotent`.
  - `test_late_event_marks_hour_dirty`.
  - `test_test_conversations_excluded`.
  - `test_day_buckets_follow_org_timezone_dst`: this tests Task 5's bucketing helper, which is defined here in `definitions.py`.
  - `test_half_hour_offset_documented_bucketing`.
  - `test_one_org_failure_isolated`.
  - `test_backfill_cli`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them, plus `make test` and `alembic check`.** Expected: PASS.

---

### Task 5: Business analytics API

**Files:**
- Create:
  - `analytics/queries.py`
  - `routers/analytics.py`
  - `schemas/analytics.py`
- Test:
  - `packages/core/tests/analytics/test_queries.py`
  - `apps/api/tests/test_analytics_routes.py`

**Interfaces:**
- **Routes** (`analytics.view`):
  - `GET /orgs/{org}/analytics/overview?from&to&channel&interval`:
    - `totals`: every metric, plus derived rates and averages.
    - `series[{bucket_start, …}]`.
    - `open_queue{open, pending, unassigned, bot}`: live.
    - `timezone`.
  - `GET …/analytics/agents?from&to`: per agent `{membership_id, name, assigned, handled, avg_handle_seconds, avg_first_response_seconds, csat_avg, csat_count}`.
  - `GET …/analytics/reasons?from&to&kind=escalation|topic`: top 20 by count.
  - `GET …/analytics/usage?from&to`: messages by role, bot turns, tokens, cost (null when unpriced, plus the count of unpriced turns), by provider/model.
  - `GET …/analytics/knowledge-gaps?from&to&kb_id&cursor`: needs `analytics.view` plus `bot.configure` or `kb.manage`. Each recent gap returns `{bot_turn_id, conversation_id, question_text (the customer message, truncated to 300), kb_ids, kb_names, occurred_at, best_confidence}`. It is read live from `BotTurn` + `Message`, newest first.
- **`queries.py`:**
  - Read rollups for closed hours, plus `compute_hour` for the current hour.
  - Bucket by org timezone with the helper from Task 4.
  - Validate the range (at most 92 days), returning 422 `invalid_date_range`.

- [ ] **Step 1: Write failing tests.**
  - `test_overview_matches_hand_computed_fixture`.
  - `test_current_hour_live_plus_rollups`.
  - `test_range_validation_422`.
  - `test_agents_and_reasons`.
  - `test_usage_cost_null_when_unpriced`.
  - `test_knowledge_gaps_permission_and_truncation`.
  - `test_analytics_org_isolated`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them, plus `make test`.** Expected: PASS.

---

### Task 6: Web — business dashboard and integration-error log

**Files:**
- Create:
  - `apps/web/components/ui/chart.jsx`: the shadcn chart component, added via the shadcn CLI or copied from its registry. Use the Recharts version shadcn targets. Pin it exactly, after checking it supports React 19.3.
  - `components/analytics/{range-picker.jsx,kpi-cards.jsx,volume-chart.jsx,times-chart.jsx,csat-card.jsx,agent-table.jsx,reasons-list.jsx,knowledge-gaps.jsx,usage-card.jsx,open-queue.jsx}`
  - `lib/analytics.js` (formatting: durations, rates, micros to currency, "—" for null)
  - `app/(dashboard)/orgs/[orgId]/logs/integration-errors/page.jsx`
- Modify:
  - `app/(dashboard)/orgs/[orgId]/page.jsx`: the dashboard. Keep the AI-not-ready banner.
  - `components/app-shell.jsx`: a "Logs" group with Audit log, Integration errors and Indexing activity (link to the knowledge pages).
- Test: `apps/web/tests/unit/analytics.test.js`

**Interfaces:**
- The range picker offers Today, 7d, 30d, 90d and custom, in the org's timezone. Its state is kept in the URL search params.
- Charts:
  - Conversations by channel, stacked by day or hour.
  - AI-resolved versus escalated.
  - Average first response and resolution time.
  - CSAT trend.
- Agent table: sortable.
- Reasons: escalation reasons and top topics, as bar lists.
- Knowledge gaps:
  - A list with links to the conversation (inbox) and to the KB.
  - Shown only with the permission. Otherwise a "You need knowledge base or bot permissions to see questions" note.
- Usage card: tokens and cost; "—" plus "Set model prices (platform admin)" when unpriced.
- Accessibility: every chart has a data table toggle and an `aria-label` summary.
- Empty states: shown for no data.

- [ ] **Step 1: Write failing unit tests.**
  - `formatDuration`.
  - `formatRate`.
  - `microsToCurrency(null) === "—"`.
  - `rangeFromPreset in org timezone`.
  - `series to chart data`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run `npm test && npm run build && npm run lint`.** Expected: PASS.

---

### Task 7: Langfuse — compose services and the LLM tracer

**Files:**
- Modify:
  - `infra/docker-compose.yml`: profile `langfuse` with `clickhouse`, `langfuse-web` and `langfuse-worker`.
    - Pin exact image tags, current stable v3 at implementation time.
    - Use `LANGFUSE_INIT_*` env vars for a headless org, project and API keys from `.env`.
    - Reuse Postgres (a `langfuse` database), Redis (db 2) and MinIO (bucket `langfuse`, created by `minio-init`).
    - Web UI on port 3030.
  - `infra/postgres/init.sql`
  - `.env.example`
  - `config.py`: `llm_tracer`, `langfuse_host`, `langfuse_public_key`, `langfuse_secret_key` (SecretStr), `langfuse_capture_content`, plus prod validation.
  - `wiring.py`
  - `ai/chat_setup.py` or the call sites (bot generate, classifier, playground) to attach callbacks
  - `bot/turns.py`: store `langfuse_trace_id` in `BotTurn.model` / trace.
  - `test_layering.py`
- Create: `llmtrace/{ports.py,none_tracer.py,langfuse_tracer.py}`
- Test: `packages/core/tests/llmtrace/test_tracer.py`

**Interfaces:**
- `class LlmTracer(Protocol)`:
  - `def callbacks(self, *, organisation_id, conversation_id=None, turn_key=None, kind, provider, model) -> list`: LangChain callback handlers; an empty list for none.
  - `def trace_url(self, trace_id) -> str | None`
  - `async def aclose()`: flush.
- The langfuse adapter wraps the Langfuse SDK's LangChain `CallbackHandler`:
  - Tags and metadata as in the Global Constraints.
  - `session_id = conversation_id`, `user_id = None` (never customer identifiers).
  - SDK errors are caught: an `llm_trace_failed` log once per minute, ids only.
  - Flushing happens in the background; never on the request path.
- Read the installed SDK version's docs/source before coding.

- [ ] **Step 1: Write failing tests.** Use a fake HTTP endpoint or monkeypatched SDK; no real Langfuse in unit tests.
  - `test_none_tracer_no_callbacks`.
  - `test_langfuse_callbacks_tagged`.
  - `test_capture_content_false_strips_io`.
  - `test_tracer_failure_does_not_break_turn`: the SDK raises and the turn still answers.
  - `test_api_key_never_in_trace_payload`.
  - `test_prod_requires_keys`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them, plus `make test`.** Then run `docker compose --profile langfuse up -d` and one real playground call; the trace appears in Langfuse at :3030. Document the RAM used. Stop the profile afterwards if memory is tight; never stop other containers.

---

### Task 8: Logs to Loki, Grafana dashboards, extra metrics

**Files:**
- Create:
  - `infra/loki/loki.yml` (filesystem storage, 7-day retention)
  - `infra/alloy/config.alloy` (tails `/logs/*.log`, parses JSON, labels `service` and `level` only; `organisation_id` stays a structured field, not a label, to bound cardinality)
  - `infra/grafana/provisioning/dashboards/{dashboards.yml,platform-overview.json,bot-and-llm.json,jobs.json}`
- Modify:
  - `infra/docker-compose.yml` (`loki`, `alloy`; mount `../.data/logs`)
  - `infra/grafana/provisioning/datasources/datasources.yml` (Loki, with the `trace_id` derived field linking to Jaeger)
  - `observability/logging.py` (`log_file` rotating JSON sink)
  - `observability/metrics.py`:
    - `support_llm_requests_total{provider, kind, status}`
    - `support_job_queue_depth{queue}` gauge
    - `support_ws_connections{kind}` gauge
  - call sites
  - `apps/worker/src/supportworker/metrics.py` (queue-depth collector reading Redis `LLEN` on scrape, with a 2 s timeout)
  - `Makefile` (dev targets set `SUPPORT_LOG_FILE=.data/logs/<service>.log`)
- Test:
  - `packages/core/tests/observability/test_file_sink.py`
  - `apps/worker/tests/test_queue_depth.py`
  - `packages/core/tests/observability/test_metrics_labels.py` (bounded labels)

**Interfaces:**
- **Dashboards:**
  - **Platform overview:** request rate, error rate (5xx share), p50/p95 per route and service, active WebSockets, LLM requests by status, provider failure rate.
  - **Bot & LLM:** turns by outcome, risk decisions, tokens by provider, tool calls by status.
  - **Jobs:** indexing jobs, durations, queue depth, SLA breaches.
- All panels use Prometheus. Logs panels use Loki with `{service=~".+"} | json | organisation_id="$org"`.

- [ ] **Step 1: Write failing tests.**
  - `test_log_file_sink_writes_json_lines_with_trace_id`.
  - `test_queue_depth_collector`: fake Redis; a timeout gives no sample.
  - `test_llm_request_metric_labels_bounded`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Then `docker compose up -d loki alloy grafana`, confirm the dashboards load, and confirm a log line is searchable by `trace_id` with the Jaeger link working. Document it.
- [ ] **Step 4: Run them, plus `make test`.** Expected: PASS.

---

### Task 9: Platform admin API

**Files:**
- Create:
  - `metricsquery/{ports.py,prometheus_query.py}`
  - admin overview code in `routers/admin.py`
  - `schemas/admin.py`
- Modify: `config.py` (`prometheus_url`, `grafana_url`, `jaeger_url`, `langfuse_url`), `wiring.py`
- Test: `apps/api/tests/test_admin_overview.py`, `packages/core/tests/metricsquery/test_prometheus_query.py`

**Interfaces:**
- **`MetricsQuery` port:** `async query(promql: str) -> list[Sample]`. Bounded timeout of 3 s; errors give an empty result plus a `metrics_unavailable` flag.
- **`GET /admin/overview`** returns:
  - `orgs {total, active_7d, active_30d}` (active means having conversation events)
  - `conversations {7d, 30d}` from rollups
  - `llm {tokens_30d, cost_micros_30d, unpriced_turns_30d}`
  - `system {error_rate_5m, p50_ms, p95_ms, queue_depth, provider_failure_rate_1h by provider}` from Prometheus (null with `metrics_unavailable` on failure)
  - `links {grafana, jaeger, langfuse}`
- **`GET /admin/orgs`** gains per-org: members, conversations_30d, tokens_30d, cost_micros_30d, last_activity_at, and links (Grafana filtered by org, Langfuse filtered by the org tag).
- Platform admin only, everywhere.

- [ ] **Step 1: Write failing tests.**
  - `test_admin_routes_require_platform_admin`.
  - `test_overview_from_rollups`.
  - `test_prometheus_unavailable_flag`.
  - `test_promql_built_safely`: no user input is interpolated.
  - `test_org_links_encoded`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them, plus `make test`.** Expected: PASS.

---

### Task 10: Web — platform admin area and trace links

**Files:**
- Create:
  - `app/(dashboard)/admin/{layout.jsx,page.jsx,orgs/page.jsx,prices/page.jsx}`
  - `components/admin/*`
- Modify:
  - `components/app-shell.jsx`, or a top-level link: "Platform admin" visible only when `user.is_platform_admin`.
  - The inbox `turn-trace.jsx`: for platform admins, add "Open in Jaeger" (`trace_id`) and "Open in Langfuse" (`langfuse_trace_id`) links, built with URL encoding from the admin overview's `links`.
- Test: `apps/web/tests/unit/admin.test.js`

**Interfaces:**
- **Overview:** KPI cards, system health (or "Metrics unavailable"), and quick links.
- **Orgs table:** sortable by cost, tokens and activity; links per org.
- **Prices editor:** list, add and edit, with integer micros entered as dollars per million tokens (exact decimal to micros conversion, no floats), and delete with confirmation.

- [ ] **Step 1: Write failing unit tests.**
  - `dollarsPerMillionToMicros("3.00") === 3_000_000`, with no float error for "0.15" and similar.
  - `buildGrafanaOrgLink encodes`.
  - `admin nav hidden for non-admin`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run `npm test && npm run build && npm run lint`.** Expected: PASS.

---

### Task 11: End-to-end — numbers on the dashboard match what happened

**Files:**
- Create: `apps/web/tests/e2e/analytics.spec.js`
- Modify: `helpers.js`, plus a test-only trigger for the rollup (`POST /admin/rollup-now`, platform admin and dev/test env only) or the CLI from the spec setup.

**Steps:**
1. **Setup:** owner, a stub LLM, a KB, a widget, 24×7 hours, and the owner Online. Also a platform admin user: create it with `supportapi.cli make-platform-admin`, run from the spec's setup.
2. **Customer conversations:**
   - **A:** gets a bot answer, then the customer ends it, rating 5. It counts as AI-resolved.
   - **B:** asks "I want a refund", which escalates; the owner replies and resolves; the customer rates 4.
   - **C:** asks a question the stub answers with `[NO_ANSWER] …` (make the stub return it for a marked question), which is a knowledge gap.
3. **Rollup:** trigger it.
4. **Owner dashboard (7d):** checks:
   - conversations 3
   - AI resolution rate 50% (1 of 2 resolved)
   - escalations 1
   - the "Refunds" reason listed
   - CSAT avg 4.5 (2 ratings)
   - the knowledge gap shows C's question with a link
   - tokens > 0
   - cost "—"
5. **Prices:** the platform admin sets a price for the stub model. A new conversation, D, follows. After the rollup, cost shows non-"—" for the range.
6. **Admin pages:** `/admin` loads, with active orgs ≥ 1 and the org row with conversations_30d ≥ 3.

- [ ] **Step 1: Write the spec, then run the whole suite** with `--workers=1` (the Phase 4 setup, plus the rollup trigger).
- [ ] **Step 2: Fix what it uncovers** in the owning task's files, until three consecutive passes. Then run `make test`, apps/web `npm test && npm run build && npm run lint`, and apps/widget `npm test`.
