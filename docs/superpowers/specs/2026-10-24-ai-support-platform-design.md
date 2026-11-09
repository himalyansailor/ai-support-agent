# AI Customer Support Platform — Design

Date: 2026-10-24
Status: Draft for review

Items marked **(my call)** were decided by the designer, not the owner. Everything else was decided by the owner in conversation.

---

## 1. Intent

A multi-tenant SaaS platform that lets any business add AI-first customer support to its product without building RAG pipelines, voice agents, or a support desk themselves.

A business signs up, creates an organisation, uploads its knowledge documents, configures how they are chunked and embedded, drops a widget onto its website, and optionally connects a phone number and a support mailbox. Customers then talk to a bot (chat or voice) that answers from the business's documents, assesses risk, and hands off to a human on the business's team when needed. The team works all channels from one inbox.

Because the platform is business-facing, **logs, metrics and traces are first-class features**, not an afterthought — both for the business (scoped to their organisation) and for the platform operator (across all organisations).

### Reference businesses (used to validate every feature)

1. **E-commerce store** — customers want chat, voice, and email support.
2. **SaaS company** — customers want chat and email support.

### Success looks like

- A business can go from sign-up to a working, embedded chat bot answering from its own docs without writing code beyond pasting a snippet.
- Any external dependency (vector DB, LLM, telephony, email, billing, storage, observability backend, etc.) can be swapped by writing one adapter and changing configuration — no edits to business logic.

### Anti-goals (failure even if it "works")

- Providers hard-coded into business logic (e.g. `import chromadb` inside the chat flow).
- One organisation able to see another organisation's data, logs, metrics or traces.
- Observability bolted on so that a business cannot answer "why did the bot say that?" for a given conversation.

---

## 2. Tech stack (decided by owner)

| Area | Choice |
|---|---|
| Frontend | Next.js (App Router), **JavaScript (not TypeScript)**, Tailwind CSS, shadcn/ui |
| Backend API | Python, FastAPI |
| Agent orchestration | LangGraph |
| Python tooling | uv |
| ORM / migrations | SQLAlchemy 2 (async) + Alembic |
| Relational DB (prototype) | Local Postgres |
| Vector store (prototype default) | pgvector (Chroma adapter also provided) |
| Background jobs | Celery + Redis |
| Auth | FastAPI-owned (email+password, JWT access + refresh), behind an auth interface |
| LLM providers | Anthropic (Claude), OpenAI, any OpenAI-compatible endpoint |
| Telephony | Twilio (behind an interface) |
| Billing | Stripe in test mode for the prototype (behind an interface; Paddle/Razorpay adapters must be possible) |
| Local environment | Docker Compose for all supporting services |
| Repo layout | Monorepo |
| Version control | Owner does not want git operations performed by the builder. |

### Monorepo layout (my call on exact names)

```
apps/
  web/        Next.js dashboard (business + platform-admin UI)
  api/        FastAPI app (HTTP + WebSocket)
  worker/     Celery workers (indexing, email sync, callbacks, metering)
  voice/      Voice gateway (Twilio media streams, browser WebRTC/WS audio) — may live inside api/ if simpler
packages/
  widget/     Embeddable JS widget (vanilla JS bundle, no framework dependency on host site)
  core/       Shared Python domain + ports (imported by api, worker, voice)
infra/
  docker-compose.yml, Grafana dashboards, Prometheus config, OTel collector config
docs/
```

---

## 3. Architecture principles

The owner's hard requirement: loosely coupled, extensible, every external dependency swappable.

