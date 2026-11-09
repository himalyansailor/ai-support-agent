import { test, expect } from "@playwright/test";
import {
  GREENMAIL_SUPPORT,
  connectMailbox,
  fetchMailbox,
  randomSuffix,
  sendCustomerEmail,
  setAgentStatus,
  setAlwaysOpen,
  signUpAndCreateOrg,
} from "./helpers.js";

// Needs GreenMail (`make up-email`: IMAP :3143, SMTP :3025) and the API in dev (private mail hosts allowed) on the
// real mail adapters (SUPPORT_MAIL_INBOUND=imap, SUPPORT_MAIL_OUTBOUND=smtp, the defaults) with
// SUPPORT_JOB_QUEUE=inline (or celery plus `make dev-worker`): Check now and the reply's send run as jobs.
// mail_tool.py runs with `uv` from the repo root.
const REPLY = "We are on it";

// Clicks Check now on Settings -> Email until `check` passes (it throws until then). Check now is rate limited
// (one per 10 s per org) and a sync already holding the mailbox lease makes a new one a no-op, so it is retried.
async function checkNowUntil(page, orgId, check) {
  await expect(async () => {
    await page.goto(`/orgs/${orgId}/settings/email`);
    const button = page.getByRole("button", { name: "Check now" });
    await button.click();
    await expect(
      page.getByText("Checking for new mail. This can take a moment.").or(page.getByRole("alert")).first(),
    ).toBeVisible({ timeout: 15_000 });
    await check();
  }).toPass({ timeout: 240_000, intervals: [2_000, 5_000, 10_000] });
}

test("email: connect a mailbox, thread inbound mail, reply by email with threading headers", async ({ page }) => {
  test.setTimeout(600_000);
  const rand = randomSuffix();
  const customer = `cust-${rand}@example.test`;
  const subject = `Order ${rand} missing`;

  // 1. Owner signs up, the org is open 24x7 and the owner is Online.
  const { orgId } = await signUpAndCreateOrg(page);
  await setAlwaysOpen(page, orgId);
  await page.goto(`/orgs/${orgId}/inbox`);
  await expect(page.getByRole("heading", { name: "Inbox" })).toBeVisible();
  await setAgentStatus(page, "Online");

  // 2. Settings -> Email: GreenMail's support account; Test connection says both sides connect; saved.
  await connectMailbox(page, orgId, GREENMAIL_SUPPORT);

  // 3. The customer emails in; Check now turns it into an email conversation in the inbox.
  const first = await sendCustomerEmail({
    from: customer,
    to: GREENMAIL_SUPPORT.address,
    subject,
    body: "Hi, my order has not arrived yet.",
  });
  const rows = page.getByRole("list", { name: "Conversations" }).getByRole("link").filter({ hasText: subject });
  await checkNowUntil(page, orgId, async () => {
    await page.goto(`/orgs/${orgId}/inbox`);
    await page.getByRole("navigation", { name: "Inbox views" }).getByRole("button", { name: /^All/ }).click();
    await expect(rows).toHaveCount(1, { timeout: 20_000 });
  });
  await expect(rows.getByRole("img", { name: "Email" })).toBeVisible();
  await rows.click();
  await expect(page).toHaveURL(new RegExp(`/orgs/${orgId}/inbox/[0-9a-f-]+$`));
  const conversationUrl = page.url();
  const pane = page.getByRole("region", { name: "Conversation", exact: true });
  const emails = pane.getByTestId("email-message");
  const fromCustomer = emails.filter({ hasText: `From: ${customer}` });
  await expect(fromCustomer).toHaveCount(1);
  await expect(fromCustomer.first()).toContainText(subject);

  // 4. A second email answering the first threads into the same conversation.
  const second = await sendCustomerEmail({
    from: customer,
    to: GREENMAIL_SUPPORT.address,
    subject: `Re: ${subject}`,
    body: "Any update? It was a birthday present.",
    inReplyTo: first,
    references: [first],
  });
  await checkNowUntil(page, orgId, async () => {
    await page.goto(conversationUrl);
    await expect(fromCustomer).toHaveCount(2, { timeout: 20_000 });
  });
  await page.goto(`/orgs/${orgId}/inbox`);
  await page.getByRole("navigation", { name: "Inbox views" }).getByRole("button", { name: /^All/ }).click();
  // Only this subject is checked: GreenMail's support INBOX is shared by every run, and a newly connected mailbox
  // also imports mail from the 10 minutes before it was connected (earlier runs' customers).
  await expect(rows).toHaveCount(1);

  // 5. The agent replies; the reply goes out by email and its delivery status reaches Sent.
  await page.goto(conversationUrl);
  await expect(pane.getByText(`Reply by email to ${customer}`)).toBeVisible();
  const reply = pane.getByLabel("Reply", { exact: true });
  await reply.fill(REPLY);
  await reply.press("Enter");
  const outbound = emails.filter({ hasText: `From: ${GREENMAIL_SUPPORT.address}` });
  await expect(outbound).toHaveCount(1, { timeout: 30_000 });
  // Live via the email.status event relayed by the inbox WebSocket: no reload.
  await expect(outbound.getByTestId("delivery-status").getByText("Sent", { exact: true })).toBeVisible({
    timeout: 120_000,
  });

  // 6. The customer receives the reply, threaded under their second email.
  let received = null;
  await expect
    .poll(
      async () => {
        const inbox = await fetchMailbox({ user: customer, password: customer });
        received = inbox.find((m) => m.subject === `Re: ${subject}`) ?? null;
        return received;
      },
      { message: `reply delivered to ${customer}`, timeout: 60_000, intervals: [1_000, 2_000, 5_000] },
    )
    .not.toBeNull();
  expect(received.in_reply_to).toBe(second);
  expect(received.references).toContain(first);
  expect(received.references).toContain(second);
  expect(received.from).toContain(GREENMAIL_SUPPORT.address);
});
