import AxeBuilder from "@axe-core/playwright";
import type { Page, TestInfo } from "@playwright/test";

import { expect, test } from "./fixtures";

// See the matching comment in grant-state-closure.spec.ts: the web app's
// port is dynamically OS-assigned per invocation by playwright.config.ts,
// which memoizes it into PLAYWRIGHT_WEB_PORT for every worker process to
// read -- a stale hardcoded fallback here would make this regex never match
// the real request URL, hanging every waitForRequest/waitForResponse below
// until the test timeout.
const BASE_URL =
  process.env.PLAYWRIGHT_BASE_URL ??
  `http://127.0.0.1:${process.env.PLAYWRIGHT_WEB_PORT ?? "3000"}`;
const LITERATURE_RUN_URL = new RegExp(
  `^${BASE_URL.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}/api/backend/api/studios/literature/run$`,
);

async function gotoView(page: Page, view: string) {
  await page.goto(`/?view=${view}`);
  await expect(page.locator(".workbench-shell")).toHaveAttribute(
    "data-workspace-ready",
    "true",
  );
}

async function capture(page: Page, testInfo: TestInfo, id: string) {
  const filename = `${id}-${testInfo.project.name}.png`;
  const path = testInfo.outputPath(filename);
  await page.screenshot({ path, fullPage: true });
  await testInfo.attach(id, { path, contentType: "image/png" });
}

async function expectAccessible(page: Page) {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(results.violations).toEqual([]);
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

const BASE_RUN = {
  capability: "literature",
  current_stage: "Citation audit complete",
  durable_instance_id: "research-fixture-run",
  id: "fixture-literature-run",
  owner: "Dr. Maya Chen",
  progress: 100,
  started_at: "2026-07-16T12:00:00Z",
  status: "completed",
  title: "Fixture literature synthesis",
};

const BASE_LITERATURE_RESULT = {
  run: BASE_RUN,
  protocol: {
    research_question:
      "Compare current approaches to auditable retrieval-augmented research synthesis.",
    date_from: 2020,
    date_to: 2026,
    sources: ["PubMed", "Europe PMC", "Crossref", "OpenAlex"],
    inclusion_criteria: [
      "Primary or benchmark study",
      "Methods available",
      "Limitations reported",
    ],
    exclusion_criteria: ["No extractable evidence", "Duplicate record"],
  },
  search_queries: [
    "Compare current approaches to auditable retrieval-augmented research synthesis.",
  ],
  candidate_count: 2,
  screening: [
    {
      source_id: "source-1",
      title: "Study A",
      decision: "include",
      reason: "Matches protocol",
      duplicate_group: null,
    },
    {
      source_id: "source-2",
      title: "Study B",
      decision: "include",
      reason: "Needs claim-level follow-up",
      duplicate_group: null,
    },
  ],
  extraction_matrix: [
    {
      source_id: "source-1",
      method: "Method A",
      population: "Population A",
      outcome: "Outcome A",
      limitation: "Limitation A",
      citation_ids: ["cite-1"],
    },
    {
      source_id: "source-2",
      method: "Method B",
      population: "Population B",
      outcome: "Outcome B",
      limitation: "Limitation B",
      citation_ids: ["cite-2"],
    },
  ],
  synthesis: ["Synthesis paragraph."],
  citations: [
    {
      id: "cite-1",
      title: "Study A",
      section: "Results",
      quote: "Quote A",
      source_id: "source-1",
      checksum: "sha256:a",
      license: "CC BY",
      chunk_id: "chunk-1",
      page_start: 1,
    },
    {
      id: "cite-2",
      title: "Study B",
      section: "Discussion",
      quote: "Quote B",
      source_id: "source-2",
      checksum: "sha256:b",
      license: "CC BY",
      chunk_id: "chunk-2",
      page_start: 4,
    },
  ],
  insight: {
    agent_name: "Literature synthesis",
    content: "Analysis with [Study A](https://example.com/study-a).",
    evidence_state: "verified",
    online_research_used: false,
    referenced_source_ids: ["source-1"],
    unresolved_source_ids: ["source-2"],
  },
};

async function mockLiteratureRun(page: Page, results: object | object[]) {
  const queue = Array.isArray(results) ? [...results] : [results];
  await page.route(LITERATURE_RUN_URL, async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue();
      return;
    }

    const next = queue.length > 1 ? queue.shift() : queue[0];
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(next),
    });
  });
}

