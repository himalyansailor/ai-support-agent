# Phase 6 — Email Channel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Plan 6 of 8.** Builds on Phases 1–5 (`docs/superpowers/plans/2026-10-25-phase-5-observability-dashboards.md`). Next: (7) voice channel.

**Goal:** A business connects its support mailbox, and the team works email in the same inbox as chat.

- **Connecting.** An organisation enters its IMAP and SMTP settings, tests the connection, and saves. Onboarding offers this as an optional step.
- **Receiving.** Customer email appears in the human inbox as threaded conversations. Email has no bot. The existing round-robin assignment, SLA timers and live updates apply.
- **Replying.** An agent's reply goes out as an email that threads correctly in the customer's mail client. Send failures are retried safely and logged as integration errors.
- **Reading safely.** HTML mail is sanitised and shown in a sandboxed frame. Remote images stay blocked until the agent clicks, quoted history is collapsed, and attachments download only through an authenticated route.
- **Follow-up tickets.** The after-hours widget ticket, which asks the customer for an email address, can now be answered by email. The customer's reply threads back into the same conversation.

**Architecture:**

- **New package `supportcore/mail/`.** It is not named `email`, so it never shadows the standard library. It holds:
  - two ports: `InboundMailbox` and `OutboundMailSender`;
  - pure modules for MIME parsing, sanitising, loop detection and threading;
  - the inbound ingestion and outbound reply services;
  - the sync and send jobs.
- **Adapters, wired in `wiring.py`:**
  - `imap` (imap-tools, run in `asyncio.to_thread`) and `smtp` (aiosmtplib);
  - `fake` versions of both, registered only when `env == "test"`.
- **Inbound sync.** Celery beat runs `sync_mailboxes` every 60 s. It fans out one `sync_mailbox` job per enabled mailbox. Each job:
  1. Takes a DB lease on the mailbox and commits.
  2. Fetches from IMAP with no session open.
  3. Stores attachments with no session open.
  4. Ingests each message in its own short transaction, which also advances the UID cursor.
- **Outbound.** The reply route stores the agent message plus a `queued` outbound `EmailMessage`, with its RFC Message-ID fixed at that moment. After commit it enqueues `send_email`. The job claims the row, commits, sends over SMTP with no session open, then records the outcome.

**Tech Stack (owner-decided):**

- imap-tools for inbound, run inside Celery jobs; aiosmtplib for outbound. Both sit behind ports, and fakes exist only in tests.
- Polling every 60 s, fanned out per mailbox by beat. The jobs track UIDVALIDITY and the last UID, and each run handles a bounded batch.
- GreenMail for dev and e2e IMAP/SMTP, as the opt-in compose profile `email`. The image is pinned, `mem_limit` is 256m, and the JVM heap is small.
- nh3 for HTML sanitising. Sanitised HTML is stored next to the text part and rendered in a sandboxed iframe. Remote images are blocked until clicked, quoted history is collapsed, and plain text is the fallback.
- The existing stack: Fernet `SecretCipher`, the `FileStorage` port (MinIO), `InboxPublisher`, `AssignmentService`, `analytics.events.record`, `record_integration_error`, and OpenTelemetry/Prometheus.

**Spec:** `docs/superpowers/specs/2026-10-24-ai-support-platform-design.md`:

- §3: the Email row of the swappable components (IMAP + SMTP, per-org mailbox)
- §4.1: onboarding (optional mailbox) and the `manage channels` permission
- §4.3: Email, and the unified customer record
- §4.6: the unified inbox, and the no-agent follow-up ticket answered by email
- §4.7: integration errors (email sync)
- §5: secrets encrypted and never returned
- §6: phase 6

## Assumptions I made

These are owner-level calls the spec leaves open. Each is cheap to change later.

1. **One mailbox per organisation.** One address, one IMAP folder (default `INBOX`) and one SMTP account. The schema has a `mailbox_id` everywhere, so supporting several mailboxes later is additive.
2. **Where sync starts.** The first sync imports mail whose IMAP INTERNALDATE is at or after the moment the mailbox was connected, minus 10 minutes. Older mail is never imported. After a UIDVALIDITY change, the job rescans mail since `last_sync_at` minus 1 day, and Message-ID dedup absorbs the repeats.
3. **The mailbox is read-only to us.** We fetch with `BODY.PEEK[]` and never set flags, move or delete. We do not append our replies to a Sent folder; some providers, such as Gmail, keep a copy of SMTP-sent mail themselves.
4. **Outbound mail is plain text only.** There are no outbound attachments, no CC and no reply-all. A reply goes to the latest inbound message's `Reply-To`, or its `From` when there is no `Reply-To`. For a chat follow-up, it goes to `contact_email`.
5. **When a message counts as sent.** It is sent once the SMTP server answers 250 to the end of DATA.
   - Failures before DATA starts are retried. The schedule is 30 s, 2 min, 10 min, 30 min and 2 h: 5 attempts in all.
   - Authentication and permanent (5xx) failures are not retried.
   - An *ambiguous* failure (the connection drops or times out after DATA was sent) is never retried automatically. Neither is a send interrupted by a crash, detected by an expired claim.
   - Ambiguous and interrupted sends are marked `failed`, with `error_type` set to `ambiguous` or `interrupted`. The agent can click **Retry**, which reuses the same Message-ID.
6. **Chat follow-up.** On a chat conversation with a `contact_email`, the composer shows **Also send by email**, ticked by default. This needs a connected mailbox. The customer's reply threads back into that chat conversation through In-Reply-To.
7. **Email enters the team directly.** An inbound email conversation is created and then routed with `route_to_team(..., announce=False)`. The analytics event is `entered_team`, with `source="email"` and `rule_key=None`.
   - It is not counted as an escalation *reason*.
   - The escalation rate of the email channel is 100% by definition; the dashboard's channel filter separates it.
   - No customer-facing system message is posted.
8. **Attachment limits.**
   - At most 10 MB per file, 25 MB per message, and 20 files per message.
   - Allowed types: `image/png`, `image/jpeg`, `image/gif`, `image/webp`, `application/pdf`, `text/plain`, `text/csv`, `.docx`, `.xlsx`.
   - Anything else is listed on the message as skipped, with a reason, and is not stored.
   - Inline `cid:` images are not shown inline; they appear in the attachment list.
   - A raw message over 30 MB is not downloaded. It is recorded as an `email` integration error (`message_too_large`), and the cursor moves past it.
9. **Size caps.** Stored email text is capped at 100 000 characters, with a truncation marker. Sanitised HTML is capped at 1 MB; above that only text is kept.
10. **Loop guard.** More than 20 inbound messages from one sender to one mailbox within an hour are skipped as `rate_limited`, on top of the header-based detection.
11. **Subject fallback.** It applies only when the header chain matches nothing, the normalised subject is non-empty, the customer is the same, the conversation is not closed, and its last message is less than 30 days old.
12. **Late replies.** A reply to a `resolved` conversation reopens it. A reply to a `closed` one starts a new conversation, which the customer record links to the old one.
13. **Transport security.** Each server has a security mode of `ssl`, `starttls` or `none`. `none` is refused when `env=prod`. Hosts that resolve to private or loopback addresses are refused in prod, using the same policy and helper as LLM base URLs (`supportcore/ai/base_url.public_addresses`).
14. **What "test connection" does.** It logs in to IMAP and selects the folder, then runs EHLO, (STARTTLS) and AUTH on SMTP. It sends nothing, and each side has a 15 s timeout.
15. **Auth-failed mailboxes.** A mailbox whose last sync failed authentication is retried every 15 minutes, not every 60 s. Saving new settings clears the failure.
16. **E2E mail helper.** It is a stdlib Python script (`smtplib`/`imaplib`) run with `uv`, in the same way as the existing `runSupportCli`. No new npm dependency.
17. **Language.** An email conversation takes `Organisation.language`.

## Global Constraints

- **Still in force:** all Phase 1–5 global constraints (`.superpowers/sdd/2026-10-25-phase-5-observability-dashboards/global-constraints.md`):
  - **No git operations.**
  - Cross-tenant access returns 404.
  - Queries go through `TenantScopedRepository`.
  - Routes use `SessionDep`.
  - `DOMAIN_ERRORS` codes.
  - The adapter layering rule (`tests/test_layering.py`).
  - Secrets are encrypted.
  - No DB session is held across provider calls.
  - Publishing happens after commit only.
  - `organisation_id` is on every log line, metric and span.
