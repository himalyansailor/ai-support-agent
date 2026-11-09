# Phase 7a — Voice Channel (Phone + Browser Calls, Chained Voice) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Plan 7a of 8.** Builds on Phases 1–6 (`docs/superpowers/plans/2026-10-26-phase-6-email-channel.md`). Next: 7b (speech-to-speech mode, live transfer to agents), then 8 (billing).

**Goal:** Customers can phone a business, or call from the website widget, and talk to the same bot that answers chat. When the bot hands off, the caller is promised a call back and the team sees a callback ticket in the inbox.

- **Numbers.** An organisation either buys a number on the platform's Twilio account (one Twilio subaccount per organisation), or connects its own Twilio account and picks numbers from it.
- **Phone calls.** A call to a connected number is answered by the voice bot: Deepgram turns speech into text, the existing LangGraph bot answers, and OpenAI TTS speaks the reply. The caller can interrupt the bot.
- **Browser calls.** When enabled, the widget shows a call button. The call runs through the Twilio Voice JS SDK into the same voice bot.
- **Handoff.** In 7a there is no live transfer (that is 7b). Any escalation ends with a spoken promise of a call back, and the conversation enters the team inbox as a callback ticket.
- **Records.** Every caller utterance and bot reply is stored as conversation messages. Call details (duration, how it ended) and, when the organisation turns recording on, the recording are shown in the inbox.

**Architecture:**

