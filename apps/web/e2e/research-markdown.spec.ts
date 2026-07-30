import AxeBuilder from "@axe-core/playwright";

import { expect, test } from "./fixtures";

test("renders structured agent Markdown without executable or exfiltration sinks", async ({
  page,
}) => {
  const content = [
    "## Findings",
    "",
    "- Evidence is bounded.",
    "- Unsupported links remain blocked.",
    "",
    "[Jump to **Findings**](#findings)",
    "",
    "[Broken destination]()",
    "",
    "| Measure | Value |",
    "| --- | --- |",
    "| Coverage | 100% |",
    "",
    "<script>alert('xss')</script>",
    "<iframe src='https://evil.example'></iframe>",
    "[Run code](javascript:alert('xss'))",
    "[Exfiltrate](https://evil.example/collect?secret=1)",
    "![Tracker](https://evil.example/pixel.gif)",
    "[Verified source](https://evidence.example/source-1)",
    "",
    "```python",
    "x".repeat(20_100),
    "```",
  ].join("\n");
  await page.route(
    "**/api/backend/api/agent-chat/threads/*",
    async (route) => {
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      const threadId = new URL(route.request().url()).pathname.split("/").at(-1);
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          id: threadId,
          capability: "literature",
          agent_name: "literature-agent",
          created_at: "2026-07-22T12:00:00Z",
          updated_at: "2026-07-22T12:01:00Z",
          attachments: [],
          messages: [
            {
              id: "message-user",
              role: "user",
              content: "Render the supplied analysis.",
              created_at: "2026-07-22T12:00:30Z",
              agent_name: null,
              attachments: [],
            },
            {
              id: "message-assistant",
              role: "assistant",
              content,
              created_at: "2026-07-22T12:01:00Z",
              agent_name: "literature-agent",
              attachments: [],
            },
          ],
        }),
      });
    },
  );

  await page.goto("/");
  await expect(page.locator(".workbench-shell")).toHaveAttribute(
    "data-workspace-ready",
    "true",
  );
  await page
    .getByRole("button", { name: /literature review synthesis/i })
    .click();
  await page.getByRole("textbox", { name: "Message" }).fill("Render the supplied analysis.");
  await page.getByRole("button", { name: "Send" }).click();

  const markdown = page.getByRole("region", { name: "literature-agent response" });
  await expect(
    markdown.getByRole("heading", { name: "Findings" }),
  ).toBeVisible();
  await expect(markdown.getByRole("table")).toBeVisible();
  await expect(markdown.locator("script, iframe, img")).toHaveCount(0);
  await expect(
    markdown.getByRole("link", { name: /Run code|Exfiltrate/ }),
  ).toHaveCount(0);
  await expect(
    markdown.getByRole("link", { name: /Verified source/ }),
  ).toHaveCount(0);
  const hashLink = markdown.getByRole("link", {
    name: "Jump to Findings (opens in a new tab)",
  });
  await expect(hashLink).toHaveAttribute("href", "#findings");
  await expect(hashLink).toHaveAttribute("target", "_blank");
  await expect(hashLink).toHaveAttribute("rel", "noopener noreferrer");
  await expect(
    markdown.getByRole("link", { name: /Broken destination/ }),
  ).toHaveCount(0);
  await expect(
    markdown.locator("a", { hasText: "Broken destination" }),
  ).toHaveCount(0);
  await expect(
    markdown.getByText("Broken destination [blocked]"),
  ).toBeVisible();
  await expect(markdown.getByText("[code block truncated]")).toBeVisible();

  const accessibility = await new AxeBuilder({ page })
    .include(".research-markdown")
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(accessibility.violations).toEqual([]);
});
