# Phase 4 — Unified Inbox, Real-time & Assignment — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Plan 4 of 8.** Builds on Phases 1–3 (`docs/superpowers/plans/2026-10-25-phase-3-widget-bot-risk.md`). Next: (5) observability surfaces, Langfuse.

**Goal:** The business's team works conversations from one live inbox.

- **Assignment.** Escalated chats are assigned round-robin to an available agent. An agent is available when they are online, have the dashboard open, and it is within business hours. Agents can also claim or reassign conversations.
- **Working a conversation.** Agents reply to the customer live in the widget, write internal notes with @mentions, use saved replies, tag a conversation and set its priority. They can resolve it, close it, reopen it, or hand it back to the bot.
- **Seeing everything.** Agents filter and search everything, and see the customer's other conversations.
- **Response-time targets.** Targets per priority are counted in business hours, and breaches are flagged.
- **Alerts.** Agents are alerted in the app, and optionally by a browser notification, when something is assigned to them or they are mentioned.
- **Nobody available.** When no agent is available, the widget tells the customer and asks for their email.

**Architecture:**

- **New ports in `supportcore`**, registered in `wiring.py`:
  - `AgentPresence`: redis, memory
  - `AssignmentStrategy`: round_robin
  - `ConversationSearch`: postgres
  - `BusinessCalendar`: plain functions, no port
- **Live events:** one dashboard WebSocket per agent tab (`/inbox/v1/ws`) relays three pub/sub channels:
  - the org's inbox channel, which carries conversation summary changes;
  - the agent's notification channel;
  - the channel of whichever conversation the agent has open, which already exists and carries messages and bot tokens.
- **Publishing:** services publish only after commit, through one `InboxPublisher` that uses `after_commit` + `PubSub`. The conversation status model widens to `bot / open / pending / resolved / closed`.

**Tech Stack (owner-decided):**

- Postgres full-text search, using a `simple`-config tsvector on messages plus `pg_trgm` for names and emails, behind the `ConversationSearch` port.
- A plain-text + markdown composer, rendered with a safe markdown subset and no raw HTML.
- In-app alerts plus opt-in browser `Notification` and a sound.
- The existing stack: Redis pub/sub, native WebSocket, `zoneinfo` for business hours, and shadcn.

**Spec:** `docs/superpowers/specs/2026-10-24-ai-support-platform-design.md`:

- §4.1: team management and agent status
- §4.3: unified customer record
- §4.4: hand back to bot
- §4.6: support queries page
- §4.7: business-facing metrics this phase feeds
- §5: languages

## Global Constraints

- **Still in force:** all Phase 1–3 global constraints (`.superpowers/sdd/2026-10-25-phase-3-widget-bot-risk/global-constraints.md`):
  - **No git operations.**
  - Cross-tenant access returns 404.
  - Queries go through `TenantScopedRepository`.
  - Routes use `SessionDep`.
  - `DOMAIN_ERRORS` codes.
  - The adapter layering rule.
  - Secrets are encrypted.
  - Message text, emails, names and secrets are never logged or put in spans.
  - No DB session is held across provider calls.
  - Publishing goes through after-commit only.
- **Conversation statuses:** `bot`, `open` (with the team, waiting on the team), `pending` (the team replied, waiting on the customer), `resolved`, `closed`.
  - Migration 0009 renames existing `pending_human` rows to `open`.
  - The string `pending_human` must not remain anywhere in code or tests outside migration 0006 and 0009's data step.
- **Status transitions:**

  | From | Event | To |
  |---|---|---|
  | `bot` | escalation, claim, or "talk to a person" | `open` |
  | `open` | agent reply | `pending` |
  | `pending` | customer message | `open` |
  | `open` / `pending` | resolve | `resolved` |
  | `open` / `pending` | hand back | `bot` |
  | `resolved` | reopen | `open` |
  | any non-`closed` | close | `closed` |

  `closed` is final. Every other transition gives 409 `invalid_conversation_state`.
- **Priorities:** `low`, `normal` (default), `high`, `urgent`. A risk-rule escalation sets `high`; any other entry into `open` keeps the current priority.
- **Default SLA targets**, in business minutes, as (first response, resolution):

  | Priority | First response | Resolution |
  |---|---|---|
  | `urgent` | 15 | 240 |
  | `high` | 60 | 480 |
  | `normal` | 240 | 1440 |
  | `low` | 480 | 2880 |

  Each target is an integer from 1 to 100000.
- **Default business hours:**
  - Monday–Friday 09:00–17:00, in the org's `timezone` (default `"UTC"`, any IANA name accepted by `zoneinfo`). No holidays.
  - Each weekday has 0–3 intervals of `HH:MM`–`HH:MM`, which may not overlap. 24:00 is allowed as an end.
  - There are at most 100 holidays, given as ISO dates.
- **Agent availability:** an agent is available only when all of these hold:
  - the membership is `active`;
  - its role has `queries.answer`;
  - `agent_status == "online"`;
  - it has presence, meaning a dashboard WebSocket heartbeat in the last 60 s;
  - it is within business hours.
- **Presence:**
  - TTL 60 s; the client heartbeats every 20 s.
  - `agent_status` is one of `online`, `away` or `offline`, and is set by the agent.
- **Round-robin:**
  - Candidates are the available agents, ordered by `membership.created_at`, then `id`.
  - The next agent is the first one after the org's `last_assigned_membership_id`, wrapping around.
  - The pointer is updated under a `FOR UPDATE` lock on the org's support-settings row.
- **Text limits:**
  - Agent reply: 1–10000 characters after stripping.
  - Note: 1–5000 characters.
  - Saved reply: title 1–100 and body 1–5000 characters; at most 500 per org.
  - Tag: name 1–40 characters, case-insensitively unique per org; at most 200 per org.
  - At most 10 tags per conversation.
  - Search query: 2–200 characters.
- **Mentions:**
  - In note text, a mention token is `@[<display name>](<membership uuid>)`.
  - Only memberships in the same org count. Unknown ids are left as plain text.
  - There are at most 20 mentions per note.
