import { execFile } from "node:child_process";
import path from "node:path";
import { promisify } from "node:util";
import { expect } from "@playwright/test";

export const PASSWORD = "correct-horse-battery";

export function randomSuffix() {
  return Math.random().toString(36).slice(2, 10);
}

// Signs up a new owner, walks the onboarding steps (organisation, language; AI, email and phone calls skipped)
// and lands on the org dashboard. Returns { orgId, orgName, email }. The plan step is a single "Continue" with billing
// off (the dev adapter, which the other specs run on); `choosePlan(page)` completes it instead (billing.spec.js).
export async function signUpAndCreateOrg(page, { name = "Olivia Owner", orgName, email, choosePlan } = {}) {
  const rand = randomSuffix();
  email ??= `owner+${rand}@example.com`;
  orgName ??= `Acme ${rand}`;

  await page.goto("/signup");
  // The button is disabled until the page has hydrated and the session check is done; text typed
  // before hydration is lost (the controlled inputs keep their empty state).
  await expect(page.getByRole("button", { name: "Sign up" })).toBeEnabled();
  await page.getByLabel("Name").fill(name);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign up" }).click();
  await expect(page).toHaveURL(/\/onboarding$/);

  await page.getByLabel("Organisation name").fill(orgName);
  await page.getByRole("button", { name: "Create organisation" }).click();

  // Language step: keep the default (English).
  await page.getByRole("button", { name: "Continue" }).click();

  // AI step: skip; the dashboard banner reminds later.
  await page.getByRole("button", { name: "I'll do this later" }).click();

  // Plan step.
  await expect(page.getByText("Choose your plan", { exact: true })).toBeVisible();
  if (choosePlan) await choosePlan(page);
  else await page.getByRole("button", { name: "Continue" }).click();

  // Email step: optional, skip.
  await page.getByRole("button", { name: "Skip for now" }).click();

  // Phone calls step: optional, skip.
  await expect(page.getByText("Set up phone calls")).toBeVisible();
  await page.getByRole("button", { name: "Skip for now" }).click();

  await expect(page).toHaveURL(/\/orgs\/[0-9a-f-]+$/);
  const orgId = new URL(page.url()).pathname.split("/").pop();
  return { orgId, orgName, email };
}

// Radix Select: opens the trigger labelled `label` and picks the option named `option`.
export async function selectOption(scope, page, label, option) {
  await scope.getByLabel(label, { exact: true }).click();
  await page.getByRole("option", { name: option }).click();
}

// On AI settings: adds an OpenAI-compatible key for `baseUrl` (validated by the API with
// GET /models) and makes `model` on it the chat model. Ends with the settings Ready.
export async function useOpenAiCompatibleChatModel(page, orgId, { baseUrl, model, label = "Stub LLM" }) {
  await page.goto(`/orgs/${orgId}/settings/ai`);
  await page.getByRole("button", { name: "Add API key" }).click();
  const dialog = page.getByRole("dialog", { name: "Add an API key" });
  await selectOption(dialog, page, "Provider", "OpenAI-compatible");
  await dialog.getByLabel("Label").fill(label);
  await dialog.getByLabel("API key", { exact: true }).fill("sk-stub-key");
  await dialog.getByLabel("Base URL").fill(baseUrl);
  await dialog.getByRole("button", { name: "Save key" }).click();
  await expect(dialog).toBeHidden({ timeout: 20_000 });

  await selectOption(page, page, "Provider", "OpenAI-compatible");
  await page.getByLabel("Model", { exact: true }).fill(model);
  await selectOption(page, page, "API key", new RegExp(`^${label}`));
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Ready", { exact: true })).toBeVisible();
}