- **Never in logs, spans, metrics or integration-error rows:** email bodies (text or HTML), subjects, email addresses, display names, attachment file names, raw headers, or passwords.
  - Allowed: ids, UIDs, counts, byte sizes, durations, short reason codes, SMTP reply codes, and exception class names.
- **No DB session across network calls:** no session or transaction is open during an IMAP call, an SMTP call or a storage put. A route that tests a connection commits before connecting, as `BusinessToolService.test` does.
- **Permissions:**

  | Action | Permission |
  |---|---|
  | Mailbox settings (read, save, disconnect), test connection, check now | `channels.manage` |
  | Read email messages, HTML and attachments in the inbox | inbox view (any of `queries.answer`, `analytics.view`, `bot.configure`) |
  | Reply by email, and retry a failed send | `queries.answer` |

- **Secrets:**
  - IMAP and SMTP passwords are stored only as Fernet ciphertext (`*_password_encrypted`).
  - The API returns `imap_password_set: bool` and `smtp_password_set: bool`, and never the value.
  - `__repr__` hides them.
  - Decrypted values exist only in a `MailServer` passed to one adapter call. The field has `repr=False`.
- **Mailbox fields:**
  - `address`: a valid email, ≤ 320 characters.
  - `display_name`: 0–100 characters.
  - Hosts: 1–253 characters, hostname syntax.
  - Ports: 1–65535.
  - Security: `ssl`, `starttls` or `none`.
  - Usernames: 1–320 characters.
  - Passwords: 1–1024 characters, required on create and optional on update (omitted means keep).
  - `imap_folder`: 1–200 characters, default `INBOX`.
  - Defaults: IMAP 993/`ssl`, SMTP 587/`starttls`.
- **Mailbox status:** `active`, `auth_failed` or `error`, with `last_error_type`, `last_error_at` and `last_sync_at`. Shown in settings.
- **Conversation channels:** `chat`, `email` or `voice`, enforced by a check constraint `ck_conversations_channel`. Email conversations start in `open` (via `bot` → `route_to_team`) and use the Phase 4 status table unchanged.
- **Inbound handling:**
  - Dedup is unique on `(organisation_id, rfc_message_id)`.
  - A message with no Message-ID gets `<sha256(raw)[:32]@missing-message-id.invalid>`.
  - Message-IDs are compared after stripping `<>` and whitespace. They are case-sensitive.
- **Threading order:**
  1. `In-Reply-To`.
  2. `References`, newest first.
  3. Any match on an inbound *or outbound* `EmailMessage` of the organisation; the newest match wins.
  4. The subject fallback (Assumption 11).
- **Normalised subject:**
  - Repeatedly strip leading `re:`, `fw:`, `fwd:`, `aw:`, `sv:`, `wg:` and `[...]` tags.
  - Collapse whitespace and casefold.
- **Outbound headers:**
  - `Message-ID: <{uuid4 hex}@{domain of mailbox address}>`, generated when the row is created and never changed.
  - `In-Reply-To`: the parent's Message-ID.
  - `References`: the parent's references followed by the parent's id. Beyond 20 entries, keep the first one plus the last 19.
  - `Subject`: `Re: ` + the conversation subject, unless it already starts with `Re:` case-insensitively. A chat follow-up's first email uses the `email_followup_subject` string.
  - `Auto-Submitted` is not set. Agent replies are human.
  - `X-Support-Mailbox: <mailbox uuid>`, which loop detection uses.
- **Skip reasons** (not stored as messages): `auto_submitted`, `bulk`, `autoreply_header`, `bounce`, `own_address`, `own_message`, `rate_limited`, `too_large`, `unparseable`.
  - `auto_submitted` means `Auto-Submitted` is present and not `no`.
  - `bulk` means `Precedence` is `bulk`, `junk`, `list` or `auto_reply`.
  - `autoreply_header` means `X-Autoreply` or `X-Autorespond` is present.
  - `bounce` means `multipart/report; report-type=delivery-status`, an empty `Return-Path`, or a From local part of `mailer-daemon` or `postmaster`.
  - A bounce whose original Message-ID matches one of our outbound rows marks that row `failed`/`bounced`.
- **Delivery statuses:** `received` for inbound. Outbound goes `queued` → `sending` → `sent`, or to `retry` → `sending` …, or to `failed`. The send claim lasts 120 s.
- **Sync:**

  | Setting | Value |
  |---|---|
  | Beat interval | `MAIL_SYNC_INTERVAL_SECONDS = 60` |
  | Batch | 50 messages per run |
  | Mailbox lease | 300 s |
  | Auth-failed retry | 900 s |
  | IMAP/SMTP timeouts | 30 s per operation (15 s for test connection) |

- **Integration errors:** `KINDS` gains `email`.
  - Providers are `imap` and `smtp`.
  - Error types: `auth_failed`, `connection_failed`, `timeout`, `tls_error`, `uidvalidity_changed`, `message_too_large`, `unparseable`, `smtp_4xx`, `smtp_5xx`, `ambiguous`, `interrupted` and `bounce`.
- **New `DOMAIN_ERRORS`:**

  | Error | Status | Code |
  |---|---|---|
  | `InvalidMailboxSettingsError` | 422 | `invalid_mailbox_settings` |
  | `EmailNotAvailableError` | 409 | `email_not_available` |
  | `EmailNotRetryableError` | 409 | `email_not_retryable` |
  | Mailbox missing | 404 | (`NotFoundError`) |

- **UI strings** (new, in `supportcore/mail/strings.py`, keyed like `widget/strings.py`): `email_followup_subject` ("Your support request") and `email_truncated` ("[Message truncated]").

## Review Focus

1. **A customer replies from a different address, or forwards the thread.**
   - Expected: header threading still puts it in the original conversation. The message shows its real sender, and the conversation's customer does not change.
   - Test: Task 5 `test_reply_from_other_address_threads_by_headers`.
2. **Odd or malformed MIME.** For example HTML-only mail, an unknown or wrong charset, nested `multipart/alternative` inside `multipart/mixed`, no Message-ID, or a garbage Date.
   - Expected: ingested with the best text available. One unparseable message never blocks the batch or the cursor.
   - Tests: Task 3 `test_html_only_gets_text_fallback`, `test_bad_charset_replaced_not_raised`, `test_missing_message_id_synthesised_deterministically`; Task 6 `test_unparseable_message_skipped_cursor_advances`.
3. **Hostile HTML.** For example `<script>`, `on*` handlers, `javascript:` links, `<form>`, `<meta http-equiv=refresh>`, `<iframe>`, SVG, CSS `url()` trackers, and remote `<img>`.
   - Expected: nothing executes, and nothing remote is fetched until the agent clicks "Load images".
   - Tests: Task 3 `test_sanitize_strips_active_content`, `test_remote_images_detected`; Task 10 `buildSrcdoc blocks remote images until loadImages`.
4. **The mailbox password changes at the provider.**
   - Expected: the mailbox shows `auth_failed` in settings, and an `email`/`imap`/`auth_failed` integration error is recorded (deduplicated per hour). Sync backs off to every 15 min. Saving new settings resets the state. Replies fail fast with a visible status.
   - Tests: Task 6 `test_auth_failure_marks_mailbox_and_backs_off`; Task 2 `test_save_clears_auth_failed`; Task 7 `test_smtp_auth_failure_fails_without_retry`.
5. **A customer replies weeks later.**
   - Expected: a `resolved` conversation is reopened, with SLA restarted through `sla.on_reopen` and a `reopened` event. A `closed` one gets a new conversation, which the customer panel links to the old one.
   - Tests: Task 5 `test_reply_to_resolved_reopens`, `test_reply_to_closed_starts_new_conversation`.

---

## File Structure