- **Permissions:**

  | Action | Permission |
  |---|---|
  | View the inbox | any of `queries.answer`, `analytics.view`, `bot.configure` |
  | Reply, notes, claim, assign or reassign, status, priority, tags, hand back, saved-reply use | `queries.answer` |
  | Saved replies create, edit and delete | `queries.answer` |
  | Business hours and SLA settings | `org.manage` |
  | Read business hours and SLA settings | any member |

- **Inbox WebSocket:**
  - Origin must equal `settings.web_origin`, else 4403.
  - The first frame is `{"type": "auth", "token": <dashboard access token>, "org_id"}`, sent within 5 s, else 4401.
  - The caller must have inbox-view permission, else 4403.
  - Frames are at most 16384 bytes, with a frame budget of `(120, 60)`.
  - Per-IP connects are limited to `(60, 60)`.
- **Never in logs or spans:** reply and note text, customer emails or names, and search queries.
- **Widget UI text:** new widget strings come from `supportcore/widget/strings.py`:
  - `agent_joined`, with a `{name}` placeholder
  - `no_agent_available`
  - `back_to_bot`
  - `conversation_resolved`

## Review Focus

1. **Two escalations at once, or an agent going offline mid-assignment.**
   - Expected: two simultaneous escalations go to two different available agents in round-robin order, never to the same agent twice in a row while others are free. An agent who closed the dashboard more than 60 s ago, or who is outside business hours, is never assigned.
   - Tests: Task 4 `test_concurrent_escalations_round_robin`, `test_stale_presence_not_assigned`, `test_outside_business_hours_unassigned`.
2. **The customer writes while an agent replies, or two agents reply at once.**
   - Expected: every message appears once, in order, in the widget and in every open inbox tab. Status follows the transition table, and an agent reply never re-triggers the bot.
   - Tests: Task 6 `test_customer_message_reopens_pending`, `test_agent_reply_does_not_start_bot_turn`; Task 9 `test_two_agents_see_messages_once`.
3. **Business hours across timezone, DST and holidays.**
   - Expected: SLA deadlines skip nights, weekends and holidays. They are correct across a DST change in the org's timezone (for example `Europe/London` on the last Sunday of March).
   - Tests: Task 2 `test_add_business_minutes_skips_weekend_and_holiday`, `test_dst_spring_forward`.
4. **A mention or assignment for someone who can't see the conversation, or a cross-org id.**
   - Expected: mentions of memberships from another org are plain text and notify no one. A membership without `queries.answer` cannot be assigned (422 `assignee_not_eligible`). Inbox events for org A never reach a socket for org B.
   - Tests: Task 6 `test_cross_org_mention_ignored`; Task 4 `test_assign_ineligible_422`; Task 9 `test_inbox_socket_org_isolation`.
5. **Searching for markup or SQL-ish input.**
   - Expected: `"refund' OR 1=1"`, `"<script>"` and `"%_"` search literally, with no error. Results are org-scoped, and test conversations are excluded unless asked for.
   - Tests: Task 5 `test_search_literal_special_characters`, `test_search_org_scoped`.

---

## File Structure

```
packages/core/src/supportcore/
  ai/embeddings/local_embedder.py, ai/rerank/cross_encoder_reranker.py   per-model lock (MPS crash fix)
  db/models/{support.py,inbox.py}          SupportSettings, Tag, ConversationTag, Note, Mention, SavedReply, Notification
  db/models/conversation.py                + assignee/priority/SLA columns, new statuses
  support/{calendar.py,settings_service.py,events.py}
  presence/{ports.py,redis_presence.py,memory_presence.py}
  assignment/{ports.py,round_robin.py,availability.py,service.py}
  inbox/{publisher.py,service.py,notes.py,mentions.py,tags.py,saved_replies.py,notifications.py,search_ports.py,postgres_search.py,sla.py,jobs.py,events.py,customers.py}
  widget/strings.py                        + 4 strings
apps/api/src/supportapi/
  routers/{inbox.py,inbox_ws.py,support_settings.py,saved_replies.py,tags.py,notifications.py,presence.py}
  schemas/{inbox.py,support_settings.py,saved_replies.py,tags.py,notifications.py}
apps/api/migrations/versions/0009_inbox_statuses_assignment.py, 0010_support_settings.py, 0011_search.py, 0012_inbox_collaboration.py, 0013_saved_replies.py, 0014_notifications.py
apps/worker/src/supportworker/celery_app.py   + sla-check beat entry
apps/web/
  lib/{inbox-socket.js,inbox.js,markdown.js,notify.js,business-hours.js}
  app/(dashboard)/orgs/[orgId]/inbox/page.jsx, inbox/[conversationId]/page.jsx
  app/(dashboard)/orgs/[orgId]/settings/{support,saved-replies}/page.jsx
  components/inbox/*, components/notifications/*, components/presence/*
  components/embed/*                        agent messages, no-agent prompt, back-to-bot
  tests/e2e/inbox.spec.js
```

---

### Task 1: Groundwork — the local-model crash fix, the new status model and conversation columns

**Files:**
- Modify:
  - `ai/embeddings/local_embedder.py`
  - `ai/rerank/cross_encoder_reranker.py`
  - `db/models/conversation.py`
  - `conversations/service.py`
  - `widget/session.py`
  - `bot/turns.py`
  - `bot/nodes.py` (handoff)
  - `apps/web/components/embed/chat-window.jsx`
  - `apps/web/components/conversations/conversation-table.jsx`
  - every test that references `pending_human`
- Create: `apps/api/migrations/versions/0009_inbox_statuses_assignment.py`
- Test:
  - `packages/core/tests/ai/test_local_model_lock.py`
  - the existing conversation, turns, WebSocket and widget tests (updated)

