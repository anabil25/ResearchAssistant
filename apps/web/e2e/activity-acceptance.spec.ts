import { expect, test } from "@playwright/test";


test("live Screening streams safe activity before the final answer", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "chromium", "One live-agent invocation is sufficient.");
  test.setTimeout(360_000);

  const failedResponses: string[] = [];
  const consoleErrors: string[] = [];
  page.on("response", (response) => {
    if (response.status() >= 400) {
      failedResponses.push(`${response.status()} ${response.url()}`);
    }
  });
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });

  await page.goto("/?view=screening");
  await expect(page.getByRole("heading", { name: "Screening Studio" })).toBeVisible();
  await page.getByPlaceholder("Ask the agent anything, or drop a file here").fill(
    "Use the screening-protocol skill to explain why no screening can be performed when no papers or inclusion criteria are supplied. Use only the supplied turn data.",
  );
  await page.getByRole("button", { name: "Send" }).click();

  const liveAssistant = page.locator('.agent-chat-assistant[data-live="true"]');
  const hostedAgentError = page.getByRole("alert").filter({ hasText: "Hosted Agent" });
  await expect(liveAssistant.or(hostedAgentError)).toBeVisible({ timeout: 30_000 });
  await expect(hostedAgentError).toHaveCount(0);
  await expect(liveAssistant).toBeVisible();
  await expect(liveAssistant.getByText("Live", { exact: true })).toBeVisible();

  const liveActivity = liveAssistant.locator("details.agent-chat-activity");
  await expect(liveActivity).toBeVisible();
  expect(await liveActivity.evaluate((element) => (element as HTMLDetailsElement).open)).toBe(true);
  const liveTool = liveActivity.locator(".agent-chat-activity-body li").first();
  await expect(liveTool).toBeVisible({ timeout: 60_000 });
  await expect(liveTool.locator(".agent-chat-activity-status")).toContainText(
    /in progress|running|completed/i,
  );
  await expect(liveAssistant).toBeVisible();
  const liveText = await liveActivity.innerText();
  expect(liveText).toContain("Private reasoning and tool payloads remain hidden.");
  expect(liveText).not.toMatch(
    /call_id|encrypted_content|reasoning_content|"arguments"\s*:|"output"\s*:/i,
  );
  await page.screenshot({ path: "test-results/activity-live-desktop.png", fullPage: true });

  await expect(liveAssistant).toHaveCount(0, { timeout: 300_000 });
  await expect(hostedAgentError).toHaveCount(0);
  const assistant = page.locator('.agent-chat-assistant:not([data-live="true"])').last();
  await expect(assistant).toBeVisible();
  const answer = assistant.getByRole("region", { name: /response$/i });
  await expect(answer).toBeVisible();
  const answerText = await answer.innerText();
  expect(answerText.length).toBeGreaterThan(100);

  const activity = assistant.locator("details.agent-chat-activity");
  await expect(activity).toBeVisible();
  expect(await activity.evaluate((element) => (element as HTMLDetailsElement).open)).toBe(false);
  const facts = await activity.locator(".agent-chat-activity-facts").innerText();
  expect(facts).toMatch(/\d+ tools?/);
  expect(facts).toMatch(/\d+(?:\.\d+)? (?:ms|s)/);
  await activity.locator("summary").click();
  const activityRows = activity.locator(".agent-chat-activity-body li");
  expect(await activityRows.count()).toBeGreaterThanOrEqual(1);
  await expect(activity.locator(".agent-chat-activity-status").last()).toContainText(
    "completed",
  );
  const labels = await activity.locator(".agent-chat-activity-copy strong").allInnerTexts();
  expect(labels).toContain("Load Skill");
  await page.screenshot({ path: "test-results/activity-complete-desktop.png", fullPage: false });
  await activity.locator("summary").click();

  await page.setViewportSize({ width: 390, height: 844 });
  await assistant.scrollIntoViewIfNeeded();
  const overflow = await page.evaluate(() => {
    const viewportWidth = document.documentElement.clientWidth;
    const selectors = [
      ".agent-chat",
      ".agent-chat-header",
      ".agent-chat-transcript",
      ".agent-chat-assistant:last-of-type",
      ".agent-chat-activity",
      ".agent-chat-composer",
    ];
    return selectors.flatMap((selector) =>
      Array.from(document.querySelectorAll<HTMLElement>(selector))
        .filter((element) => {
          const rect = element.getBoundingClientRect();
          return rect.left < -1 || rect.right > viewportWidth + 1 || element.scrollWidth > element.clientWidth + 1;
        })
        .map((element) => {
          const rect = element.getBoundingClientRect();
          return `${selector}:${element.className}:client=${element.clientWidth}:scroll=${element.scrollWidth}:left=${rect.left}:right=${rect.right}`;
        }),
    );
  });
  expect(overflow).toEqual([]);
  await page.screenshot({ path: "test-results/activity-complete-mobile.png", fullPage: true });

  console.log(JSON.stringify({ answerCharacters: answerText.length, facts, labels }));
  expect(failedResponses).toEqual([]);
  expect(consoleErrors).toEqual([]);
});