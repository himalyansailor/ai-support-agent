import { test, expect } from "@playwright/test";
import { PASSWORD, inviteLinkFor, randomSuffix, signUpAndCreateOrg } from "./helpers.js";

test("owner signs up, creates org, invites agent, agent accepts and sees no Team nav", async ({
  page,
  browser,
  request,
}) => {
  const rand = randomSuffix();
  const ownerEmail = `owner+${rand}@example.com`;
  const agentEmail = `agent+${rand}@example.com`;
  const orgName = `Acme ${rand}`;

  // 1. Owner signs up and creates an organisation (language kept, AI step skipped).
  const { orgId } = await signUpAndCreateOrg(page, { name: "Olivia Owner", orgName, email: ownerEmail });

  // 2. Owner invites an agent; the invite shows as pending.
  const nav = page.getByRole("navigation", { name: "Main" });
  await nav.getByRole("link", { name: "Team" }).click();
  await expect(page).toHaveURL(`/orgs/${orgId}/team`);
  await page.getByRole("button", { name: "Invite member" }).click();
  // Scope to the dialog: Next keeps previously visited routes (e.g. /signup) mounted but hidden.
  const dialog = page.getByRole("dialog", { name: "Invite a team member" });
  await dialog.getByLabel("Email").fill(agentEmail);
  await expect(dialog.getByLabel("Role")).toContainText("Agent");
  await dialog.getByRole("button", { name: "Send invite" }).click();
  await expect(page.getByRole("cell", { name: agentEmail })).toBeVisible();

  // 3. Agent opens the emailed link in a fresh context and joins.
  const link = await inviteLinkFor(request, agentEmail);
  const agentContext = await browser.newContext();
  const agentPage = await agentContext.newPage();
  await agentPage.goto(link);
  await agentPage.getByLabel("Name").fill("Alex Agent");
  await agentPage.getByLabel("Password").fill(PASSWORD);
  await agentPage.getByRole("button", { name: `Join ${orgName}` }).click();
  await expect(agentPage).toHaveURL(`/orgs/${orgId}`);

  // 4. Agent sees no Team / Roles nav and is denied on /team.
  const agentNav = agentPage.getByRole("navigation", { name: "Main" });
  await expect(agentNav.getByRole("link", { name: "Dashboard" })).toBeVisible();
  await expect(agentNav.getByRole("link", { name: "Team" })).toHaveCount(0);
  await expect(agentNav.getByRole("link", { name: "Roles" })).toHaveCount(0);
  await agentPage.goto(`/orgs/${orgId}/team`);
  await expect(agentPage.getByText("You don't have access")).toBeVisible();
  await agentContext.close();

  // 5. Owner's audit log records the invite and its acceptance.
  await nav.getByRole("link", { name: "Audit log" }).click();
  await expect(page.getByRole("cell", { name: "member.invited", exact: true })).toBeVisible();
  await expect(page.getByRole("cell", { name: "invite.accepted", exact: true })).toBeVisible();
});
