import type { Page, TestInfo } from "@playwright/test";

import { completeWorkspaceRequests, expect, test } from "./fixtures";

const STUDIOS = [
  { id: "literature", heading: "Literature Studio", filename: "papers.md" },
  { id: "grant", heading: "Grant Studio", filename: "notice.txt" },
  { id: "matching", heading: "Matching Explorer", filename: "roster.csv" },
  { id: "dataset", heading: "Dataset Lab", filename: "outcomes.csv" },
] as const;

async function openStudio(page: Page, id: string, heading: string) {
  await completeWorkspaceRequests(page, () => page.goto(`/?view=${id}`));
  await expect(page.locator(".workbench-shell")).toHaveAttribute(
    "data-workspace-ready",
    "true",
  );
  await expect(page.getByRole("heading", { name: heading, level: 1 })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Message" })).toBeEnabled();
}

async function sendMessage(page: Page, text: string) {
  const response = page.waitForResponse(
    (candidate) =>
      candidate.request().method() === "POST" &&
      /\/api\/backend\/api\/agent-chat\/threads\/[^/]+\/messages$/.test(
        new URL(candidate.url()).pathname,
      ),
  );
  await page.getByRole("textbox", { name: "Message" }).fill(text);
  await page.getByRole("button", { name: "Send" }).click();
  expect((await response).status()).toBe(200);
  await expect(page.getByText("Local mock runtime").last()).toBeVisible();
}

async function attachFile(page: Page, filename: string) {
  const response = page.waitForResponse(
    (candidate) =>
      candidate.request().method() === "POST" &&
      /\/api\/backend\/api\/agent-chat\/threads\/[^/]+\/files$/.test(
        new URL(candidate.url()).pathname,
      ),
  );
  await page.getByTestId("agent-chat-file-input").setInputFiles({
    name: filename,
    mimeType: filename.endsWith(".csv") ? "text/csv" : "text/plain",
    buffer: Buffer.from("name,value\nalpha,1\nbeta,2\n"),
  });
  expect((await response).status()).toBe(200);
  await expect(page.getByText(filename, { exact: true }).last()).toBeVisible();
}

async function capture(page: Page, testInfo: TestInfo, id: string) {
  const path = testInfo.outputPath(`${id}-${testInfo.project.name}.png`);
  await page.screenshot({ path, fullPage: true });
  await testInfo.attach(id, { path, contentType: "image/png" });
}

test("[pw.agent-chat-composer] [pw.agent-chat-attachments] [pw.agent-chat-thread] shared chat exposes real empty, keyboard, in-flight, success, file, and conversation states [pw.studio.chat.composer:empty][pw.studio.chat.composer:keyboard][pw.studio.chat.composer:loading][pw.studio.chat.composer:success][pw.studio.chat.attachments:empty][pw.studio.chat.attachments:uploading][pw.studio.chat.attachments:ready][pw.studio.chat.thread:empty][pw.studio.chat.thread:conversation]", async ({
  page,
}) => {
  await openStudio(page, "literature", "Literature Studio");
  await expect(page.locator(".agent-chat-message")).toHaveCount(0);
  await expect(page.locator(".agent-chat-pending-files li")).toHaveCount(0);

  let releaseMessage: (() => void) | undefined;
  const messageReleased = new Promise<void>((resolve) => {
    releaseMessage = resolve;
  });
  await page.route("**/api/backend/api/agent-chat/threads/*/messages", async (route) => {
    await messageReleased;
    await route.continue();
  });
  const composer = page.getByRole("textbox", { name: "Message" });
  await composer.pressSequentially("Compare the attached evidence.");
  await expect(composer).toHaveValue("Compare the attached evidence.");
  const messageResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" && response.url().endsWith("/messages"),
  );
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText(/is working/i)).toBeVisible();
  releaseMessage?.();
  expect((await messageResponse).status()).toBe(200);
  await page.unroute("**/api/backend/api/agent-chat/threads/*/messages");
  await expect(page.getByText("Local mock runtime").last()).toBeVisible();

  let releaseUpload: (() => void) | undefined;
  const uploadReleased = new Promise<void>((resolve) => {
    releaseUpload = resolve;
  });
  await page.route("**/api/backend/api/agent-chat/threads/*/files", async (route) => {
    await uploadReleased;
    await route.continue();
  });
  const uploadResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" && response.url().endsWith("/files"),
  );
  await page.getByTestId("agent-chat-file-input").setInputFiles({
    name: "papers.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("primary evidence"),
  });
  await expect(page.getByText("Uploading...")).toBeVisible();
  releaseUpload?.();
  expect((await uploadResponse).status()).toBe(200);
  await page.unroute("**/api/backend/api/agent-chat/threads/*/files");
  await expect(page.getByText("papers.txt", { exact: true })).toBeVisible();

  await sendMessage(page, "Use papers.txt and retain the earlier context.");
  await expect(page.locator(".agent-chat-message.agent-chat-user")).toHaveCount(2);
  await expect(page.locator(".agent-chat-message.agent-chat-assistant")).toHaveCount(2);
});