```
infra/docker-compose.yml               + greenmail (profile "email", pinned, mem_limit 256m, small heap)
Makefile                               + up-email / stop-email; e2e notes
packages/core/pyproject.toml           + imap-tools, nh3
packages/core/src/supportcore/
  mail/__init__.py
  mail/ports.py                        MailServer, FetchedMessage, FetchResult, InboundMailbox, OutboundEmail,
                                       OutboundMailSender, MailAuthError, MailConnectionError, MailSendError
  mail/imap_inbound.py                 ImapToolsInboundMailbox  (adapter)
  mail/smtp_outbound.py                AiosmtplibOutboundSender (adapter)
  mail/fake_inbound.py, fake_outbound.py   test-only adapters
  mail/compose.py                      build_mime(OutboundEmail) -> email.message.EmailMessage, make_message_id
  mail/parse.py                        parse_email(raw) -> ParsedEmail
  mail/sanitize.py                     sanitize_html, html_to_text
  mail/classify.py                     skip_reason(parsed, mailbox) (pure checks)
  mail/threading.py                    normalize_subject, candidate_ids, find_thread
  mail/attachments.py                  ALLOWED_TYPES, limits, select_attachments, store_attachments
  mail/mailbox_service.py              MailboxService (settings CRUD, test connection, check now)
  mail/inbound.py                      InboundEmailService.ingest
  mail/sync.py                         sync_mailboxes (fan-out), sync_mailbox
  mail/outbound.py                     OutboundEmailService (queue_reply, retry), send_email job
  mail/views.py                        email_meta_for(...) for the inbox API
  mail/events.py                       MailboxConnected / MailboxUpdated / MailboxDisconnected (audit)
  mail/metrics.py                      Prometheus counters/histogram
  mail/strings.py
  db/models/mail.py                    EmailMailbox, EmailMessage, EmailAttachment
  db/models/conversation.py            + Conversation.subject, channel check, customers lower(email) index
  conversations/service.py             create(channel=, subject=)
  assignment/service.py                route_to_team(..., announce=True)
  inbox/summary.py, inbox/service.py   + subject, contact_email; email meta on message windows
  inbox/publisher.py                   + email_status(...)
  analytics/integration_errors.py      KINDS + "email"
  knowledge/jobs.py                    dispatch: sync_mailboxes, sync_mailbox, send_email
  config.py, wiring.py                 mail_inbound / mail_outbound registries
apps/api/src/supportapi/
  routers/email_channel.py, schemas/email_channel.py
  routers/inbox.py, schemas/inbox.py   reply send_email, email meta, html, attachment download, retry
  errors.py, main.py (inline periodic sync), migrations/versions/0017_email_channel.py
apps/worker/src/supportworker/celery_app.py   + sync-mailboxes beat
apps/web/
  app/(dashboard)/orgs/[orgId]/settings/email/page.jsx
  components/email/{mailbox-form.jsx,connection-status.jsx}
  components/onboarding/email-step.jsx, lib/onboarding-steps.js
  components/inbox/{email-message.jsx,email-frame.jsx,attachment-list.jsx,delivery-status.jsx}
  components/inbox/{conversation-pane.jsx,composer.jsx,conversation-list.jsx,customer-panel.jsx}
  lib/email.js, components/app-shell.jsx, logs/integration-errors/page.jsx
  tests/unit/email.test.js, tests/e2e/email.spec.js, tests/e2e/mail_tool.py
```

---

### Task 1: Mail data model, ports, fakes and wiring

**Files:**
- Create:
  - `db/models/mail.py`
  - `mail/ports.py`
  - `mail/fake_inbound.py`
  - `mail/fake_outbound.py`
  - `mail/compose.py`
  - `mail/strings.py`
  - `apps/api/migrations/versions/0017_email_channel.py` (one file; it grows in no later task)
- Modify:
  - `db/models/conversation.py`
  - `db/models/__init__.py`
  - `config.py`
  - `wiring.py`
  - `analytics/integration_errors.py` (`KINDS`)
  - `packages/core/pyproject.toml` (`imap-tools`, `nh3`; run `uv lock`)
  - `tests/test_layering.py` (`_ADAPTER_GLOBS` + `"mail/imap_inbound.py"`, `"mail/smtp_outbound.py"`, `"mail/fake_*.py"`)
- Test:
  - `packages/core/tests/mail/test_models_and_ports.py`
  - `packages/core/tests/mail/test_compose.py`

**Interfaces:**
- Produces, in `db/models/mail.py`:
  - **`EmailMailbox`** (Tenant, Timestamp). Unique on `organisation_id`.
    - `id`
    - `address`, `display_name`
    - `imap_host`, `imap_port`, `imap_security`, `imap_username`, `imap_password_encrypted`, `imap_folder`
    - `smtp_host`, `smtp_port`, `smtp_security`, `smtp_username`, `smtp_password_encrypted`
    - `enabled` (default true)
    - `status` (check: active / auth_failed / error)
    - `last_error_type`, `last_error_at`, `last_sync_at`
    - `connected_at`
    - `uidvalidity: BigInteger | None`
    - `last_uid: BigInteger` (default 0)
    - `sync_lease_until`
    - `__repr__` without ciphertext.
  - **`EmailMessage`** (Tenant).
    - `id`
    - `message_id` (FK `messages.id` CASCADE, unique)
    - `conversation_id` (FK CASCADE)
    - `mailbox_id` (FK SET NULL)
    - `direction` (inbound / outbound)
    - `rfc_message_id` (String 998)
    - `in_reply_to`
    - `references` (JSONB list)
    - `subject`
    - `from_address`, `from_name`
    - `to_addresses`, `cc_addresses` (JSONB)
    - `reply_to`
    - `html` (sanitised, nullable)
    - `has_remote_images`
    - `skipped_attachments` (JSONB `[{filename, size, reason}]`)
    - `imap_uid`
    - `delivery_status`
    - `attempts`, `next_attempt_at`, `sending_until`, `sent_at`
    - `error_type`
    - `created_at`
    - Indexes:
      - unique `(organisation_id, rfc_message_id)`;
      - `(conversation_id, created_at)`;
      - `(mailbox_id, from_address, created_at)`, for the loop guard.
  - **`EmailAttachment`** (Tenant).
    - `id`
    - `email_message_id` (FK CASCADE)
    - `filename`, `content_type`, `size_bytes`, `sha256`, `storage_key`, `content_id`
    - `created_at`
- Changes to existing models:
  - `Conversation.subject: String(998) | None`.
  - `CONVERSATION_CHANNELS = ("chat", "email", "voice")`, with check `ck_conversations_channel`.
  - The index `ix_customers_organisation_id_lower_email` on `(organisation_id, lower(email))`.
- **`mail/ports.py`** (exact shapes):

```python
Security = Literal["ssl", "starttls", "none"]

@dataclass(frozen=True)
class MailServer:
    host: str
    port: int
    security: Security
    username: str
    password: str = field(repr=False)

@dataclass(frozen=True)
class FetchedMessage:
    uid: int
    internal_date: datetime
    size: int
    raw: bytes | None  # None when size > max_bytes

@dataclass(frozen=True)
class FetchResult:
    uidvalidity: int
    messages: list[FetchedMessage]  # ascending uid, at most `limit`

class MailAuthError(Exception): ...          # login refused
class MailConnectionError(Exception):         # error_type: connection_failed | timeout | tls_error
    def __init__(self, error_type: str) -> None: ...

class InboundMailbox(Protocol):
    async def check(self, server: MailServer, folder: str) -> None: ...
    async def uidvalidity(self, server: MailServer, folder: str) -> int: ...
    async def fetch(self, server: MailServer, folder: str, *, after_uid: int | None, since: datetime | None,
                    limit: int, max_bytes: int) -> FetchResult: ...

@dataclass(frozen=True)
class OutboundEmail:
    from_address: str
    from_name: str | None
    to: str
    subject: str
    text: str
    message_id: str
    in_reply_to: str | None
    references: tuple[str, ...]
    mailbox_id: UUID

class MailSendError(Exception):               # kind: auth | transient | permanent | ambiguous
    def __init__(self, kind: str, error_type: str) -> None: ...

class OutboundMailSender(Protocol):
    async def check(self, server: MailServer) -> None: ...
    async def send(self, server: MailServer, email: OutboundEmail) -> None: ...
```

- **`compose.py`:**
  - `make_message_id(mailbox_address: str) -> str`.
  - `build_mime(email: OutboundEmail) -> email.message.EmailMessage`, which sets every Outbound header from the Global Constraints and a `Date`.
