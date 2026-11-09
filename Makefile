.PHONY: up up-langfuse stop-langfuse up-email stop-email up-tunnel stop-tunnel tunnel-url up-stripe stop-stripe stripe-secret down migrate test dev-api dev-worker dev-beat dev-voice dev-web e2e gen-secrets-key build-widget

up:
	mkdir -p .data/logs
	docker compose -f infra/docker-compose.yml up -d

# Langfuse (LLM tracing; needs ~2-2.5 GB free in Docker). Compose reads infra/.env by default, so the
# repo-root .env is passed explicitly when it exists.
LANGFUSE_COMPOSE = docker compose $(if $(wildcard .env),--env-file .env) -f infra/docker-compose.yml --profile langfuse

up-langfuse:
	mkdir -p .data/logs
	$(LANGFUSE_COMPOSE) up -d

stop-langfuse:
	$(LANGFUSE_COMPOSE) stop clickhouse langfuse-web langfuse-worker

# GreenMail (local IMAP :3143 / SMTP :3025, ~150 MB) for the email channel's integration tests
# (packages/core/tests/mail/test_imap_adapter.py skips without it). Stop it when done.
up-email:
	docker compose -f infra/docker-compose.yml --profile email up -d greenmail

stop-email:
	docker compose -f infra/docker-compose.yml --profile email stop greenmail

# cloudflared quick tunnel to apps/voice (:8002) for Twilio in dev. The URL changes each run: `make tunnel-url`
# prints the SUPPORT_VOICE_PUBLIC_URL line to set. dev-api, dev-worker and dev-voice all read it (the API builds the
# number and TwiML App webhooks, the worker the recording callback for old jobs, voice checks signatures against
# it), so set it for all three and restart all three (dev-api's --reload does not watch .env), then use
# Settings > Voice > Sync webhooks.
up-tunnel:
	docker compose -f infra/docker-compose.yml --profile tunnel up -d cloudflared

stop-tunnel:
	docker compose -f infra/docker-compose.yml --profile tunnel stop cloudflared

tunnel-url:
	@docker compose -f infra/docker-compose.yml --profile tunnel ps --status running --services 2>/dev/null | grep -qx cloudflared || { echo "The tunnel is not running: run 'make up-tunnel'" >&2; exit 1; }; \
	url=$$(docker compose -f infra/docker-compose.yml --profile tunnel logs cloudflared 2>&1 | grep -o 'https://[a-zA-Z0-9-]*\.trycloudflare\.com' | tail -1); \
	if [ -z "$$url" ]; then echo "No tunnel URL yet: run 'make up-tunnel' and retry in a few seconds" >&2; exit 1; fi; \
	echo "SUPPORT_VOICE_PUBLIC_URL=$$url"

# Stripe test mode (SUPPORT_BILLING=stripe): the Stripe CLI forwards the account's test events to the API's webhook
# (host port 8000, `make dev-api`). Needs SUPPORT_STRIPE_SECRET_KEY (sk_test_...) in the environment or the
# repo-root .env. `make stripe-secret` prints the SUPPORT_STRIPE_WEBHOOK_SECRET line to set (the CLI's signing
# secret, the same each run for a key), then restart dev-api; `make up-stripe` starts forwarding, `make stop-stripe`
# stops it.
STRIPE_COMPOSE = docker compose $(if $(wildcard .env),--env-file .env) -f infra/docker-compose.yml --profile stripe

up-stripe:
	$(STRIPE_COMPOSE) up -d stripe-cli

stop-stripe:
	$(STRIPE_COMPOSE) stop stripe-cli

stripe-secret:
	@secret=$$($(STRIPE_COMPOSE) run --rm --no-deps stripe-cli listen --print-secret 2>/dev/null | grep -o 'whsec_[A-Za-z0-9]*' | tail -1); \
	if [ -z "$$secret" ]; then echo "No webhook secret: is SUPPORT_STRIPE_SECRET_KEY a valid test key?" >&2; exit 1; fi; \
	echo "SUPPORT_STRIPE_WEBHOOK_SECRET=$$secret"

down:
	docker compose -f infra/docker-compose.yml down

migrate:
	uv run alembic -c apps/api/alembic.ini upgrade head
	uv run python -m supportapi.cli setup-checkpointer

# dev-api / dev-worker / dev-beat also write JSON logs to .data/logs/<service>.log (SUPPORT_LOG_FILE) for Loki.
# The file sink is per process (a dev convenience): `uvicorn --workers N`, or several celery invocations on one
# host, must each get a distinct SUPPORT_LOG_FILE (a shared one breaks rotation). Production logs to stdout.
# Config comes from the environment or, failing that, the repo-root .env (`cp .env.example .env`).

# `alembic check` fails if the models have drifted from the migrations (needs a DB at head: make migrate).
test:
	uv run alembic -c apps/api/alembic.ini check
	uv run pytest

# Works out of the box after `cp .env.example .env` (Settings reads the repo-root .env).
dev-api:
	mkdir -p .data/logs
	SUPPORT_LOG_FILE=$${SUPPORT_LOG_FILE:-.data/logs/api.log} \
		uv run uvicorn supportapi.main:create_app --factory --reload --ws-max-size 16384

# Celery worker (JSON logs, traces as support-worker, metrics on :9101). Requires `make up`.
# PROMETHEUS_MULTIPROC_DIR lets /metrics aggregate every pool process (wiped at worker start).
dev-worker:
	mkdir -p .data/logs
	SUPPORT_LOG_FILE=$${SUPPORT_LOG_FILE:-.data/logs/worker.log} \
		PROMETHEUS_MULTIPROC_DIR=$${PROMETHEUS_MULTIPROC_DIR:-.data/worker-metrics} \
		uv run celery -A supportworker.celery_app worker --loglevel=INFO --concurrency=2

