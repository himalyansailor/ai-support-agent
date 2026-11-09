import path from "node:path";
import { test, expect } from "@playwright/test";
import {
  createIndexedKnowledgeBase,
  createWidget,
  inviteAgent,
  randomSuffix,
  setAgentStatus,
  setAlwaysOpen,
  signUpAndCreateOrg,
  useOpenAiCompatibleChatModel,
} from "./helpers.js";

// Needs the widget spec's servers (stub LLM on :5555, demo page on :5500, `make build-widget`, the API
// with `--ws-max-size 16384`) and Mailpit for the invite (API on SUPPORT_EMAIL_SENDER=smtp).
const STUB = "http://localhost:5555";
const DEMO = "http://localhost:5500";
const FIXTURE = path.join(process.cwd(), "tests", "e2e", "fixtures", "returns.md"); // npm run e2e runs from apps/web
const OWNER = "Olivia Owner";
const AGENT_B = "Bea Agent";
// supportcore/widget/strings.py (en)
const GREETING = "Hi! How can we help you today?";
const JOINED = new RegExp(`^(${OWNER}|${AGENT_B}) has joined the conversation$`);
const NO_AGENT = "Our team isn't available right now. Leave your email and we'll get back to you.";
const EMAIL_PROMPT = "Nobody is available right now. Leave your email and we will get back to you.";
const BACK_TO_BOT = "You're chatting with our assistant again.";
const RESOLVED = "This conversation has been resolved.";
const CSAT_PROMPT = "How was your experience?";
const CSAT_THANKS = "Thanks for your feedback!";
const REPLY = "Hi, I can help with that. **Order number?**";

const chatFrame = (page) => page.frameLocator('iframe[src*="/embed/chat"]');

