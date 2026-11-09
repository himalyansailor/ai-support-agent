import path from "node:path";
import { test, expect } from "@playwright/test";
import {
  answerTransferLeg,
  apiAccessToken,
  apiTelephonyCalls,
  beatSoftphone,
  connectFakeTwilio,
  createIndexedKnowledgeBase,
  createWidget,
  disconnectFakeTwilio,
  FAKE_TWILIO,
  hangUpCaller,
  inviteAgent,
  membershipIdOf,
  placedCall,
  postCallbackDone,
  randomSuffix,
  repostAfterStream,
  runCallTool,
  setAgentStatus,
  setAlwaysOpen,
  setMemberCalls,
  setTransfers,
  signUpAndCreateOrg,
  useFakeRealtime,
  useOpenAiCompatibleChatModel,
  voiceTelephonyCalls,
} from "./helpers.js";

// Live transfer, the no-answer callback, speech-to-speech and calling back, against apps/voice on :8002 and the API,
// both on the fake Twilio, speech and Realtime models (SUPPORT_ENV=test, SUPPORT_TELEPHONY=fake,
// SUPPORT_VOICE_PUBLIC_URL=http://localhost:8002), with SUPPORT_JOB_QUEUE=inline (or a worker), the stub LLM (:5555)
// and Mailpit (the second agent is invited). call_tool.py plays Twilio: the caller, the agents' legs and the callbacks.
// The browser softphone itself needs a microphone and real Twilio (unit tests cover it); its heartbeat goes through
// the API here, which is what makes an agent reachable in the browser. What Twilio was asked to do (who was dialled,
// what a caller was redirected to) is read back from each process's fake telephony (test-only routes).
const FIXTURE = path.join(process.cwd(), "tests", "e2e", "fixtures", "opening-hours.md"); // npm run e2e runs from apps/web
const STUB = "http://localhost:5555";
const OWNER = "Olivia Owner";
const AGENT = "Bruno Agent";
const TRANSFERRING = "Please hold while I connect you to someone from our team.";
const TRANSFER_FAILED_PHONE =
  "Sorry, no one could take your call just now. Someone will call you back on this number as soon as they can. Goodbye.";

const randomCaller = () => `+1415555${String(Math.floor(Math.random() * 10_000)).padStart(4, "0")}`;

