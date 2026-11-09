import path from "node:path";
import { test, expect } from "@playwright/test";
import {
  PASSWORD,
  createIndexedKnowledgeBase,
  createWidget,
  randomSuffix,
  rollUpAnalytics,
  runSupportCli,
  setAgentStatus,
  setAlwaysOpen,
  signUpAndCreateOrg,
  useOpenAiCompatibleChatModel,
} from "./helpers.js";

// Needs the inbox spec's servers (stub LLM on :5555, demo page on :5500, `make build-widget`, the API with
// `--ws-max-size 16384`) and `uv` on PATH with the API's database settings in the environment (or the repo-root
// .env): the spec runs `supportapi.cli make-platform-admin` and `supportapi.cli rollup-backfill`.
const STUB = "http://localhost:5555";
const DEMO = "http://localhost:5500";
const FIXTURE = path.join(process.cwd(), "tests", "e2e", "fixtures", "returns.md"); // npm run e2e runs from apps/web
const OWNER = "Olivia Owner";
// supportcore/widget/strings.py (en)
const GREETING = "Hi! How can we help you today?";
const CSAT_PROMPT = "How was your experience?";
const CSAT_THANKS = "Thanks for your feedback!";
const RESOLVED = "This conversation has been resolved.";
// stub-llm.mjs: a question containing "teleporter" gets a [NO_ANSWER] reply.
const GAP_QUESTION = "Do you sell a teleporter for small parcels?";
const NO_ANSWER_REPLY = "Sorry, I could not find that in our help articles.";
const DASH = "—";
const STUB_MODEL = "stub-model";

const chatFrame = (page) => page.frameLocator('iframe[src*="/embed/chat"]');

// A customer on the demo page with the chat open; `say` sends a message, `bubble(text)` finds a bot or agent
// bubble containing `text`.
async function openChat(browser, key) {
  const context = await browser.newContext();
  const site = await context.newPage();
  await site.goto(`${DEMO}/?key=${key}`);
  await site.getByRole("button", { name: "Open chat" }).click();
  const chat = chatFrame(site);
  await expect(chat.getByText(GREETING)).toBeVisible({ timeout: 30_000 });
  const composer = chat.getByPlaceholder("Type your message...");
  const log = chat.getByRole("log");
  return {
    context,
    chat,
    log,
    say: async (text) => {
      await composer.fill(text);
      await composer.press("Enter");
    },
    bubble: (text) => log.locator("div.rounded-2xl").filter({ hasText: text }),
  };
}

// The widget asks for a rating once the conversation is over; the customer gives `score`.
async function rate(chat, score) {
  await expect(chat.getByText(CSAT_PROMPT)).toBeVisible({ timeout: 15_000 });
  await chat.getByRole("button", { name: String(score), exact: true }).click();
  await expect(chat.getByText(CSAT_THANKS)).toBeVisible();
}

// Owner dashboard KPI card (components/analytics/kpi-cards.jsx) by its title.
const kpi = (page, label) =>
  page
    .getByRole("region", { name: "Key numbers" })
    .locator('[data-slot="card"]')
    .filter({ has: page.getByText(label, { exact: true }) });
const kpiValue = (page, label) => kpi(page, label).locator('[data-slot="card-content"] > div').first();
// A <dt> label's value (<dd>) inside `scope`.
const dd = (scope, label) => scope.locator("dt", { hasText: label }).locator("+ dd");
const card = (page, title) =>
  page.locator('[data-slot="card"]').filter({ has: page.locator('[data-slot="card-title"]', { hasText: title }) });

// Deletes every price row for the stub model (prices are platform-wide), from the admin's prices page.
async function deleteStubPrices(admin) {
  await admin.goto("/admin/prices");
  await expect(admin.getByRole("heading", { name: "Model prices" })).toBeVisible();
  await expect(admin.getByRole("button", { name: "Add price" })).toBeVisible(); // the list has loaded
  const stubPrices = admin.getByRole("table", { name: "Model prices" }).getByRole("row").filter({ hasText: STUB_MODEL });
  while ((await stubPrices.count()) > 0) {
    const before = await stubPrices.count();
    await stubPrices.first().getByRole("button", { name: "Delete" }).click();
    const confirm = admin.getByRole("dialog", { name: "Delete model price" });
    await confirm.getByRole("button", { name: "Delete" }).click();
    await expect(confirm).toBeHidden();
    await expect(stubPrices).toHaveCount(before - 1);
  }
}

// The platform admin's page, kept for the cleanup: the stub model's price must not outlive the test (it would
// price every later run's turns), whether the test passes or fails.
let adminPage = null;
test.afterEach(async () => {
  if (!adminPage) return;
  const admin = adminPage;
  adminPage = null;
  try {
    await deleteStubPrices(admin);
  } finally {
    await admin.context().close();
  }
});