**Interfaces:**
- Produces:
  - `CONVERSATION_STATUSES = ("bot", "open", "pending", "resolved", "closed")`
  - `PRIORITIES = ("low", "normal", "high", "urgent")`
  - New `Conversation` columns:
    - `assignee_membership_id: UUID | None` (FK memberships, SET NULL; indexed)
    - `priority: str = "normal"` (CHECK)
    - `first_response_at`, `first_response_due_at`, `resolution_due_at`, `resolved_at`: `datetime | None`
    - `sla_breached: list[str]` (JSONB, a subset of `["first_response", "resolution"]`)
  - Index on `(organisation_id, status, assignee_membership_id, last_message_at)`.
  - `ConversationService.escalate` now sets `open`. A risk escalation also sets priority `high`.
  - Local models: a module-level `threading.Lock` per loaded model, held around every `encode` / `predict` call.

- [ ] **Step 1: Write failing tests.**
  - `test_local_model_lock.py::test_concurrent_encode_is_serialised`:
    - Setup: patch the model getter with a stub whose `encode` records overlap using a counter and a sleep.
    - Expected: two concurrent `embed_documents` calls never overlap. The same holds for the cross-encoder's `predict`.
  - `test_service.py::test_escalate_sets_open_and_high_priority`.
  - `test_migration_renames_pending_human`: run migration 0009 upgrade on a row inserted at revision 0008 with status `pending_human`; it becomes `open`. Use alembic's `command` API against the test DB, as existing migration checks do (if none exist, run it with `alembic upgrade` in a subprocess on a scratch schema).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - **Rename:** replace `pending_human` with `open` everywhere it appears (see the Global Constraints), including the widget front end and the Phase 3 conversation table.
  - **Migration 0009:**
    - Drop and re-add the status CHECK.
    - `UPDATE conversations SET status='open' WHERE status='pending_human'`.
    - Add the columns and index.
    - Downgrade maps `open` and `pending` back to `pending_human`.
- [ ] **Step 4: Run** `make test`, apps/web `npm test`, and the Phase 3 Playwright `widget.spec.js` (with `--workers=1`). Expected: PASS, with no `pending_human` left (check with `grep -rn pending_human packages apps --include=*.py --include=*.js --include=*.jsx`; only migrations 0006 and 0009 may match).

---

### Task 2: Business hours, timezone, holidays and SLA targets

**Files:**
- Create:
  - `db/models/support.py` (`SupportSettings`)
  - `migrations/versions/0010_support_settings.py`
  - `support/{calendar.py,settings_service.py,events.py}`
  - `routers/support_settings.py`
  - `schemas/support_settings.py`
- Modify: `errors.py`, `main.py`, `audit/subscribers.py`, `db/models/__init__.py`
- Test:
  - `packages/core/tests/support/test_calendar.py`
  - `packages/core/tests/support/test_settings_service.py`
  - `apps/api/tests/test_support_settings_routes.py`

**Interfaces:**
- Produces:
  - **`SupportSettings`** (Tenant, Timestamp; PK `organisation_id`):
    - `timezone: str = "UTC"`
    - `weekly_hours: dict` (JSONB `{"mon": [["09:00", "17:00"]], …, "sun": []}`)
    - `holidays: list[str]` (JSONB of ISO dates)
    - `sla_targets: dict` (JSONB `{"urgent": {"first_response": 15, "resolution": 240}, …}`)
    - `last_assigned_membership_id: UUID | None` (FK memberships, SET NULL)
  - **`support/calendar.py`** (pure functions over a `Calendar` frozen dataclass of `tz`, intervals by weekday, and a holiday set):
    - `def is_open(cal: Calendar, at: datetime) -> bool`
    - `def add_business_minutes(cal: Calendar, start: datetime, minutes: int) -> datetime`
    - `def business_minutes_between(cal: Calendar, start: datetime, end: datetime) -> int`
    - All of them take and return aware UTC datetimes.
    - With no open interval at all (every weekday empty), `add_business_minutes` raises `NoBusinessHoursError`. The validator also refuses settings like that: 422 `invalid_business_hours`.
  - **`SupportSettingsService(session, organisation_id, events)`:**
    - `get()`: creates the defaults lazily, with ON CONFLICT DO NOTHING.
    - `update(timezone, weekly_hours, holidays, sla_targets, actor)`: validates against the Global Constraints, raising `InvalidBusinessHoursError` (422 `invalid_business_hours`) or `InvalidSlaTargetsError` (422 `invalid_sla_targets`). It publishes the audited `SupportSettingsUpdated` with a diff.
    - `calendar() -> Calendar`
  - **Routes:**
    - `GET /orgs/{org}/support-settings`: any member.
    - `PUT /orgs/{org}/support-settings`: `org.manage`.

- [ ] **Step 1: Write failing tests.**
  - `test_is_open_respects_intervals_and_timezone`: `Asia/Kolkata` 09:00–17:00 Mon; 03:29Z Monday is closed and 03:31Z is open.
  - `test_add_business_minutes_skips_weekend_and_holiday`: a Friday 16:30 start plus 60 minutes, with Monday a holiday, ends Tuesday 09:30 local.
  - `test_add_business_minutes_within_interval`.
  - `test_dst_spring_forward`: `Europe/London` 2027-03-28 (a Sunday) is closed. The next Monday 09:00 local is 08:00Z after the change, and 30 minutes from Friday 16:45 local lands at Monday 09:15 local.
  - `test_multiple_intervals_lunch_break`: 09:00–12:00 and 13:00–17:00; 11:50 plus 20 minutes gives 13:10.
  - `test_business_minutes_between_is_inverse_of_add`, property-style, over 50 seeded random starts and minutes.
  - `test_all_closed_raises`.
  - Service:
    - `test_defaults_created_once`.
    - `test_update_validates_overlap_and_format`.
    - `test_unknown_timezone_422`.
  - Routes:
    - `test_support_settings_requires_org_manage_to_write`.
    - `test_any_member_can_read`.
    - `test_cross_tenant_404`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - Walk interval by interval in local time using `zoneinfo`.
  - Turn local wall times into UTC with `fold=0`. A local time that does not exist (the spring-forward gap) is moved to the first valid instant after it.
  - Cap any loop at 3660 iterated days, and raise beyond that.
- [ ] **Step 4: Run the tests and `alembic check`.** Expected: PASS.

---

### Task 3: Agent presence and availability

**Files:**
- Create:
  - `presence/{ports.py,redis_presence.py,memory_presence.py}`
  - `assignment/availability.py`
  - `routers/presence.py`
