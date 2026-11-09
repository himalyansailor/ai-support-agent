import path from "node:path";
import { test, expect } from "@playwright/test";
import {
  PASSWORD,
  apiAccessToken,
  createIndexedKnowledgeBase,
  createWidget,
  randomSuffix,
  runSupportCli,
  signUpAndCreateOrg,
  useOpenAiCompatibleChatModel,
} from "./helpers.js";

// Billing on the fake provider. Start the API for it with
//   SUPPORT_ENV=test SUPPORT_BILLING=fake SUPPORT_JOB_QUEUE=inline make dev-api
// (inline: the plan sync runs in the API process, whose fake also records the meter events read back below), plus the
// widget spec's servers (stub LLM on :5555, demo page on :5500, `make build-widget`) and `uv` with the API's database
// settings in the environment (the spec runs `supportapi.cli make-platform-admin`). The test-only routes it calls
// (/testing/billing/*) exist only with SUPPORT_ENV=test and SUPPORT_BILLING=fake. The other specs need the API back on
// the dev adapter (with fake billing an organisation without a plan is locked, so their bots would not answer).
const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const STUB = "http://localhost:5555";
const DEMO = "http://localhost:5500";
const FIXTURE = path.join(process.cwd(), "tests", "e2e", "fixtures", "returns.md"); // npm run e2e runs from apps/web
// supportcore/widget/strings.py (en)
const GREETING = "Hi! How can we help you today?";
const QUESTION = "How long do I have to return an item?";
const ANSWER = "30 days"; // stub-llm.mjs answers every question with the return window
// lib/billing.js bannerMessage
const MESSAGES_USED_UP = "You've used all your messages for this period — the bot is paused";

const chatFrame = (page) => page.frameLocator('iframe[src*="/embed/chat"]');

// A customer on the demo page with the chat open; `say` sends a message, `answers` are the bot's bubbles with ANSWER.
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
    log,
    say: async (text) => {
      await composer.fill(text);
      await composer.press("Enter");
    },
    answers: log.locator("div.rounded-2xl").filter({ hasText: ANSWER }),
  };
}

// The current UTC calendar month, the period the fake subscription is given (the usage period falls back to it too).
function utcMonth() {
  const now = new Date();
  return {
    start: new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1)).toISOString(),
    end: new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1)).toISOString(),
  };
}

// Plans are platform-wide: the spec's plan is deactivated at the end so later runs and organisations never see it.
let cleanup = null;
test.afterEach(async () => {
  if (!cleanup) return;
  const run = cleanup;
  cleanup = null;
  await run();
});