- **Ports and adapters (hexagonal).** Domain logic depends only on interfaces ("ports"). Each external service is an adapter implementing a port.
- **Provider registry + factory.** Adapters register under a string key (`"pgvector"`, `"chroma"`, `"twilio"`, `"stripe"`). Which adapter is used is resolved from config — platform-level env/config, and where noted, per-organisation settings stored in the DB.
- **Dependency injection** at the composition root (FastAPI dependencies / a small container). No adapter is instantiated inside domain code.
- **Strategy pattern** for pluggable behaviour: chunking, risk assessment, agent assignment, retrieval/reranking.
- **Repository + Unit of Work** over SQLAlchemy so the DB can move to a hosted Postgres (or other) without touching services.
- **Domain events** (in-process event bus, optionally relayed via Redis) for cross-cutting reactions: `ConversationEscalated`, `DocumentIndexed`, `UsageRecorded`, etc. Notifications, metering and metrics subscribe to events instead of being called directly.
- **Multi-tenancy:** every tenant-owned row carries `organisation_id`; repositories enforce scoping so a query cannot omit it. Every log line, metric and span is tagged with `organisation_id`.

### Swappable components (complete list)

| Port | Prototype adapter(s) | Must be possible later | Selected by |
|---|---|---|---|
| Chat LLM | Anthropic, OpenAI, OpenAI-compatible | any | per org, org's own key (recommended model pre-selected) |
| Embeddings | OpenAI embeddings; local sentence-transformers **(my call)** | Cohere, Voyage, … | per knowledge base |
| Vector store | pgvector (default), Chroma | Milvus, Pinecone, Qdrant, Weaviate | platform config |
| Relational DB | local Postgres | Supabase, RDS, Neon | platform config (connection URL) |
| File/object storage | local disk or MinIO **(my call)** | S3, GCS, Azure Blob | platform config |
| Document parsers | PDF, DOCX, TXT/MD, HTML | web crawler, Notion, Google Drive | registry by MIME type |
| Chunking strategy | fixed-size, recursive, markdown-aware | semantic, custom | per knowledge base |
| Reranker | none; local cross-encoder **(my call)** | Cohere Rerank | per knowledge base |
| Risk assessment | rules + LLM classifier | fine-tuned classifier | per org |
| Agent assignment | round-robin | least-busy, skill-based | per org |
| Speech-to-text | local Whisper or Deepgram free tier **(my call)** | others | per org |
| Text-to-speech | Piper (local) or OpenAI TTS **(my call)** | ElevenLabs, … | per org |
| Speech-to-speech | OpenAI Realtime **(my call on first adapter)** | others | per org |
| Telephony | Twilio | Vonage, Plivo | platform config + per-org numbers |
| Email | IMAP (inbound) + SMTP (outbound) | Gmail API, Microsoft Graph, SendGrid | per org mailbox |
| Billing / paywall | Stripe (test mode), Dev (bypass) | Paddle, Razorpay | platform config |
| Job queue | Celery + Redis | cloud queues | platform config |
| Cache / pub-sub | Redis (in-memory fallback) | managed Redis | platform config |
| Real-time transport | WebSockets | managed realtime service | platform config |
| Auth | built-in JWT | Auth0, Clerk, Supabase Auth | platform config |
| Secrets (org API keys) | encrypted columns in DB | Vault, cloud KMS | platform config |
| Notifications to agents | in-app | email, Slack, webhooks | per org / per user |
| Logs sink | structured JSON → stdout + Postgres | Loki, ELK, Datadog | platform config |
| Metrics | Prometheus + Grafana | Datadog, cloud monitoring | platform config |
| Traces | OpenTelemetry → Jaeger; Langfuse (self-hosted) for LLM traces | Tempo, LangSmith, Honeycomb | platform config |
| LangGraph checkpointer | Postgres checkpointer | Redis | platform config |
| Business-system tools | generic HTTP API tool | more tool types | per org |

---

## 4. Features

### 4.1 Organisations, auth, team and roles