- **Fakes:**
  - `FakeInboundMailbox`:
    - `deliver(raw: bytes, internal_date=None) -> int` returns the uid;
    - `reset_uidvalidity()`;
    - `fail_with(exc)`;
    - `fetch` honours `after_uid`, `since`, `limit` and `max_bytes`;
    - `calls: int`.
  - `FakeOutboundMailSender`:
    - `sent: list[OutboundEmail]`;
    - `fail_next(*errors: MailSendError)`;
    - `sent_mime()` returns the built MIME messages.
- **Settings:** `mail_inbound: str = "imap"` and `mail_outbound: str = "smtp"`. `"fake"` is valid only with `env == "test"`.
- **`Adapters`** gains `inbound_mailbox: InboundMailbox` and `outbound_mail: OutboundMailSender`.

- [ ] **Step 1: Write failing tests.**
  - `test_channel_check_rejects_unknown`: inserting `channel="fax"` raises IntegrityError.
  - `test_email_message_unique_per_org`: the same `rfc_message_id` succeeds in org A and org B, and a second insert in org A raises IntegrityError.
  - `test_mailbox_repr_hides_ciphertext`.
  - `test_mail_server_repr_hides_password`.
  - `test_fake_adapters_only_in_test_env`: `build_adapters` with `env="dev"`, `mail_inbound="fake"` raises.
  - `test_fake_inbound_after_uid_and_limit`.
  - `test_build_mime_threading_headers`: asserts `In-Reply-To`, `References` (exactly 20 entries, the first one kept, when given 25), `Message-ID` with the mailbox domain, `X-Support-Mailbox`, and no `Auto-Submitted`.
  - `test_reply_subject_prefix_not_doubled`.
  - `test_integration_error_kind_email_accepted`.
- [ ] **Step 2: Run them.** Expected: FAIL (`ModuleNotFoundError: supportcore.mail`).
- [ ] **Step 3: Implement the models, migration 0017, ports, compose, fakes and wiring.** Register the fakes only in `build_adapters` when `env == "test"`, as the existing fake embedder is registered.
- [ ] **Step 4: Run them, plus `make test`.** `make test` includes `alembic check` and `test_layering.py`. Expected: PASS.
- [ ] **Step 5: Run the task's tests and record the results in the report.**

---

### Task 2: Mailbox settings service and API

**Files:**
- Create:
  - `mail/mailbox_service.py`
  - `mail/events.py`
  - `apps/api/src/supportapi/routers/email_channel.py`
  - `apps/api/src/supportapi/schemas/email_channel.py`
- Modify:
  - `audit/subscribers.py` (the three events, with ids only)
  - `apps/api/src/supportapi/main.py` (router)
  - `deps.py` (`build_mailbox_service`)
  - `errors.py`
- Test:
  - `packages/core/tests/mail/test_mailbox_service.py`
  - `apps/api/tests/test_email_channel_routes.py`

**Interfaces:**
- Consumes: Task 1 models, `MailServer`, `Adapters.inbound_mailbox` / `outbound_mail`, `SecretCipher`, `public_addresses`, and `RateLimiter`.
- Produces:
  - `MailboxService(session, organisation_id, events, adapters, settings)`, with these methods:
    - `get() -> EmailMailbox | None`.
    - `save(actor: User, **fields) -> EmailMailbox`: upserts and validates per the Global Constraints. The first save sets `connected_at=now`, and a password is required then. A host, port or username change clears `uidvalidity`, sets `last_uid=0` and sets `connected_at=now`. Every save sets `status="active"` and clears `last_error_*`.
    - `disconnect(actor) -> None`: deletes the row. Conversations and messages stay; `mailbox_id` becomes NULL.
    - `test(overrides: dict) -> ConnectionTestResult`: merges unsaved form values over the stored passwords, commits, then calls `check` on both adapters with a 15 s timeout. Returns `{imap: {ok, error_type}, smtp: {ok, error_type}}`.
    - `request_sync() -> None`: `after_commit` enqueues `sync_mailbox` with payload `{organisation_id, mailbox_id}`. The job handler arrives in Task 6, so Task 2 tests assert the enqueue on a recording queue and never run the job.
  - `servers(mailbox, cipher) -> tuple[MailServer, MailServer]`, a module function that Tasks 6 and 7 also use.
  - **Routes**, all with `channels.manage`:
    - `GET /orgs/{org}/channels/email` returns `MailboxOut | null`.
    - `PUT /orgs/{org}/channels/email` returns `MailboxOut`.
    - `DELETE /orgs/{org}/channels/email` returns 204.
    - `POST /orgs/{org}/channels/email/test` returns `ConnectionTestOut`.
    - `POST /orgs/{org}/channels/email/sync` returns 202. It is rate-limited to `(1, 10)` per org; over the limit is 429 `rate_limited`.
  - `MailboxOut` has every non-secret field, plus `imap_password_set`, `smtp_password_set`, `status`, `last_error_type`, `last_error_at` and `last_sync_at`.

- [ ] **Step 1: Write failing tests.**
  - `test_password_encrypted_and_never_returned`: the response JSON contains neither the plaintext nor the ciphertext, and has `imap_password_set is True`.
  - `test_update_without_password_keeps_it`.
  - `test_create_requires_passwords_422`.
  - `test_security_none_refused_in_prod` and `test_private_host_refused_in_prod`: `env=prod` settings and a monkeypatched resolver give 422 `invalid_mailbox_settings`.
  - `test_host_change_resets_cursor`.
  - `test_save_clears_auth_failed`.
  - `test_test_connection_reports_each_side`: fake IMAP raises `MailAuthError`, giving `imap.ok False` and `error_type "auth_failed"`, and `smtp.ok True`.
  - `test_test_connection_holds_no_session`: the fake `check` asserts that `session.in_transaction()` is False through a hook.
  - `test_routes_require_channels_manage`: a default Agent gets 403.
  - `test_cross_tenant_404`.
  - `test_audit_rows_have_no_secrets_or_addresses`.
  - `test_sync_now_rate_limited`.
- [ ] **Step 2: Run them.** Expected: FAIL (404 on routes).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them, plus `make test`.** Expected: PASS.
- [ ] **Step 5: Run the task's tests and record the results in the report.**

---

### Task 3: MIME parsing, HTML sanitising and attachment selection

**Files:**
- Create:
  - `mail/parse.py`
  - `mail/sanitize.py`
  - `mail/attachments.py` (`select_attachments` only in this task)
- Test:
  - `packages/core/tests/mail/test_parse.py`
  - `packages/core/tests/mail/test_sanitize.py`
  - `packages/core/tests/mail/test_attachments.py`
  - `packages/core/tests/mail/fixtures/*.eml` (hand-written, synthetic addresses only)

**Interfaces:**
- **`parse_email(raw: bytes) -> ParsedEmail`.** It uses stdlib `email` with `policy.default`, and raises `UnparseableEmailError` only when there are no headers at all. `ParsedEmail` (frozen) fields:
  - `message_id`
  - `in_reply_to`
  - `references: tuple[str, ...]`
  - `subject`
  - `from_address`, `from_name`
  - `reply_to`
  - `to: tuple[str, ...]`, `cc`
  - `date: datetime | None`
  - `text` (capped per Assumption 9)
  - `html_raw: str | None`
  - `attachments: list[RawAttachment(filename, content_type, data, content_id, inline)]`
  - `auto_submitted`, `precedence`
  - `has_autoreply_header: bool`
  - `return_path_empty: bool`
  - `is_delivery_report: bool`
  - `bounced_message_id: str | None`
  - `x_support_mailbox: str | None`
- **Parsing rules:**
  - Text comes from the `text/plain` alternative. With no plain part, it is `html_to_text(html_raw)`.
  - Undecodable bytes are decoded with `errors="replace"`.
  - Message-IDs are normalised (strip `<>` and whitespace).
  - Without a Message-ID, the synthetic id from the Global Constraints is used.