- Modify:
  - `wiring.py`
  - `config.py`: `presence: Literal["redis", "memory"] = "redis"`; memory is refused in prod, like the other Phase 3 adapters.
  - `team/service.py`: `agent_status` validation, already present in Phase 1; reuse it.
  - `test_layering.py`
- Test:
  - `packages/core/tests/presence/test_presence.py`
  - `packages/core/tests/assignment/test_availability.py`
  - `apps/api/tests/test_presence_routes.py`

**Interfaces:**
- Produces:
  - `class AgentPresence(Protocol)`, with methods:
    - `async beat(organisation_id, membership_id, ttl=60)`
    - `async clear(organisation_id, membership_id)`
    - `async present(organisation_id) -> set[UUID]`
  - The Redis adapter uses a per-org sorted set, scored by expiry milliseconds: prune, then read.
  - `async def available_agents(session, organisation_id, presence, now) -> list[Membership]`: implements the Global Constraints availability rule, using `SupportSettingsService.calendar()`. Ordering is `created_at`, then `id`.
  - Routes:
    - `PUT /orgs/{org}/me/agent-status {status}`: needs `queries.answer`; publishes a `presence` inbox event.
    - `GET /orgs/{org}/agents`: needs `queries.answer` or `team.manage`. Returns `[{membership_id, name, agent_status, present, available}]`.

- [ ] **Step 1: Write failing tests.** Parametrise over both adapters; Redis is `integration`.
  - `test_beat_then_present`.
  - `test_expired_not_present`: injected clock.
  - `test_clear`.
  - Availability:
    - `test_offline_status_not_available`.
    - `test_no_queries_answer_not_available`.
    - `test_deactivated_member_not_available`.
    - `test_outside_hours_not_available`.
    - `test_ordering_stable`.
  - Routes:
    - `test_set_own_status`.
    - `test_invalid_status_422`.
    - `test_agents_list_shows_presence`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests.** Expected: PASS.

---

### Task 4: Assignment — round-robin, claim and reassign, SLA due dates, and no agent available

**Files:**
- Create:
  - `assignment/{ports.py,round_robin.py,service.py}`
  - `inbox/publisher.py`
  - `inbox/sla.py` (due-date computation only)
  - `inbox/events.py`
- Modify:
  - `conversations/service.py`
  - `bot/turns.py`: `request_human`, and the handoff path calls the assignment service.
  - `bot/nodes.py`
  - `widget/strings.py`
  - `wiring.py`
  - `errors.py`
- Test:
  - `packages/core/tests/assignment/test_assignment.py`
  - `apps/api/tests/test_assignment_routes.py`

**Interfaces:**
- Produces:
  - `class AssignmentStrategy(Protocol)`: `async def pick(self, candidates: list[Membership], last_assigned: UUID | None) -> Membership | None`. Register `round_robin` in `assignment_registry` in wiring.
  - `inbox/publisher.py`:
    - `InboxPublisher(pubsub)`
    - `def conversation_changed(session, conversation_id)`: registers an `after_commit` callback that publishes `{"type": "conversation.upsert", "conversation": <summary>}` on `inbox_channel(org_id) = "inbox:" + hex`. The summary is loaded in a fresh short session, with the shape from Task 5.
    - `def notify(session, membership_id, payload)`: publishes on `user_channel(membership_id) = "notify:" + hex`.
  - `AssignmentService(session, organisation_id, events, adapters, settings)`:
    - `async def route_to_team(conversation, decision) -> AssignmentResult(assignee: Membership | None, reason: str)`
      1. Set `open`, and priority `high` for a risk escalation.
      2. Lock the support settings row.
      3. Pick an agent, set the pointer, and set the assignee.
      4. Compute `first_response_due_at` and `resolution_due_at` with `add_business_minutes` from now. When the org has no hours, the due dates are null.
      5. Add a `system` message: `agent_joined` with the agent's display name, or `no_agent_available`.
      6. Publish the conversation change and an assignment notification (Task 8 consumes it). Publish the widget `handoff` event with `{"status": "open", "agent": {"name"} | null}`.
    - `async def claim(conversation_id, actor_membership)`: sets the actor as assignee. A `bot` conversation becomes `open`; the bot stops because the runner only acts on `bot`.
    - `async def assign(conversation_id, membership_id | None, actor)`: the target must be active with `queries.answer`, else `AssigneeNotEligibleError` (422 `assignee_not_eligible`). `None` unassigns.
  - Errors: `assignee_not_eligible`.
  - Routes (`queries.answer`):
    - `POST /orgs/{org}/inbox/conversations/{id}/claim`
    - `PUT /orgs/{org}/inbox/conversations/{id}/assignee` with body `{membership_id | null}`
  - Widget strings: `agent_joined` ("{name} has joined the conversation"), `no_agent_available` ("Our team isn't available right now. Leave your email and we'll get back to you."), `back_to_bot`, `conversation_resolved`.

- [ ] **Step 1: Write failing tests.** Use memory presence and the real Postgres. Business hours are 24×7 unless the test says otherwise.
  - `test_escalation_assigns_available_agent_and_sets_due_dates`.
  - `test_round_robin_cycles`: agents A, B and C, with four escalations, are assigned A, B, C, A.
  - `test_concurrent_escalations_round_robin`: two escalations in separate sessions committing concurrently go to A and B.
  - `test_stale_presence_not_assigned`.
  - `test_outside_business_hours_unassigned`: no assignee, a `no_agent_available` system message, and the widget event has `agent: null`.
  - `test_claim_bot_conversation_stops_bot`: after a claim, a new customer message starts no turn.
  - `test_assign_ineligible_422`.
  - `test_reassign_publishes_upsert_once`: memory pub/sub records exactly one upsert after commit and none on rollback.
  - `test_talk_to_human_routes_to_team`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** The Phase 3 graph handoff node and `request_human` both call `route_to_team`, so it is the single entry point.
- [ ] **Step 4: Run** the tests plus `packages/core/tests/bot` and `apps/api/tests/test_widget_ws.py`. Expected: PASS.

---