test("inbox: escalate, assign, reply live, mention, resolve, hand back, nobody available", async ({
  page,
  browser,
  request,
}) => {
  test.setTimeout(420_000);
  await request.post(`${STUB}/__reset`);
  const rand = randomSuffix();
  const orgName = `Acme ${rand}`;

  // 1. Setup: org, stub chat model, KB, widget, 24x7 hours; agent B invited; both Online with the inbox open.
  const { orgId } = await signUpAndCreateOrg(page, { name: OWNER, orgName });
  await useOpenAiCompatibleChatModel(page, orgId, { baseUrl: `${STUB}/v1`, model: "stub-model" });
  await createIndexedKnowledgeBase(page, orgId, { name: "Returns KB", file: FIXTURE });
  const key = await createWidget(page, orgId, { kbName: "Returns KB" });
  await setAlwaysOpen(page, orgId);
  const b = await inviteAgent(page, browser, request, {
    orgId,
    orgName,
    email: `agent+${rand}@example.com`,
    name: AGENT_B,
  });

  const agents = { [OWNER]: page, [AGENT_B]: b.page };
  for (const p of Object.values(agents)) {
    await p.goto(`/orgs/${orgId}/inbox`);
    await expect(p.getByRole("heading", { name: "Inbox" })).toBeVisible();
    await setAgentStatus(p, "Online");
    await p.getByRole("navigation", { name: "Inbox views" }).getByRole("button", { name: /^Mine/ }).click();
    await expect(p.getByText("No conversations here.")).toBeVisible();
  }

  const customer = await browser.newContext();
  const site = await customer.newPage();
  await site.goto(`${DEMO}/?key=${key}`);
  await site.getByRole("button", { name: "Open chat" }).click();
  const chat = chatFrame(site);
  const log = chat.getByRole("log");
  await expect(chat.getByText(GREETING)).toBeVisible({ timeout: 30_000 });
  const composer = chat.getByPlaceholder("Type your message...");
  async function say(text) {
    await composer.fill(text);
    await composer.press("Enter");
  }

  // 2. Escalation: round-robin picks an agent; the widget names them and their "Mine" fills in live.
  await say("I want a refund");
  const joined = log.getByText(JOINED);
  await expect(joined).toHaveCount(1, { timeout: 30_000 });
  const assigneeName = (await joined.innerText()).match(JOINED)[1];
  const otherName = assigneeName === OWNER ? AGENT_B : OWNER;
  const assignee = agents[assigneeName];
  const other = agents[otherName];

  const mine = assignee.getByRole("list", { name: "Conversations" }).getByRole("link");
  await expect(mine).toHaveCount(1, { timeout: 15_000 });
  await expect(other.getByText("No conversations here.")).toBeVisible(); // not in the other agent's Mine
  await expect(mine).toContainText("High");
  await expect(mine).toContainText(/first response (in|due)/);
  await mine.click();
  await expect(assignee).toHaveURL(new RegExp(`/orgs/${orgId}/inbox/[0-9a-f-]+$`));
  const pane = assignee.getByRole("region", { name: "Conversation", exact: true });
  const header = pane.locator("header");
  const transcript = pane.getByRole("list", { name: "Transcript" });
  await expect(header.getByText("Waiting for a person", { exact: true })).toBeVisible();

  // 3. Live reply: Markdown bold in the dashboard and in the widget (agent text renders the same safe
  // Markdown subset there), with the author's name. The customer's answer arrives live and reopens it.
  await pane.getByLabel("Reply", { exact: true }).fill(REPLY);
  await pane.getByLabel("Reply", { exact: true }).press("Enter");
  await expect(transcript.locator("strong", { hasText: "Order number?" })).toBeVisible();
  await expect(header.getByText("Waiting for the customer", { exact: true })).toBeVisible();
  const agentBubble = log.locator("div.rounded-2xl").filter({ hasText: "Order number?" });
  await expect(agentBubble.locator("strong", { hasText: "Order number?" })).toBeVisible({ timeout: 15_000 });
  await expect(agentBubble).toHaveText("Hi, I can help with that. Order number?");
  await expect(log.getByText(assigneeName, { exact: true })).toBeVisible();

  await say("12345");
  await expect(transcript.getByText("12345", { exact: true })).toBeVisible({ timeout: 15_000 });
  await expect(header.getByText("Waiting for a person", { exact: true })).toBeVisible();

  // 4. Mention: a note for the other agent via the picker; they get a toast and a bell badge.
  await pane.getByRole("tab", { name: /^Note/ }).click();
  const note = pane.getByLabel("Note", { exact: true });
  await note.fill(`@${otherName.split(" ")[0]}`);
  await pane.getByRole("listbox", { name: "Mention a teammate" }).getByRole("option", { name: `@${otherName}` }).click();
  await expect(note).toHaveValue(new RegExp(`^@\\[${otherName}\\]\\([0-9a-f-]{36}\\) $`));
  await note.press("End");
  await note.pressSequentially("can you check?");
  await note.press("Enter");
  await expect(pane.getByTestId("note")).toContainText(`@${otherName} can you check?`, { timeout: 30_000 });

  await expect(other.getByText(/^You were mentioned: /)).toBeVisible({ timeout: 15_000 });
  await expect(other.getByRole("button", { name: /^Notifications \(\d+ unread\)$/ })).toBeVisible();
  await expect(log.getByText("can you check?")).toHaveCount(0);

  // 5. Resolve: the widget says so and asks for a rating; the inbox shows it resolved.
  await pane.getByRole("button", { name: "Resolve", exact: true }).click();
  await expect(header.getByText("Resolved", { exact: true })).toBeVisible();
  await expect(chat.getByText(RESOLVED).first()).toBeVisible({ timeout: 15_000 });
  await expect(chat.getByText(CSAT_PROMPT)).toBeVisible();
  await chat.getByRole("button", { name: "5", exact: true }).click();
  await expect(chat.getByText(CSAT_THANKS)).toBeVisible();
  await expect(log.getByText("can you check?")).toHaveCount(0);

  // 6. Hand back: with B away the next escalation goes to the owner; B comes back, takes it over and
  // hands it back to the bot, which answers again.
  const bPage = b.page;
  await setAgentStatus(bPage, "Away");
  await chat.getByRole("button", { name: "Start a new conversation" }).click();
  await say("I want a refund");
  await expect(log.getByText(JOINED)).toHaveText(`${OWNER} has joined the conversation`, { timeout: 30_000 });
  const ownerWaiting = page
    .getByRole("list", { name: "Conversations" })
    .getByRole("link")
    .filter({ hasText: "Waiting for a person" });
  await expect(ownerWaiting).toHaveCount(1, { timeout: 15_000 }); // in the owner's Mine
  await expect(ownerWaiting).toContainText(OWNER);

  await setAgentStatus(bPage, "Online");
  await bPage.getByRole("navigation", { name: "Inbox views" }).getByRole("button", { name: /^All/ }).click();
  const waiting = bPage
    .getByRole("list", { name: "Conversations" })
    .getByRole("link")
    .filter({ hasText: "Waiting for a person" });
  await expect(waiting).toHaveCount(1, { timeout: 15_000 });
  await waiting.click();
  const bPane = bPage.getByRole("region", { name: "Conversation", exact: true });
  await expect(bPane.locator("header").getByText("Waiting for a person", { exact: true })).toBeVisible();
  await expect(bPane.getByLabel("Assignee", { exact: true })).toContainText(OWNER);
  const takeOver = bPane.getByRole("button", { name: "Take over" });
  await expect(takeOver).toBeVisible();
  await takeOver.click();
  await expect(log.getByText(`${AGENT_B} has joined the conversation`)).toBeVisible({ timeout: 15_000 });
  await expect(bPane.getByLabel("Assignee", { exact: true })).toContainText(AGENT_B);
  await bPane.getByRole("button", { name: "Hand back to bot" }).click();
  await expect(bPane.locator("header").getByText("Bot", { exact: true })).toBeVisible();
  await expect(log.getByText(BACK_TO_BOT)).toBeVisible({ timeout: 15_000 });
  await say("How long do I have to return an item?");
  await expect(log.locator("div.rounded-2xl").filter({ hasText: "30 days" })).toBeVisible({ timeout: 60_000 });

  // 7. Nobody available: both agents Away, so the next escalation offers the email form.
  for (const p of Object.values(agents)) await setAgentStatus(p, "Away");
  await say("I want a refund");
  await expect(chat.getByText(NO_AGENT).first()).toBeVisible({ timeout: 30_000 });
  await expect(chat.getByLabel(EMAIL_PROMPT)).toBeVisible();

  await customer.close();
  await b.context.close();
});