- **`sanitize_html(html: str) -> SanitizedHtml(html: str | None, has_remote_images: bool)`.** It uses nh3:
  - Allowed tags: the email-safe set of block, inline, table, list, `img` and `blockquote` elements.
  - Allowed attributes: `href`, `src`, `alt`, `title`, `width`, `height`, `style`, `align`, `colspan` and `rowspan`, plus `class` on `div` and `blockquote` so quoted blocks can be collapsed.
  - `url_schemes={"http","https","mailto","data"}`. `data:` is allowed on `img` only; enforce it in an `attribute_filter`.
  - `<a>` gets `target="_blank" rel="noopener noreferrer nofollow"`.
  - Remote images are detected in the output: `img src=http(s)` or `url(http` in a style.
  - Output over 1 MB becomes `html=None`.
- **`html_to_text(html: str) -> str`** uses BeautifulSoup (already a dependency) with block-level newlines.
- **`select_attachments(parsed) -> tuple[list[RawAttachment], list[dict]]`** returns (kept, skipped `{filename, size, reason}`). Reasons:
  - `type_not_allowed`;
  - `too_large`;
  - `message_total_exceeded`;
  - `too_many`.

  Each file name is reduced to its basename, stripped of control characters, and capped at 255.

- [ ] **Step 1: Write failing tests.**
  - `test_plain_and_html_alternative`.
  - `test_html_only_gets_text_fallback`.
  - `test_bad_charset_replaced_not_raised`.
  - `test_missing_message_id_synthesised_deterministically`: the same bytes give the same id.
  - `test_references_parsed_in_order`.
  - `test_auto_reply_headers_extracted`.
  - `test_delivery_report_original_id`: a DSN fixture gives the original Message-ID.
  - `test_text_truncated_with_marker`.
  - `test_sanitize_strips_active_content`: the output contains none of `<script`, `onerror`, `javascript:`, `<form`, `<iframe`, `<meta`, `<svg` or `<object`.
  - `test_remote_images_detected`: covers both an `img` and a CSS `url()`.
  - `test_data_uri_only_on_img`.
  - `test_links_get_noopener`.
  - `test_attachment_limits_and_types`: an 11 MB pdf gives `too_large`, `.exe` gives `type_not_allowed`, and 21 files give `too_many`.
  - `test_filename_path_stripped`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Run the task's tests and record the results in the report.**

---

### Task 4: Loop detection and threading

**Files:**
- Create:
  - `mail/classify.py`
  - `mail/threading.py`
- Test:
  - `packages/core/tests/mail/test_classify.py`
  - `packages/core/tests/mail/test_threading.py`

**Interfaces:**
- Consumes: `ParsedEmail` (Task 3), and `EmailMessage` / `Conversation` (Task 1).
- Produces:
  - `skip_reason(parsed: ParsedEmail, mailbox: EmailMailbox) -> str | None`. Pure. Covers the header-based reasons, `own_address` (`From` equals `mailbox.address`, case-insensitively) and `x_support_mailbox` equal to `mailbox.id`, which is `own_message`.
  - `async def db_skip_reason(session, organisation_id, mailbox_id, parsed, now) -> str | None`:
    - `own_message`: the Message-ID equals one of our outbound rows.
    - `rate_limited`: more than 20 inbound rows in the last hour from the same `from_address` to this mailbox.
  - `normalize_subject(subject: str | None) -> str`.
  - `candidate_ids(parsed) -> list[str]`: In-Reply-To first, then References newest first, de-duplicated.
  - `async def find_thread(session, organisation_id, parsed, customer_id, now) -> Conversation | None`. It follows the Global Constraints order and the subject fallback rules (Assumption 11). All queries go through `TenantScopedRepository`.

- [ ] **Step 1: Write failing tests.**
  - `test_auto_submitted_no_is_accepted`.
  - `test_auto_submitted_auto_replied_skipped`.
  - `test_precedence_bulk_skipped`.
  - `test_bounce_variants_skipped`: covers multipart/report, an empty Return-Path and MAILER-DAEMON.
  - `test_own_address_and_header_skipped`.
  - `test_own_outbound_message_id_skipped`.
  - `test_rate_limit_after_20_per_hour`.
  - `test_normalize_subject_strips_prefixes_and_tags`: `"RE: Fwd: [Ticket] Order  late"` gives `"order late"`.
  - `test_thread_by_in_reply_to`.
  - `test_thread_by_references_newest_first`.
  - `test_thread_by_our_outbound_id`: covers the chat follow-up case, where a chat conversation is matched.
  - `test_subject_fallback_same_customer_only`.
  - `test_subject_fallback_ignores_closed_and_older_than_30_days`.
  - `test_thread_lookup_tenant_scoped`: org B's identical Message-ID is never matched.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Run the task's tests and record the results in the report.**

---

### Task 5: Inbound ingestion into the human inbox

**Files:**
- Create:
  - `mail/inbound.py`
  - `mail/attachments.py` (+ `store_attachments`)
- Modify:
  - `conversations/service.py` (`create(..., channel="chat", subject=None)`, validated against `CONVERSATION_CHANNELS`)
  - `assignment/service.py` (`route_to_team(conversation, decision, *, announce: bool = True)`; with `announce=False` there is no system message and no widget events)
  - `analytics/rollup.py` (reason stats skip `entered_team` rows with a null `rule_key`), only if they are not skipped already
- Test:
  - `packages/core/tests/mail/test_inbound.py`
  - `packages/core/tests/assignment/test_assignment.py` (+1)

**Interfaces:**
- Consumes: Tasks 1, 3 and 4; `AssignmentService`, `ConversationService`, `sla.on_reopen`, `InboxPublisher` and `record`.
- Produces:
  - `async def store_attachments(storage: FileStorage, organisation_id, rfc_message_id, kept: list[RawAttachment]) -> list[StoredAttachment]`. Keys are deterministic: `orgs/{org}/email/{sha256(rfc_message_id)}/{index}-{sha256(data)}`. It is called with no session open.
  - `InboundEmailService(session, organisation_id, events, adapters, settings, publisher)`.
  - `.ingest(mailbox, parsed, sanitized, stored, skipped, imap_uid) -> IngestResult(conversation_id, message_id, created: bool, duplicate: bool)`. It runs in the caller's transaction:
    1. If the `rfc_message_id` exists, it returns `duplicate=True`.
    2. It finds the customer by `lower(email)`: the newest `last_seen_at` wins. If none exists, it creates `Customer(email, name)`.
    3. It calls `find_thread`. With no thread, it uses `ConversationService.create(customer_id, None, org.language, False, channel="email", subject=parsed.subject)`, then `route_to_team(conv, {"action": "escalate", "rule_key": None, "rule_name": None, "reason": None, "source": "email"}, announce=False)`.
    4. With a thread:
       - `pending` → `open` (`ensure_transition(..., "customer_message")`);
       - `resolved` → reopen (`sla.on_reopen`, a `reopened` event);
       - `bot` → `route_to_team(announce=False)`;
       - `closed` → a new conversation, as in step 3.
    5. It adds the message as role `customer` through `ConversationService.add_message`, writes the `EmailMessage` (`direction="inbound"`, `delivery_status="received"`) and the `EmailAttachment` rows, and calls `publisher.conversation_changed`.
  - `.handle_bounce(parsed) -> bool`: marks a matching outbound row `failed`/`bounced`, publishes `email_status` and returns True.

- [ ] **Step 1: Write failing tests.** Use the fake storage and the memory pubsub.
  - `test_new_email_creates_open_email_conversation_assigned_round_robin`: `channel == "email"`, `status == "open"`, the assignee is the available agent, and the SLA due dates are set.
  - `test_email_conversation_routed_without_system_message`: no `system` message exists.
  - `test_customer_linked_by_email_across_channels`: a widget customer with the same email (any case) is reused, and `linked_conversations` returns the chat conversation.
  - `test_reply_threads_into_existing_and_pending_becomes_open`.
  - `test_reply_from_other_address_threads_by_headers`.
  - `test_reply_to_resolved_reopens`.
  - `test_reply_to_closed_starts_new_conversation`.
  - `test_chat_followup_reply_threads_into_chat_conversation`.
  - `test_duplicate_message_id_ignored`.
  - `test_attachments_stored_with_deterministic_keys_and_rows`.
  - `test_publishes_after_commit_only`: a rollback publishes nothing.
  - `test_email_conversation_counted_under_email_channel`: `compute_hour` has `channels["email"]["conversations_started"] == 1`.
  - `test_email_entry_not_counted_as_escalation_reason`.
  - `test_bounce_marks_outbound_failed`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them, plus `make test`.** Phase 4 assignment tests must still pass with the default `announce=True`. Expected: PASS.