// Creates a knowledge base on the local embedder, uploads `file` and waits until it is Indexed.
// Returns the knowledge base page URL.
export async function createIndexedKnowledgeBase(page, orgId, { name, file }) {
  await page.goto(`/orgs/${orgId}/knowledge`);
  await page.getByRole("button", { name: "New knowledge base" }).first().click();
  const dialog = page.getByRole("dialog", { name: "New knowledge base" });
  await dialog.getByLabel("Name").fill(name);
  await expect(dialog.getByLabel("Embedding model")).toContainText("BGE Small (English)");
  await dialog.getByRole("button", { name: "Create" }).click();
  await expect(dialog).toBeHidden();
  await page.getByRole("link", { name: new RegExp(name) }).click();
  await expect(page).toHaveURL(new RegExp(`/orgs/${orgId}/knowledge/[0-9a-f-]+$`));

  const fileName = file.split(/[\\/]/).pop();
  await page.getByLabel("Choose files to upload").setInputFiles(file);
  const docRow = page.getByRole("row", { name: new RegExp(fileName.replace(/\./g, "\\.")) });
  await expect(docRow.getByText("Uploaded", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Start embedding (1)" }).click();
  await expect(docRow.getByText("Indexed", { exact: true })).toBeVisible({ timeout: 120_000 });
  return page.url();
}

const MAILPIT = "http://localhost:8025";

// Newest Mailpit message addressed to `address`; returns its accept-invite URL.
export async function inviteLinkFor(request, address) {
  let link = null;
  await expect
    .poll(
      async () => {
        const list = await request.get(`${MAILPIT}/api/v1/messages`);
        const { messages } = await list.json();
        const hit = messages.find((m) => m.To.some((t) => t.Address.toLowerCase() === address));
        if (!hit) return null;
        const detail = await request.get(`${MAILPIT}/api/v1/message/${hit.ID}`);
        const { Text } = await detail.json();
        link = Text.match(/https?:\/\/\S*accept-invite\?token=[A-Za-z0-9_-]+/)?.[0] ?? null;
        return link;
      },
      { message: `invite email for ${address}`, timeout: 15_000 },
    )
    .not.toBeNull();
  return link;
}

// The owner (on `page`) invites `email` with the default Agent role; the agent accepts the emailed link
// in a fresh browser context (its own storage) as `name`. Returns { context, page } of the agent, on the
// org dashboard.
export async function inviteAgent(page, browser, request, { orgId, orgName, email, name }) {
  await page.goto(`/orgs/${orgId}/team`);
  await page.getByRole("button", { name: "Invite member" }).click();
  const dialog = page.getByRole("dialog", { name: "Invite a team member" });
  await dialog.getByLabel("Email").fill(email);
  await expect(dialog.getByLabel("Role")).toContainText("Agent");
  await dialog.getByRole("button", { name: "Send invite" }).click();
  await expect(page.getByRole("cell", { name: email })).toBeVisible();

  const link = await inviteLinkFor(request, email);
  const context = await browser.newContext();
  const agentPage = await context.newPage();
  await agentPage.goto(link);
  await agentPage.getByLabel("Name").fill(name);
  await agentPage.getByLabel("Password").fill(PASSWORD);
  await agentPage.getByRole("button", { name: `Join ${orgName}` }).click();
  await expect(agentPage).toHaveURL(`/orgs/${orgId}`);
  return { context, page: agentPage };
}

// On Support settings: every day of the week open 00:00-24:00, saved.
export async function setAlwaysOpen(page, orgId) {
  await page.goto(`/orgs/${orgId}/settings/support`);
  await expect(page.getByRole("button", { name: "Save settings" })).toBeVisible(); // the form has loaded
  for (const [day, label] of [
    ["mon", "Monday"],
    ["tue", "Tuesday"],
    ["wed", "Wednesday"],
    ["thu", "Thursday"],
    ["fri", "Friday"],
    ["sat", "Saturday"],
    ["sun", "Sunday"],
  ]) {
    const row = page.getByTestId(`hours-${day}`);
    const start = row.getByLabel(`${label} interval 1 start`);
    if ((await start.count()) === 0) await row.getByRole("button", { name: "Add interval" }).click();
    await start.fill("00:00");
    await row.getByLabel(`${label} interval 1 end`).fill("24:00");
  }
  await page.getByRole("button", { name: "Save settings" }).click();
  await expect(page.getByText("Support settings saved")).toBeVisible();
}

// The agent's own status from the sidebar switcher ("Online", "Away" or "Offline").
export async function setAgentStatus(page, status) {
  const trigger = page.getByLabel("Your status", { exact: true });
  await selectOption(page, page, "Your status", status);
  await expect(trigger).toContainText(status);
}

// Creates a widget allowed on `domain` that answers from the knowledge base `kbName` (none when omitted); returns its key
// (read from the embed snippet).
export async function createWidget(page, orgId, { name = "Shop site", domain = "localhost:5500", kbName = null }) {
  await page.goto(`/orgs/${orgId}/widgets`);
  await page.getByRole("button", { name: "New widget" }).click();
  const create = page.getByRole("dialog", { name: "New widget" });
  await create.getByLabel("Name").fill(name);
  await create.getByRole("button", { name: "Create" }).click();
  await expect(page).toHaveURL(new RegExp(`/orgs/${orgId}/widgets/[0-9a-f-]+$`));
  await page.getByLabel("Allowed domains").fill(domain);
  await page.getByRole("button", { name: "Add", exact: true }).click();
  if (kbName) await page.getByLabel(kbName).check();
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Saved" })).toBeVisible();
  const snippet = await page.getByLabel("Embed snippet").innerText();
  const key = snippet.match(/data-key="(wk_[A-Za-z0-9_-]{24})"/)?.[1];
  expect(key).toBeTruthy();
  return key;
}

// Runs the operator CLI (`uv run python -m supportapi.cli ...`) from the repo root, with this process's
// environment (the same database settings as the API: exported, or from the repo-root .env). Returns stdout;
// rejects (with the CLI's stderr) on a non-zero exit.
export async function runSupportCli(...args) {
  const root = path.resolve(process.cwd(), "..", ".."); // npm run e2e runs from apps/web
  const { stdout } = await promisify(execFile)("uv", ["run", "python", "-m", "supportapi.cli", ...args], {
    cwd: root,
    env: process.env,
    timeout: 120_000,
  });
  return stdout;
}

// Recomputes the analytics rollups of `orgId` for yesterday..tomorrow (UTC dates), which covers today in any
// support timezone. The backfill stores closed hours only; the open hour is computed live by the dashboards.
export async function rollUpAnalytics(orgId) {
  const day = (offset) => new Date(Date.now() + offset * 86_400_000).toISOString().slice(0, 10);
  return runSupportCli("rollup-backfill", "--org", orgId, "--from", day(-1), "--to", day(1));
}

// --- Email channel (GreenMail: `make up-email`, IMAP :3143 / SMTP :3025) ---

// Runs tests/e2e/mail_tool.py (stdlib only) with `uv run python` from the repo root. Returns stdout.
async function runMailTool(...args) {
  const root = path.resolve(process.cwd(), "..", ".."); // npm run e2e runs from apps/web
  const { stdout } = await promisify(execFile)(
    "uv",
    ["run", "python", path.join("apps", "web", "tests", "e2e", "mail_tool.py"), ...args],
    { cwd: root, env: process.env, timeout: 60_000 },
  );
  return stdout;
}

// Sends one plain-text email over GreenMail's SMTP as the customer. Returns its Message-ID (with <>).
export async function sendCustomerEmail({ from, to, subject, body, inReplyTo, references = [] }) {
  const args = ["send", "--from", from, "--to", to, "--subject", subject, "--body", body];
  if (inReplyTo) args.push("--in-reply-to", inReplyTo);
  if (references.length) args.push("--references", ...references);
  const messageId = (await runMailTool(...args)).trim();
  expect(messageId).toMatch(/^<[^<>\s]+>$/);
  return messageId;
}

// The INBOX of a GreenMail account as [{ message_id, in_reply_to, references: [], subject, from }]. GreenMail
// auto-creates recipients with their address as both login and password.
export async function fetchMailbox({ user, password = user }) {
  return JSON.parse(await runMailTool("fetch", "--user", user, "--password", password));
}

// The GreenMail support account (infra/docker-compose.yml, GREENMAIL_OPTS greenmail.users).
export const GREENMAIL_SUPPORT = {
  address: "support@support.test",
  username: "support",
  password: "support-dev-pass",
};

// On Settings -> Email: connects the GreenMail support mailbox (IMAP localhost:3143 and SMTP localhost:3025, no
// TLS), checks that Test connection reports both sides connected, then saves. Ends on the settings page.
export async function connectMailbox(page, orgId, { address, username, password } = GREENMAIL_SUPPORT) {
  await page.goto(`/orgs/${orgId}/settings/email`);
  await expect(page.getByRole("heading", { name: "Email", exact: true })).toBeVisible();
  await page.getByLabel("Email address").fill(address);
  for (const [legend, host, port] of [
    ["Incoming mail (IMAP)", "localhost", "3143"],
    ["Outgoing mail (SMTP)", "localhost", "3025"],
  ]) {
    const side = page.getByRole("group", { name: legend });
    await side.getByLabel("Host").fill(host);
    await selectOption(side, page, "Security", "None (not encrypted)");
    await side.getByLabel("Port").fill(port);
    await side.getByLabel("Username").fill(username);
    await side.getByLabel("Password").fill(password);
  }
  await page.getByRole("button", { name: "Test connection" }).click();
  await expect(page.getByText("IMAP: connected")).toBeVisible({ timeout: 45_000 });
  await expect(page.getByText("SMTP: connected")).toBeVisible();
  await page.getByRole("button", { name: "Save", exact: true }).click();
  // The saved mailbox re-mounts the form (its "Saved." notice with it); Check now exists only once saved.
  await expect(page.getByRole("button", { name: "Check now" })).toBeVisible({ timeout: 20_000 });
}

// --- Voice channel (apps/voice on :8002 with the fake Twilio, STT and TTS; the API on the same fakes) ---

// The one number the fake telephony lists, and the Twilio credentials it accepts (AC + 32 hex; any token
// that does not start with "bad-token"). A Twilio account belongs to one organisation, so the SID is new each run, and
// the fake's one number is released again by `disconnectFakeTwilio`.
export const FAKE_TWILIO = {
  accountSid: "AC" + Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join(""),
  authToken: "e2e-twilio-token-000000000000000",
  number: "+15005550006",
};

// Runs tests/e2e/call_tool.py (stdlib + websockets) with `uv run python` from the repo root, signed as the fake
// Twilio account; `args` are the sub-command and its arguments. Returns the tool's JSON.
async function callTool(args, { accountSid = FAKE_TWILIO.accountSid, authToken = FAKE_TWILIO.authToken } = {}) {
  const root = path.resolve(process.cwd(), "..", ".."); // npm run e2e runs from apps/web
  const { stdout } = await promisify(execFile)(
    "uv",
    [
      "run",
      "python",
      path.join("apps", "web", "tests", "e2e", "call_tool.py"),
      ...args,
      "--account-sid",
      accountSid,
      "--auth-token",
      authToken,
    ],
    { cwd: root, env: process.env, timeout: 120_000 },
  );
  return JSON.parse(stdout);
}

// A scripted phone call. `say` is the caller's utterances in order. Returns the tool's JSON:
// { call_sid, spoken, closed_by }. `realtime` speaks to the fake Realtime model (speech-to-speech); with
// `expectTransfer` the bot must hand the call over: the caller stays on the line (hang up with `hangUpCaller`) and the
// JSON gains { after_stream_url, said, conference, transfer_id }.
export async function runCallTool({
  to = FAKE_TWILIO.number,
  from,
  accountSid,
  authToken,
  say = [],
  hangupAfterTurn,
  realtime = false,
  expectTransfer = false,
}) {
  const args = ["call", "--to", to, "--from", from];
  for (const text of say) args.push("--say", text);
  if (hangupAfterTurn !== undefined) args.push("--hangup-after-turn", String(hangupAfterTurn));
  if (realtime) args.push("--realtime");
  if (expectTransfer) args.push("--expect-transfer");
  return callTool(args, { accountSid, authToken });
}

// One agent leg of a live transfer, as Twilio reports it: answered (a browser leg joins; a phone leg presses `digits`,
// "1" or "none"), then in the room or ended unanswered. Returns { answer, accept, outcome, said }.
export async function answerTransferLeg({ transferId, attempt, digits }) {
  return callTool(["agent", "--transfer-id", transferId, "--attempt", String(attempt), "--digits", digits]);
}

// The caller of `callSid` hangs up (the signed `completed` status callback).
export async function hangUpCaller({ callSid, from, to = FAKE_TWILIO.number }) {
  return callTool(["hangup", "--call-sid", callSid, "--from", from, "--to", to]);
}

// Twilio posts the call's <Connect action> again; returns { said, conference } of its TwiML.
export async function repostAfterStream({ url, callSid, from, to = FAKE_TWILIO.number }) {
  return callTool(["after-stream", "--url", url, "--call-sid", callSid, "--from", from, "--to", to]);
}

// The <Dial action> of the outbound callback `callId` (`dialStatus` "completed": the customer answered).
export async function postCallbackDone({ callId, dialStatus = "completed" }) {
  return callTool(["callback-done", "--call-id", callId, "--dial-status", dialStatus]);
}

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

const VOICE = process.env.SUPPORT_VOICE_PUBLIC_URL || "http://localhost:8002";

// What the fake Twilio of a process (apps/voice places transfer legs and redirects callers; the API rings an agent's
// phone for a callback) was asked to do, from its test-only read-back (env=test, telephony=fake only):
// { create_call: [{ account_sid, to, from, answer_url, status_callback, sid }], update_call: [{ call_sid, twiml, end }] }.
async function fakeTelephonyCalls(request, base) {
  const res = await request.get(`${base}/test/fake-telephony/calls`);
  expect(res.ok()).toBe(true);
  return res.json();
}
export const voiceTelephonyCalls = (request) => fakeTelephonyCalls(request, VOICE);
export const apiTelephonyCalls = (request) => fakeTelephonyCalls(request, API);

// The create_call whose answer URL carries all of `query` (e.g. { transfer_id, attempt } or { call_id }).
export function placedCall(calls, query) {
  return calls.create_call.find((placed) => {
    const params = new URL(placed.answer_url).searchParams;
    return Object.entries(query).every(([key, value]) => params.get(key) === String(value));
  });
}

// The membership id of the member with `email`, through the API (`token`: any member's access token).
export async function membershipIdOf(request, orgId, token, email) {
  const res = await request.get(`${API}/orgs/${orgId}/members`, { headers: { Authorization: `Bearer ${token}` } });
  expect(res.ok()).toBe(true);
  const member = (await res.json()).find((m) => m.email === email);
  expect(member).toBeTruthy();
  return member.membership_id;
}

// An access token for `email`, from the API's own login (a separate session: the page's stays as it is).
export async function apiAccessToken(request, email, password = PASSWORD) {
  const res = await request.post(`${API}/auth/login`, { data: { email, password } });
  expect(res.ok()).toBe(true);
  return (await res.json()).access_token;
}

// The softphone heartbeat the inbox sends while "Take calls" is on (the browser's Twilio Device needs a microphone
// and real Twilio): the member of `token` can take transferred calls in the browser for the next 60 s.
export async function beatSoftphone(request, orgId, token) {
  const res = await request.put(`${API}/orgs/${orgId}/voice/agent/softphone`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(res.status()).toBe(204);
}

// On Team: sets the member `name`'s phone number and how they take transferred calls ("Browser" or "Phone").
export async function setMemberCalls(page, orgId, name, { phone, takeCallsOn }) {
  await page.goto(`/orgs/${orgId}/team`);
  const actions = page.getByRole("button", { name: `Actions for ${name}` });
  if (phone !== undefined) {
    await actions.click();
    await page.getByRole("menuitem", { name: "Set phone number" }).click();
    const dialog = page.getByRole("dialog", { name: "Phone number" });
    await dialog.getByLabel("Phone number").fill(phone);
    await dialog.getByRole("button", { name: "Save" }).click();
    await expect(dialog).toBeHidden();
  }
  if (takeCallsOn !== undefined) {
    await actions.click();
    await page.getByRole("menuitem", { name: "Take calls on" }).click();
    const dialog = page.getByRole("dialog", { name: "Take calls on" });
    await selectOption(dialog, page, "Take calls on", takeCallsOn);
    await dialog.getByRole("button", { name: "Save" }).click();
    await expect(dialog).toBeHidden();
  }
}

// On Settings -> Voice -> Transfers: rings agents in their browser and/or on their phones.
export async function setTransfers(page, orgId, { browser, phone }) {
  await page.goto(`/orgs/${orgId}/settings/voice`);
  const browserSwitch = page.getByLabel("Ring agents in their browser");
  const phoneSwitch = page.getByLabel(/^Ring agents' phones/);
  await expect(browserSwitch).toBeVisible({ timeout: 20_000 });
  for (const [control, on] of [
    [browserSwitch, browser],
    [phoneSwitch, phone],
  ]) {
    if ((await control.getAttribute("aria-checked")) !== String(on)) await control.click();
  }
  const form = page.locator("form").filter({ has: browserSwitch });
  await form.getByRole("button", { name: "Save", exact: true }).click();
  await expect(form.getByRole("status").filter({ hasText: "Saved." })).toBeVisible();
}

// On Settings -> Voice -> Voice AI: speech-to-speech with the fake Realtime model (no key needed).
export async function useFakeRealtime(page, orgId) {
  await page.goto(`/orgs/${orgId}/settings/voice`);
  const modeField = page.getByLabel("How the AI talks");
  await expect(modeField).toBeVisible({ timeout: 20_000 });
  await selectOption(page, page, "How the AI talks", /^Speech-to-speech/);
  const rt = page.getByRole("group", { name: "Speech-to-speech" });
  await selectOption(rt, page, "Provider", "fake");
  await selectOption(rt, page, "Model", /Fake Realtime/);
  await selectOption(rt, page, "Voice", "fake");
  const form = page.locator("form").filter({ has: rt });
  await form.getByRole("button", { name: "Save", exact: true }).click();
  await expect(form.getByRole("status").filter({ hasText: "Saved." })).toBeVisible();
}

// On Settings -> Voice: connects the fake Twilio account (bring your own), connects its number to the widget
// `widgetName`, picks the fake speech to text and text to speech, and enables phone and browser calls (browser
// calls on the same widget). Ends with the readiness showing no problems.
export async function connectFakeTwilio(page, orgId, { widgetName }) {
  await page.goto(`/orgs/${orgId}/settings/voice`);
  await page.getByLabel("Account SID").fill(FAKE_TWILIO.accountSid);
  await page.getByLabel("Auth Token").fill(FAKE_TWILIO.authToken);
  await page.getByRole("button", { name: "Connect Twilio" }).click();
  await expect(page.getByTestId("twilio-connected")).toBeVisible({ timeout: 20_000 });

  await page.getByRole("button", { name: "Connect a number" }).click();
  const dialog = page.getByRole("dialog", { name: "Connect a number" });
  await dialog.getByRole("listitem").filter({ hasText: "500 555 0006" }).getByRole("button", { name: "Connect" }).click();
  await expect(dialog).toBeHidden({ timeout: 20_000 });
  const row = page.getByRole("row").filter({ hasText: "500 555 0006" });
  await expect(row).toBeVisible();
  await selectOption(row, page, "Widget for +1 500 555 0006", widgetName);

  const stt = page.getByRole("group", { name: "Speech to text" });
  await selectOption(stt, page, "Provider", "fake");
  await selectOption(stt, page, "Model", /Fake STT/);
  const tts = page.getByRole("group", { name: "Text to speech" });
  await selectOption(tts, page, "Provider", "fake");
  await selectOption(tts, page, "Model", /Fake TTS/);
  await selectOption(tts, page, "Voice", "fake");
  await page.locator("form").filter({ has: stt }).getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Saved." }).first()).toBeVisible();

  await page.getByLabel("Enable phone calls").check();
  await page.getByLabel("Enable browser calls").check();
  await selectOption(page, page, "Widget for browser calls", widgetName);
  await page.locator("form").filter({ has: page.getByLabel("Enable phone calls") }).getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByTestId("voice-readiness")).toContainText("Voice is ready", { timeout: 20_000 });
}

// On Settings -> Voice: releases the connected number and disconnects the Twilio account, so the next run (or
// organisation) can connect the fake's one number again. Nothing happens when they are already gone.
export async function disconnectFakeTwilio(page, orgId) {
  await page.goto(`/orgs/${orgId}/settings/voice`);
  const release = page.getByRole("button", { name: /^Release / });
  const disconnect = page.getByRole("button", { name: "Disconnect", exact: true });
  const accountForm = page.getByLabel("Account SID");
  await expect(disconnect.or(accountForm)).toBeVisible({ timeout: 20_000 }); // the page has loaded
  if (await accountForm.isVisible()) return;
  if ((await release.count()) > 0) {
    await release.first().click();
    await page.getByRole("button", { name: "Release number" }).click();
    await expect(release).toHaveCount(0, { timeout: 20_000 });
  }
  await disconnect.click();
  await page.getByRole("dialog", { name: "Disconnect Twilio?" }).getByRole("button", { name: "Disconnect" }).click();
  await expect(accountForm).toBeVisible({ timeout: 20_000 });
}