test.describe.serial("voice transfers", () => {
  let page; // the owner, also an agent
  let agent; // { context, page } of the invited agent
  let agentEmail;
  let orgId;
  let orgName;

  async function openView(p, view) {
    await p.goto(`/orgs/${orgId}/inbox`);
    await p.getByRole("navigation", { name: "Inbox views" }).getByRole("button", { name: new RegExp(`^${view}`) }).click();
    return p.getByRole("list", { name: "Conversations" }).getByRole("link");
  }

  test.beforeAll(async ({ browser }) => {
    page = await browser.newPage();
  });

  test.afterAll(async () => {
    test.setTimeout(90_000);
    // Release the number and disconnect Twilio through the product, so a re-run can connect them again.
    if (orgId) await disconnectFakeTwilio(page, orgId);
    await agent?.context.close();
    await page.close();
  });

  test("owner sets up voice with two agents online", async ({ browser, request }) => {
    test.setTimeout(300_000);
    await request.post(`${STUB}/__reset`);
    ({ orgId, orgName } = await signUpAndCreateOrg(page, { name: OWNER, orgName: `Transfers ${randomSuffix()}` }));
    await useOpenAiCompatibleChatModel(page, orgId, { baseUrl: `${STUB}/v1`, model: "stub-model" });
    await createIndexedKnowledgeBase(page, orgId, { name: "Hours KB", file: FIXTURE });
    await createWidget(page, orgId, { name: "Shop site", kbName: "Hours KB" });
    await connectFakeTwilio(page, orgId, { widgetName: "Shop site" });
    await setAlwaysOpen(page, orgId);
    await setTransfers(page, orgId, { browser: true, phone: false });
    agentEmail = `agent+${randomSuffix()}@example.com`;
    agent = await inviteAgent(page, browser, request, { orgId, orgName, email: agentEmail, name: AGENT });
    for (const p of [page, agent.page]) {
      await p.goto(`/orgs/${orgId}/inbox`);
      await expect(p.getByRole("heading", { name: "Inbox" })).toBeVisible();
      await setAgentStatus(p, "Online");
    }
  });

  test("caller is transferred to an online agent", async ({ request }) => {
    test.setTimeout(120_000);
    // Only the agent's browser softphone is registered (the owner has neither a softphone nor a phone).
    const agentToken = await apiAccessToken(request, agentEmail);
    await beatSoftphone(request, orgId, agentToken);
    const caller = randomCaller();
    const call = await runCallTool({ from: caller, say: ["I want to talk to a person"], expectTransfer: true });
    expect(call.closed_by).toBe("server");
    expect(call.spoken).toContain(TRANSFERRING);
    expect(call.conference).toBe(`transfer-${call.transfer_id}`); // the caller waits on hold in the room

    const leg = await answerTransferLeg({ transferId: call.transfer_id, attempt: 1, digits: "none" });
    expect(leg).toMatchObject({ answer: "join", outcome: "joined", join_status: 204 }); // a browser leg joins on answer
    // The leg rang the agent's browser (their softphone identity), and it is the leg that joined.
    const agentMembershipId = await membershipIdOf(request, orgId, agentToken, agentEmail);
    const placed = placedCall(await voiceTelephonyCalls(request), { transfer_id: call.transfer_id, attempt: 1 });
    expect(placed).toMatchObject({ to: `client:agent-${agentMembershipId}`, sid: leg.leg_sid });
    await hangUpCaller({ callSid: call.call_sid, from: caller });

    // The agent took the conversation; the call card names them.
    const rows = await openView(agent.page, "Mine");
    await expect(rows).toHaveCount(1, { timeout: 20_000 });
    await rows.click();
    const pane = agent.page.getByRole("region", { name: "Conversation", exact: true });
    await expect(pane.getByLabel("Assignee", { exact: true })).toContainText(AGENT);
    await expect(pane.getByRole("region", { name: "Call" })).toContainText(`Transferred to ${AGENT}`, {
      timeout: 20_000,
    });
  });

  test("no answer becomes a callback", async ({ request }) => {
    test.setTimeout(180_000);
    // Both agents take calls on their phones; neither presses 1.
    await setTransfers(page, orgId, { browser: true, phone: true });
    await setMemberCalls(page, orgId, OWNER, { phone: "+15550100101", takeCallsOn: "Phone" });
    await setMemberCalls(page, orgId, AGENT, { phone: "+15550100102", takeCallsOn: "Phone" });
    await page.goto(`/orgs/${orgId}/inbox`);
    await expect(page.getByRole("heading", { name: "Inbox" })).toBeVisible();

    const caller = randomCaller();
    const call = await runCallTool({ from: caller, say: ["I want to talk to a person"], expectTransfer: true });
    expect(call.spoken).toContain(TRANSFERRING);
    expect(call.transfer_id).toBeTruthy();
    for (const attempt of [1, 2]) {
      const leg = await answerTransferLeg({ transferId: call.transfer_id, attempt, digits: "none" });
      expect(leg).toMatchObject({ answer: "gather", outcome: "no_answer" }); // a phone leg must press 1
    }
    const dialled = [1, 2].map(
      async (attempt) => placedCall(await voiceTelephonyCalls(request), { transfer_id: call.transfer_id, attempt })?.to,
    );
    expect((await Promise.all(dialled)).sort()).toEqual(["+15550100101", "+15550100102"]); // both agents' phones
    // Nobody is left to ring: the live caller is redirected out of the room to the callback promise.
    await expect
      .poll(async () => {
        const redirects = (await voiceTelephonyCalls(request)).update_call.filter((u) => u.call_sid === call.call_sid);
        return redirects.map((u) => u.twiml ?? "").join("");
      })
      .toContain(TRANSFER_FAILED_PHONE);
    // Twilio would ask for the <Connect action> again; it now says the same line.
    const after = await repostAfterStream({ url: call.after_stream_url, callSid: call.call_sid, from: caller });
    expect(after).toEqual({ said: [TRANSFER_FAILED_PHONE], conference: null });
    await hangUpCaller({ callSid: call.call_sid, from: caller });

    const rows = await openView(page, "Callbacks");
    await expect(rows).toHaveCount(1, { timeout: 20_000 });
    await expect(rows.getByRole("img", { name: "Phone call" })).toBeVisible();
    await rows.click();
    const card = page.getByRole("region", { name: "Conversation", exact: true }).getByRole("region", { name: "Call" });
    await expect(card).toContainText("No one answered");
  });

  test("speech-to-speech mode answers from the knowledge base", async () => {
    test.setTimeout(120_000);
    await useFakeRealtime(page, orgId);
    // The fake Realtime model calls the tool named in the caller's words, then reads its result back.
    const question = 'What are your opening hours? [tool:search_knowledge_base {"query": "opening hours"}]';
    const call = await runCallTool({ from: randomCaller(), realtime: true, say: [question], hangupAfterTurn: 1 });
    expect(call.closed_by).toBe("client");
    expect(call.spoken).toContain(`Hello, you've reached ${orgName}. How can I help you today?`);
    const answer = call.spoken.find((line) => line.startsWith("Tool said:"));
    expect(answer).toContain("Monday to Friday");

    const rows = await openView(page, "All");
    await expect(rows.first()).toBeVisible({ timeout: 20_000 });
    await rows.first().click();
    const transcript = page.getByRole("region", { name: "Conversation", exact: true }).getByRole("list", {
      name: "Transcript",
    });
    await expect(transcript.getByText(question)).toBeVisible({ timeout: 20_000 });
    await expect(transcript.getByText(/Tool said: .*Monday to Friday/)).toBeVisible();
  });

  test("agent calls the customer back by phone", async ({ request }) => {
    test.setTimeout(120_000);
    const rows = await openView(page, "Callbacks");
    await expect(rows).toHaveCount(1, { timeout: 20_000 });
    await rows.click();
    const pane = page.getByRole("region", { name: "Conversation", exact: true });
    // The owner's softphone is off and their profile has an E.164 number: the call rings their phone first.
    const [response] = await Promise.all([
      page.waitForResponse((r) => /\/voice\/agent\/callbacks\/[0-9a-f-]+\/call$/.test(r.url())),
      pane.getByRole("button", { name: "Call my phone" }).click(),
    ]);
    expect(response.status()).toBe(202);
    const { call_id: callId } = await response.json();
    // The API rang the owner's own phone (their profile number) first, from the organisation's number.
    const placed = placedCall(await apiTelephonyCalls(request), { call_id: callId });
    expect(placed).toMatchObject({ to: "+15550100101", from: FAKE_TWILIO.number });

    // The customer answered (the callback's <Dial action>): the callback is done.
    await postCallbackDone({ callId, dialStatus: "completed" });
    await expect(pane.getByText("Called back", { exact: true })).toBeVisible({ timeout: 20_000 });
    await expect(pane.getByRole("button", { name: "Call my phone" })).toHaveCount(0);
  });
});