test("analytics: the dashboard numbers match what happened, cost appears once priced, admin pages count the org", async ({
  page,
  browser,
  request,
}) => {
  test.setTimeout(480_000);
  await request.post(`${STUB}/__reset`);
  const rand = randomSuffix();

  // 1. Setup: owner, stub chat model, KB, widget, 24x7 hours, owner Online in the inbox; a platform admin
  // (signed up, then flagged with the operator CLI).
  const { orgId, orgName } = await signUpAndCreateOrg(page, { name: OWNER, orgName: `Metrics ${rand}` });
  await useOpenAiCompatibleChatModel(page, orgId, { baseUrl: `${STUB}/v1`, model: STUB_MODEL });
  await createIndexedKnowledgeBase(page, orgId, { name: "Returns KB", file: FIXTURE });
  const key = await createWidget(page, orgId, { kbName: "Returns KB" });
  await setAlwaysOpen(page, orgId);
  await page.goto(`/orgs/${orgId}/inbox`);
  await expect(page.getByRole("heading", { name: "Inbox" })).toBeVisible();
  await setAgentStatus(page, "Online");

  const adminEmail = `admin+${rand}@example.com`;
  const adminContext = await browser.newContext();
  const admin = await adminContext.newPage();
  adminPage = admin;
  await admin.goto("/signup");
  await expect(admin.getByRole("button", { name: "Sign up" })).toBeEnabled();
  await admin.getByLabel("Name").fill("Pat Platform");
  await admin.getByLabel("Email").fill(adminEmail);
  await admin.getByLabel("Password").fill(PASSWORD);
  await admin.getByRole("button", { name: "Sign up" }).click();
  await expect(admin).toHaveURL(/\/onboarding$/);
  expect(await runSupportCli("make-platform-admin", adminEmail)).toContain("is now a platform admin");

  // Prices are platform-wide: a price for the stub model left by an interrupted run goes, so A to C start unpriced.
  await deleteStubPrices(admin);

  // 2a. Conversation A: the bot answers, the customer ends the chat (AI-resolved) and rates 5.
  const a = await openChat(browser, key);
  await a.say("How long do I have to return an item?");
  await expect(a.bubble("30 days")).toBeVisible({ timeout: 60_000 });
  await a.chat.getByRole("button", { name: "End chat" }).click();
  await rate(a.chat, 5);
  await a.context.close();

  // 2b. Conversation B: "I want a refund" escalates to the owner (the only one Online), who replies and
  // resolves; the customer rates 4.
  const b = await openChat(browser, key);
  await b.say("I want a refund");
  await expect(b.log.getByText(`${OWNER} has joined the conversation`)).toBeVisible({ timeout: 30_000 });
  await page.getByRole("navigation", { name: "Inbox views" }).getByRole("button", { name: /^Mine/ }).click();
  const mine = page.getByRole("list", { name: "Conversations" }).getByRole("link");
  await expect(mine).toHaveCount(1, { timeout: 15_000 });
  await mine.click();
  const pane = page.getByRole("region", { name: "Conversation", exact: true });
  await pane.getByLabel("Reply", { exact: true }).fill("Sure, your refund is on its way.");
  await pane.getByLabel("Reply", { exact: true }).press("Enter");
  await expect(b.bubble("your refund is on its way")).toBeVisible({ timeout: 15_000 });
  await pane.getByRole("button", { name: "Resolve", exact: true }).click();
  await expect(pane.locator("header").getByText("Resolved", { exact: true })).toBeVisible();
  await expect(b.chat.getByText(RESOLVED).first()).toBeVisible({ timeout: 15_000 });
  await rate(b.chat, 4);
  await b.context.close();

  // 2c. Conversation C: the bot cannot answer (a knowledge gap); it stays open with the bot.
  const c = await openChat(browser, key);
  await c.say(GAP_QUESTION);
  await expect(c.bubble(NO_ANSWER_REPLY)).toBeVisible({ timeout: 60_000 });
  await expect(c.bubble("[NO_ANSWER]")).toHaveCount(0); // the marker never reaches the customer

  // 3. Rollup: closed hours from the CLI backfill; the open hour is computed live either way. This run's events
  // all fall inside the dashboards' live window, so the numbers below do not prove the rollup: its correctness
  // is covered by the unit tests and by the API test that reads a closed hour from the rollup tables only
  // (apps/api/tests/test_analytics_routes.py::test_overview_reads_a_closed_hour_from_the_rollup).
  expect(await rollUpAnalytics(orgId)).toMatch(/^rolled up \d+ hours$/m);

  // 4. Owner dashboard, last 7 days (the default range).
  await page.goto(`/orgs/${orgId}`);
  await expect(kpiValue(page, "Conversations")).toHaveText("3", { timeout: 30_000 });
  await expect(kpiValue(page, "Resolved")).toHaveText("2");
  await expect(kpiValue(page, "AI resolution rate")).toHaveText("50%");
  await expect(kpi(page, "AI resolution rate")).toContainText("1 resolved by the bot");
  await expect(kpi(page, "Escalation rate")).toContainText("1 reached the team");
  await expect(kpiValue(page, "Escalation rate")).toHaveText("33.3%");

  const reasons = card(page, "Escalation reasons");
  await expect(reasons.getByRole("listitem")).toHaveCount(1);
  await expect(reasons.getByRole("listitem")).toHaveText(/^Refunds\s*1$/);

  await expect(dd(page, "CSAT (1 to 5)")).toHaveText("4.50");
  await expect(dd(page, "Responses")).toHaveText("2");

  const gaps = card(page, "Knowledge gaps");
  const gap = gaps.getByRole("listitem").filter({ hasText: GAP_QUESTION });
  await expect(gap).toHaveCount(1);
  await expect(gaps.getByRole("listitem")).toHaveCount(1);
  const gapLink = gap.getByRole("link", { name: "Open conversation" });
  await expect(gapLink).toHaveAttribute("href", new RegExp(`^/orgs/${orgId}/inbox/[0-9a-f-]{36}$`));

  const usage = card(page, "AI usage");
  const tokens = async () =>
    Number((await dd(usage, "Input tokens").innerText()).replace(/,/g, "")) +
    Number((await dd(usage, "Output tokens").innerText()).replace(/,/g, ""));
  expect(await tokens()).toBeGreaterThan(0);
  await expect(dd(usage, "Cost")).toHaveText(DASH);
  // The provider id the turns were recorded under, for the price below.
  const modelCell = usage.getByRole("table", { name: "Usage by model" }).getByRole("row").nth(1).getByRole("cell").first();
  await expect(modelCell).toHaveText(new RegExp(`^\\S+ / ${STUB_MODEL}$`));
  const provider = (await modelCell.innerText()).split(" / ")[0];

  // The gap's link opens C in the inbox.
  await gapLink.click();
  await expect(page).toHaveURL(new RegExp(`/orgs/${orgId}/inbox/[0-9a-f-]{36}$`));
  await expect(
    page.getByRole("region", { name: "Conversation", exact: true }).getByRole("list", { name: "Transcript" }).getByText(GAP_QUESTION),
  ).toBeVisible();

  // 5. Prices: the platform admin prices the stub model (dollars per million tokens); a new conversation D is
  // priced, so after the rollup the range has a cost.
  await admin.goto("/admin/prices");
  await expect(admin.getByRole("navigation", { name: "Platform admin" })).toBeVisible();
  await admin.getByRole("button", { name: "Add price" }).click();
  const dialog = admin.getByRole("dialog", { name: "Add model price" });
  await dialog.getByLabel("Provider").fill(provider);
  await dialog.getByLabel("Model").fill(STUB_MODEL);
  await dialog.getByLabel("Input ($ per million tokens)").fill("1000");
  await dialog.getByLabel("Output ($ per million tokens)").fill("1000");
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(dialog).toBeHidden();
  await expect(admin.getByRole("table", { name: "Model prices" }).getByRole("row").filter({ hasText: STUB_MODEL })).toHaveCount(1);

  const d = await openChat(browser, key);
  await d.say("How long do I have to return an item?");
  await expect(d.bubble("30 days")).toBeVisible({ timeout: 60_000 });
  await d.context.close();

  expect(await rollUpAnalytics(orgId)).toMatch(/^rolled up \d+ hours$/m);
  await page.goto(`/orgs/${orgId}`);
  await expect(kpiValue(page, "Conversations")).toHaveText("4", { timeout: 30_000 });
  // A and C were recorded unpriced and keep no cost (B's keyword escalation called no model); D's cost is the
  // range's, shown as a lower bound next to the unpriced turns.
  const cost = dd(card(page, "AI usage"), "Cost");
  await expect(cost).toHaveText(/^≥ \$\d+\.\d+$/);
  await expect(cost.locator("+ div")).toHaveText("2 unpriced turns");

  // 6. Admin pages: the overview counts the organisation as active; its row has the dashboard's 4 conversations.
  await admin.goto("/admin");
  await expect(admin.getByRole("heading", { name: "Platform overview" })).toBeVisible();
  const active = admin.getByRole("group", { name: "Active organisations (7 days)" }).locator("div.text-2xl");
  await expect(active).toHaveText(/^\d[\d,]*$/, { timeout: 30_000 });
  expect(Number((await active.innerText()).replace(/,/g, ""))).toBeGreaterThanOrEqual(1);

  await admin.goto("/admin/orgs");
  const row = admin.getByRole("table", { name: "Organisations" }).getByRole("row").filter({ hasText: orgName });
  await expect(row).toHaveCount(1, { timeout: 30_000 });
  const conversations30d = row.getByRole("cell").nth(2); // Organisation, Members, Conversations (30 days)
  await expect(conversations30d).toHaveText("4");

  await c.context.close();
});