test("[pw.agent-chat-composer] [pw.agent-chat-attachments] failed turns and uploads remain actionable [pw.studio.chat.composer:error][pw.studio.chat.attachments:error]", async ({
  page,
  releaseDiagnostics,
}) => {
  await openStudio(page, "grant", "Grant Studio");
  releaseDiagnostics.expectConsoleError(/status of 503 \(Service Unavailable\)/);
  releaseDiagnostics.expectConsoleError(/status of 503 \(Service Unavailable\)/);
  await page.route("**/api/backend/api/agent-chat/threads/*/messages", async (route) => {
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: "The selected agent is unavailable." }),
    });
  });
  const composer = page.getByRole("textbox", { name: "Message" });
  await composer.fill("Retry this turn");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.locator(".error-banner[role='alert']")).toContainText(
    "The selected agent is unavailable.",
  );
  await expect(composer).toHaveValue("Retry this turn");
  await page.unroute("**/api/backend/api/agent-chat/threads/*/messages");

  await page.route("**/api/backend/api/agent-chat/threads/*/files", async (route) => {
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: "The session file store is unavailable." }),
    });
  });
  await page.getByTestId("agent-chat-file-input").setInputFiles({
    name: "notice.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("notice"),
  });
  await expect(page.locator('.agent-chat-pending-files li[data-state="failed"]')).toContainText(
    "The session file store is unavailable.",
  );
});

for (const studio of STUDIOS) {
  test(`${studio.heading} completes a chat and attachment flow`, async ({ page }, testInfo) => {
    await openStudio(page, studio.id, studio.heading);
    await expect(page.getByText(/you do not need to configure a workflow/i)).toBeVisible();

    await sendMessage(page, "Remember that the study label is alpha.");
    await attachFile(page, studio.filename);
    await sendMessage(page, `Use ${studio.filename} and summarize the two rows.`);
    await sendMessage(page, "What study label did I give you earlier?");

    await expect(page.locator(".agent-chat-message.agent-chat-user")).toHaveCount(3);
    await expect(page.locator(".agent-chat-message.agent-chat-assistant")).toHaveCount(3);
    await capture(page, testInfo, `agent-chat-${studio.id}`);
  });
}

test("[pw.agent-chat-agent] switching the Foundry agent starts a fresh thread [pw.studio.chat.agent-picker:ready][pw.studio.chat.agent-picker:selected]", async ({ page }) => {
  await openStudio(page, "literature", "Literature Studio");
  await sendMessage(page, "Keep this turn in the authorized-evidence thread.");

  const opened = page.waitForResponse(
    (candidate) =>
      candidate.request().method() === "POST" &&
      new URL(candidate.url()).pathname.endsWith("/api/backend/api/agent-chat/threads"),
  );
  await page.locator(".agent-chat-picker select").selectOption("literature-online-agent");
  expect((await opened).status()).toBe(201);

  await expect(page.getByText("literature-online-agent")).toBeVisible();
  await expect(page.locator(".agent-chat-message")).toHaveCount(0);
  await expect(page.getByText(/you do not need to configure a workflow/i)).toBeVisible();
});
