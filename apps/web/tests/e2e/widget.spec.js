import path from "node:path";
import { test, expect } from "@playwright/test";
import { createIndexedKnowledgeBase, signUpAndCreateOrg, useOpenAiCompatibleChatModel } from "./helpers.js";

// Needs, besides the onboarding/knowledge e2e servers: the stub LLM (`node tests/e2e/stub-llm.mjs`),
// the demo page on :5500 (`python3 -m http.server 5500 --directory apps/widget/demo`) and the
// loader built into public/widget/v1.js (`make build-widget`).
const STUB = "http://localhost:5555";
const DEMO = "http://localhost:5500";
const UNLISTED_DEMO = "http://127.0.0.1:5500";
const FIXTURE = path.join(process.cwd(), "tests", "e2e", "fixtures", "returns.md"); // npm run e2e runs from apps/web
const GREETING = "Hi! How can we help you today?";
const NO_AGENT = "Our team isn't available right now. Leave your email and we'll get back to you.";
const EMAIL_PROMPT = "Nobody is available right now. Leave your email and we will get back to you.";
const EMAIL_SAVED = "Thanks, we have your email and will be in touch soon.";

const chatFrame = (page) => page.frameLocator('iframe[src*="/embed/chat"]');

function userTexts(body) {
  return (body.messages || [])
    .filter((m) => m.role === "user")
    .map((m) => (typeof m.content === "string" ? m.content : JSON.stringify(m.content)));
}

test("widget on a third-party page answers with a citation, escalates refunds and is blocked on unlisted origins", async ({
  page,
  context,
  request,
}) => {
  test.setTimeout(300_000);
  await request.post(`${STUB}/__reset`);

  // 1. Setup: org, OpenAI-compatible chat model on the stub, and an indexed knowledge base.
  const { orgId } = await signUpAndCreateOrg(page);
  await useOpenAiCompatibleChatModel(page, orgId, { baseUrl: `${STUB}/v1`, model: "stub-model" });
  await createIndexedKnowledgeBase(page, orgId, { name: "Returns KB", file: FIXTURE });

  // 2. Widget: allowed on localhost:5500, answering from the knowledge base; key from the snippet.
  await page.goto(`/orgs/${orgId}/widgets`);
  await page.getByRole("button", { name: "New widget" }).click();
  const create = page.getByRole("dialog", { name: "New widget" });
  await create.getByLabel("Name").fill("Shop site");
  await create.getByRole("button", { name: "Create" }).click();
  await expect(page).toHaveURL(new RegExp(`/orgs/${orgId}/widgets/[0-9a-f-]+$`));
  await page.getByLabel("Allowed domains").fill("localhost:5500");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  await page.getByLabel("Returns KB").check();
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Saved" })).toBeVisible();
  const snippet = await page.getByLabel("Embed snippet").innerText();
  const key = snippet.match(/data-key="(wk_[A-Za-z0-9_-]{24})"/)?.[1];
  expect(key).toBeTruthy();

  // 3. Answer: on the third-party page, the streamed reply cites returns.md.
  const site = await context.newPage();
  await site.goto(`${DEMO}/?key=${key}`);
  await site.getByRole("button", { name: "Open chat" }).click();
  const chat = chatFrame(site);
  await expect(chat.getByText(GREETING)).toBeVisible({ timeout: 30_000 });
  const composer = chat.getByPlaceholder("Type your message...");
  await composer.fill("How long do I have to return an item?");
  await composer.press("Enter");
  const log = chat.getByRole("log");
  const answer = log.locator("div.rounded-2xl").filter({ hasText: "30 days" });
  await expect(answer).toBeVisible({ timeout: 60_000 });
  await expect(answer.getByRole("listitem").filter({ hasText: "returns.md" })).toBeVisible({ timeout: 30_000 });

  // 4. Escalation: a refund goes to a person by keyword, without the LLM seeing it.
  await composer.fill("I want a refund");
  await composer.press("Enter");
  await expect(chat.getByText(NO_AGENT).first()).toBeVisible({ timeout: 30_000 });
  const email = chat.getByLabel(EMAIL_PROMPT);
  await email.fill("c@x.com");
  await chat.locator("form").filter({ has: email }).getByRole("button", { name: "Send" }).click();
  await expect(chat.getByText(EMAIL_SAVED)).toBeVisible();

  const recorded = await (await request.get(`${STUB}/__requests`)).json();
  expect(recorded.length).toBeGreaterThan(0); // the first question did reach the stub
  const customerTexts = recorded.flatMap(userTexts);
  expect(customerTexts.some((t) => t.includes("return an item"))).toBe(true);
  expect(customerTexts.filter((t) => /refund/i.test(t))).toEqual([]);

  // 5. Inbox: waiting for a person because of "Refunds"; the trace shows rule and source.
  await page.goto(`/orgs/${orgId}/inbox`);
  await page.getByRole("button", { name: /^Unassigned/ }).click();
  const row = page.getByRole("list", { name: "Conversations" }).getByRole("link").filter({ hasText: "Waiting for a person" });
  await expect(row).toHaveCount(1);
  await row.click();
  await expect(page).toHaveURL(new RegExp(`/orgs/${orgId}/inbox/[0-9a-f-]+$`));
  await expect(page.getByLabel("Customer").getByText("Refunds", { exact: true })).toBeVisible();
  const trace = page.getByTestId("turn-trace").filter({ hasText: "Refunds" });
  await expect(trace).toHaveCount(1);
  await trace.locator("summary").click();
  await expect(trace.locator("dt", { hasText: "Rule" }).locator("+ dd")).toHaveText("Refunds");
  await expect(trace.locator("dt", { hasText: "Source" }).locator("+ dd")).toHaveText("keyword");

  // 6. Unlisted origin: frame-ancestors leaves 127.0.0.1:5500 out, so the chat never renders.
  const unlisted = await context.newPage();
  const embedResponse = unlisted.waitForResponse((r) => r.url().includes("/embed/chat"));
  await unlisted.goto(`${UNLISTED_DEMO}/?key=${key}`);
  const csp = (await embedResponse).headers()["content-security-policy"] ?? "";
  expect(csp).toContain("frame-ancestors");
  expect(csp).toContain("http://localhost:5500");
  expect(csp).not.toContain("127.0.0.1");
  await unlisted.getByRole("button", { name: "Open chat" }).click();
  await unlisted.waitForTimeout(3_000); // give a (wrongly) allowed frame time to render
  await expect(chatFrame(unlisted).getByText(GREETING)).toHaveCount(0);
});