async function runLiteratureAndCapturePayload(page: Page) {
  const requestPromise = page.waitForRequest(
    (request) =>
      request.method() === "POST" && LITERATURE_RUN_URL.test(request.url()),
  );
  const responsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      LITERATURE_RUN_URL.test(response.url()),
  );

  await page.getByRole("button", { name: "Search & screen evidence" }).click();

  const request = await requestPromise;
  const response = await responsePromise;
  expect(response.status()).toBe(200);
  return request.postDataJSON() as {
    objective: string;
    online_research: boolean;
    inputs: Record<string, unknown>;
  };
}

test.describe("Literature state closure", () => {
  test("[pw.literature.protocol.question:keyboard][pw.literature.protocol.date-window:ready][pw.literature.protocol.date-window:keyboard][pw.literature.protocol.sources:selected] edits protocol fields via keyboard and submits selected sources", async ({
    page,
  }, testInfo) => {
    await mockLiteratureRun(page, clone(BASE_LITERATURE_RESULT));
    await gotoView(page, "literature");

    const question = page.getByLabel("Research question");
    await expect(question).toHaveValue(
      "Compare current approaches to auditable retrieval-augmented research synthesis.",
    );
    await question.focus();
    await page.keyboard.press("Control+A");
    await page.keyboard.type("Audit retrieval quality");
    await expect(question).toHaveValue("Audit retrieval quality");

    const publishedFrom = page.getByLabel("Published from");
    await expect(publishedFrom).toHaveValue("2020");
    await publishedFrom.focus();
    await page.keyboard.press("Control+A");
    await page.keyboard.type("2018");
    await expect(publishedFrom).toHaveValue("2018");

    const currentYear = String(new Date().getFullYear());
    const through = page.getByLabel("Through");
    await expect(through).toHaveValue(currentYear);
    await through.focus();
    await page.keyboard.press("Control+A");
    await page.keyboard.type("2019");
    await expect(through).toHaveValue("2019");

    const arxiv = page.getByRole("checkbox", { name: "arXiv" });
    await expect(arxiv).not.toBeChecked();
    await arxiv.focus();
    await page.keyboard.press(" ");
    await expect(arxiv).toBeChecked();

    const payload = await runLiteratureAndCapturePayload(page);
    expect(payload.objective).toBe("Audit retrieval quality");
    expect(payload.inputs.date_from).toBe(2018);
    expect(payload.inputs.date_to).toBe(2019);
    expect(payload.inputs.sources).toEqual(
      expect.arrayContaining(["PubMed", "Europe PMC", "Crossref", "OpenAlex", "arXiv"]),
    );

    await capture(page, testInfo, "literature-protocol-keyboard-selected-sources");
    await expectAccessible(page);
  });

  test("[pw.literature.protocol.date-window:invalid][pw.literature.protocol.run:disabled] blocks submission for an out-of-order or future-dated window and recovers once corrected", async ({
    page,
  }, testInfo) => {
    await gotoView(page, "literature");

    const publishedFrom = page.getByLabel("Published from");
    const through = page.getByLabel("Through");
    const errorBanner = page.locator("#literature-date-window-error");
    const runButton = page.getByRole("button", {
      name: "Search & screen evidence",
    });

    async function setYear(field: typeof publishedFrom, value: string) {
      await field.focus();
      await page.keyboard.press("Control+A");
      await page.keyboard.type(value);
    }

    await setYear(publishedFrom, "2022");
    await setYear(through, "2019");
    await expect(errorBanner).toBeVisible();
    await expect(errorBanner).toContainText('must not be after "Through"');
    await expect(publishedFrom).toHaveAttribute("aria-invalid", "true");
    await expect(through).toHaveAttribute(
      "aria-describedby",
      "literature-date-window-error",
    );
    await expect(runButton).toBeDisabled();
    await capture(page, testInfo, "literature-date-window-invalid-order");
    await expectAccessible(page);

    const futureYear = String(new Date().getFullYear() + 1);
    await setYear(publishedFrom, "2020");
    await setYear(through, futureYear);
    await expect(errorBanner).toBeVisible();
    await expect(errorBanner).toContainText("future year");
    await expect(runButton).toBeDisabled();
    await capture(page, testInfo, "literature-date-window-invalid-future");
    await expectAccessible(page);

    await setYear(through, "2024");
    await expect(errorBanner).toHaveCount(0);
    await expect(runButton).toBeEnabled();
  });

  test("[pw.literature.protocol.criteria:empty][pw.literature.protocol.criteria:duplicate] ignores blank and duplicate criteria additions", async ({
    page,
  }, testInfo) => {
    await gotoView(page, "literature");

    const inclusionInput = page.getByPlaceholder("Add inclusion criterion");
    const removalButtons = page.locator('button[aria-label^="Remove inclusion criterion:"]');
    await expect(removalButtons).toHaveCount(3);

    await inclusionInput.fill("   ");
    await page.getByRole("button", { name: "Add inclusion criterion" }).click();
    await expect(removalButtons).toHaveCount(3);

    await inclusionInput.fill("Methods available");
    await page.getByRole("button", { name: "Add inclusion criterion" }).click();
    await expect(removalButtons).toHaveCount(3);
    await expect(inclusionInput).toHaveValue("");
    await expect(
      page.getByRole("button", {
        name: "Remove inclusion criterion: Methods available",
      }),
    ).toHaveCount(1);

    await capture(page, testInfo, "literature-criteria-empty-duplicate");
    await expectAccessible(page);
  });

  test("[pw.literature.audit.tab:empty][pw.literature.audit.tab:warning] shows empty audit before a run and unresolved warnings after a run", async ({
    page,
  }, testInfo) => {
    await mockLiteratureRun(page, clone(BASE_LITERATURE_RESULT));
    await gotoView(page, "literature");

    const auditTab = page.getByRole("button", { name: "Audit" });
    await auditTab.click();
    await expect(auditTab).toHaveAttribute("data-active", "true");
    await expect(page.getByText("No screening run yet")).toBeVisible();
    await capture(page, testInfo, "literature-audit-empty");
    await expectAccessible(page);

    await runLiteratureAndCapturePayload(page);
    await expect(page.locator(".audit-board")).toBeVisible();
    await expect(page.locator("[data-audit-status]")).toHaveAttribute(
      "data-audit-status",
      "warning",
    );
    await expect(page.locator("[data-audit-status]")).toContainText(
      "Unresolved references found",
    );
    await expect(page.locator(".audit-board .metric-line")).toContainText(
      "1 unresolved",
    );
    await expect(
      page.locator(".audit-citation-row").filter({ hasText: "Study B" }),
    ).toContainText("Unresolved");

    await capture(page, testInfo, "literature-audit-warning");
    await expectAccessible(page);
  });

  test("[pw.literature.audit.tab:passed] shows a genuine passed outcome only when insight is present with zero unresolved citations", async ({
    page,
  }, testInfo) => {
    const passedResult = {
      ...clone(BASE_LITERATURE_RESULT),
      insight: {
        ...clone(BASE_LITERATURE_RESULT.insight),
        unresolved_source_ids: [],
      },
    };
    await mockLiteratureRun(page, passedResult);
    await gotoView(page, "literature");

    await page.getByRole("button", { name: "Audit" }).click();
    await runLiteratureAndCapturePayload(page);

    await expect(page.locator(".audit-board")).toBeVisible();
    await expect(page.locator("[data-audit-status]")).toHaveAttribute(
      "data-audit-status",
      "passed",
    );
    await expect(page.locator("[data-audit-status]")).toContainText(
      "Passed",
    );
    await expect(page.locator("[data-audit-status]")).toContainText(
      "zero unresolved references",
    );
    await expect(page.locator(".audit-board .metric-line")).toContainText(
      "0 unresolved",
    );

    await capture(page, testInfo, "literature-audit-passed");
    await expectAccessible(page);
  });

  test("[pw.literature.screen.decision:keyboard][pw.literature.screen.tab:partial][pw.literature.extract.tab:partial] applies screening decisions by keyboard and keeps only the remaining extraction rows", async ({
    page,
  }, testInfo) => {
    await mockLiteratureRun(page, clone(BASE_LITERATURE_RESULT));
    await gotoView(page, "literature");
    await runLiteratureAndCapturePayload(page);

    const firstRecord = page.locator(".screening-record").first();
    const exclude = firstRecord.getByRole("button", { name: "Exclude" });
    await exclude.focus();
    await page.keyboard.press("Enter");
    await expect(exclude).toHaveAttribute("data-active", "true");

    const secondRecord = page.locator(".screening-record").nth(1);
    const maybe = secondRecord.getByRole("button", { name: "Maybe" });
    await maybe.click();
    await expect(maybe).toHaveAttribute("data-active", "true");

    await expect(page.locator(".screening-board .metric-line")).toContainText(
      "0 included",
    );
    await expect(page.locator(".screening-board .metric-line")).toContainText(
      "1 excluded",
    );
    await expect(page.locator(".screening-board .metric-line")).toContainText(
      "1 maybe",
    );

    // Also exercises the "maybe" active-state contrast fix (WCAG AA) via the
    // axe scan below -- the color was previously 4.42:1 against the panel
    // background and is now 5.18:1.
    await capture(page, testInfo, "literature-screen-partial");
    await expectAccessible(page);

    await page.getByRole("button", { name: "Extract", exact: true }).click();
    await expect(page.locator(".extraction-row")).toHaveCount(1);
    await expect(page.locator(".extraction-row").first()).toContainText("Study B");
    await expect(page.locator(".extraction-board .panel-heading")).toContainText(
      "1 studies",
    );

    await capture(page, testInfo, "literature-extract-partial");
    await expectAccessible(page);
  });

  test("[pw.literature.extract.tab:empty] shows the empty extraction message when every study is excluded", async ({
    page,
  }, testInfo) => {
    await mockLiteratureRun(page, clone(BASE_LITERATURE_RESULT));
    await gotoView(page, "literature");
    await runLiteratureAndCapturePayload(page);

    for (const row of await page.locator(".screening-record").all()) {
      await row.getByRole("button", { name: "Exclude" }).click();
    }

    await page.getByRole("button", { name: "Extract", exact: true }).click();
    await expect(
      page.getByText(
        "No included study currently has extractable fields. Mark a screening decision as Include or Maybe to populate this matrix.",
      ),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: "Export CSV" })).toBeDisabled();

    await capture(page, testInfo, "literature-extract-empty");
    await expectAccessible(page);
  });

  test("[pw.literature.extract.edit-export:keyboard] supports keyboard-driven extraction edits and export", async ({
    page,
  }, testInfo) => {
    await mockLiteratureRun(page, clone(BASE_LITERATURE_RESULT));
    await gotoView(page, "literature");
    await runLiteratureAndCapturePayload(page);

    await page.getByRole("button", { name: "Extract", exact: true }).click();
    const methodField = page.getByLabel("Method for Study A");
    await methodField.focus();
    await page.keyboard.press("Control+A");
    await page.keyboard.type("Keyboard revised method");
    await expect(methodField).toHaveValue("Keyboard revised method");

    const downloadPromise = page.waitForEvent("download");
    const exportButton = page.getByRole("button", { name: "Export CSV" });
    await exportButton.focus();
    await page.keyboard.press("Enter");
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toBe(
      "extraction-matrix-fixture-literature-run.csv",
    );
    await expect(page.getByRole("status")).toContainText(
      "Exported 2 extraction rows as extraction-matrix-fixture-literature-run.csv.",
    );

    await capture(page, testInfo, "literature-extract-keyboard-export");
    await expectAccessible(page);
  });

  test("[pw.literature.synthesize.tab:empty] renders an empty synthesis panel when the run contains no narrative paragraphs", async ({
    page,
  }, testInfo) => {
    const emptySynthesisResult = clone(BASE_LITERATURE_RESULT);
    emptySynthesisResult.synthesis = [];

    await mockLiteratureRun(page, emptySynthesisResult);
    await gotoView(page, "literature");
    await runLiteratureAndCapturePayload(page);

    await page.getByRole("button", { name: "Synthesize" }).click();
    await expect(page.locator(".synthesis-card")).toBeVisible();
    await expect(page.getByText("Audited synthesis")).toBeVisible();
    await expect(page.locator(".synthesis-card p")).toHaveCount(0);

    await capture(page, testInfo, "literature-synthesize-empty");
    await expectAccessible(page);
  });

  test("[pw.literature.synthesize.tab:unsupported] surfaces unsupported synthesis evidence when unresolved references remain", async ({
    page,
  }, testInfo) => {
    const unsupportedResult = clone(BASE_LITERATURE_RESULT);
    unsupportedResult.synthesis = [
      "One synthesis claim remains unsupported by stored evidence.",
    ];
    unsupportedResult.insight = {
      ...unsupportedResult.insight,
      content:
        "Unsupported synthesis with [Study A](https://example.com/study-a).",
      evidence_state: "unsupported",
      unresolved_source_ids: ["source-2"],
    };

    await mockLiteratureRun(page, unsupportedResult);
    await gotoView(page, "literature");
    await runLiteratureAndCapturePayload(page);

    await page.getByRole("button", { name: "Synthesize" }).click();
    await expect(page.locator(".synthesis-card")).toContainText(
      "One synthesis claim remains unsupported by stored evidence.",
    );
    await expect(page.getByText("Hosted Agent analysis")).toBeVisible();
    await expect(page.locator(".model-insight")).toContainText("unsupported");
    await expect(page.getByText("Unsupported references")).toBeVisible();
    await expect(page.getByRole("note")).toContainText("source-2");

    await capture(page, testInfo, "literature-synthesize-unsupported");
    await expectAccessible(page);
  });
});