- [ ] **Step 5: Run the task's tests and record the results in the report.**

---

### Task 6: IMAP adapter and the sync jobs

**Files:**
- Create:
  - `mail/imap_inbound.py`
  - `mail/sync.py`
  - `mail/metrics.py`
- Modify:
  - `knowledge/jobs.py` (dispatch `sync_mailboxes`, `sync_mailbox`)
  - `apps/worker/src/supportworker/celery_app.py` (`SYNC_MAILBOXES_TASK = "supportworker.sync_mailboxes"`, beat `"sync-mailboxes"` every `MAIL_SYNC_INTERVAL_SECONDS`)
  - `apps/api/src/supportapi/main.py` (`_sync_mailboxes_periodically` for inline)
- Test:
  - `packages/core/tests/mail/test_sync.py`
  - `packages/core/tests/mail/test_imap_adapter.py` (`@pytest.mark.integration`; skips unless GreenMail answers on `localhost:3143`)

**Interfaces:**
- Consumes: Tasks 1–5, and `record_integration_error`.
- Produces:
  - **`ImapToolsInboundMailbox`.**
    - Each call runs `imap_tools.MailBox` / `MailBoxStartTls` / `MailBoxUnencrypted` inside `asyncio.to_thread`, with `timeout=30`.
    - It fetches with `mark_seen=False` and `BODY.PEEK[]`.
    - It fetches `RFC822.SIZE` first and skips the body when the size is over `max_bytes`.
    - It filters `uid > after_uid`, which works around the `n:*` quirk.
    - It maps errors: login failures to `MailAuthError`; socket, TLS and timeout errors to `MailConnectionError(error_type)`.
  - **`async def sync_mailboxes(runtime, now) -> int`.** It reads `(id, organisation_id)` of enabled mailboxes whose status is not `auth_failed`, or whose `last_error_at` is older than 900 s. This is a platform read, like `check_sla`'s organisation list. It enqueues `sync_mailbox` for each and returns the count.
  - **`async def sync_mailbox(runtime, organisation_id: UUID, mailbox_id: UUID, now: datetime) -> SyncOutcome(fetched, ingested, skipped, duplicates, outcome)`.** Outcomes are `ok`, `busy`, `auth_failed` and `error`. The steps:
    1. Lease: an atomic `UPDATE … SET sync_lease_until=now+300s WHERE (lease is null or expired) RETURNING`. No row means `busy`. Commit.
    2. With no session open, check UIDVALIDITY.
       - If the stored value is None, call `fetch(after_uid=None, since=connected_at-10min)`.
       - If it changed, record integration error `uidvalidity_changed` and call `fetch(after_uid=None, since=last_sync_at-1day)`.
       - Otherwise call `fetch(after_uid=last_uid)`.
       - The limit is 50.
    3. For each message in uid order:
       - `raw is None` is skipped as `too_large`.
       - Parse; a failure is `unparseable`.
       - Run `skip_reason`.
       - With a short session: run `db_skip_reason`, the duplicate pre-check and `handle_bounce`.
       - With no session: `store_attachments`.
       - With one UnitOfWork: `ingest` plus `UPDATE mailbox SET last_uid=uid, uidvalidity=… WHERE id AND sync_lease_until = :lease`. A lost lease aborts the run. Commit.
       - Skipped messages advance the cursor in their own tiny transaction.
    4. Finally set `last_sync_at`, `status="active"` and release the lease.
       - `MailAuthError` sets `status="auth_failed"` and records `(email, imap, auth_failed)`.
       - `MailConnectionError` sets `status="error"` and records its `error_type`.
       - The lease is always released.
  - **Metrics** (`mail/metrics.py`):
    - `support_email_sync_runs_total{organisation_id, outcome}`
    - `support_email_inbound_total{organisation_id, outcome}`, where outcome is `ingested`, `duplicate` or a skip reason
    - `support_email_sync_duration_seconds{organisation_id}`
  - **Spans:** `mail.sync` and `mail.ingest`. Attributes are `organisation_id`, `mailbox_id`, `conversation_id`, `imap_uid`, the counts and `outcome` only.

- [ ] **Step 1: Write failing tests.** Use the fake inbound adapter.
  - `test_fan_out_enqueues_per_enabled_mailbox`.
  - `test_first_sync_imports_only_since_connected`.
  - `test_cursor_advances_and_batch_bounded`: 120 messages take 3 runs of 50, 50 and 20.
  - `test_uidvalidity_change_rescans_without_duplicates`.
  - `test_unparseable_message_skipped_cursor_advances`.
  - `test_too_large_skipped_and_recorded`.
  - `test_auto_reply_and_own_mail_not_ingested`.
  - `test_auth_failure_marks_mailbox_and_backs_off`: the next `sync_mailboxes` within 900 s does not enqueue it.
  - `test_overlapping_sync_runs_single_runner`: two concurrent runs give one `busy` and no duplicate messages.
  - `test_lost_lease_aborts_without_cursor_regression`.
  - `test_no_session_open_during_fetch_and_storage`: fake hooks assert there is no active transaction.
  - `test_sync_logs_spans_metrics_have_no_content`: a sentinel subject, address and body appear in no captured log (`structlog.testing.capture_logs`), no span attribute (`get_test_span_exporter()`) and no metric label.
  - `test_one_mailbox_failure_isolated`.
  - Integration: `test_greenmail_fetch_roundtrip` and `test_greenmail_bad_password_raises_auth_error`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Add the `sync_mailboxes` beat entry and the inline periodic task, mirroring `check_sla`.
- [ ] **Step 4: Run them, plus `make test`.** Expected: PASS. The integration tests run with `make up-email` (Task 11) or are skipped.
- [ ] **Step 5: Run the task's tests and record the results in the report**, including whether the GreenMail tests ran or were skipped.

---

### Task 7: Outbound replies — SMTP adapter, send job, retries

**Files:**
- Create:
  - `mail/smtp_outbound.py`
  - `mail/outbound.py`
- Modify:
  - `knowledge/jobs.py` (dispatch `send_email`)
  - `inbox/publisher.py` (`email_status`)
  - `apps/api/src/supportapi/routers/inbox.py` (`reply`; new retry route)
  - `schemas/inbox.py` (`ReplyIn(text, send_email: bool = False)`)
  - `errors.py`
- Test:
  - `packages/core/tests/mail/test_outbound.py`
  - `packages/core/tests/mail/test_smtp_adapter.py` (integration)
  - `apps/api/tests/test_email_reply_routes.py`