# Celery beat: sends the periodic jobs. With SUPPORT_JOB_QUEUE=celery (the .env.example default) nothing else
# does, so run it next to dev-worker: the analytics rollup (rollup_analytics, every 5 minutes; without it the
# dashboards say the rollup is behind and older hours read as zero), the idle-conversation close, the SLA checks
# and recover_stale_jobs (fails jobs a dead worker left running, re-sends jobs whose message was lost). Run one
# beat per deployment, next to the workers.
dev-beat:
	mkdir -p .data/logs
	SUPPORT_LOG_FILE=$${SUPPORT_LOG_FILE:-.data/logs/beat.log} \
		uv run celery -A supportworker.celery_app beat --loglevel=INFO --schedule .data/celerybeat-schedule

# The voice service (Twilio webhooks, Media Streams; port 8002). Twilio reaches it through SUPPORT_VOICE_PUBLIC_URL
# (`make up-tunnel` in dev). The tunnel container reaches it as host.docker.internal: that works on Docker Desktop
# (macOS/Windows) with the default loopback bind; on Linux run `VOICE_HOST=0.0.0.0 make dev-voice`. No --reload: a restart would drop live calls.
# One uvicorn worker per voice process (the call limit and active calls are per process): capacity is
# replicas x SUPPORT_VOICE_MAX_CALLS. Its DB pool is SUPPORT_VOICE_DB_POOL_SIZE (default 20) + 10 overflow.
dev-voice:
	mkdir -p .data/logs
	SUPPORT_LOG_FILE=$${SUPPORT_LOG_FILE:-.data/logs/voice.log} \
		uv run uvicorn supportvoice.main:create_app --factory --host $${VOICE_HOST:-127.0.0.1} --no-proxy-headers --port 8002 --ws-max-size 16384

# Prints a Fernet key for SUPPORT_SECRETS_KEY (required when SUPPORT_ENV=prod).
gen-secrets-key:
	@uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

dev-web:
	cd apps/web && npm run dev

# Requires `make up`, `make migrate`, the API running with SUPPORT_EMAIL_SENDER=smtp
# (invite emails go to Mailpit) and the web app on :3000. Servers are not started here.
# The knowledge spec also needs `make dev-worker` with the API on SUPPORT_JOB_QUEUE=celery
# (or the API on SUPPORT_JOB_QUEUE=inline). The first run downloads BAAI/bge-small-en-v1.5
# into SUPPORT_MODEL_CACHE_DIR (.data/models); use an absolute path when the API and worker
# are started from different directories.
# The widget spec also needs the stub LLM (`node apps/web/tests/e2e/stub-llm.mjs`, :5555), the demo
# page on :5500 (`python3 -m http.server 5500 --directory apps/widget/demo`, or
# `npx serve -l 5500 apps/widget/demo`) and `make build-widget`; the API must run in dev (private
# base URLs allowed) with `--ws-max-size 16384` as in `make dev-api`.
# The inbox spec uses the same servers as the widget spec plus Mailpit (it invites a second agent).
# The analytics spec uses the widget spec's servers and runs `supportapi.cli` (make-platform-admin,
# rollup-backfill) with `uv`, so the database settings must be in its environment or the repo-root .env.
# The email spec needs GreenMail (`make up-email`; `make stop-email` afterwards) and the API in dev on the
# real mail adapters (SUPPORT_MAIL_INBOUND=imap / SUPPORT_MAIL_OUTBOUND=smtp, the defaults) with
# SUPPORT_JOB_QUEUE=inline, or celery plus `make dev-worker` (Check now and the reply's send are jobs). It
# drives tests/e2e/mail_tool.py (stdlib, run with `uv`) as the customer: SMTP :3025 to send, IMAP :3143 to read.
# The voice spec (voice.spec.js) runs a phone call against apps/voice on :8002 through the fake Twilio, speech to text
# and text to speech. Playwright starts the voice service itself (playwright.config.mjs). Start the API for it with
#   SUPPORT_ENV=test SUPPORT_TELEPHONY=fake SUPPORT_VOICE_PUBLIC_URL=http://localhost:8002 make dev-api
# (plus SUPPORT_JOB_QUEUE=inline, or a worker). It also needs the stub LLM, the demo page and `make build-widget` like
# the widget spec. The spec releases the number (+15005550006, the only one the fake lists) and disconnects its
# Twilio account at the end, through Settings -> Voice; a run that dies midway leaves them connected to its
# organisation (release them there, or a new run cannot connect the number).
# tests/e2e/call_tool.py (stdlib + websockets, run with `uv`) plays the phone caller.
# The voice transfer spec (voice-transfer.spec.js: live transfer, no-answer callback, speech-to-speech, calling back)
# uses the voice spec's servers (no demo page needed) plus Mailpit, with the API on SUPPORT_EMAIL_SENDER=smtp (it invites
# a second agent). call_tool.py also plays Twilio for the agents' legs (`agent`), the caller's hang-up (`hangup`), a
# repeated <Connect action> (`after-stream`) and a callback's <Dial action> (`callback-done`); `call --realtime` talks
# to the fake Realtime model. The agents' browser softphones register through the API (PUT .../voice/agent/softphone).
# Specs run one at a time (--workers=1): in parallel the local embedder and the dev server time out.
e2e:
	cd apps/web && npm run e2e -- --workers=1

# Builds the embeddable widget loader into apps/web/public/widget/v1.js (build output, fails over 15 KB).
build-widget:
	cd apps/widget && npm install && npm run build