- A business signs up and creates an **organisation**. A user can belong to multiple organisations **(my call)**.
- **Onboarding wizard** collects: organisation details, language(s), LLM + embedding provider and API key (required), plan and limit behaviour (hard stop / overage), and optionally phone setup (platform number or own Twilio) and email mailbox.
- Team members are invited by email.
- **Roles:** defaults are Owner, Admin, Agent, Viewer. Businesses can create **custom roles** by ticking permissions. Permission list (my call on exact set): manage knowledge base, edit bot & risk settings, answer queries, take calls, manage team, view analytics & logs, manage billing & API keys, manage channels (widget/phone/email).
- Access is enforced on the API by permission, not role name.
- **Platform super-admin** role (operator's team) sees all organisations plus system-wide logs, metrics and traces **(my call)**.
- **Team management screen:** list members, invite, change role, deactivate, manage custom roles, set each agent's phone number for call transfers, view agent status.

### 4.2 Knowledge bases and the RAG pipeline (UI-driven)

Follows common practice in Intercom Fin / Zendesk AI / Chatbase.

- An organisation can have **multiple knowledge bases**. Each widget / phone line selects one or more knowledge bases to answer from.
- Per knowledge base, the business chooses:
  - **Embedding model** (provider + model). Changing it triggers a background re-embed of that knowledge base.
  - **Chunking strategy** and its parameters (e.g. chunk size, overlap).
  - **Reranker** (optional).
- **Document upload** from the UI (PDF, DOCX, TXT/MD, HTML). The business clicks **start embedding** to index.
- **Per-document view:** status (queued → processing → done / failed, with error), chunk preview showing exactly how it was split, re-index, delete.
- **Playground / test panel:** type a question, see retrieved chunks with scores and the answer the bot would give — before going live.
- Indexing runs as Celery jobs and emits logs/metrics/traces visible to the business.

Builder note: embedding dimensions differ by model, so the vector store port must namespace by knowledge base (collection/table per knowledge base or per dimension). Left to the builder how pgvector achieves this.

### 4.3 Channels

#### Chat widget
- Business copies a `<script>` snippet from the platform. It renders either as a floating bubble or opens from **any button on the business's site** via a JS API (e.g. `SupportWidget.open()`).
- Business configures colours, logo, greeting, position, and **allowed domains**; the widget only works on allowed domains **(my call)**.
- **Customer identity:** the business's site can pass the logged-in user via a **signed token** (HMAC/JWT with an org secret) so history follows the customer across devices. Otherwise the visitor is anonymous with a browser-stored visitor ID.
- If browser calls are enabled, the widget shows a **call button**.
- After a conversation ends, the widget asks for a rating (CSAT) **(my call)**.

#### Voice
- Customers reach the voice agent via:
  - a **Twilio phone number**, or
  - a **browser call** from the widget.
  The business enables either or both.
- Two voice modes, chosen per organisation (both must exist):
  1. **Chained:** STT → the same LangGraph bot used for chat → TTS. Each piece swappable.
  2. **Speech-to-speech:** a realtime model. It is given **knowledge-base search, risk check and human handoff as tools** so it follows the same rules and documents as the chat bot **(my call)**.
- **Handoff to a human:** the call is **transferred live** to an available agent. Agents can take calls **in the browser** inside the platform, or have calls ring **their own phone number** (set in their profile). The business enables either or both.
- If **no agent is available**, the voice agent tells the caller they will receive a call back when an agent is free, and a **callback ticket** is created in the inbox.
- Call transcripts (and recordings if the provider supports it — my call: store when available) are saved on the conversation.
- **Phone number ownership — both must be supported:**
  - **Platform-owned:** numbers bought on the platform's Twilio account (one Twilio subaccount per organisation **(my call)** for isolation and cost tracking); telephony cost is metered into billing.
  - **Bring your own:** the business connects its own Twilio account (credentials stored as encrypted org secrets) and selects numbers from it; the business pays Twilio directly.
  The telephony port therefore covers number listing/provisioning and webhook configuration, not just call handling. Inbound webhooks are routed to the right organisation by the called number and verified with the owning account's signature. Outbound legs for transfers to agents' phones are billed to whichever account owns the number.

#### Email
- **No bot on email.** Email goes straight to humans.
- The platform **fetches** emails from the business's email provider (IMAP for the prototype) and shows them in the inbox as conversations, threaded.
- Team replies from the platform; the reply is **sent as an email** (SMTP for the prototype) and threads correctly in the customer's mail client.

#### Unified customer record (my call)
Chat, call and email history are linked to one customer record when they match on email or phone, so agents see full history.

### 4.4 The bot (chat + chained voice), built in LangGraph

Flow, at minimum:

1. Receive message; load conversation state (Postgres checkpointer) and customer context.
2. **Risk assessment** of the query (see 4.5).
3. If not escalated: retrieve from the selected knowledge bases → optional rerank → generate answer with citations.
4. Optional **business tools** (see below).
5. Decide: answer / offer human / escalate. Escalation pauses the graph (interrupt) and hands to the inbox; a human can later resolve or hand back.

**LLM selection:** each organisation chooses its chat model and **must bring its own API key**; a recommended model is pre-selected by default. The platform is **not** an LLM API provider and never pays for an organisation's model usage. Onboarding therefore includes a step to enter and validate the LLM (and embedding) key; the bot cannot go live without one. The same applies to paid STT/TTS/realtime providers.

**Business-system tools (optional, business decides):** RAG over uploaded docs is the core. Additionally, a pluggable **tool registry** ships with one generic **"call my API"** tool (URL, auth header, description, input schema). Tools are **read-only by default**; anything that would create, change or delete goes through the risk rules and is escalated **(my call)**.

### 4.5 Risk assessment and escalation

- **Business-configurable.** Each organisation maintains a list of topics/intents, each mapped to one of: **escalate to human**, **offer human**, **bot handles**.
- **Defaults (placeholders):** refunds, subscription cancellation, and payment-related requests that create/update/delete → **escalate to human**.
- Additional default triggers, each switchable **(my call)**:
  - customer explicitly asks for a person → escalate
  - low retrieval confidence → offer human
  - detected frustration → offer human
  - bot failed to answer twice in a row → offer human
- Implementation: rules + LLM classifier behind the risk-assessment strategy port.
- Every decision records **which rule fired and why**, shown in the inbox and in the conversation trace.

### 4.6 Support queries page (unified inbox)

- One inbox for **chat, voice and email**.
- Filters: status (open / pending / resolved / closed), channel, assignee, priority, tag, knowledge base.
- Conversation view: full transcript including the bot's part, **escalation reason**, customer's past history, internal notes with @mentions, saved replies.
- **Assignment:** via the assignment strategy (round-robin default); agents can claim or reassign.
- **Live updates** over WebSockets. Agents set status (online / away / offline); status drives call transfers and the callback path.
- **Response-time targets (SLA)** per priority, with breaches flagged. SLA timers count **business hours only**; each organisation sets working hours, time zone and holidays **(my call)**.
- **No agent online / outside business hours (chat) (my call):** after escalation, the widget tells the customer no one is available, asks for their email, and creates a follow-up ticket; the team replies later by email from the inbox.
- Callback tickets from voice appear here with a "call back" action.

### 4.7 Observability (logs, metrics, traces) — mandatory

All three are tagged with `organisation_id` and exposed at two levels.

**Business-facing (scoped to own organisation, in the dashboard):**
- Dashboard: conversation volume by channel; AI resolution rate; escalation rate and reasons; first response time, resolution time, open queue size, SLA breaches; CSAT; agent performance (handled, average handle time, CSAT); top topics; **knowledge gaps** (questions the bot couldn't answer, linked to the knowledge base); usage vs plan (messages, voice minutes, tokens, LLM cost).
- Logs: audit log (who changed settings / documents / team), integration errors (email sync, Twilio, LLM key failures), indexing job logs.
- Traces: **per-conversation trace** — retrieved chunks, risk decision and reason, each model call, latencies.

**Platform-facing (super-admin):**
- Active organisations, usage, revenue (MRR).
- Error rates and p50/p95 latency per service; LLM cost per organisation; job queue depth; provider failure rates.
- Full distributed traces and system logs across all organisations (Jaeger/Grafana/Langfuse UIs plus an in-app super-admin view).

Instrumentation: OpenTelemetry across api, worker and voice, with context propagated through Celery jobs and into LangGraph nodes; Langfuse for LLM-level traces; Prometheus metrics; structured JSON logs carrying trace IDs.

### 4.8 Billing / paywall

- Wired into the platform behind a **billing port**; Stripe (test mode) adapter for the prototype; Paddle and Razorpay must be addable as adapters.
- **Plans** defined by the platform super-admin with limits: seats, chat messages, voice minutes, knowledge-base storage, number of knowledge bases / phone lines.
- **Usage metering always runs**, in every mode.
- **Entitlement checks happen in one place.** In **dev mode** a Dev billing adapter grants all entitlements and never requires payment.
- **Limit behaviour is chosen by the business during onboarding** (changeable later):
  - **Hard stop** — when a limit is reached, the affected feature stops. My call on specifics: bot pauses and new chats route straight to the human inbox; new voice calls go to the callback path; uploads/indexing blocked. The business is warned at 80% and 100%.
  - **Overage charges** — usage past the limit continues and is billed. The billing port therefore supports **both subscriptions and metered usage records**. Each billing adapter declares whether it supports metered billing; if the active provider does not, the overage option is hidden in onboarding **(my call)**.
- Since the platform does not pay for LLM usage (4.4), plan limits are about platform resources (seats, messages, voice minutes, storage, KBs, phone lines), not tokens. Token/cost figures are still metered and shown for the business's information.

---

## 5. Non-functional requirements

- **Tested locally, built for hosting.** Everything is tested locally, but the goal is a hosted multi-tenant SaaS, so it is built from that point of view:
  - Free alternatives for local runs; everything runs locally via Docker Compose (Postgres + pgvector, Redis, MinIO, Jaeger, Prometheus, Grafana, Langfuse, optionally Chroma).
  - All services containerised and configured purely by environment (12-factor), so they can move to any container host. The hosting target itself is not chosen yet **(my call: stay host-agnostic)**.
  - Public endpoints (Twilio webhooks/media streams, widget API) reached locally via a tunnel such as ngrok; code assumes HTTPS/WSS in hosted mode.
  - Browser calls must work behind NAT in hosted mode (TURN, or the telephony provider's client SDK — e.g. Twilio Voice JS SDK, which handles this) **(my call: use the provider's browser SDK behind the telephony port)**.
  - Data retention settings per organisation for transcripts and call recordings **(my call)**.
  - Health/readiness endpoints, graceful shutdown, DB migrations run as a separate step.
- **Languages.** English only at launch, but language is a first-class setting: the business selects its language(s) during onboarding; the language is passed through to the bot prompts, STT, TTS, realtime voice and embedding choice. Each adapter declares which languages it supports, and the UI only offers compatible models/voices for the selected language. No hard-coded English strings in bot prompts or widget UI (widget text is localisable) **(my call on mechanism)**.
- **Tenant isolation** enforced in the repository layer and in observability queries.
- **Secrets:** org-provided API keys are encrypted at rest and never returned to the frontend after saving.
- **Widget security:** signed customer tokens; allowed-domain checks; rate limiting on public endpoints **(my call)**.

---

## 6. Suggested build order (my call)

1. Organisations, auth, roles, team management
2. Knowledge bases + RAG pipeline + playground
3. Chat widget + LangGraph bot + risk assessment
4. Unified inbox + real-time + assignment
5. Observability (built incrementally from phase 1, completed and surfaced here)
6. Email channel
7. Voice channel (chained, then speech-to-speech; Twilio + browser; transfers + callbacks)
8. Billing / paywall

Each phase should leave the system runnable end-to-end.

---

## 7. Left to the builder

- Exact DB schema, API routes and module names.
- How pgvector namespaces collections of different embedding dimensions.
- Exact LangGraph graph shape beyond the flow in 4.4.
- Exact default chunk sizes, thresholds and SLA times.
- Frontend page structure and visual design within Next.js + Tailwind + shadcn/ui.
- Whether `voice/` is a separate service or part of `api/`.
- Test strategy and coverage.