**Interfaces:**
- Consumes: Task 1 (`OutboundEmail`, `build_mime`, `make_message_id`), Task 2 `servers()`, `ConversationService.add_agent_message`, and `record_integration_error`.
- Produces:
  - **`AiosmtplibOutboundSender`.**
    - It uses `aiosmtplib.SMTP(hostname, port, use_tls=security=="ssl", start_tls=security=="starttls", timeout=30)`, then `login`, then `send_message(build_mime(email))`.
    - It tracks the phase. Errors before `DATA` map as follows:
      - an auth error to `MailSendError("auth", "auth_failed")`;
      - a 4xx reply, a connect error or a timeout to `("transient", "smtp_4xx" | "connection_failed" | "timeout")`;
      - a 5xx reply to `("permanent", "smtp_5xx")`.
    - Errors once the message data is being sent:
      - a disconnect or timeout without a final reply maps to `("ambiguous", "ambiguous")`;
      - a 4xx or 5xx final reply maps as above.
  - **`OutboundEmailService(session, organisation_id, events, adapters, publisher)`.**
    - `ensure_can_email(conversation, send_email: bool) -> bool`. It returns True when the conversation will be emailed:
      - always for `channel == "email"`;
      - for `chat` only with `send_email`, a `contact_email` and a connected mailbox;
      - otherwise `send_email=True` raises `EmailNotAvailableError`;
      - an email conversation with no mailbox raises it too.
    - `queue_reply(conversation, message) -> EmailMessage`. The parent is the conversation's newest `EmailMessage`, inbound or outbound. The row is created with the headers from the Global Constraints and `delivery_status="queued"`. `after_commit` enqueues `send_email`.
    - `retry(email_message_id) -> EmailMessage`: only from `failed`, otherwise `EmailNotRetryableError`. It sets `queued`, `attempts=0` and `error_type=None`. The Message-ID is unchanged.
  - **`async def send_email(runtime, organisation_id, email_message_id, now) -> str`.**
    1. Claim: an atomic `UPDATE … SET status='sending', attempts=attempts+1, sending_until=now+120s WHERE status IN ('queued','retry')`.
       - No row: if the status is `sending` with `sending_until < now`, mark it `failed`/`interrupted` and record it. Never resend.
       - Commit.
    2. Send with no session open.
    3. Record the outcome:
       - Success: `sent` and `sent_at`.
       - `transient` with `attempts < 5`: `retry` and `next_attempt_at`. Re-enqueue with countdown `[30, 120, 600, 1800, 7200][attempts-1]`.
       - Otherwise: `failed` with `error_type`.
       - `auth` also sets the mailbox to `status="auth_failed"`.
       - Every failure records `(email, smtp, error_type)`.
       - Each outcome publishes `email_status` after commit.
  - **`InboxPublisher.email_status(session, organisation_id, conversation_id, message_id, status, error_type)`.** After commit it publishes `{"type": "email.status", conversation_id, message_id, status, error_type}` on the org inbox channel.
  - **Routes:**
    - `POST …/inbox/conversations/{cid}/messages` takes `ReplyIn`. It calls `ensure_can_email` before `add_agent_message`, then `queue_reply` in the same transaction.
    - `POST /orgs/{org}/inbox/conversations/{cid}/messages/{mid}/email/retry` (`queries.answer`) returns `EmailMetaOut` (Task 8; until then, return `{delivery_status}`).
  - **Metric:** `support_email_outbound_total{organisation_id, outcome}`, where outcome is `sent`, `retry`, `failed` or `ambiguous`. Span: `mail.send`, with ids and `outcome` only.

- [ ] **Step 1: Write failing tests.**
  - `test_reply_on_email_conversation_queues_and_sends_once`: `len(fake.sent) == 1`; `In-Reply-To` equals the last inbound id; `References` ends with it; status `sent`.
  - `test_chat_followup_send_email_uses_contact_email_and_followup_subject`.
  - `test_send_email_without_contact_or_mailbox_409`.
  - `test_transient_failure_retries_with_same_message_id`: the first attempt fails, the second succeeds, and both MIME Message-IDs are equal.
  - `test_retry_gives_up_after_five_attempts`.
  - `test_smtp_auth_failure_fails_without_retry`: the mailbox becomes `auth_failed` and an integration error is recorded.
  - `test_ambiguous_failure_not_retried`.
  - `test_expired_claim_marked_interrupted_not_resent`.
  - `test_concurrent_send_jobs_send_once`: two `send_email` calls race and `len(fake.sent) == 1`.
  - `test_manual_retry_only_from_failed_409`.
  - `test_no_session_open_during_smtp`.
  - `test_send_logs_spans_have_no_content`.
  - `test_email_status_published_after_commit`.
  - Routes:
    - `test_reply_route_cross_tenant_404`;
    - `test_retry_route_requires_queries_answer`.
  - Integration:
    - `test_greenmail_send_threads`: the message is fetched back from the recipient mailbox over IMAP, and its `In-Reply-To` is checked.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them, plus `make test`.** Expected: PASS.
- [ ] **Step 5: Run the task's tests and record the results in the report.**

---

### Task 8: Inbox API — email metadata, HTML, attachments, subject

**Files:**
- Create: `mail/views.py`
- Modify:
  - `inbox/summary.py` (`subject`, `contact_email` in `InboxSummary` and `to_payload`)
  - `inbox/service.py` (attach email meta to message windows)
  - `routers/inbox.py`
  - `schemas/inbox.py`
- Test:
  - `packages/core/tests/mail/test_views.py`
  - `apps/api/tests/test_email_inbox_routes.py`

**Interfaces:**
- Consumes: Tasks 1, 5 and 7.
- Produces:
  - `async def email_meta_for(session, organisation_id, message_ids: list[UUID]) -> dict[UUID, EmailMeta]`, one query plus an attachments query. `EmailMeta` fields:
    - `direction`
    - `subject`
    - `from_address`, `from_name`
    - `to`, `cc`
    - `has_html`
    - `has_remote_images`
    - `attachments[{id, filename, content_type, size_bytes}]`
    - `skipped_attachments`
    - `delivery_status`
    - `error_type`
  - `MessageOut.email: EmailMetaOut | None`, in the detail and older-messages responses.
  - **Routes** (inbox view permission):
    - `GET /orgs/{org}/inbox/conversations/{cid}/messages/{mid}/email-html` returns `{html: str}`. Without HTML it is 404.
    - `GET /orgs/{org}/inbox/conversations/{cid}/attachments/{aid}` returns the bytes from `FileStorage.get`. It is read before the response, and the session is committed first. Headers:
      - `Content-Type`: the stored type if allowlisted, else `application/octet-stream`;
      - `Content-Disposition: attachment; filename*=UTF-8''…`;
      - `X-Content-Type-Options: nosniff`;
      - `Cache-Control: private, no-store`.
    - Both check that the attachment or message belongs to `cid` in the org. Anything else is 404.

- [ ] **Step 1: Write failing tests.**
  - `test_detail_messages_carry_email_meta`.
  - `test_summary_has_subject_and_contact_email`.
  - `test_email_html_route_returns_sanitised_html`.
  - `test_attachment_download_headers_nosniff_and_disposition`.
  - `test_attachment_of_other_conversation_404`.
  - `test_attachment_cross_tenant_404`.
  - `test_attachment_requires_inbox_view`: a member without any inbox-view permission gets 403.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them, plus `make test`.** Expected: PASS.
- [ ] **Step 5: Run the task's tests and record the results in the report.**

---

### Task 9: Web — email settings page and onboarding step

**Files:**
- Create:
  - `app/(dashboard)/orgs/[orgId]/settings/email/page.jsx`
  - `components/email/mailbox-form.jsx`
  - `components/email/connection-status.jsx`
  - `components/onboarding/email-step.jsx`
  - `lib/email.js` (settings helpers in this task)
- Modify:
  - `components/app-shell.jsx` (nav "Email", `PERMISSIONS.CHANNELS_MANAGE`)
  - `lib/onboarding-steps.js` (append `{id: "email", title: "Connect your support mailbox", Component: EmailStep}`)
  - `logs/integration-errors/page.jsx` (`KINDS` + `"email"`, and the copy names email)
- Test:
  - `tests/unit/email.test.js`
  - `tests/unit/onboarding-steps.test.js`

**Interfaces:**
- Consumes: the Task 2 routes.
- Produces:
  - `mailboxPayload(form, existing) -> object`: omits empty password fields when `existing?.imap_password_set` (or smtp) is set.
  - `validateMailboxForm(form, {isNew}) -> {field: message}`: the same rules as the Global Constraints.
  - `defaultPorts(security, side) -> number`: IMAP ssl 993, starttls 143, none 143; SMTP ssl 465, starttls 587, none 25.
  - `connectionStatusLabel(mailbox) -> {label, tone}`.
- **Form layout:** two sections, IMAP and SMTP. Each has host, port, security select, username and password. A password field shows "•••• saved — leave blank to keep" when set.
- **Buttons:** **Test connection** shows a per-side result. **Save**, **Check now** and **Disconnect** (with a confirm dialog) complete the set.
- **Onboarding step:** the same form plus **Skip for now**, which calls `onComplete({skipped: true})`.

- [ ] **Step 1: Write failing tests.**
  - `mailboxPayload omits blank passwords when already set`.
  - `validateMailboxForm requires passwords only when new`.
  - `defaultPorts by security`.
  - `connectionStatusLabel maps auth_failed to destructive`.
  - `onboarding has optional email step last`.
  - `integration error kinds include email`.
- [ ] **Step 2: Run them.** `cd apps/web && npx vitest run tests/unit/email.test.js tests/unit/onboarding-steps.test.js`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them, plus `npm run lint` and `npm run build`.** Expected: PASS.
- [ ] **Step 5: Run the task's tests and record the results in the report.**

---

### Task 10: Web — reading and replying to email in the inbox

