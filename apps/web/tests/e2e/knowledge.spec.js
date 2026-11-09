import path from "node:path";
import { test, expect } from "@playwright/test";
import { signUpAndCreateOrg } from "./helpers.js";

const FIXTURE = path.join(process.cwd(), "tests", "e2e", "fixtures", "returns-policy.md") // npm run e2e runs from apps/web;

// Needs the API with SUPPORT_JOB_QUEUE=celery plus `make dev-worker` (or SUPPORT_JOB_QUEUE=inline),
// and BAAI/bge-small-en-v1.5 in SUPPORT_MODEL_CACHE_DIR (first run downloads it).
test("owner creates KB with local embeddings, uploads, embeds, previews chunks and tests retrieval", async ({
  page,
}) => {
  test.setTimeout(240_000);

  // 1. Sign up and create an organisation (AI step skipped).
  const { orgId } = await signUpAndCreateOrg(page);

  // 2. Create a knowledge base on the local embedding model with 500/100 recursive chunks.
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Knowledge" }).click();
  await expect(page).toHaveURL(`/orgs/${orgId}/knowledge`);
  await page.getByRole("button", { name: "New knowledge base" }).first().click();
  const dialog = page.getByRole("dialog", { name: "New knowledge base" });
  await dialog.getByLabel("Name").fill("Help Center");
  await expect(dialog.getByLabel("Embedding model")).toContainText("BGE Small (English)");
  await dialog.getByLabel("Chunk size (characters)").fill("500");
  await dialog.getByLabel("Overlap (characters)").fill("100");
  await expect(dialog.getByRole("radio", { name: /Recursive/ })).toBeChecked();
  await dialog.getByRole("button", { name: "Create" }).click();
  await expect(dialog).toBeHidden();
  const kbLink = page.getByRole("link", { name: /Help Center/ });
  await expect(kbLink).toBeVisible();
  await kbLink.click();
  await expect(page).toHaveURL(new RegExp(`/orgs/${orgId}/knowledge/[0-9a-f-]+$`));

  // 3. Upload the fixture; the document row shows Uploaded.
  await page.getByLabel("Choose files to upload").setInputFiles(FIXTURE);
  const docRow = page.getByRole("row", { name: /returns-policy\.md/ });
  await expect(docRow).toBeVisible();
  await expect(docRow.getByText("Uploaded", { exact: true })).toBeVisible();

  // 4. Start embedding; the worker indexes it within 120 s.
  await page.getByRole("button", { name: "Start embedding (1)" }).click();
  await expect(docRow.getByText("Indexed", { exact: true })).toBeVisible({ timeout: 120_000 });
  const chunkCount = Number((await docRow.getByRole("cell").nth(4).innerText()).trim());
  expect(chunkCount).toBeGreaterThan(0);

  // 5. Preview chunks: the first chunk's text is visible.
  await docRow.getByRole("button", { name: "Preview chunks of returns-policy.md" }).click();
  const sheet = page.getByRole("dialog", { name: "returns-policy.md" });
  await expect(sheet.getByText("Chunk 1", { exact: true })).toBeVisible();
  await expect(sheet.getByText("Returns policy").first()).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(sheet).toBeHidden();

  // 6. Playground: retrieval only, then the answer toggle without a chat model.
  await page.getByRole("tab", { name: "Playground" }).click();
  await page.getByLabel("Question").fill("How long do refunds take?");
  await page.getByLabel("Generate answer").click();
  await expect(page.getByLabel("Generate answer")).not.toBeChecked();
  await page.getByRole("button", { name: "Run" }).click();
  // The first query loads the embedding model in the API process, so allow time.
  await expect(page.getByRole("article").first()).toContainText("14 days", { timeout: 60_000 });
  await expect(page.getByRole("heading", { name: "Answer" })).toHaveCount(0);

  await page.getByLabel("Generate answer").click();
  await expect(page.getByLabel("Generate answer")).toBeChecked();
  await page.getByRole("button", { name: "Run" }).click();
  const warning = page.getByRole("status").filter({ hasText: "No chat model is configured" });
  await expect(warning).toBeVisible();
  await expect(warning.getByRole("link", { name: "Set one up in AI settings" })).toHaveAttribute(
    "href",
    `/orgs/${orgId}/settings/ai`,
  );

  // 7. Activity: one succeeded job with the five pipeline steps.
  await page.getByRole("tab", { name: "Activity" }).click();
  const jobRows = page.getByRole("row").filter({ hasText: "returns-policy.md" });
  await expect(jobRows).toHaveCount(1);
  await expect(jobRows.first().getByText("succeeded", { exact: true })).toBeVisible();
  await jobRows.first().getByRole("button", { name: /^Show \(5\)$/ }).click();
  const steps = page.getByRole("listitem").filter({ has: page.locator("span.font-medium") });
  await expect(steps).toHaveText([/download/, /parse/, /chunk/, /embed/, /upsert/]);

  // 8. Uploading the same file again shows the duplicate message.
  await page.getByRole("tab", { name: "Documents" }).click();
  await page.getByLabel("Choose files to upload").setInputFiles(FIXTURE);
  await expect(page.getByRole("alert").filter({ hasText: "already in the knowledge base" })).toBeVisible();
});