test("billing: plan synced, trial, messages used up, subscribe, overage reported", async ({ page, browser, request }) => {
  test.setTimeout(300_000);
  await request.post(`${STUB}/__reset`);
  const rand = randomSuffix();
  const planName = `Starter ${rand}`;

  // 1. A platform admin creates "Starter" (2 messages a month, 14-day trial, an overage rate); it shows Synced.
  const adminEmail = `admin+${rand}@example.com`;
  const adminContext = await browser.newContext();
  const admin = await adminContext.newPage();
  await admin.goto("/signup");
  await expect(admin.getByRole("button", { name: "Sign up" })).toBeEnabled();
  await admin.getByLabel("Name").fill("Pat Platform");
  await admin.getByLabel("Email").fill(adminEmail);
  await admin.getByLabel("Password").fill(PASSWORD);
  await admin.getByRole("button", { name: "Sign up" }).click();
  await expect(admin).toHaveURL(/\/onboarding$/);
  expect(await runSupportCli("make-platform-admin", adminEmail)).toContain("is now a platform admin");

  await admin.goto("/admin/plans");
  await expect(admin.getByRole("heading", { name: "Plans" })).toBeVisible();
  await admin.getByRole("button", { name: "Add plan" }).click();
  const form = admin.getByRole("dialog", { name: "Add plan" });
  await form.getByLabel("Key").fill(`starter-${rand}`);
  await form.getByLabel("Name").fill(planName);
  await form.getByLabel(/^Price per month/).fill("49");
  await form.getByLabel("Trial days").fill("14");
  await form.locator("#plan-messages-unlimited").click(); // limits start Unlimited
  await form.getByLabel("Messages per month").fill("2");
  await form.getByLabel(/^Message overage/).fill("0.01");
  await form.getByRole("button", { name: "Save" }).click();
  await expect(form).toBeHidden();

  const adminToken = await apiAccessToken(request, adminEmail);
  const planRow = admin.getByRole("table", { name: "Plans" }).getByRole("row").filter({ hasText: planName });
  await expect(planRow).toHaveCount(1);
  const planId = await (async () => {
    const res = await request.get(`${API}/admin/plans`, { headers: { Authorization: `Bearer ${adminToken}` } });
    expect(res.ok()).toBe(true);
    return (await res.json()).find((p) => p.name === planName).id;
  })();
  cleanup = async () => {
    await request.patch(`${API}/admin/plans/${planId}`, {
      headers: { Authorization: `Bearer ${adminToken}` },
      data: { active: false },
    });
    await adminContext.close();
  };
  // The sync is a job (inline, after the commit): reload until it has run.
  await expect(async () => {
    await admin.reload();
    await expect(planRow.getByText("Synced", { exact: true })).toBeVisible({ timeout: 2_000 });
  }).toPass({ timeout: 30_000 });

  // 2. A new owner signs up; onboarding's Choose-your-plan step starts the trial of Starter (hard stop).
  const { orgId, email: ownerEmail } = await signUpAndCreateOrg(page, {
    choosePlan: async (p) => {
      await p.getByRole("radio", { name: new RegExp(planName) }).check();
      await expect(p.getByRole("radio", { name: "Hard stop" })).toBeChecked();
      await p.getByRole("button", { name: "Start 14-day free trial" }).click();
    },
  });
  await useOpenAiCompatibleChatModel(page, orgId, { baseUrl: `${STUB}/v1`, model: "stub-model" });
  await createIndexedKnowledgeBase(page, orgId, { name: "Returns KB", file: FIXTURE });
  const key = await createWidget(page, orgId, { kbName: "Returns KB" });
  await page.goto(`/orgs/${orgId}/settings/billing`);
  await expect(page.getByTestId("status-chip")).toHaveText("Trial · 14 days left");

  // 3. Three customer messages: the first two are answered, the third is over the limit and goes to the team.
  const first = await openChat(browser, key);
  for (const n of [1, 2]) {
    await first.say(QUESTION);
    await expect(first.answers).toHaveCount(n, { timeout: 60_000 });
  }
  await first.say("And can I return it without the box?");
  await page.goto(`/orgs/${orgId}/inbox`);
  await page.getByRole("button", { name: /^Unassigned/ }).click();
  const waiting = page.getByRole("list", { name: "Conversations" }).getByRole("link").filter({ hasText: "Waiting for a person" });
  await expect(waiting).toHaveCount(1, { timeout: 30_000 });
  await expect(first.answers).toHaveCount(2); // no bot reply to the third
  await first.context.close();
  await page.goto(`/orgs/${orgId}`);
  await expect(page.getByRole("alert").filter({ hasText: MESSAGES_USED_UP })).toBeVisible();

  // 4. Subscribe: the fake checkout comes back to the billing page; the provider's webhook (played by the test-only
  //    route) makes the subscription active and the trial chip goes.
  await page.goto(`/orgs/${orgId}/settings/billing`);
  const card = page.locator('[data-slot="card"]').filter({ has: page.getByText(planName, { exact: true }) });
  await card.getByRole("button", { name: "Subscribe" }).click();
  await expect(page).toHaveURL(new RegExp(`/orgs/${orgId}/settings/billing\\?fake_checkout=\\d+$`));
  const period = utcMonth();
  const activated = await request.post(`${API}/testing/billing/subscription`, {
    data: {
      organisation_id: orgId,
      plan_id: planId,
      status: "active",
      period_start: period.start,
      period_end: period.end,
      event_at: new Date().toISOString(),
    },
  });
  expect(activated.ok()).toBe(true);
  expect(await activated.json()).toEqual({ applied: true });
  await page.reload();
  const chip = page.getByTestId("status-chip");
  await expect(chip).toHaveText("Active");
  await expect(chip).not.toContainText("Trial");
  await expect(card.getByText("Your current plan")).toBeVisible();

  // 5. Overage: the bot answers again past the limit, and report_usage sends a `messages` meter event.
  const overage = page.getByRole("radio", { name: /^Overage/ });
  await overage.click(); // controlled: checked once the API has saved it
  await expect(overage).toBeChecked();
  const ownerToken = await apiAccessToken(request, ownerEmail);
  await expect
    .poll(async () => {
      const res = await request.get(`${API}/orgs/${orgId}/billing`, { headers: { Authorization: `Bearer ${ownerToken}` } });
      return (await res.json()).limit_behaviour;
    })
    .toBe("overage");

  const second = await openChat(browser, key);
  await second.say(QUESTION);
  await expect(second.answers).toHaveCount(1, { timeout: 60_000 });
  await second.context.close();

  // report_usage is periodic (beat): the test-only route runs it in the API process, where the fake records the events.
  await expect
    .poll(
      async () => {
        expect((await request.post(`${API}/testing/billing/report-usage`)).ok()).toBe(true);
        const res = await request.get(`${API}/testing/billing/meter-events`);
        const { meter_events: events } = await res.json();
        return events.filter((e) => e.organisation_id === orgId && e.meter === "messages").length;
      },
      { message: "a messages meter event for the organisation", timeout: 30_000 },
    )
    .toBeGreaterThan(0);
});