**Files:**
- Create:
  - `components/inbox/email-message.jsx`
  - `components/inbox/email-frame.jsx`
  - `components/inbox/attachment-list.jsx`
  - `components/inbox/delivery-status.jsx`
- Modify:
  - `lib/email.js` (rendering helpers)
  - `components/inbox/conversation-pane.jsx` (use `EmailMessage` when `message.email` is set)
  - `components/inbox/composer.jsx`
  - `components/inbox/conversation-list.jsx` (subject line, email icon)
  - `components/inbox/customer-panel.jsx` (contact email)
  - `lib/inbox.js` / `realtime-provider.jsx` (apply `email.status`)
- Test: `tests/unit/email.test.js`, `tests/unit/inbox-ui.test.js`

**Interfaces:**
- Consumes: the Task 8 API, and the Task 7 `email.status` event and retry route.
- Produces:
  - `buildSrcdoc(html, {loadImages, showQuoted}) -> string`.
    - It prepends `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:{loadImages ? " https: http:" : ""}; font-src 'none'">`.
    - It adds a base style. With `showQuoted` false, the style includes `blockquote, .gmail_quote, .yahoo_quoted, #appendonsend ~ * { display:none }`.
  - `splitQuoted(text) -> {body, quoted}`. The quoted part starts at the first `On … wrote:` line or the first run of `>` lines that reaches the end.
  - `applyEmailStatus(messages, event) -> messages`.
  - `downloadAttachment(api, url, filename)`: an authenticated fetch to a Blob and an object-URL click. The URL is revoked afterwards.
- **`EmailFrame`.**
  - It renders `<iframe sandbox="allow-popups allow-popups-to-escape-sandbox" srcDoc=… title="Email content">`. The sandbox has no `allow-scripts` and no `allow-same-origin`.
  - It is fixed at `max-h-[480px]`, scrolls and can be resized vertically.
  - It shows a "Load remote images" button when `has_remote_images`, and a "Show quoted text" toggle.
  - Plain text, split with `splitQuoted`, is shown when there is no HTML or the HTML fetch fails.
- **Composer.**
  - On `channel === "email"`, it shows "Reply by email to {address}" above the box.
  - On chat with `contact_email`, it shows a "Also send by email to {contact_email}" checkbox, ticked by default. It sends `send_email`.
  - A 409 `email_not_available` shows an inline error with a link to the Email settings.
- **`DeliveryStatus`.** It shows Queued / Sending / Sent / Retrying / Failed. Failed shows the error type and a **Retry** button for `queries.answer`. For `ambiguous` and `interrupted` the label is "May not have been sent".

- [ ] **Step 1: Write failing tests.**
  - `buildSrcdoc blocks remote images until loadImages`: the CSP `img-src` is exactly `data:` before and includes `https:` after.
  - `buildSrcdoc hides quoted blocks unless showQuoted`.
  - `splitQuoted finds "On … wrote:" and trailing > lines`.
  - `applyEmailStatus updates only the matching message`.
  - `EmailFrame sandbox has no allow-scripts or allow-same-origin`.
  - `Composer shows follow-up checkbox only for chat with contact_email`.
  - `DeliveryStatus shows Retry only when failed and permitted`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them, plus `npm run lint` and `npm run build`.** Expected: PASS.
- [ ] **Step 5: Run the task's tests and record the results in the report.**

---

### Task 11: GreenMail profile and the end-to-end email test

**Files:**
- Modify:
  - `infra/docker-compose.yml`
  - `Makefile` (`up-email`, `stop-email`, e2e notes)
  - `.env.example` (`SUPPORT_MAIL_INBOUND=imap`, `SUPPORT_MAIL_OUTBOUND=smtp`, commented)
- Create:
  - `apps/web/tests/e2e/mail_tool.py`
  - `apps/web/tests/e2e/email.spec.js`
  - helpers in `apps/web/tests/e2e/helpers.js` (`sendCustomerEmail`, `fetchMailbox`, `connectMailbox`)
- Test: `email.spec.js`

**Interfaces:**
- **Compose service `greenmail`:**
  - `image: greenmail/standalone:2.1.3`. Pin an exact 2.1.x tag that is at least two weeks old; verify it on Docker Hub before writing it.
  - `profiles: ["email"]`.
  - `ports: ["3025:3025", "3143:3143"]`.
  - `mem_limit: 256m`.
  - `JAVA_OPTS: "-Xms32m -Xmx128m -XX:MaxMetaspaceSize=64m -XX:+UseSerialGC"`.
  - `GREENMAIL_OPTS: "-Dgreenmail.setup.test.smtp -Dgreenmail.setup.test.imap -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.users=support:support-dev-pass@support.test"`.
  - A healthcheck on the IMAP port.
  - The comment block notes:
    - memory use is about 150 MB;
    - recipients are auto-created with their address as both login and password (verify this against the GreenMail docs for the pinned version);
    - `make stop-email`.
- **Makefile:**
  - `up-email: docker compose -f infra/docker-compose.yml --profile email up -d greenmail`.
  - `stop-email: … stop greenmail`.
- **`mail_tool.py`** (stdlib only, run as `uv run python apps/web/tests/e2e/mail_tool.py`):
  - `send --from --to --subject --body [--in-reply-to ID] [--references ID ...]`: SMTP `localhost:3025`; prints the generated Message-ID.
  - `fetch --user --password`: IMAP `localhost:3143`; prints a JSON list of `{message_id, in_reply_to, references, subject, from}`.
- **Spec flow** (`--workers=1`). The API runs with `SUPPORT_JOB_QUEUE=inline`, or with celery plus `make dev-worker`.
  1. Sign up, set the org always-open, and set the owner online.
  2. Settings → Email: IMAP `localhost:3143` `none`, SMTP `localhost:3025` `none`, user `support` with password `support-dev-pass`, address `support@support.test`. Test connection shows both OK; then Save.
  3. `send` a customer email from `cust-<suffix>@example.test` with subject `Order <suffix> missing`, then click **Check now**. Poll the inbox until a conversation with that subject appears, and open it.
  4. `send` a second email with `--in-reply-to` the first id. Check now. The same conversation now shows 2 customer messages, and the list still holds 1 conversation for this subject.
  5. The agent replies "We are on it". The delivery status reaches "Sent".
  6. `fetch` as the customer, with `expect.poll`. A message with `Subject: Re: Order <suffix> missing` has `in_reply_to` equal to the second email's id, and `references` containing both customer ids.

- [ ] **Step 1: Write `email.spec.js`, `mail_tool.py` and the helpers.**
- [ ] **Step 2: Run without GreenMail.** Run `cd apps/web && npx playwright test tests/e2e/email.spec.js --workers=1`. Expected: FAIL at Test connection.
- [ ] **Step 3: Add the compose service and the Makefile targets.** Run `make up-email`, then `docker stats --no-stream support-greenmail-1`. Expected: memory under 256 MiB.
- [ ] **Step 4: Run the spec twice.** Expected: PASS both times. Then run `make e2e`; all specs, `--workers=1`. Expected: PASS.
- [ ] **Step 5: Run the task's tests and record the results in the report**, including the GreenMail memory reading. Leave GreenMail running or stopped as the controller directs; never touch other containers.

---

## Self-review notes

- **Spec coverage:**

  | Requirement | Task |
  |---|---|
  | IMAP fetch | 6 |
  | SMTP replies that thread | 7 |
  | No bot on email | 5 |
  | Per-org mailbox and secrets | 1, 2 |
  | `manage channels` permission | 2, 9 |
  | Onboarding optional step | 9 |
  | Unified customer record | 5 |
  | Follow-up ticket by email | 7, 10 |
  | Integration errors (email sync) | 1, 6, 7, 9 |
  | Analytics by channel | 5 |
  | Observability without content | 6, 7 |
  | Tenant isolation | 2, 4, 8 |
  | E2E | 11 |

- **Types used across tasks:**
  - `MailServer`, `servers()`, `ParsedEmail`, `SanitizedHtml`, `StoredAttachment`, `EmailMeta`, `IngestResult`, `SyncOutcome`.
  - `route_to_team(..., announce=)`.
  - `ConversationService.create(..., channel=, subject=)`.
  - The `email.status` event, used the same way in Tasks 7 and 10.
