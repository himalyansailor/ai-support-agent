import path from "node:path";
import { test, expect } from "@playwright/test";
import {
  connectFakeTwilio,
  createIndexedKnowledgeBase,
  createWidget,
  disconnectFakeTwilio,
  randomSuffix,
  runCallTool,
  signUpAndCreateOrg,
  useOpenAiCompatibleChatModel,
} from "./helpers.js";

// Needs, besides the inbox e2e servers: apps/voice on :8002 and the API both running with SUPPORT_ENV=test,
// SUPPORT_TELEPHONY=fake and SUPPORT_VOICE_PUBLIC_URL=http://localhost:8002 (the fake Twilio, speech to text and
// text to speech), SUPPORT_JOB_QUEUE=inline (or a worker), the stub LLM (:5555), the demo page (:5500) and
// `make build-widget`. call_tool.py plays the phone caller (Twilio and the caller) with `uv run`.
const FIXTURE = path.join(process.cwd(), "tests", "e2e", "fixtures", "opening-hours.md"); // npm run e2e runs from apps/web
const STUB = "http://localhost:5555";
const DEMO = "http://localhost:5500";
const CALLBACK_PHONE =
  "I'm passing this to our team. Someone will call you back on this number as soon as they can. Goodbye.";

test.describe.serial("voice", () => {
  let page;
  let orgId;
  let orgName;
  let widgetKey;
  let call;
  const caller = `+1415555${String(Math.floor(Math.random() * 10_000)).padStart(4, "0")}`;

  test.beforeAll(async ({ browser, request }) => {
    page = await browser.newPage();
    await request.post(`${STUB}/__reset`);
  });

  test.afterAll(async () => {
    test.setTimeout(90_000);
    // Release the number and disconnect Twilio through the product, so a re-run can connect them again.
    if (orgId) await disconnectFakeTwilio(page, orgId);
    await page.close();
  });

  test("owner connects voice", async () => {
    test.setTimeout(300_000);
    ({ orgId, orgName } = await signUpAndCreateOrg(page, { orgName: `Voice ${randomSuffix()}` }));
    await useOpenAiCompatibleChatModel(page, orgId, { baseUrl: `${STUB}/v1`, model: "stub-model" });
    await createIndexedKnowledgeBase(page, orgId, { name: "Hours KB", file: FIXTURE });
    widgetKey = await createWidget(page, orgId, { name: "Shop site", kbName: "Hours KB" });
    await connectFakeTwilio(page, orgId, { widgetName: "Shop site" });
    const readiness = page.getByTestId("voice-readiness");
    await expect(readiness).toContainText("Voice is ready (phone and browser)");
    await expect(readiness.getByRole("listitem")).toHaveCount(0); // no problems listed
  });

  test("caller talks to the bot and gets a callback", async () => {
    call = await runCallTool({ from: caller, say: ["What are your opening hours?", "I want to talk to a person"] });
    expect(call.closed_by).toBe("server");
    expect(call.spoken).toContain(`Hello, you've reached ${orgName}. How can I help you today?`);
    expect(call.spoken.some((line) => line.includes("30 days"))).toBe(true); // the stub LLM's answer
    expect(call.spoken).toContain(CALLBACK_PHONE);

    // The Callbacks view lists it with a phone icon; the transcript has both caller utterances and the reply.
    await page.goto(`/orgs/${orgId}/inbox`);
    await page.getByRole("navigation", { name: "Inbox views" }).getByRole("button", { name: /^Callbacks/ }).click();
    const rows = page.getByRole("list", { name: "Conversations" }).getByRole("link");
    await expect(rows).toHaveCount(1, { timeout: 20_000 });
    await expect(rows.getByRole("img", { name: "Phone call" })).toBeVisible();
    await rows.click();
    await expect(page).toHaveURL(new RegExp(`/orgs/${orgId}/inbox/[0-9a-f-]+$`));
    const pane = page.getByRole("region", { name: "Conversation", exact: true });
    const transcript = pane.getByRole("list", { name: "Transcript" });
    await expect(transcript.getByText("What are your opening hours?")).toBeVisible({ timeout: 20_000 });
    await expect(transcript.getByText("I want to talk to a person")).toBeVisible();
    await expect(transcript.getByText(/30 days/)).toBeVisible();

    // The call card and the callback actions.
    const card = pane.getByRole("region", { name: "Call" });
    await expect(card).toContainText("Phone");
    await expect(card).toContainText("Handed to team");
    // No microphone here, so the browser softphone is not ready: calling back rings the agent's phone first.
    await expect(pane.getByRole("button", { name: "Call my phone" })).toBeVisible();
    await expect(pane.getByRole("link", { name: /^\+1 415 555 / })).toHaveAttribute("href", `tel:${caller}`);
    await pane.getByRole("button", { name: "Mark called back" }).click();
    await expect(pane.getByText("Called back", { exact: true })).toBeVisible();
    await expect(pane.getByRole("button", { name: "Mark called back" })).toHaveCount(0);
  });

  test("widget shows the call button", async () => {
    // The browser call itself needs a microphone and Twilio (unit tests cover it); the embed offers it.
    const site = page;
    await site.goto(`${DEMO}/?key=${widgetKey}`);
    await site.getByRole("button", { name: "Open chat" }).click();
    const chat = site.frameLocator('iframe[src*="/embed/chat"]');
    await expect(chat.getByRole("button", { name: "Call us" })).toBeVisible({ timeout: 30_000 });
  });
});