- **New package `supportcore/voice/`** with three ports, each with a real adapter and a test-only fake:
  - `Telephony` (Twilio): accounts, numbers, browser-calling setup, access tokens, webhook signatures, recordings.
  - `SpeechToText` (Deepgram streaming over WebSocket).
  - `TextToSpeech` (OpenAI TTS, streamed, converted to Twilio's 8 kHz µ-law).
- **New service `apps/voice`** (FastAPI, port 8002), sharing `packages/core`. It owns everything Twilio talks to: the voice webhooks (TwiML), the status and recording callbacks, and the Media Streams WebSocket. Each call runs a `VoiceSession`. The main API (`apps/api`) owns settings, numbers, the inbox and the widget's call token.
- **One bot.** A voice turn goes through the existing `TurnRunner` and bot graph, with a new `channel="voice"` on the turn. A sink receives the streamed reply, splits it into sentences and feeds them to TTS as they arrive.
- **Session rule.** As everywhere: no DB session is open across a Twilio, Deepgram, OpenAI, storage or LLM call. Each DB step in a call is its own short unit of work.

**Tech Stack (owner-decided 2026-10-27):** Deepgram streaming STT; OpenAI TTS (`gpt-4o-mini-tts`); OpenAI Realtime is reserved for 7b; Twilio Voice JS SDK for browser calls; a new `apps/voice` service; cloudflared quick tunnel as an opt-in compose profile for dev (tests use fakes); recordings copied into our `FileStorage` and then deleted from Twilio.

**Libraries this plan adds (flagged for owner review with the plan):**

- `twilio` (official Python helper library): REST client, `AccessToken`/`VoiceGrant`, `RequestValidator`. Its blocking calls run in `asyncio.to_thread`.
- `websockets` (already in `uv.lock`, 17.x): Deepgram's streaming socket. The Deepgram SDK is not used.
- `openai` (already locked, via langchain-openai): TTS streaming.
- `numpy` (already locked): µ-law encoding and resampling. Python 3.13 has no `audioop`.
- `@twilio/voice-sdk` in `apps/web`, loaded with dynamic `import()` only when a call starts.
- `cloudflare/cloudflared` image, pinned, in compose profile `tunnel`.

**Spec:** `docs/superpowers/specs/2026-10-24-ai-support-platform-design.md`:

- §3: STT / TTS / telephony rows of the swappable components
- §4.3: Voice (phone + browser, chained mode, handoff, no agent → callback, transcripts, recordings, number ownership)
- §4.3: Chat widget (call button) and the unified customer record (match on phone)
- §4.6: callback tickets in the inbox with a "call back" action
- §4.7: integration errors (Twilio), traces per conversation
- §5: browser calls via the provider's SDK; data retention for recordings; public endpoints via a tunnel locally; languages declared per adapter
- §6: phase 7

## Assumptions I made

Owner-level calls the spec leaves open. Each is cheap to change later.

1. **7a has no live transfer.** Every voice escalation becomes a callback ticket, even when an agent is online. `route_to_team` still assigns an agent, so the ticket lands with someone. 7b adds the live transfer.
2. **"Call back" in 7a is a `tel:` link plus a "Mark called back" button.** Placing the call from the platform (browser or agent phone) comes with 7b.
3. **Voice `offer_human` becomes `escalate`.** A phone caller cannot press the "talk to a person" button the chat shows, so on `channel="voice"` the risk node upgrades `offer_human` to `escalate`.
4. **Knowledge for a number comes from a widget.** Each phone number links to one of the organisation's widgets, and calls use that widget's knowledge bases (the same `widget_id` a chat turn uses). Browser calls use the widget they come from.
5. **Recording is off by default.** When an organisation turns it on, the greeting adds "This call may be recorded." Recordings are kept 90 days by default (1–3650, per organisation) and then deleted by a daily job.
6. **Calls last at most 30 minutes by default** (1–120, per organisation). At the limit the bot says so, hangs up and opens a callback ticket.
7. **Silence.** 10 s after the bot finishes speaking with no caller speech, it asks "Are you still there?". 10 s later it says goodbye and hangs up. No callback ticket is opened for silence.
8. **A browser-call conversation can continue as text.** It is linked to the widget customer and shows in the widget's history. After the call, an agent's reply reaches the widget like a chat reply. A phone-call conversation cannot be replied to in text: the composer is replaced by the callback actions (notes still work).
9. **When voice is not ready** (feature off, keys missing, credentials rejected, or this voice process at its call limit), the call hears a short "we'll call you back" message through Twilio's own `<Say>` and a callback ticket is opened when the caller's number is known.
10. **When our TTS fails mid-call,** the stream ends with `end_reason="error"`, and the `<Connect action>` callback answers with a Twilio `<Say>` of the trouble message plus `<Hangup/>`. The caller is never left in silence.
11. **Platform-owned numbers need platform Twilio credentials** (`SUPPORT_TWILIO_ACCOUNT_SID` / `SUPPORT_TWILIO_AUTH_TOKEN`). Without them the "Buy a number" option is hidden and only bring-your-own works. Buying a number shows its monthly price from Twilio, and the owner must confirm.
12. **Telephony cost metering is Phase 8.** 7a records `call_started` / `call_ended` analytics events with duration, STT seconds and TTS characters; Phase 8 turns these into billable usage.
13. **The quick-tunnel URL changes each run.** Settings shows "Webhooks out of date" when a number's stored webhook URL differs from the current `SUPPORT_VOICE_PUBLIC_URL`, and a **Sync webhooks** button re-points them.
14. **One voice reply is at most 1 200 spoken characters**, cut at a sentence end. The voice prompt asks for at most three short sentences.
15. **Transcript retention is not part of 7a.** Transcripts are ordinary conversation messages; a per-organisation retention setting for all conversations belongs with a later data-retention task. Only recordings get retention here.

## Global Constraints

- **Still in force:** every Phase 1–6 global constraint. In particular:
  - No git operations.
  - Cross-tenant access returns 404; every tenant query goes through `TenantScopedRepository`. The only unscoped lookups are by a public identifier (called number `e164`, Twilio `AccountSid`), exactly like the widget's `public_key`. Each such lookup carries a comment saying so.
  - Routes use `SessionDep`; `DOMAIN_ERRORS` codes; the adapter layering rule (`tests/test_layering.py`).
  - Secrets are encrypted at rest and never returned; publishing happens only after commit.
  - No DB session or transaction is open across a Twilio, Deepgram, OpenAI (TTS or LLM), storage, Prometheus or Langfuse call.
  - Fakes are registered only when `env == "test"`.
  - `organisation_id` is on every log line, metric (where the metric already has it) and span.
- **Never in logs, spans, metrics or integration-error rows:** caller or customer phone numbers, transcripts or any spoken text, audio bytes, Twilio auth tokens, API key secrets, access tokens, stream tokens, call tickets, recording URLs. Allowed: ids, Twilio SIDs (`CA…`, `PN…`, `RE…`, `AC…`), counts, durations, byte sizes, reason codes, HTTP status codes, exception class names.
- **Permissions:**

  | Action | Permission |
  |---|---|
  | Voice settings, Twilio account, numbers (search, buy, connect, release, sync webhooks) | `channels.manage` |
  | See calls, transcripts and recordings in the inbox | inbox view (`INBOX_VIEW_PERMISSIONS`) |
  | Mark a callback done | `queries.answer` |

- **Secrets:**
  - Twilio auth tokens and API key secrets are stored only as Fernet ciphertext; the API returns `auth_token_last4` and `browser_calling_ready: bool`.
  - STT/TTS keys are ordinary `ProviderCredential` rows: provider `deepgram` (new) and provider `openai` (existing).
  - Every dataclass that carries a decrypted value marks the field `repr=False`.
- **Settings (`config.py`):**

  | Setting | Default | Notes |
  |---|---|---|
  | `telephony` | `"twilio"` | `"fake"` exists only when env=test |
  | `twilio_account_sid` | `None` | the platform's master account; unset hides "Buy a number" |
  | `twilio_auth_token` | `None` (SecretStr) | |
  | `voice_public_url` | `None` | the `https://` origin Twilio reaches `apps/voice` at; prod refuses `http://` |
  | `voice_max_calls` | `50` | concurrent sessions per voice process |
  | `voice_stream_token_ttl_seconds` | `60` | |

- **Twilio endpoints in `apps/voice`** (all `POST` form posts from Twilio, signature-checked against `voice_public_url + path + query`, except the WebSocket):

  | Path | Purpose |
  |---|---|
  | `/twilio/voice/inbound` | a call to a connected number |
  | `/twilio/voice/browser` | the TwiML App's voice URL (widget calls) |
  | `/twilio/voice/after-stream?call_id=` | `<Connect action>`: what to play when the stream ends |
  | `/twilio/voice/status` | call status callback |
  | `/twilio/recording/status?call_id=` | recording status callback |
  | `WS /twilio/media` | Media Streams (stream token in the `start` event's `customParameters.token`) |

  A failed signature returns 403 with an empty body and logs `twilio_signature_invalid` with the path only.
- **Twilio Media Streams protocol:** inbound events `connected`, `start`, `media`, `mark`, `stop`; outbound `media` (base64 µ-law 8 kHz mono, 160-byte / 20 ms frames), `mark` (`turn-{n}` / `goodbye`), `clear`. Closing the WebSocket ends the `<Connect>`; Twilio then requests the `action` URL.
- **Deepgram live request:** `wss://api.deepgram.com/v1/listen?model={model}&language={language}&encoding=mulaw&sample_rate=8000&channels=1&interim_results=true&endpointing=300&utterance_end_ms=1000&vad_events=true&smart_format=true&punctuate=true`, header `Authorization: Token {key}`, `{"type":"KeepAlive"}` every 5 s while no audio is sent, `{"type":"CloseStream"}` on close.
- **OpenAI TTS:** `audio.speech.with_streaming_response.create(model=, voice=, input=, response_format="pcm")` → 24 kHz 16-bit LE mono → resampled to 8 kHz → µ-law → 160-byte frames. 15 s timeout to the first byte.
- **Catalogs (`voice/catalog.py`):**
  - STT `deepgram`: `nova-3` (recommended), `nova-2`. Languages `{"en","es","fr","de","it","pt","nl","ja","ru","hi"}`.
  - TTS `openai`: `gpt-4o-mini-tts` (recommended), `tts-1`. Voices `alloy, ash, ballad, coral, echo, sage, shimmer, verse`; default `coral`. Languages: any.
  - Test-only `fake` STT and TTS (`requires_credential=False`), registered only when env=test.
  - The settings API only offers models whose languages include `Organisation.language`.
- **Voice session timing constants (`voice/session.py`):** `SILENCE_PROMPT_SECONDS = 10`, `SILENCE_HANGUP_SECONDS = 10`, `GOODBYE_MARK_TIMEOUT_SECONDS = 15`, `MAX_SPOKEN_CHARS = 1200`, `TURN_RETRY_SECONDS = 0.5` (lease busy), `TURN_LEASE_WAIT_SECONDS = 10`.
- **Statuses and reasons:**
  - `Call.source`: `phone` | `browser`.
  - `Call.status`: `ringing` → `in_progress` → `completed` | `failed` | `busy` | `no_answer` | `canceled`.
  - `Call.end_reason`: `caller_hangup` | `handoff_callback` | `max_duration` | `silence` | `voice_unavailable` | `capacity` | `error`.
  - `Conversation.callback_status`: `NULL` | `requested` | `done`.
- **Integration errors:** `KINDS` gains `voice`. Providers `twilio`, `deepgram`, `openai`. Error types `auth_failed`, `connection_failed`, `timeout`, `rate_limited`, `stream_error`, `recording_failed`, `number_unavailable`.
- **Analytics `KINDS`** gain `call_started` (data `{source}`) and `call_ended` (data `{source, duration_seconds, end_reason, stt_seconds, tts_characters}`). Rollups ignore them in 7a.
- **New `DOMAIN_ERRORS`:**

  | Error | Status | Code |
  |---|---|---|
  | `InvalidVoiceSettingsError` | 422 | `invalid_voice_settings` |
  | `TelephonyCredentialsError` | 422 | `telephony_credentials_invalid` |
  | `TelephonyNotConfiguredError` | 409 | `telephony_not_configured` |
  | `PhoneNumberUnavailableError` | 409 | `phone_number_unavailable` |
  | `VoiceNotAvailableError` | 409 | `voice_not_available` |

- **Strings.** `voice/strings.py`, keyed per language like `widget/strings.py`:
  - `voice_greeting`: "Hello, you've reached {organisation}. How can I help you today?"
  - `voice_recording_notice`: "This call may be recorded."
  - `voice_callback_phone`: "I'm passing this to our team. Someone will call you back on this number as soon as they can. Goodbye."
  - `voice_callback_browser`: "I'm passing this to our team. Someone will follow up with you in the chat. Goodbye."
  - `voice_still_there`: "Are you still there?"
  - `voice_goodbye`: "Thanks for calling. Goodbye."
  - `voice_max_duration`: "We've reached the time limit for this call. Someone from our team will follow up. Goodbye."
  - `voice_unavailable`: "Sorry, we can't take your call right now. Someone from our team will call you back. Goodbye."
  - `voice_trouble`: "Sorry, I'm having trouble right now. Someone from our team will call you back. Goodbye."
  - `voice_turn_failed`: "Sorry, I couldn't find an answer just now. Could you say that again?"

  Widget strings (`widget/strings.py`): `call_button` "Call us", `call_connecting` "Connecting…", `call_in_progress` "On a call", `call_end` "End call", `call_mute` "Mute", `call_unmute` "Unmute", `call_ended` "Call ended", `call_mic_denied` "Allow microphone access to call us.", `call_failed` "The call could not be connected."

## Review Focus

1. **The caller talks over the bot, or there is background noise.**
   - Expected: real words stop playback at once (Twilio `clear`, the TTS task cancelled) and are answered next. Noise with no words does not interrupt.
   - Tests: Task 8 `test_barge_in_clears_playback_and_cancels_tts`, `test_vad_event_without_words_does_not_barge_in`.
2. **A provider key is missing or is revoked mid-call** (Deepgram 401, OpenAI TTS 401/429).
   - Expected: the caller hears the trouble message via Twilio `<Say>`, a callback ticket opens, and a `voice` integration error is recorded. No stack trace reaches Twilio.
   - Tests: Task 8 `test_stt_auth_failure_ends_with_error_and_callback`, `test_tts_failure_ends_with_error_and_callback`; Task 7 `test_after_stream_says_trouble_after_error`.
3. **Forged or replayed Twilio traffic.** An unsigned or wrongly signed webhook, a reused stream token, a reused browser call ticket, a call to a number we do not know.
   - Expected: 403 or a closed socket, with no customer, conversation or call created.
   - Tests: Task 7 `test_inbound_rejects_bad_signature`, `test_unknown_number_hangs_up_without_records`, `test_browser_ticket_single_use`; Task 9 `test_stream_token_single_use`.
4. **The caller hangs up mid-turn, and Twilio's status callback races the socket close.**
   - Expected: the bot turn still persists, and the call is finalised exactly once (duration, status, analytics event) whichever arrives first.
   - Tests: Task 9 `test_hangup_mid_turn_persists_turn_and_finalises_once`; Task 7 `test_status_callback_idempotent`.
5. **The dev tunnel URL changes.**
   - Expected: settings show "Webhooks out of date" for each affected number; Sync re-points only those.
   - Test: Task 5 `test_sync_webhooks_updates_changed_urls_only`.

---

## File Structure

```
infra/docker-compose.yml               + cloudflared (profile "tunnel", pinned, mem_limit 64m)
Makefile                               + dev-voice, up-tunnel, stop-tunnel, tunnel-url; test paths
pyproject.toml                         + apps/voice workspace member, test path
packages/core/pyproject.toml           + twilio
packages/core/src/supportcore/
  voice/__init__.py
  voice/catalog.py                     STT/TTS model specs, languages
  voice/strings.py
  voice/audio.py                       mulaw encode/decode, resample_24k_to_8k, frames
  voice/speech_text.py                 SentenceSplitter, to_speech_text
  voice/telephony/ports.py             Telephony, TelephonyAccountRef, AvailableNumber, OwnedNumber, ...
  voice/telephony/signature.py         twilio_signature(), is_valid_signature()
  voice/telephony/twilio_telephony.py  TwilioTelephony (adapter)
  voice/telephony/fake_telephony.py    FakeTelephony (test only)
  voice/stt/ports.py, deepgram_stt.py, fake_stt.py
  voice/tts/ports.py, openai_tts.py, fake_tts.py
  voice/tokens.py                      stream tokens, browser call tickets
  voice/settings_service.py            VoiceSettingsService (+ readiness)
  voice/numbers_service.py             TelephonyService (accounts, numbers, webhooks)
  voice/calls.py                       CallService (start/finish calls, utterances, callback)
  voice/session.py                     VoiceSession (the per-call engine)
  voice/transport.py                   MediaTransport port + TwilioMediaTransport framing
  voice/recordings.py                  start/fetch/purge recording jobs
  voice/metrics.py, voice/events.py (audit)
  db/models/voice.py                   TelephonyAccount, PhoneNumber, VoiceSettings, Call, CallRecording
  db/models/conversation.py            + Customer.phone, Conversation.callback_status / callback_phone
  bot/state.py, bot/prompts.py, bot/nodes.py, bot/turns.py   channel on turns; answer_now
  ai/catalog.py, ai/validation.py      + deepgram credential provider
  analytics/events.py, analytics/integration_errors.py
  inbox/summary.py, inbox/service.py   call meta, callback filter
  knowledge/jobs.py                    dispatch: start_call_recording, fetch_call_recording, purge_call_recordings
  config.py, wiring.py                 telephony / stt / tts registries
apps/api/src/supportapi/
  routers/voice_channel.py, schemas/voice_channel.py
  routers/inbox.py                     recording route, callback done
  routers/widget_public.py             config.voice_call_enabled, POST /calls/token
  errors.py, main.py
apps/api/migrations/versions/0019_voice_channel.py
apps/voice/                            pyproject.toml, src/supportvoice/{main.py,container.py,twilio_routes.py,media_ws.py,twiml.py}, tests/
apps/worker/src/supportworker/celery_app.py   + purge-call-recordings beat (daily)
apps/widget/src/loader.js              iframe allow="microphone"
apps/web/
  app/(dashboard)/orgs/[orgId]/settings/voice/page.jsx
  components/voice/{twilio-account-form.jsx,number-list.jsx,buy-number-dialog.jsx,voice-ai-form.jsx,call-settings-form.jsx}
  components/onboarding/voice-step.jsx, lib/onboarding-steps.js
  components/inbox/{call-card.jsx,recording-player.jsx,callback-actions.jsx}, conversation-pane, conversation-list, composer, customer-panel, view-list
  components/embed/{call-button.jsx,call-panel.jsx}, lib/voice-call.js, lib/voice.js
  tests/unit/voice.test.js, tests/unit/voice-call.test.js, tests/e2e/voice.spec.js, tests/e2e/call_tool.py
```

---

### Task 1: Voice data model, ports, fakes, settings and wiring

**Files:**
- Create: `db/models/voice.py`, `voice/__init__.py`, `voice/catalog.py`, `voice/strings.py`, `voice/telephony/ports.py`, `voice/telephony/signature.py`, `voice/telephony/fake_telephony.py`, `voice/stt/ports.py`, `voice/stt/fake_stt.py`, `voice/tts/ports.py`, `voice/tts/fake_tts.py`, `apps/api/migrations/versions/0019_voice_channel.py` (the only migration of this phase)
- Modify: `db/models/conversation.py`, `db/models/__init__.py`, `config.py`, `wiring.py`, `analytics/events.py` (`KINDS`), `analytics/integration_errors.py` (`KINDS`), `widget/strings.py`, `packages/core/pyproject.toml` (`twilio`; `uv lock`), `tests/test_layering.py` (`_ADAPTER_GLOBS` + `voice/telephony/twilio_telephony.py`, `voice/telephony/fake_telephony.py`, `voice/stt/deepgram_stt.py`, `voice/stt/fake_stt.py`, `voice/tts/openai_tts.py`, `voice/tts/fake_tts.py`)
- Test: `packages/core/tests/voice/test_models_and_ports.py`, `packages/core/tests/voice/test_signature.py`

**Interfaces:**
- Produces, in `db/models/voice.py`:
  - **`TelephonyAccount`** (Tenant, Timestamp), unique on `organisation_id`: `id`, `kind` (check `platform`/`byo`), `account_sid` String(34) unique, `auth_token_encrypted`, `auth_token_last4`, `api_key_sid` String(34) null, `api_key_secret_encrypted` null, `twiml_app_sid` String(34) null, `status` (check `active`/`auth_failed`/`error`, default `active`), `last_error_type`, `last_error_at`. `__repr__` without ciphertext.
  - **`PhoneNumber`** (Tenant, Timestamp): `id`, `telephony_account_id` (FK CASCADE), `e164` String(16) unique, `provider_sid` String(34), `ownership` (check `platform`/`byo`), `label` String(100) null, `widget_id` (FK widgets SET NULL), `enabled` (default true), `webhook_url` String(2048) null (what we last configured on Twilio). Index `organisation_id`.
  - **`VoiceSettings`** (Tenant, Timestamp), `organisation_id` is the PK: `phone_enabled` (false), `browser_calls_enabled` (false), `mode` (check `chained`/`speech_to_speech`, default `chained`), `stt_provider`, `stt_model`, `stt_credential_id` (FK provider_credentials SET NULL), `tts_provider`, `tts_model`, `tts_voice`, `tts_credential_id` (FK SET NULL), `record_calls` (false), `recording_retention_days` (90, check 1–3650), `max_call_minutes` (30, check 1–120), `browser_widget_id` (FK widgets SET NULL; the widget whose call button is enabled; NULL = all of the org's widgets).
  - **`Call`** (Tenant): `id`, `conversation_id` (FK CASCADE, unique), `phone_number_id` (FK SET NULL), `source`, `direction` (`inbound`), `provider_call_sid` String(34) unique, `caller_e164` String(16) null, `ticket_jti` String(64) unique null, `status`, `end_reason` null, `stream_connected_at`, `started_at`, `answered_at`, `ended_at`, `duration_seconds` int null, `stt_seconds` float default 0, `tts_characters` int default 0, `recording_sid` String(34) null, `finalised` bool default false, `created_at`. Checks for `source`, `status`, `end_reason` from Global Constraints.
  - **`CallRecording`** (Tenant): `id`, `call_id` (FK CASCADE, unique), `storage_key`, `content_type` (`audio/mpeg`), `size_bytes`, `duration_seconds`, `delete_after` (DateTime tz), `created_at`.
- Changes to existing models: `Customer.phone: String(16) | None` with index `ix_customers_organisation_id_phone`; `Conversation.callback_status: String(16) | None` (check `requested`/`done`), `Conversation.callback_phone: String(16) | None`; index `ix_conversations_organisation_id_callback_status` partial `WHERE callback_status = 'requested'`.
- **`voice/telephony/ports.py`** (exact shapes):

```python
@dataclass(frozen=True)
class TelephonyAccountRef:
    account_sid: str
    auth_token: str = field(repr=False)

@dataclass(frozen=True)
class BrowserCalling:
    api_key_sid: str
    api_key_secret: str = field(repr=False)
    twiml_app_sid: str

@dataclass(frozen=True)
class AvailableNumber:
    e164: str
    locality: str | None
    region: str | None
    monthly_price: str | None   # as Twilio reports it, e.g. "1.15"
    price_unit: str | None      # e.g. "USD"

@dataclass(frozen=True)
class OwnedNumber:
    provider_sid: str
    e164: str
    voice_url: str | None

@dataclass(frozen=True)
class NumberWebhooks:
    voice_url: str
    status_callback: str

class TelephonyAuthError(Exception): ...         # credentials rejected (Twilio 401/20003)
class TelephonyError(Exception):                  # anything else; .error_type is one of the voice error types
    def __init__(self, error_type: str, status: int | None = None) -> None: ...

class Telephony(Protocol):
    async def verify(self, account: TelephonyAccountRef) -> None: ...
    async def create_subaccount(self, master: TelephonyAccountRef, friendly_name: str) -> TelephonyAccountRef: ...
    async def search_numbers(self, account: TelephonyAccountRef, country: str, area_code: str | None, limit: int = 20) -> list[AvailableNumber]: ...
    async def buy_number(self, account: TelephonyAccountRef, e164: str, webhooks: NumberWebhooks) -> OwnedNumber: ...
    async def list_numbers(self, account: TelephonyAccountRef) -> list[OwnedNumber]: ...
    async def configure_number(self, account: TelephonyAccountRef, provider_sid: str, webhooks: NumberWebhooks | None) -> None: ...  # None clears our URLs
    async def release_number(self, account: TelephonyAccountRef, provider_sid: str) -> None: ...
    async def ensure_browser_calling(self, account: TelephonyAccountRef, voice_url: str, existing: BrowserCalling | None) -> BrowserCalling: ...
    def browser_access_token(self, account_sid: str, calling: BrowserCalling, identity: str, ttl_seconds: int) -> str: ...
    def is_valid_signature(self, auth_token: str, url: str, params: Mapping[str, str], signature: str) -> bool: ...
    async def start_recording(self, account: TelephonyAccountRef, call_sid: str, status_callback: str) -> str: ...  # recording sid
    async def download_recording(self, account: TelephonyAccountRef, recording_sid: str, max_bytes: int) -> bytes: ...
    async def delete_recording(self, account: TelephonyAccountRef, recording_sid: str) -> None: ...
```

- **`voice/telephony/signature.py`:** `twilio_signature(auth_token: str, url: str, params: Mapping[str, str]) -> str` (HMAC-SHA1 of `url` + each `key+value` in sorted key order, base64) and `is_valid_signature(...) -> bool` (constant-time compare). Both adapters use it; the e2e call tool reimplements it in stdlib.
- **`voice/stt/ports.py`:**

```python
@dataclass(frozen=True)
class SttConfig:
    provider: str
    model: str
    language: str
    api_key: str | None = field(repr=False, default=None)

@dataclass(frozen=True)
class Transcript:          # is_final: this text is settled; speech_final: the speaker paused (end of utterance)
    text: str = field(repr=False)
    is_final: bool
    speech_final: bool

@dataclass(frozen=True)
class SpeechStarted: ...

@dataclass(frozen=True)
class UtteranceEnd: ...

SttEvent = Transcript | SpeechStarted | UtteranceEnd

class SttAuthError(Exception): ...
class SttError(Exception): ...            # .error_type

class SttStream(Protocol):
    async def send(self, mulaw_8k: bytes) -> None: ...
    def events(self) -> AsyncIterator[SttEvent]: ...   # ends when the stream closes; raises SttError / SttAuthError
    async def aclose(self) -> None: ...
    @property
    def audio_seconds(self) -> float: ...              # audio sent so far, for metering

class SpeechToText(Protocol):
    async def open(self, config: SttConfig) -> SttStream: ...
```

- **`voice/tts/ports.py`:**

```python
@dataclass(frozen=True)
class TtsConfig:
    provider: str
    model: str
    voice: str
    language: str
    api_key: str | None = field(repr=False, default=None)

class TtsAuthError(Exception): ...
class TtsError(Exception): ...            # .error_type

class TextToSpeech(Protocol):
    def synthesize(self, text: str, config: TtsConfig) -> AsyncIterator[bytes]: ...  # 8 kHz µ-law, 160-byte frames
```

- **Fakes (test only):**
  - `FakeTelephony`: in-memory accounts and numbers. `verify` accepts any `AC` + 32 hex SID whose token is not `"bad-token"`. `list_numbers` returns `+15005550006` (`PN` + 32 × `"0"`) for any account. Records every call it receives in `.calls` for assertions. `browser_access_token` returns `"fake-token:{identity}"`. Signatures use the real `signature.py`.
  - `FakeSpeechToText`: a received frame whose bytes start with `b"FAKE-STT:"` makes the stream emit `SpeechStarted`, then `Transcript(text, True, True)` with the UTF-8 text after the prefix. A frame `b"FAKE-NOISE"` emits only `SpeechStarted`. A frame `b"FAKE-STT-AUTHFAIL"` raises `SttAuthError` from `events()`. Other frames are counted as audio (`audio_seconds` += 0.02 each).
  - `FakeTextToSpeech`: yields `b"FAKE-TTS:" + text.encode()` split into 160-byte frames (last one padded with `b"\xff"`). Text `"FAIL"` anywhere raises `TtsAuthError`.
- **`wiring.py`:** `telephony_registry` (`twilio`; `fake` per build only in test), `stt_registry` (`deepgram`; `fake` in test), `tts_registry` (`openai`; `fake` in test). `Adapters` gains `telephony: Telephony`, `speech_to_text: SttFactory`, `text_to_speech: TtsFactory`, where the factories are `create(provider: str) -> SpeechToText / TextToSpeech` over the registry (unknown provider → `ValueError`).
- **`voice/catalog.py`:** `SttModelSpec(provider, model, label, languages, recommended, requires_credential)`, `TtsModelSpec(..., voices, default_voice)`, `stt_models(language, include_fake) -> list`, `tts_models(language, include_fake) -> list`, `get_stt_spec(provider, model, include_fake)`, `get_tts_spec(...)` (unknown → `ValueError`). `credential_provider_for(provider) -> str | None` (`deepgram` → `deepgram`, `openai` → `openai`, `fake` → None).

- [ ] **Step 1: Write the failing tests.** In `test_models_and_ports.py`: `test_voice_tables_round_trip` (create one of each model in an org, read back); `test_phone_number_e164_unique_across_orgs` (IntegrityError); `test_call_provider_sid_unique`; `test_conversation_callback_status_check` (`"maybe"` → IntegrityError); `test_telephony_account_repr_hides_token`; `test_fakes_only_in_test_env` (`build_adapters` with `env="dev"`, `telephony="fake"` → `ValueError`); `test_fake_stt_emits_transcript_for_marker_frame`; `test_fake_tts_frames_are_160_bytes`; `test_stt_models_filtered_by_language` (`"en"` includes `nova-3`; `"zz"` returns `[]`). In `test_signature.py`: `test_signature_matches_twilio_reference` using Twilio's documented example (auth token `12345`, URL `https://mycompany.com/myapp.php?foo=1&bar=2`, params `CallSid=CA1234567890ABCDE, Caller=+12349013030, Digits=1234, From=+12349013030, To=+18005551212`): our result equals `twilio.request_validator.RequestValidator("12345").compute_signature(url, params)` (the SDK is the oracle) and the documented `"0/KCTR6DLpKmkAf8muzZqo1nDgQ="`; `test_signature_rejects_tampered_param`.
- [ ] **Step 2: Run** `uv run pytest packages/core/tests/voice -q` — FAIL (modules missing).
- [ ] **Step 3: Implement** the models, migration `0019_voice_channel` (all tables, columns, checks and indexes above; downgrade drops them), ports, signature helper, fakes, catalog, strings, settings, `KINDS` additions and wiring. `Settings` refuses a `voice_public_url` that is not `https://` when `env=prod`.
- [ ] **Step 4: Run** `uv run pytest packages/core/tests/voice tests/test_layering.py -q` and `make migrate` against the dev DB, then `alembic downgrade -1 && alembic upgrade head` — PASS.
- [ ] **Step 5: Run the full core suite** `uv run pytest packages/core -q` — PASS (no regressions from the new `KINDS` or columns).

### Task 2: Audio and speech-text helpers

**Files:**
- Create: `voice/audio.py`, `voice/speech_text.py`
- Test: `packages/core/tests/voice/test_audio.py`, `packages/core/tests/voice/test_speech_text.py`

**Interfaces:**
- Produces, `voice/audio.py`: `FRAME_BYTES = 160`; `pcm16_to_mulaw(pcm: bytes) -> bytes`; `mulaw_to_pcm16(data: bytes) -> bytes` (ITU-T G.711 µ-law, bias 0x84, clip 32635); `class Resampler24kTo8k` with `feed(pcm16_24k: bytes) -> bytes` and `flush() -> bytes` (keeps the odd byte and the filter history across calls: a 31-tap windowed-sinc low-pass at 3.4 kHz, then every 3rd sample; numpy); `class Framer` with `feed(data: bytes) -> list[bytes]` (whole 160-byte frames) and `flush() -> bytes | None` (the remainder padded with `0xFF`, µ-law silence).
- Produces, `voice/speech_text.py`: `to_speech_text(markdown: str) -> str` (removes `**`, `__`, `*`/`_` emphasis, `#` headings, backticks and code fences; `[text](url)` → `text`; bare URLs removed; list markers become sentence breaks; collapses whitespace); `class SentenceSplitter` with `feed(delta: str) -> list[str]` (a sentence ends at `.`, `!` or `?` followed by whitespace, or at a newline; a run past 200 characters is cut at its last whitespace) and `flush() -> str | None`; `cap_spoken(text: str, limit: int = MAX_SPOKEN_CHARS) -> str` (cut at the last sentence end ≤ limit, else hard cut at whitespace).

- [ ] **Step 1: Write the failing tests:** `test_mulaw_round_trip_within_quantisation` (a 1 kHz sine at amplitude 8000: decode(encode(x)) within 2 % of each sample's magnitude + 16); `test_mulaw_known_values` (`pcm16_to_mulaw(b"\x00\x00") == b"\xff"`, full-scale positive → `b"\x80"`); `test_resampler_length_and_streaming_equivalence` (24 000 samples in one feed and in 7-byte chunks give the same 8 000-sample output); `test_resampler_attenuates_above_4khz` (a 6 kHz tone loses ≥ 20 dB); `test_framer_pads_last_frame`; `test_to_speech_text_strips_markdown_and_links`; `test_splitter_streams_sentences` (`["Hi there. How", " are you? Fine"]` → `["Hi there.", "How are you?"]`, flush → `"Fine"`); `test_splitter_does_not_split_decimals` (`"It costs 4.50 dollars."` stays one sentence); `test_cap_spoken_cuts_at_sentence`.
- [ ] **Step 2: Run** `uv run pytest packages/core/tests/voice/test_audio.py packages/core/tests/voice/test_speech_text.py -q` — FAIL.
- [ ] **Step 3: Implement** both modules (pure, numpy only).
- [ ] **Step 4: Run** the same tests — PASS.

### Task 3: Deepgram STT and OpenAI TTS adapters, Deepgram credentials

**Files:**
- Create: `voice/stt/deepgram_stt.py`, `voice/tts/openai_tts.py`
- Modify: `ai/catalog.py` (`CREDENTIAL_PROVIDERS` + `deepgram`; not offered as a chat or embedding provider), `ai/validation.py` (`deepgram` check), `wiring.py` (register the real adapters)
- Test: `packages/core/tests/voice/test_deepgram_stt.py`, `packages/core/tests/voice/test_openai_tts.py`, `packages/core/tests/ai/test_validation.py` (additions)

**Interfaces:**
- Consumes: Task 1 ports; Task 2 `Resampler24kTo8k`, `pcm16_to_mulaw`, `Framer`.
- Produces: `DeepgramSpeechToText(settings, *, url: str = DEEPGRAM_LIVE_URL)` and `OpenAITextToSpeech(settings, *, client_factory=None)`.
  - Deepgram: opens the socket with the Global Constraints query and header, 10 s connect timeout. A 401/403 handshake → `SttAuthError`; other handshake failures → `SttError("connection_failed")`; a mid-stream close other than after `CloseStream` → `SttError("stream_error")`. Messages map: `Results` → `Transcript(channel.alternatives[0].transcript, is_final, speech_final)` (empty text is dropped unless `speech_final`); `SpeechStarted` → `SpeechStarted()`; `UtteranceEnd` → `UtteranceEnd()`; `Metadata` ignored. Sends `KeepAlive` when no audio was sent for 5 s.
  - OpenAI TTS: `AsyncOpenAI(api_key=, timeout=15)`; streams PCM chunks → resample → µ-law → `Framer`; yields frames as they are ready. `AuthenticationError` → `TtsAuthError`; `RateLimitError` → `TtsError("rate_limited")`; timeouts → `TtsError("timeout")`; others → `TtsError("connection_failed")`. The input text is capped by the caller.
  - `ProviderCredentialValidator._check("deepgram")`: `GET https://api.deepgram.com/v1/projects` with `Authorization: Token {key}`, 10 s timeout; 200 → ok, 401/403 → `CredentialInvalidError("The provider rejected this API key")`, other → `CredentialInvalidError("Could not validate the API key with the provider")`.

- [ ] **Step 1: Write the failing tests.** Deepgram against a local `websockets.serve` fake server: `test_deepgram_sends_audio_and_maps_results`; `test_deepgram_query_and_auth_header` (server asserts `encoding=mulaw`, `sample_rate=8000`, `endpointing=300`, `utterance_end_ms=1000`, header `Token k`); `test_deepgram_401_raises_auth_error`; `test_deepgram_unexpected_close_raises_stream_error`; `test_deepgram_keepalive_when_idle` (patch the interval to 0.05 s); `test_deepgram_close_sends_closestream`. OpenAI TTS with a stub client: `test_tts_resamples_and_frames` (48 000 bytes of PCM in → 2 000 µ-law bytes out as 160-byte frames, last padded); `test_tts_auth_error_mapped`; `test_tts_rate_limit_mapped`. Validation: `test_deepgram_key_validated_via_projects_endpoint` (httpx `MockTransport`); `test_deepgram_not_a_chat_provider`.
- [ ] **Step 2: Run** `uv run pytest packages/core/tests/voice packages/core/tests/ai -q` — FAIL.
- [ ] **Step 3: Implement** both adapters and the validator branch. Neither logs transcript text, input text or keys.
- [ ] **Step 4: Run** the same tests — PASS.

### Task 4: Twilio telephony adapter

**Files:**
- Create: `voice/telephony/twilio_telephony.py`
- Modify: `wiring.py`
- Test: `packages/core/tests/voice/test_twilio_telephony.py`

**Interfaces:**
- Consumes: Task 1 `Telephony` port, `signature.py`.
- Produces: `TwilioTelephony(settings, *, http_client: twilio.http.HttpClient | None = None)`. Every SDK call runs in `asyncio.to_thread` with a 15 s timeout. Twilio error code 20003 or HTTP 401 → `TelephonyAuthError`; 21422/21452 (number not available) → `TelephonyError("number_unavailable", status)`; timeouts → `TelephonyError("timeout")`; other → `TelephonyError("connection_failed", status)`.
  - `buy_number` / `configure_number` set `voice_url`, `voice_method="POST"`, `status_callback`, `status_callback_method="POST"`; `configure_number(..., None)` sets both URLs to `""`.
  - `ensure_browser_calling` creates (or, when `existing` is given, updates the voice URL of) a TwiML Application named `support-browser-calls` with `voice_url`, and creates a Standard API key named `support-browser-calls` when none exists.
  - `browser_access_token` builds `AccessToken(account_sid, api_key_sid, api_key_secret, identity=identity, ttl=ttl_seconds)` with `VoiceGrant(outgoing_application_sid=twiml_app_sid, incoming_allow=False)`.
  - `start_recording` calls `calls(sid).recordings.create(recording_status_callback=..., recording_status_callback_event=["completed"], recording_channels="dual")`.
  - `download_recording` fetches `…/Recordings/{sid}.mp3` with the account's credentials through the guarded HTTP client, streaming, aborting past `max_bytes` (→ `TelephonyError("recording_failed")`).

- [ ] **Step 1: Write the failing tests** with a recording fake `HttpClient` (subclass of `twilio.http.HttpClient` returning canned `Response`s): `test_buy_number_sets_webhooks`; `test_configure_none_clears_urls`; `test_auth_error_mapped` (status 401, code 20003); `test_number_unavailable_mapped`; `test_ensure_browser_calling_creates_app_and_key_once` (second call with `existing` only updates the app); `test_access_token_has_voice_grant` (decode the JWT with the secret: `grants.voice.outgoing.application_sid` matches, `incoming.allow` false, `exp - iat == ttl`); `test_signature_uses_shared_helper`; `test_download_recording_aborts_past_max_bytes`.
- [ ] **Step 2: Run** `uv run pytest packages/core/tests/voice/test_twilio_telephony.py -q` — FAIL.
- [ ] **Step 3: Implement** the adapter.
- [ ] **Step 4: Run** the same tests — PASS.

### Task 5: Voice settings, Twilio account and numbers services + API

**Files:**
- Create: `voice/settings_service.py`, `voice/numbers_service.py`, `voice/events.py` (audit events `TelephonyConnected`, `TelephonyDisconnected`, `PhoneNumberAdded`, `PhoneNumberReleased`, `VoiceSettingsUpdated`), `apps/api/src/supportapi/routers/voice_channel.py`, `apps/api/src/supportapi/schemas/voice_channel.py`
- Modify: `apps/api/src/supportapi/errors.py`, `apps/api/src/supportapi/main.py`, `audit/subscribers.py`
- Test: `packages/core/tests/voice/test_settings_service.py`, `packages/core/tests/voice/test_numbers_service.py`, `apps/api/tests/test_voice_channel.py`

**Interfaces:**
- Consumes: Tasks 1, 3, 4; `CredentialService`, `SecretCipher`, `record_integration_error`.
- Produces:
  - `VoiceSettingsService(session, organisation_id, events, *, include_fake)`: `get() -> VoiceSettings` (lazy row), `update(actor, **fields) -> VoiceSettings` (validates models against the catalog and the org language, credential provider matches `credential_provider_for`, ranges; raises `InvalidVoiceSettingsError`), `readiness() -> VoiceReadiness`.
  - `VoiceReadiness` (frozen): `phone: bool`, `browser: bool`, `reasons: list[str]` from `{"voice_disabled", "stt_not_configured", "tts_not_configured", "telephony_not_connected", "telephony_auth_failed", "no_numbers", "browser_calling_not_set_up", "public_url_missing"}`.
  - `load_voice_setup(session, organisation_id, adapters, settings, events) -> VoiceSetup | None`: database only. `VoiceSetup` holds `stt: SttConfig`, `tts: TtsConfig`, `record_calls`, `max_call_seconds`, `organisation_name`, `language`, `telephony: TelephonyAccountRef` (decrypted, `repr=False`). None when not ready.
  - `TelephonyService(session, organisation_id, events, adapters, settings)`. Methods that call Twilio take no session across the call: they read and commit first, call Twilio, then open a new unit of work (the route uses the `BusinessToolService.test` pattern):
    - `connect_byo(account_sid, auth_token, actor)`: format checks (`^AC[0-9a-f]{32}$`, 32-char token), `verify`, `ensure_browser_calling`, stores encrypted → `TelephonyAccount`.
    - `platform_available() -> bool` (`settings.twilio_account_sid` and token set).
    - `search(country, area_code)` (platform: the org's subaccount, else the master; BYO: own account) → `list[AvailableNumber]`.
    - `buy(e164, widget_id, actor)`: platform only; creates the org's subaccount on first use (`friendly_name=f"support-org-{organisation_id}"`) and its browser calling; buys with this deployment's webhooks → `PhoneNumber(ownership="platform")`.
    - `list_account_numbers()` (BYO): Twilio's numbers with `connected: bool`.
    - `connect_number(provider_sid, widget_id, actor)` (BYO): configures webhooks → `PhoneNumber(ownership="byo")`.
    - `update_number(id, label, widget_id, enabled)`.
    - `release(id, actor)`: platform → `release_number`; BYO → `configure_number(..., None)` only if Twilio's current `voice_url` is ours; then deletes the row.
    - `sync_webhooks() -> int`: re-points every number whose `webhook_url` differs from the current URL, and refreshes the TwiML App URL; returns the count changed.
    - `disconnect(actor)`: BYO only, releases our webhooks from every number, deletes the account and numbers.
    - On `TelephonyAuthError` the account becomes `auth_failed` and a `voice`/`twilio`/`auth_failed` integration error is recorded; saving new credentials resets it.
  - Webhooks: `voice_url = {voice_public_url}/twilio/voice/inbound`, `status_callback = {voice_public_url}/twilio/voice/status`, browser `voice_url = {voice_public_url}/twilio/voice/browser`. Without `voice_public_url`, buy/connect/sync raise `TelephonyNotConfiguredError`.
- API under `/orgs/{org_id}/voice` (`channels.manage`), schemas never carry tokens or secrets:
  - `GET /settings`, `PUT /settings`, `GET /readiness`, `GET /catalog` (STT/TTS models and voices for the org language).
  - `GET /telephony` → `{kind, account_sid, auth_token_last4, status, last_error_type, browser_calling_ready, platform_available, public_url_set}` or `{connected: false, platform_available, public_url_set}`; `PUT /telephony` (BYO credentials); `DELETE /telephony`.
  - `GET /numbers` (with `webhooks_out_of_date: bool` per number), `GET /numbers/search?country=US&area_code=415`, `POST /numbers/buy {e164, widget_id}`, `GET /numbers/available` (BYO), `POST /numbers/connect {provider_sid, widget_id}`, `PATCH /numbers/{id}`, `DELETE /numbers/{id}`, `POST /numbers/sync-webhooks`.

- [ ] **Step 1: Write the failing tests** (fake telephony): `test_connect_byo_encrypts_token_and_sets_up_browser_calling`; `test_connect_byo_bad_token_is_422_and_nothing_stored`; `test_auth_failure_marks_account_and_records_error`; `test_buy_creates_subaccount_once`; `test_buy_hidden_without_platform_credentials` (409 `telephony_not_configured`); `test_connect_number_configures_webhooks`; `test_release_byo_leaves_foreign_webhooks`; `test_sync_webhooks_updates_changed_urls_only`; `test_settings_reject_model_for_other_language`; `test_settings_reject_credential_of_wrong_provider`; `test_readiness_reasons`; `test_cross_org_number_is_404`; `test_requires_channels_manage` (403 for an agent role); `test_responses_never_contain_secrets` (no auth token, API key secret or ciphertext anywhere in any response body); `test_no_session_open_during_twilio_calls` (the fake telephony asserts `get_current_session()` has no open transaction, as the Phase 6 mailbox test did).
- [ ] **Step 2: Run** `uv run pytest packages/core/tests/voice apps/api/tests/test_voice_channel.py -q` — FAIL.
- [ ] **Step 3: Implement** services, router, schemas, errors and audit subscribers.
- [ ] **Step 4: Run** the same tests — PASS.

### Task 6: The bot on voice turns

**Files:**
- Modify: `bot/state.py` (`TurnInput.channel: str = "chat"`, included in `__repr__`), `bot/prompts.py`, `bot/nodes.py`, `bot/turns.py`, `inbox/customers.py` (or the module that owns customer lookup)
- Test: `packages/core/tests/bot/test_voice_turns.py`

**Interfaces:**
- Produces:
  - `build_bot_messages(...)` adds the voice style instruction when `channel == "voice"`: "You are speaking with the customer on a phone call. Answer in at most three short sentences of plain spoken language. Do not use markdown, lists, links, emoji or citations markers." It lives with the other prompt texts, through the same language mechanism.
  - `BotNodes.assess_risk`: on `turn.channel == "voice"`, an `offer_human` decision becomes `escalate` with the same rule fields.
  - `BotNodes.handoff`: calls `route_to_team(..., announce=turn.channel != "voice")`.
  - `TurnRunner._turn` reads `conversation.channel` into `_Pending.channel` and passes it to `TurnInput`.
  - `TurnRunner.answer_now(organisation_id, conversation_id, sink: TurnSink | None = None) -> TurnResult | None`: takes the lease (retrying every `0.5` s for up to `10` s while another turn holds it), answers all pending messages in one turn with a sink that forwards to both the publishing sink and `sink`, persists like `_turn`, releases, and returns the result. Returns None when the conversation is not in `bot` status or has nothing pending. `_turn` is refactored to share this path and return its `TurnResult`.
  - `find_or_create_customer_by_phone(session, organisation_id, e164) -> Customer`: newest `last_seen_at` match on `(organisation_id, phone)`, else a new customer with `phone=e164`; sets `last_seen_at`.

- [ ] **Step 1: Write the failing tests** (scripted chat model, memory checkpointer): `test_voice_prompt_has_spoken_style`; `test_chat_prompt_unchanged`; `test_voice_offer_human_becomes_escalate`; `test_voice_handoff_posts_no_system_message`; `test_answer_now_returns_result_and_streams_to_both_sinks`; `test_answer_now_waits_for_busy_lease`; `test_answer_now_none_when_not_bot`; `test_find_or_create_customer_by_phone_reuses_match`. Existing `packages/core/tests/bot` and `apps/api/tests/test_widget_ws.py` keep passing.
- [ ] **Step 2: Run** `uv run pytest packages/core/tests/bot -q` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest packages/core/tests/bot apps/api/tests/test_widget_ws.py -q` — PASS.

### Task 7: `apps/voice` service: Twilio webhooks, tokens, calls

**Files:**
- Create: `apps/voice/pyproject.toml`, `apps/voice/src/supportvoice/{__init__.py,main.py,container.py,twilio_routes.py,twiml.py}`, `voice/tokens.py`, `voice/calls.py`, `voice/metrics.py`, `apps/voice/tests/{conftest.py,test_twilio_routes.py}`
- Modify: root `pyproject.toml` (workspace member, pytest paths), `Makefile` (`dev-voice` on port 8002, `test` includes `apps/voice/tests`), `infra/docker-compose.yml` (`cloudflared` service, profile `tunnel`, image `cloudflare/cloudflared` pinned to an exact release tag that `docker pull` confirms exists (never `latest`), `command: tunnel --no-autoupdate --url http://host.docker.internal:8002`, `extra_hosts: ["host.docker.internal:host-gateway"]`, `mem_limit: 64m`), `Makefile` (`up-tunnel`, `stop-tunnel`, `tunnel-url` that greps the `trycloudflare.com` URL from the container logs and prints the `SUPPORT_VOICE_PUBLIC_URL=` line to set), `.env.example` (voice settings, commented)
- Test: as above

**Interfaces:**
- Consumes: Tasks 1, 5 (`load_voice_setup`, readiness), 6 (`find_or_create_customer_by_phone`).
- Produces:
  - `voice/tokens.py`: `issue_stream_token(settings, call_id, organisation_id) -> str` and `verify_stream_token(settings, token) -> tuple[UUID, UUID]` (HS256 with key `sha256(jwt_secret + ":voice-stream")`, claims `call_id`, `org`, `purpose="voice_stream"`, `exp` = now + `voice_stream_token_ttl_seconds`; raises `InvalidVoiceTokenError`). `issue_call_ticket(settings, organisation_id, widget_id, customer_id) -> tuple[str, str]` (token, jti; purpose `voice_call_ticket`, 120 s) and `verify_call_ticket(settings, token) -> CallTicket`.
  - `voice/calls.py` `CallService(session, organisation_id, events, publisher)`:
    - `start_phone_call(number: PhoneNumber, call_sid, caller_e164) -> Call`: customer by phone, `Conversation(channel="voice", widget_id=number.widget_id, language=Organisation.language)`, `Call(source="phone", status="ringing", started_at=now)`, analytics `call_started`. A repeated `CallSid` returns the existing call (Twilio retries webhooks).
    - `start_browser_call(ticket: CallTicket, call_sid) -> Call`: the widget customer; `Call.ticket_jti` unique makes a ticket single use (`IntegrityError` → `InvalidVoiceTokenError`).
    - `mark_stream_connected(call_id) -> Call` (sets `stream_connected_at` and `in_progress`; a second connect raises `InvalidVoiceTokenError`).
    - `add_caller_utterance(call_id, text) -> Message` (customer message through `ConversationService.add_customer_message`, publisher upsert, message event after commit).
    - `request_callback(call_id, end_reason) -> None`: routes a `bot` conversation to the team with decision `{"action":"escalate","rule_key":"voice_callback","rule_name":"Voice callback","reason":None,"source":"voice"}` and `announce=False`; phone calls set `callback_status="requested"` and `callback_phone=caller_e164`; sets `Call.end_reason`.
    - `set_end_reason(call_id, reason)` (first one wins), `add_usage(call_id, stt_seconds, tts_characters)`.
    - `finalise(call_id, status, duration_seconds | None) -> bool`: idempotent through `Call.finalised` under a row lock; sets `ended_at`, `status`, `duration_seconds` (Twilio's, else from `answered_at`), `end_reason` default `caller_hangup`; ends a conversation still in `bot` via `ConversationService.end`; records `call_ended`. Returns False when already finalised.
  - `twiml.py`: `connect_stream(stream_url, token, action_url) -> str` (`<Response><Connect action="…"><Stream url="…"><Parameter name="token" value="…"/></Stream></Connect></Response>`), `say_and_hangup(text, language) -> str`, `hangup() -> str`. All values XML-escaped.
  - Routes (`twilio_routes.py`), each validating the signature first:
    - `/twilio/voice/inbound`: unscoped lookup of `PhoneNumber` by `To` (unknown or disabled number → `hangup()` TwiML, nothing stored), signature with the number's account token. Not ready / at `voice_max_calls` → `start_phone_call`, `request_callback(end_reason="voice_unavailable"|"capacity")`, `finalise` later by the status callback, TwiML `say_and_hangup(voice_unavailable)`. Ready → `start_phone_call` and `connect_stream(wss://…/twilio/media, token, action_url)`. Number webhooks carry no org secret in the URL.
    - `/twilio/voice/browser`: unscoped `TelephonyAccount` lookup by `AccountSid`; signature; `verify_call_ticket(params["ticket"])`; browser calling enabled for that widget; then like inbound with `start_browser_call`.
    - `/twilio/voice/after-stream`: `say_and_hangup(voice_trouble)` when the call's `end_reason == "error"`, else `hangup()`.
    - `/twilio/voice/status`: maps `CallStatus` (`completed`, `busy`, `failed`, `no-answer` → `no_answer`, `canceled`), `CallDuration` → `finalise`. Non-terminal statuses are ignored.
    - `/twilio/recording/status`: enqueues `fetch_call_recording` (Task 10) when `RecordingStatus == "completed"`.
  - `voice/metrics.py`: `voice_calls_total{source,end_reason}`, `voice_active_calls` gauge, `voice_response_latency_seconds` histogram (buckets 0.25…8 s), `voice_provider_errors_total{provider,error_type}`.
  - `main.py`: lifespan builds the container (settings, engine, adapters, `BotRuntime`, a `TurnRunner`), OTel with `service_name="support-voice"`, `/health` and `/ready`, `/metrics` (same token rule as the API).

- [ ] **Step 1: Write the failing tests** (`httpx.AsyncClient` on the ASGI app, fake telephony, signed with `twilio_signature`): `test_inbound_connects_stream_with_token`; `test_inbound_rejects_bad_signature` (403, nothing in `calls`/`customers`/`conversations`); `test_unknown_number_hangs_up_without_records`; `test_inbound_reuses_customer_by_phone`; `test_inbound_not_ready_says_unavailable_and_requests_callback`; `test_inbound_at_capacity_requests_callback`; `test_inbound_retry_same_callsid_is_idempotent`; `test_browser_call_uses_ticket_customer`; `test_browser_ticket_single_use`; `test_after_stream_says_trouble_after_error`; `test_after_stream_hangs_up_otherwise`; `test_status_callback_idempotent` (two `completed` posts → one `call_ended` event); `test_twiml_escapes_values`; `test_logs_never_contain_phone_numbers` (caplog/structlog capture over an inbound call has no `+1` digits of the caller).
- [ ] **Step 2: Run** `uv run pytest apps/voice/tests -q` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest apps/voice/tests -q` — PASS. Start `make dev-voice` and `curl localhost:8002/health` — `{"status":"ok"}`.

### Task 8: `VoiceSession` — the per-call engine

**Files:**
- Create: `voice/session.py`, `voice/transport.py`
- Test: `packages/core/tests/voice/test_session.py`

**Interfaces:**
- Consumes: Task 1 ports and fakes, Task 2 helpers, Task 6 `TurnRunner.answer_now`, Task 7 `CallService`, metrics.
- Produces:
  - `voice/transport.py`: `MediaTransport` protocol — `events() -> AsyncIterator[TransportEvent]` (`AudioIn(bytes)`, `MarkPlayed(name)`, `Stop()`), `send_audio(frame: bytes)`, `send_mark(name)`, `clear()`, `close()`. `FakeTransport` for tests (scripted inbound events, records outbound).
  - `VoiceSession(call_id, organisation_id, conversation_id, source, setup: VoiceSetup, transport, adapters, runner: TurnRunner, session_factory, events, settings, *, clock=time.monotonic)`; `async run() -> None` never raises; it always closes STT and the transport and stores usage (`add_usage`).
  - Behaviour:
    1. Opens STT. Speaks the greeting (`voice_greeting` with the org name, plus `voice_recording_notice` when recording), then `mark("turn-0")`.
    2. Audio frames go to STT. Final transcripts accumulate; on `speech_final` or `UtteranceEnd` the accumulated text (stripped, non-empty) becomes one utterance.
    3. An utterance: `add_caller_utterance`, then `answer_now` with a speaking sink. The sink runs deltas through `SentenceSplitter`; each sentence goes through `to_speech_text`, the running `MAX_SPOKEN_CHARS` cap, then TTS, frames out in order; `reset()` cancels the queued speech and sends `clear`. After the turn: `mark("turn-{n}")`. A turn with outcome `error` and no reply speaks `voice_turn_failed`. Latency (utterance end → first frame sent) is observed.
    4. Utterances during a running turn queue and are answered by the next `answer_now` (one at a time).
    5. Barge-in: while audio is playing (frames sent since the last `clear` and the latest mark not yet played), an interim or final `Transcript` with non-empty text sends `clear` and cancels the TTS task. `SpeechStarted` alone does not.
    6. `TurnResult.routed` (or the conversation left `bot` during the turn): speak `voice_callback_phone` / `voice_callback_browser`, `request_callback(end_reason="handoff_callback")`, wait for the `goodbye` mark (≤ `GOODBYE_MARK_TIMEOUT_SECONDS`), close.
    7. Silence: `SILENCE_PROMPT_SECONDS` after the last mark played with no speech, speak `voice_still_there`; another `SILENCE_HANGUP_SECONDS` → `voice_goodbye`, `set_end_reason("silence")`, close.
    8. `max_call_seconds` from start: `voice_max_duration`, `request_callback(end_reason="max_duration")`, close.
    9. `Stop` or transport closed: cancel speech; a turn already running completes (`answer_now` is shielded so its persistence finishes) and nothing more is spoken; `set_end_reason("caller_hangup")`.
    10. `SttAuthError`/`SttError`/`TtsAuthError`/`TtsError`: record a `voice` integration error (`deepgram` or `openai`, its error type), increment `voice_provider_errors_total`, `request_callback(end_reason="error")`, close at once (the `<Connect action>` speaks `voice_trouble`).
  - No DB session is held while awaiting STT, TTS, the transport or `answer_now`.

- [ ] **Step 1: Write the failing tests** with `FakeTransport`, fake STT/TTS and a scripted chat model (patch timing constants to tens of ms): `test_greeting_spoken_first_with_recording_notice`; `test_utterance_answered_and_spoken_in_sentences` (fake TTS frames decode to the reply's sentences in order); `test_barge_in_clears_playback_and_cancels_tts`; `test_vad_event_without_words_does_not_barge_in` (`FAKE-NOISE`); `test_utterances_during_turn_answered_next`; `test_escalation_speaks_callback_and_requests_callback` (phone: `callback_status == "requested"`, `callback_phone` set; browser: no callback phone, `voice_callback_browser` spoken); `test_silence_prompts_then_hangs_up`; `test_max_duration_requests_callback`; `test_stt_auth_failure_ends_with_error_and_callback` (integration error row `voice/deepgram/auth_failed`); `test_tts_failure_ends_with_error_and_callback`; `test_markdown_not_spoken`; `test_long_reply_capped`; `test_usage_recorded` (stt seconds and TTS characters on the call); `test_no_session_open_while_awaiting_providers`.
- [ ] **Step 2: Run** `uv run pytest packages/core/tests/voice/test_session.py -q` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same tests, then `uv run pytest packages/core/tests/voice -q` — PASS.

### Task 9: Media Streams WebSocket

**Files:**
- Create: `apps/voice/src/supportvoice/media_ws.py`, `apps/voice/tests/test_media_ws.py`
- Modify: `voice/transport.py` (`TwilioMediaTransport`), `apps/voice/src/supportvoice/main.py`

**Interfaces:**
- Consumes: Task 7 tokens, `CallService`, readiness; Task 8 `VoiceSession`.
- Produces:
  - `TwilioMediaTransport(websocket)`: parses `connected`/`start`/`media`/`mark`/`stop` (other events ignored); `media.payload` base64 → `AudioIn`; outbound JSON per the Global Constraints with the `streamSid` from `start`. Rejects inbound frames over 4 KiB and messages over 64 KiB (closes 1009).
  - `WS /twilio/media`: accepts, waits ≤ 5 s for `start`, verifies `customParameters.token`, `mark_stream_connected` (single use), loads `VoiceSetup` (gone or not ready → `request_callback(call_id, end_reason="voice_unavailable")`, close), counts toward `voice_max_calls`, increments `voice_active_calls`, enqueues `start_call_recording` (after commit) when recording is on, runs `VoiceSession`, then closes. The span `voice.call` carries `organisation.id`, `conversation.id`, `call.id`, `call.source`.

- [ ] **Step 1: Write the failing tests** (Starlette `TestClient` WebSocket against the ASGI app, fake STT/TTS, scripted chat): `test_full_call_transcript_persisted` (greeting frames; `FAKE-STT:What are your opening hours?` → customer and bot messages stored on the voice conversation, bot reply frames sent); `test_stream_token_single_use`; `test_bad_token_closes_without_session`; `test_no_start_within_timeout_closes`; `test_oversized_frame_closes`; `test_hangup_mid_turn_persists_turn_and_finalises_once` (send `stop` while the scripted model is mid-reply, then POST the status callback: the bot turn row exists, one `call_ended` event); `test_recording_job_enqueued_when_enabled`; `test_inbox_receives_live_message_events` (pubsub has `message` events for the conversation).
- [ ] **Step 2: Run** `uv run pytest apps/voice/tests -q` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest apps/voice/tests packages/core/tests/voice -q` — PASS.

### Task 10: Recordings

**Files:**
- Create: `voice/recordings.py`, `packages/core/tests/voice/test_recordings.py`
- Modify: `knowledge/jobs.py` (dispatch `start_call_recording`, `fetch_call_recording`, `purge_call_recordings`), `apps/worker/src/supportworker/celery_app.py` (beat `purge-call-recordings` daily at 03:30 UTC), `apps/api/src/supportapi/routers/inbox.py` (recording route)
- Test: `packages/core/tests/voice/test_recordings.py`, `apps/api/tests/test_inbox_voice.py` (recording route part)

**Interfaces:**
- Produces:
  - `start_call_recording(runtime, organisation_id, call_id)`: reads the call and account (commit), `telephony.start_recording(..., status_callback={voice_public_url}/twilio/recording/status?call_id={id})`, stores `recording_sid`. Failures → integration error `voice/twilio/recording_failed`, no retry.
  - `fetch_call_recording(runtime, organisation_id, call_id, recording_sid, attempt=1)`: `download_recording(max_bytes=200 MiB)` → `storage.put("orgs/{org}/calls/{call_id}/{uuid4}.mp3")` → `CallRecording(delete_after = now + retention days)` committed → `delete_recording` on Twilio (a failure there is logged, not retried). Download failures retry at 60 s, 300 s, 900 s (attempts 2–4), then record `recording_failed`. A call that already has a recording is a no-op.
  - `purge_call_recordings(runtime)`: deletes, in batches of 100, recordings with `delete_after < now`: storage delete first, then the row.
  - `GET /orgs/{org_id}/inbox/conversations/{conversation_id}/recording` (inbox view): streams the object with `Content-Type: audio/mpeg`, `Cache-Control: private, no-store`, `Content-Disposition: inline`; supports a single `Range: bytes=a-b` (206) so the player can seek; 404 when none.

- [ ] **Step 1: Write the failing tests:** `test_fetch_stores_then_deletes_on_twilio`; `test_fetch_is_idempotent`; `test_fetch_retries_then_records_error`; `test_purge_deletes_expired_only`; `test_recording_route_streams_and_supports_range`; `test_recording_route_cross_org_404`; `test_storage_put_outside_session` (no transaction open during `download_recording` or `storage.put`).
- [ ] **Step 2: Run** `uv run pytest packages/core/tests/voice/test_recordings.py apps/api/tests/test_inbox_voice.py -q` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same tests and `uv run pytest apps/worker/tests -q` — PASS.

### Task 11: Inbox and widget APIs for voice

**Files:**
- Modify: `inbox/summary.py`, `inbox/service.py`, `apps/api/src/supportapi/schemas/inbox.py`, `apps/api/src/supportapi/routers/inbox.py`, `apps/api/src/supportapi/routers/widget_public.py`, `apps/api/src/supportapi/schemas/widget.py` (or the module holding `PublicConfigOut`)
- Test: `apps/api/tests/test_inbox_voice.py`, `apps/api/tests/test_widget_calls.py`

**Interfaces:**
- Produces:
  - Conversation summary and detail gain `callback_status`, `callback_phone` (inbox view only), and for voice conversations `call: {id, source, status, end_reason, started_at, duration_seconds, has_recording}`. The customer panel's customer gains `phone`.
  - Inbox list filter `callback=requested` (uses the partial index); the "Callbacks" view counts it.
  - `POST /orgs/{org_id}/inbox/conversations/{id}/callback/done` (`queries.answer`): `requested` → `done`, publishes `conversation_changed`; anything else → 409 `invalid_conversation_state`.
  - Agent replies on a phone-sourced voice conversation are refused with 409 `voice_not_available` ("Phone callers can't receive text replies; call them back"). Browser-sourced ones are allowed (Assumption 8).
  - Widget: `GET /widget/v1/config` gains `voice_call_enabled: bool` (browser readiness for that widget). `POST /widget/v1/calls/token` (widget session auth, rate limit 5 per minute per session through the `RateLimiter` port) → `{token, ticket, expires_in: 600}`; 409 `voice_not_available` when not ready. Token identity is `customer-{customer_id}`.

- [ ] **Step 1: Write the failing tests:** `test_voice_summary_has_call_meta`; `test_callback_filter_lists_requested_only`; `test_callback_done_transitions_once`; `test_callback_done_requires_queries_answer`; `test_phone_voice_reply_refused`; `test_browser_voice_reply_allowed`; `test_widget_config_voice_flag`; `test_call_token_issues_ticket_bound_to_customer`; `test_call_token_rate_limited`; `test_call_token_409_when_not_ready`; `test_cross_org_voice_conversation_404`.
- [ ] **Step 2: Run** `uv run pytest apps/api/tests/test_inbox_voice.py apps/api/tests/test_widget_calls.py -q` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same tests plus `apps/api/tests/test_inbox*.py` — PASS.

### Task 12: Web — voice settings and onboarding

**Files:**
- Create: `apps/web/app/(dashboard)/orgs/[orgId]/settings/voice/page.jsx`, `apps/web/components/voice/{twilio-account-form.jsx,number-list.jsx,buy-number-dialog.jsx,voice-ai-form.jsx,call-settings-form.jsx}`, `apps/web/components/onboarding/voice-step.jsx`, `apps/web/lib/voice.js`, `apps/web/tests/unit/voice.test.js`
- Modify: `apps/web/lib/onboarding-steps.js`, `apps/web/components/app-shell.jsx` (Settings → Voice, `channels.manage`)

**Interfaces:**
- Consumes: Task 5 API.
- Produces: `lib/voice.js` — API wrappers for every Task 5 route; `readinessMessages(reasons) -> string[]` (one plain sentence per reason code); `formatE164(e164) -> string` (`+14155550123` → `+1 415 555 0123`; others unchanged); `numberStatus(number) -> "ok" | "webhooks_out_of_date" | "disabled"`.
- Page sections, in order: **Readiness** banner (lists `readinessMessages`); **Twilio** (connected state with `account_sid`, last4 and status; BYO form with Account SID + Auth Token; "Disconnect" with confirmation); **Numbers** (table: number, label, widget, status, actions; "Buy a number" when `platform_available` opens the dialog: country select (`US`, `CA`, `GB`), area code, results with monthly price, a confirm step that states the price; "Connect a number" for BYO lists account numbers; "Sync webhooks" shown when any number is out of date; "Release" with a confirm that says platform numbers are given up); **Voice AI** (STT provider/model/key, TTS provider/model/voice/key, keys picked from the org's existing `ProviderCredential`s with a link to add one); **Calls** (enable phone calls, enable browser calls + widget select, record calls, retention days, max call minutes).
- Onboarding: an optional "Voice" step after "Email" with a skip button; it embeds the Twilio form and the Voice AI form only.

- [ ] **Step 1: Write the failing unit tests** in `voice.test.js`: `readinessMessages maps every reason`; `formatE164 formats NANP numbers`; `numberStatus flags stale webhooks`.
- [ ] **Step 2: Run** `cd apps/web && npx vitest run tests/unit/voice.test.js` — FAIL.
- [ ] **Step 3: Implement** the lib, page, components and onboarding step, matching the email settings page's layout and components.
- [ ] **Step 4: Run** `npx vitest run && npm run lint && npm run build` — PASS.

### Task 13: Web — calls in the inbox

**Files:**
- Create: `apps/web/components/inbox/{call-card.jsx,recording-player.jsx,callback-actions.jsx}`
- Modify: `apps/web/components/inbox/{conversation-pane.jsx,conversation-list.jsx,composer.jsx,customer-panel.jsx,view-list.jsx}`, `apps/web/lib/inbox.js`, `apps/web/tests/unit/voice.test.js`

**Interfaces:**
- Consumes: Task 11 API, `lib/voice.js` `formatE164`.
- Produces:
  - `lib/inbox.js`: `formatCallDuration(seconds) -> string` (`75` → `"1:15"`, `3725` → `"1:02:05"`, null → `"—"`); `endReasonLabel(reason) -> string`; `canReplyInText(conversation) -> boolean` (false for phone-sourced voice).
  - `call-card.jsx`: at the top of a voice conversation — source (Phone/Browser), status, start time, duration, how it ended; the recording player when `has_recording` (`<audio controls preload="none">` with the authenticated recording URL fetched as a blob through the existing authenticated fetch helper).
  - `callback-actions.jsx`: when `callback_status === "requested"`: a "Call back" `tel:` link with `formatE164(callback_phone)` and "Mark called back" (`queries.answer` only). When `done`: a "Called back" badge.
  - The composer is replaced by an explanatory line plus the callback actions when `!canReplyInText`; notes stay.
  - The conversation list shows a phone icon for voice and a "Callback" badge; the view list gains "Callbacks" (`callback=requested`). The customer panel shows the phone number.

- [ ] **Step 1: Write the failing unit tests:** `formatCallDuration formats`; `canReplyInText is false only for phone calls`; `endReasonLabel covers every reason`.
- [ ] **Step 2: Run** `npx vitest run tests/unit/voice.test.js` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `npx vitest run && npm run lint && npm run build` — PASS.

### Task 14: Widget call button (Twilio Voice JS SDK)

**Files:**
- Create: `apps/web/components/embed/{call-button.jsx,call-panel.jsx}`, `apps/web/lib/voice-call.js`, `apps/web/tests/unit/voice-call.test.js`
- Modify: `apps/web/package.json` (`@twilio/voice-sdk`, exact version, `npm install`), `apps/web/components/embed/chat-window.jsx`, `apps/web/lib/widget-client.js` (`callToken()`), `apps/widget/src/loader.js` (`frame.setAttribute("allow", "microphone")`), `apps/widget/tests` (loader test)

**Interfaces:**
- Consumes: Task 11 widget routes and strings.
- Produces: `lib/voice-call.js` `createCallController({ fetchToken, loadSdk = () => import("@twilio/voice-sdk"), onState })` → `{ start(), hangUp(), toggleMute() }`. States: `idle` → `requesting_mic` → `connecting` → `in_call` → `ended`, or `failed` with `reason` `mic_denied` | `not_available` | `connect_failed`. `start()` asks for the microphone with `navigator.mediaDevices.getUserMedia({audio: true})` first (stopping that probe stream at once), then fetches the token, loads the SDK, `new Device(token, {codecPreferences: ["opus","pcmu"]})`, `device.connect({params: {ticket}})`. `hangUp()` disconnects and destroys the device. The SDK is never in the initial embed bundle.
- `call-button.jsx` shows only when `config.voice_call_enabled`; `call-panel.jsx` shows state text from the widget strings, mute and end buttons, and an elapsed timer. When the call ends, the widget refreshes its conversation list so the call transcript appears.

- [ ] **Step 1: Write the failing unit tests** (mocked SDK and `getUserMedia`): `start goes through states to in_call`; `mic denied fails with mic_denied without fetching a token`; `409 from token fails with not_available`; `hangUp disconnects and destroys`; `toggleMute calls call.mute`; `sdk is loaded lazily` (`loadSdk` not called before `start`). Widget loader test: `iframe allows microphone`.
- [ ] **Step 2: Run** `cd apps/web && npx vitest run tests/unit/voice-call.test.js` and `cd apps/widget && npx vitest run` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** both suites, `npm run lint && npm run build` in `apps/web`, `npm run build` in `apps/widget` — PASS. Check the build output: `@twilio/voice-sdk` is in a separate chunk, not in the `/embed/chat` first-load JS.

### Task 15: End-to-end voice test

**Files:**
- Create: `apps/web/tests/e2e/call_tool.py`, `apps/web/tests/e2e/voice.spec.js`
- Modify: `apps/web/tests/e2e/helpers.js` (`runCallTool`, `connectFakeTwilio`), `apps/web/playwright.config.mjs` (start the voice service on 8002 with `SUPPORT_ENV=test`, `SUPPORT_TELEPHONY=fake`, `SUPPORT_VOICE_PUBLIC_URL=http://localhost:8002`; the API gets the same telephony and public URL settings), `Makefile` (`e2e` notes)

**Interfaces:**
- Consumes: everything above.
- Produces: `call_tool.py` (stdlib + `websockets`, run with `uv run`, like `mail_tool.py`): `call --to E164 --from E164 --auth-token T --say "text" [--say "text" …] [--hangup-after-turn N]`. It signs and posts `/twilio/voice/inbound`, parses the TwiML stream URL and token, opens the WebSocket, sends `connected`/`start`, then for each `--say` sends `FAKE-STT:` frames and waits until it receives the `turn-{n}` mark; prints JSON `{"call_sid", "spoken": [utterances decoded from FAKE-TTS frames], "closed_by": "server"|"client"}`; finally posts a signed `completed` status callback.

- [ ] **Step 1: Write the e2e spec** `voice.spec.js`:
  1. `owner connects voice`: as the owner, open Settings → Voice, connect a BYO Twilio account (fake telephony; auth token `e2e-twilio-token-000000000000000`), connect `+15005550006` to the default widget, pick the fake STT/TTS, enable phone and browser calls; readiness shows no problems.
  2. `caller talks to the bot and gets a callback`: `call_tool` says "What are your opening hours?" then "I want to talk to a person"; the output's spoken lines include the greeting, a reply and the `voice_callback_phone` text; `closed_by == "server"`. In the inbox, the "Callbacks" view lists the conversation with a phone icon; the conversation shows both caller utterances and the bot reply, the call card says "Phone" and "Handed to team", and "Mark called back" changes the badge to "Called back".
  3. `widget shows the call button`: the embed page shows "Call us" (the browser call itself is covered by unit tests: Playwright has no Twilio).
- [ ] **Step 2: Run** `cd apps/web && npx playwright test tests/e2e/voice.spec.js --workers=1` — FAIL before wiring helpers/config, then implement `call_tool.py`, helpers and config.
- [ ] **Step 3: Run** `npx playwright test tests/e2e/voice.spec.js --workers=1` — PASS. Then the full `make e2e` — PASS.
- [ ] **Step 4: Run the full backend suite** `uv run pytest -q` and `npx vitest run` in `apps/web` and `apps/widget` — PASS (the known flake `test_widget_ws.py::test_idle_conversation_closed` may need one rerun).

---

## Manual check with a real Twilio number (owner, optional, after Task 15)

1. `make up-tunnel && make tunnel-url`; put the printed `SUPPORT_VOICE_PUBLIC_URL` in your environment; `make dev-api dev-voice dev-web dev-worker`.
2. Add a Deepgram key and an OpenAI key under Settings → AI, then connect your Twilio account under Settings → Voice and connect a number (or buy one if platform credentials are set).
3. Call the number. After each tunnel restart, press **Sync webhooks**.