### Task 5: Inbox queries and search

**Files:**
- Create:
  - `inbox/{service.py,search_ports.py,postgres_search.py,customers.py}`
  - `migrations/versions/0011_search.py` (also creates the `tags` and `conversation_tags` tables)
  - `db/models/inbox.py` with `Tag` and `ConversationTag` (Task 6 adds the rest)
  - `routers/inbox.py` (read routes)
  - `schemas/inbox.py`
- Modify: `wiring.py`, `test_layering.py`, `infra/postgres/init.sql` (pg_trgm, idempotent)
- Test:
  - `packages/core/tests/inbox/test_inbox_queries.py`
  - `packages/core/tests/inbox/test_search.py`
  - `apps/api/tests/test_inbox_routes.py`

**Interfaces:**
- Produces:
  - **`InboxSummary`** (frozen dataclass, also the `conversation.upsert` payload):
    - `id`, `status`, `channel`, `priority`
    - `assignee {membership_id, name} | None`
    - `customer {id, name, email, identified}`
    - `last_message_preview` (120 characters), `last_message_role`, `last_message_at`
    - `escalation`
    - `tags [{id, name}]`
    - `sla {first_response_due_at, resolution_due_at, breached: [..]}`
    - `unread_for_assignee: bool`: the last message is from the customer and newer than the assignee's last reply.
    - `is_test`
  - **`InboxService(session, organisation_id, adapters)`:**
    - `list(view, *, status, channel, assignee, priority, tag_id, kb_id, include_test, q, cursor, limit=25) -> Page[InboxSummary]`:
      - `view` is one of `mine`, `unassigned`, `all`, `bot`, `mentions` (the last needs Task 6's mentions; return an empty list until that table exists, and Task 6 wires it).
      - `assignee` is one of `me`, `none`, or a membership uuid.
      - `kb_id` filters through the conversation's widget's KBs.
    - `counts(viewer_membership_id) -> dict[view, int]`, for `mine`, `unassigned` and `bot`, counting `open` + `pending` (`bot` counts `bot`).
    - `summary(conversation_id) -> InboxSummary`.
    - `detail(conversation_id)`: the summary plus messages (the newest 200, with `has_more`), notes (Task 6), BotTurn traces (reuse `admin_detail`) and customer history.
    - `messages_before(conversation_id, before: datetime, limit=100)`: older pages.
  - **`inbox/customers.py`:** `linked_conversations(session, org_id, customer) -> list[InboxSummary]` returns other conversations whose customer is the same row, or whose customer `email` or `contact_email` matches case-insensitively. It returns at most 20, newest first.
  - **Search:**
    - `class ConversationSearch(Protocol)`: `async def search(self, session, organisation_id, query, *, include_test, limit, cursor) -> Page[UUID]`.
    - The postgres adapter uses:
      - a generated `messages.search_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED`, with a GIN index;
      - a trigram GIN index on `lower(customers.name)` and `lower(customers.email)`.
    - The query is passed through `websearch_to_tsquery('simple', :q)` (never string-built), OR a trigram `%` match on name or email.
    - Results are ordered by `last_message_at desc`.
    - The query text is never logged.
  - **Read routes** (inbox-view permission):
    - `GET /orgs/{org}/inbox/conversations`
    - `GET /orgs/{org}/inbox/counts`
    - `GET /orgs/{org}/inbox/conversations/{id}`
    - `GET /orgs/{org}/inbox/conversations/{id}/messages?before=`

- [ ] **Step 1: Write failing tests.**
  - `test_views_mine_unassigned_bot`.
  - `test_filters_priority_tag_kb_channel`: tag rows are inserted directly; Task 6 adds the tag service.
  - `test_counts_match_lists`.
  - `test_detail_includes_history_linked_by_email`.
  - `test_unread_for_assignee_flag`.
  - `test_search_message_text_and_customer_email`.
  - `test_search_literal_special_characters`: the inputs `refund' OR 1=1`, `<script>` and `%_` give no error, and their results match the literal text only.
  - `test_search_org_scoped`.
  - `test_search_excludes_test_by_default`.
  - `test_search_query_not_logged`.
  - Routes:
    - `test_inbox_view_permission`: Agent, Viewer and bot.configure get 200; a role with only `kb.manage` gets 403.
    - `test_cross_tenant_404`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** The migration creates the `pg_trgm` extension (`CREATE EXTENSION IF NOT EXISTS pg_trgm`), the generated column and the indexes. Exclude nothing new from `alembic check` (generated columns are modelled with `Computed`).
- [ ] **Step 4: Run the tests and `alembic check`.** Expected: PASS.

---

### Task 6: Agent actions — reply, notes and mentions, status, priority, tags, hand back

**Files:**
- Create:
  - `Note` and `Mention` in `db/models/inbox.py` (`Tag` and `ConversationTag` come from Task 5)
  - `migrations/versions/0012_inbox_collaboration.py`
  - `inbox/{notes.py,mentions.py,tags.py}`
  - action routes in `routers/inbox.py`
  - `routers/tags.py`
  - `schemas/tags.py`
- Modify:
  - `inbox/service.py` (mentions view, notes in detail)
  - `conversations/service.py` (agent message, transitions)
  - `bot/turns.py` (`post_customer_message` from `pending` reopens to `open` and starts no turn)
  - `bot/graph.py` (hand back uses `resume_conversation` when waiting)
  - `errors.py`
- Test:
  - `packages/core/tests/inbox/test_actions.py`
  - `packages/core/tests/inbox/test_mentions.py`
  - `apps/api/tests/test_inbox_action_routes.py`

**Interfaces:**
- Produces:
  - **`ConversationService.add_agent_message(conversation, membership, text) -> Message`:**
    - Role `agent`, plus a new nullable `Message.author_membership_id` (FK SET NULL, added in 0012).
    - Sets `first_response_at` when it is null, moves `open` to `pending`, updates `last_message_at`, and publishes the message on the conversation channel and an inbox upsert.
    - Only allowed in `open` or `pending`: 409 otherwise.
  - **`transition(conversation, to, actor)`:** implements the Global Constraints table.
    - Resolve sets `resolved_at` and publishes `status` to the widget, which shows `conversation_resolved` and then CSAT.
    - Hand back: when the graph is waiting it calls `resume_conversation(graph, id, {"action": "hand_back"})`. It clears the assignee, sets `bot`, and posts a system message with `back_to_bot`.
  - **Notes:**
    - `Note(id, conversation_id, author_membership_id, text, created_at)`.
    - `parse_mentions(text, org_member_ids) -> list[UUID]`: uses the token format from the Global Constraints; deduplicated and capped at 20.
    - A `Mention` row is stored per valid id.
    - Notes are never sent to the widget channel.
  - **Tags:**
    - `Tag(id, name)`: unique on `lower(name)` per org.
    - `ConversationTag(conversation_id, tag_id)`.
    - `TagService`: list, create, rename, delete, and `set_tags(conversation_id, tag_ids)` (at most 10).
  - **Routes** (`queries.answer` unless noted):
    - `POST .../conversations/{id}/messages {text}`
    - `POST .../conversations/{id}/notes {text}`
    - `POST .../conversations/{id}/status {status}` (resolved, closed, open = reopen)
    - `POST .../conversations/{id}/hand-back`
    - `PUT .../conversations/{id}/priority {priority}`
    - `PUT .../conversations/{id}/tags {tag_ids}`
    - `GET`/`POST /orgs/{org}/tags`, `PATCH`/`DELETE /orgs/{org}/tags/{id}`
  - **Errors:**
    - 422: `invalid_tag`, `too_many_tags`, `invalid_priority`
    - 409: `tag_name_taken`, `invalid_conversation_state` (existing)

- [ ] **Step 1: Write failing tests.**
  - `test_agent_reply_sets_first_response_and_pending`.
  - `test_customer_message_reopens_pending`.
  - `test_agent_reply_does_not_start_bot_turn`.
  - `test_transitions_table`: parametrised over every pair; the allowed pairs succeed and the rest give 409.
  - `test_resolve_publishes_status_to_widget`.
  - `test_hand_back_resumes_interrupted_graph_and_bot_answers_next`.
  - `test_hand_back_without_interrupt`.
  - `test_note_not_visible_to_widget`.
  - `test_mentions_parsed_and_capped`.
  - `test_cross_org_mention_ignored`.
  - `test_tags_unique_case_insensitive_and_max_10`.
  - `test_reply_text_not_logged`.
  - Routes:
    - `test_viewer_cannot_reply_403`.
    - `test_cross_tenant_404_on_all_actions`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests and `alembic check`.** Expected: PASS.

---

### Task 7: Saved replies

**Files:**
- Create:
  - `inbox/saved_replies.py`
  - the `SavedReply` model in `db/models/inbox.py`
  - `migrations/versions/0013_saved_replies.py`
  - `routers/saved_replies.py`
  - `schemas/saved_replies.py`
- Test:
  - `packages/core/tests/inbox/test_saved_replies.py`
  - `apps/api/tests/test_saved_reply_routes.py`

**Interfaces:**
- Produces:
  - `SavedReply(id, title, body, shortcut: str | None, created_by_membership_id)`. `shortcut` matches `^[a-z0-9_-]{1,30}$` and is unique per org.
  - `render_saved_reply(body, *, customer_name, agent_name) -> str`:
    - Replaces `{customer_name}` and `{agent_name}`.
    - Missing values become an empty string.
    - Any other `{…}` is left as is.
  - Routes (`queries.answer`): CRUD on `/orgs/{org}/saved-replies`, with list search `?q=` over title and shortcut. The org limit is 500 (422 `too_many_saved_replies`).

- [ ] **Step 1: Write failing tests.**
  - `test_render_placeholders`.
  - `test_unknown_placeholder_kept`.
  - `test_shortcut_unique_and_format`.
  - `test_limit_500`.
  - `test_routes_permissions_and_cross_tenant`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests.** Expected: PASS.

---

### Task 8: Notifications

**Files:**
- Create:
  - `inbox/notifications.py`
  - the `Notification` model in `db/models/inbox.py`
  - `migrations/versions/0014_notifications.py`
  - `routers/notifications.py`
  - `schemas/notifications.py`
- Modify: `assignment/service.py`, `inbox/notes.py` (emit), `inbox/publisher.py`
- Test:
  - `packages/core/tests/inbox/test_notifications.py`
  - `apps/api/tests/test_notification_routes.py`

**Interfaces:**
- Produces:
  - `Notification(id, membership_id, kind: "assigned" | "mentioned" | "sla_breached", conversation_id, actor_membership_id | None, read_at | None, created_at)`.
  - `NotificationService.create(...)` stores the row and calls `InboxPublisher.notify(...)` with `{"type": "notification", "notification": {...}}`. The payload carries ids, kind, the actor name and the customer display name. It never carries note or message text.
  - No self-notifications: an agent who assigns themselves, or mentions themselves, gets no notification.
  - Routes, for the caller's own membership:
    - `GET /orgs/{org}/notifications?unread_only=`
    - `GET /orgs/{org}/notifications/unread-count`
    - `POST /orgs/{org}/notifications/read {ids | "all"}`

- [ ] **Step 1: Write failing tests.**
  - `test_assignment_notifies_assignee_not_self`.
  - `test_mention_notifies_each_once`.
  - `test_payload_has_no_text`.
  - `test_mark_read_only_own`: another member's ids are ignored.
  - `test_unread_count`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests.** Expected: PASS.

---

### Task 9: The inbox WebSocket

**Files:**
- Create: `routers/inbox_ws.py`
- Modify: `main.py`, `apps/api/src/supportapi/deps.py` (shared access-token decode helper)
- Test: `apps/api/tests/test_inbox_ws.py`

**Interfaces:**
- Produces: `WS /inbox/v1/ws`, following the Global Constraints, Inbox WebSocket section.
- **After auth:**
  - The server sends `{"type": "ready", "membership_id", "counts"}`.
  - It subscribes to `inbox_channel(org)` and `user_channel(membership)`.
  - It calls `presence.beat` on auth and on every `ping`.
- **Client frames:**
  - `ping` → `pong` (plus a beat).
  - `{"type": "watch", "conversation_id"}`: subscribe to that conversation's channel, replacing any previous watch. The conversation must belong to the org, else `error not_found`.
  - `{"type": "unwatch"}`.
- **Relayed events:**
  - `conversation.upsert`
  - `notification`
  - `presence`
  - for the watched conversation: `message`, `bot_token`, `bot_reset`, `bot_done`, `status`, `handoff`
- **Disconnect:** presence clears after the last socket of a membership closes. Track a per-process count; otherwise TTL expiry handles it.
- **Behaviour:** the handler holds no DB session while waiting. It relays with a plain `async for` per channel, merging channels with a TaskGroup. A failure ends the socket with close 1011.

- [ ] **Step 1: Write failing tests** (Starlette TestClient, memory adapters).
  - `test_rejects_foreign_origin_4403`.
  - `test_requires_auth_frame_4401`.
  - `test_widget_token_rejected_4401`.
  - `test_no_inbox_permission_4403`.
  - `test_ready_with_counts`.
  - `test_receives_upsert_after_assignment`.
  - `test_watch_streams_customer_message_and_bot_tokens`.
  - `test_watch_other_org_conversation_not_found`.
  - `test_inbox_socket_org_isolation`.
  - `test_two_agents_see_messages_once`.
  - `test_ping_refreshes_presence`.
  - `test_frame_budget_1008`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Reuse the Phase 3 `_Connection` patterns: the send lock, the frame budget, and the TaskGroup relay.
- [ ] **Step 4: Run the tests three times.** Expected: PASS.

---

### Task 10: SLA breach job

**Files:**
- Create: `inbox/jobs.py` (`check_sla`)
- Modify: `knowledge/jobs.py` (dispatch table), `apps/worker/src/supportworker/celery_app.py` (beat every 60 s), `main.py` (inline periodic), `inbox/sla.py`
- Test: `packages/core/tests/inbox/test_sla.py`

**Interfaces:**
- Produces: `check_sla(runtime, now)`:
  1. Use `FOR UPDATE SKIP LOCKED` to find `open`/`pending` conversations where either:
     - `first_response_at IS NULL AND first_response_due_at < now` and `first_response` is not yet in `sla_breached`; or
     - `resolution_due_at < now` and `resolution` is not yet in `sla_breached`.
  2. Append the breach kind and publish an upsert.
  3. Notify the assignee (`sla_breached`), or, when there is none, nobody.
  4. Increment the metric `support_sla_breaches_total{kind}`.

  It works per org in its own try/except, and in batches of 500.
- Due dates are recomputed:
  - on a priority change, from the time the conversation entered `open`, for targets not yet met;
  - on reopen (`resolved` → `open`), using a new resolution due date from now.

- [ ] **Step 1: Write failing tests.**
  - `test_first_response_breach_flagged_once`.
  - `test_resolution_breach`.
  - `test_met_first_response_not_breached`.
  - `test_priority_change_recomputes_due`.
  - `test_reopen_new_resolution_due`.
  - `test_one_org_failure_does_not_stop_others`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests.** Expected: PASS.

---

### Task 11: The widget side — agent messages, no agent, back to bot, resolved

**Files:**
- Modify:
  - `apps/web/components/embed/{chat-window.jsx,message-list.jsx,handoff-panel.jsx}`
  - `apps/web/lib/widget-socket.js` (no protocol change expected)
  - `apps/api/src/supportapi/schemas/widget_public.py` (`MessageOut.author {name} | null` for agent messages)
  - `bot/turns.py` (`message_payload` includes the author name)
- Test: `apps/web/tests/unit/chat-window.test.js`, `apps/api/tests/test_widget_ws.py`

**Interfaces:**
- **Agent messages:** rendered with the agent's display name above the bubble. Only the name is sent, never the email.
- **`handoff` with `agent: null`:** the widget shows `no_agent_available` and the email form. With an agent, it shows `agent_joined`.
- **`status` events:**
  - `bot`: hides the human panel.
  - `resolved`: shows `conversation_resolved`, then CSAT when it is enabled.
  - `pending` and `open`: keep the composer.
- All text comes from `config.strings`.

- [ ] **Step 1: Write failing tests.**
  - `agent message shows author name`.
  - `handoff without agent shows email prompt`.
  - `status bot returns to bot mode`.
  - `resolved shows message then csat`.
  - Backend: `test_agent_message_payload_has_author_name_only`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** apps/web `npm test && npm run build && npm run lint` and the backend WS tests. Expected: PASS.

---

### Task 12: Web — the inbox

**Files:**
- Create:
  - `apps/web/lib/{inbox-socket.js,inbox.js,markdown.js,notify.js}`
  - `apps/web/app/(dashboard)/orgs/[orgId]/inbox/page.jsx`
  - `apps/web/app/(dashboard)/orgs/[orgId]/inbox/[conversationId]/page.jsx` (or one page with a selected id; your choice, documented)
  - `apps/web/components/inbox/{view-list.jsx,conversation-list.jsx,conversation-pane.jsx,composer.jsx,note-composer.jsx,mention-picker.jsx,saved-reply-picker.jsx,tag-picker.jsx,assignee-picker.jsx,customer-panel.jsx,sla-badge.jsx}`
  - `apps/web/components/notifications/{bell.jsx,notification-list.jsx}`
  - `apps/web/components/presence/status-switcher.jsx`
- Modify: `components/app-shell.jsx` (Inbox nav with counts, bell, status switcher; the old "Conversations" nav item is removed and its route redirects to the inbox)
- Test: `apps/web/tests/unit/{inbox-socket.test.js,markdown.test.js,inbox.test.js,notify.test.js}`

**Interfaces:**
- **`inbox-socket.js`:**
  - `connectInbox({apiUrl, orgId, getToken, onEvent})` returns `{watch(id), unwatch(), close()}`.
  - It sends `auth` first, pings every 20 s, and reconnects with backoff of 1/2/4/8 s, capped at 30 s.
  - It does not reconnect on 4401, 4403 or 1008.
  - It re-sends `watch` after reconnect, and refetches the counts and the open conversation's messages to fill any gap.
- **`markdown.js`:**
  - `renderMarkdown(text) -> React nodes`.
  - Supported: paragraphs, line breaks, **bold**, *italic*, `code`, fenced code, lists, and links limited to `http`/`https`/`mailto` with `rel="noopener noreferrer"`.
  - Raw HTML is shown as text.
  - Mentions are rendered as chips.
- **`notify.js`:**
  - Asks for `Notification.requestPermission()` only after a user click on "Enable desktop alerts".
  - Shows a browser notification (title from the kind plus the customer name) and plays a short sound only when `document.hidden`. In-app toasts always show.
  - Remembers the opt-out in localStorage, with try/catch.
- **Inbox page:**
  - Left: views `mine`, `unassigned`, `all`, `bot`, `mentions`, each with a count, plus filters and search.
  - Middle: the conversation list, with live upserts merged by id and kept sorted by `last_message_at`.
  - Right: the conversation pane:
    - the transcript, with bot traces collapsible from the Phase 3 trace UI;
    - notes interleaved, styled distinctly;
    - the reply composer (a markdown textarea; Enter sends and Shift+Enter adds a newline);
    - a note tab with the @mention picker;
    - the saved-reply picker, triggered with `/` plus a shortcut;
    - status buttons following the transition table;
    - assignee, priority and tag pickers;
    - an SLA badge;
    - a customer panel with linked history.
  - Viewers (analytics.view only) see everything read-only.
- **Keyboard:** `j`/`k` move through the list, `r` replies, `n` adds a note and `e` resolves. These do nothing while typing.

- [ ] **Step 1: Write failing unit tests.**
  - inbox-socket:
    - `auth first, ping interval, rewatch after reconnect, gap refill, terminal codes`.
  - markdown:
    - `escapes html`.
    - `rejects javascript: links`.
    - `renders bold/list/code`.
    - `mentions become chips`.
  - inbox.js:
    - `mergeUpsert keeps sort and dedupes`.
    - `allowedTransitions matches backend table`.
  - notify:
    - `no permission request without user action`.
    - `background only browser notification`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement**, following the existing dashboard patterns (shadcn, `InlineError`, states, `RequirePermission`).
- [ ] **Step 4: Run** `npm test && npm run build && npm run lint`. Expected: PASS.

---

### Task 13: Web — support settings, saved replies, tags and team presence

**Files:**
- Create:
  - `app/(dashboard)/orgs/[orgId]/settings/support/page.jsx` (business hours editor, timezone select, holidays, SLA targets table)
  - `app/(dashboard)/orgs/[orgId]/settings/saved-replies/page.jsx`
  - `components/support/*`
  - `components/saved-replies/*`
  - `lib/business-hours.js` (client validation mirroring Task 2)
- Modify: `app/(dashboard)/orgs/[orgId]/team/page.jsx` (live `present`/`available` from `GET /agents` plus inbox socket `presence` events), `components/app-shell.jsx` (nav)
- Test: `apps/web/tests/unit/business-hours.test.js`

**Interfaces:**
- **Timezone select:** uses `Intl.supportedValuesOf("timeZone")`.
- **Weekly editor:** up to 3 intervals per day, with an overlap check and a "Copy Monday to weekdays" action.
- **SLA table:** 4 priorities × 2 targets, entered in minutes or hours.
- **Saved replies:** list, search, create, edit and delete, with a placeholder hint listing `{customer_name}` and `{agent_name}`.
- **Team page:** a presence dot (online and present, online but not present, away, offline) and an "available now" badge.

- [ ] **Step 1: Write failing tests.**
  - `validateWeeklyHours mirrors backend cases`: copy the Task 2 cases.
  - `slaMinutes from hours/minutes input`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `npm test && npm run build && npm run lint`. Expected: PASS.

---

### Task 14: End-to-end — escalate, assign, reply live, mention, resolve

**Files:**
- Create: `apps/web/tests/e2e/inbox.spec.js`
- Modify: `apps/web/tests/e2e/helpers.js` (second agent invite and login, reusing the onboarding spec's Mailpit flow), `Makefile` (e2e notes; `--workers=1` if the local embedder still requires it after Task 1)

**Interfaces:**
- Consumes: everything above, plus the Phase 3 stub LLM and demo page setup.

- [ ] **Step 1: Write the spec** `inbox.spec.js`:
  1. **Setup:**
     - The owner sets up the org, the AI credential (stub), a KB and a widget, as in `widget.spec.js`, and sets business hours to 24×7.
     - The owner invites agent B with the Agent role. B accepts in a second browser context.
     - The owner and B both set their status to Online and keep the inbox open.
  2. **Escalation:** a customer on the demo page types "I want a refund". In the widget, "… has joined the conversation" appears with the assignee's name.
     - The assignee is whichever is first by round-robin.
     - The assignee's inbox shows the conversation under "Mine" live, with priority High and an SLA badge, without a reload.
  3. **Live reply:** the assignee replies "Hi, I can help with that. **Order number?**". The widget shows it, with the author name and bold text. The customer answers "12345", and the assignee sees it live; the status goes from Pending back to Open.
  4. **Mention:** the assignee adds a note "@B can you check?" via the mention picker. B gets a bell badge and an in-app toast. The widget never shows the note.
  5. **Resolve:** the assignee resolves. The widget shows the resolved message, then CSAT. The customer rates 5, and the inbox shows the conversation as resolved.
  6. **Hand back:** a new conversation is escalated and claimed by B, and B hands it back to the bot. The customer asks the returns question and gets the stub's "30 days" answer.
  7. **Nobody available:** both agents set Away. A new escalation shows the `no_agent_available` text and the email form.
- [ ] **Step 2: Run it.** Needs:
  - the Phase 3 e2e servers (stub LLM, demo on 5500, API with `--ws-max-size 16384`, web);
  - `make build-widget`.

  Run the whole Playwright suite. Expected: PASS.
- [ ] **Step 3: Fix whatever the e2e uncovers** in the owning task's files. Re-run the whole suite until three consecutive passes.
