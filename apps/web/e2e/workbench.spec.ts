import path from "node:path";

import AxeBuilder from "@axe-core/playwright";

import { expect, test } from "./fixtures";

async function waitForWorkspace(page: import("@playwright/test").Page) {
  await page.goto("/");
  await expect(page.locator(".workbench-shell")).toHaveAttribute(
    "data-workspace-ready",
    "true",
  );
}

async function runStudioWorkflow(
  page: import("@playwright/test").Page,
  capability: string,
  buttonName: string,
) {
  const responsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().includes(`/api/studios/${capability}/run`),
  );
  await page.getByRole("button", { name: buttonName }).click();
  const response = await responsePromise;
  const responseBody = await response.text();
  expect(response.status(), responseBody).toBe(200);
}

test("[pw.distinct-studios] overview presents six purpose-built research studios", async ({ page }) => {
  await waitForWorkspace(page);

  await expect(
    page.getByRole("heading", { name: /move from question to/i }),
  ).toBeVisible();
  await expect(page.locator(".capability-card")).toHaveCount(6);
  await expect(page.getByText("Evidence control plane")).toBeVisible();
  await expect(page.getByText("Governance is product state")).toBeVisible();
});

test("[pw.literature-open] [pw.literature-protocol] keyboard opens the literature protocol workspace", async ({ page }) => {
  await waitForWorkspace(page);
  const literature = page.getByRole("button", {
    name: /literature review synthesis/i,
  });
  await literature.focus();
  await literature.press("Enter");

  await expect(
    page.getByRole("heading", { name: "Literature Studio", level: 1 }),
  ).toBeVisible();
  await expect(page.getByLabel("Research question")).toHaveValue(
    /auditable retrieval/i,
  );
  await expect(page.getByText("Scholarly sources")).toBeVisible();
  await expect(page.getByText("No screening run yet")).toBeVisible();
});

test("[pw.route-state] workspace routes survive direct links and browser history", async ({
  page,
}) => {
  await page.goto("/?view=dataset");
  await expect(page.locator(".workbench-shell")).toHaveAttribute(
    "data-workspace-ready",
    "true",
  );
  await expect(
    page.getByRole("heading", { name: "Dataset Lab", level: 1 }),
  ).toBeVisible();

  await page.getByLabel("Open project settings").click();
  await expect(page).toHaveURL(/view=settings/);
  await expect(
    page.getByRole("heading", { name: "Project Settings", level: 1 }),
  ).toBeVisible();

  await page.goBack();
  await expect(page).toHaveURL(/view=dataset/);
  await expect(
    page.getByRole("heading", { name: "Dataset Lab", level: 1 }),
  ).toBeVisible();
});

test("visible workbench text never renders below twelve pixels", async ({
  page,
}) => {
  await waitForWorkspace(page);
  for (const view of [
    "Literature Studio",
    "Grant Studio",
    "Matching Explorer",
    "Dataset Lab",
    "Institutional Q&A",
    "Workflow Automation",
  ]) {
    await page.getByRole("button", { name: view, exact: true }).first().click();
    const undersized = await page.locator(".workbench-shell *").evaluateAll(
      (elements) =>
        elements.flatMap((element) => {
          const hasText = [...element.childNodes].some(
            (node) =>
              node.nodeType === Node.TEXT_NODE &&
              Boolean(node.textContent?.trim()),
          );
          const style = window.getComputedStyle(element);
          const visible =
            style.display !== "none" &&
            style.visibility !== "hidden" &&
            element.getClientRects().length > 0;
          const size = Number.parseFloat(style.fontSize);
          return hasText && visible && size < 12
            ? [
                {
                  element: element.tagName.toLowerCase(),
                  className: element.getAttribute("class"),
                  size,
                  text: element.textContent?.trim().slice(0, 80),
                },
              ]
            : [];
        }),
    );
    expect(undersized, `${view} contains undersized text`).toEqual([]);
  }
});

test("[pw.mobile-navigation] interactive targets meet desktop and mobile size floors", async ({
  page,
}) => {
  const undersizedButtons = async (minimum: number) =>
    page.locator("button").evaluateAll(
      (buttons, sizeFloor) =>
        buttons.flatMap((button) => {
          const style = window.getComputedStyle(button);
          const visible =
            style.display !== "none" &&
            style.visibility !== "hidden" &&
            button.getClientRects().length > 0;
          const bounds = button.getBoundingClientRect();
          return visible &&
            (bounds.width + 0.01 < sizeFloor ||
              bounds.height + 0.01 < sizeFloor)
            ? [
                {
                  name:
                    button.getAttribute("aria-label") ??
                    button.textContent?.trim().slice(0, 80),
                  width: bounds.width,
                  height: bounds.height,
                },
              ]
            : [];
        }),
      minimum,
    );

  await waitForWorkspace(page);
  expect(await undersizedButtons(32)).toEqual([]);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByLabel("Open navigation").click();
  expect(await undersizedButtons(44)).toEqual([]);
});

test("[pw.distinct-studios] every studio exposes a distinct workflow and artifact surface", async ({
  page,
}) => {
  await waitForWorkspace(page);
  const cases = [
    ["Grant Studio", "Requirement matrix"],
    ["Matching Explorer", "Match criteria"],
    ["Dataset Lab", "Schema & quality"],
    ["Institutional Q&A", "Authorized corpus"],
    ["Workflow Automation", "Evidence review graph"],
  ] as const;

  for (const [studio, uniqueSurface] of cases) {
    await page
      .getByRole("button", { name: studio, exact: true })
      .first()
      .click();
    await expect(
      page.getByRole("heading", { name: studio, level: 1 }),
    ).toBeVisible();
    await expect(page.getByText(uniqueSurface).first()).toBeVisible();
  }
});

test("[pw.literature-run] [pw.literature-screen] [pw.literature-extract] literature workflow returns screening, extraction, and resolved evidence", async ({
  page,
}) => {
  await waitForWorkspace(page);
  await page
    .getByRole("button", { name: /literature review synthesis/i })
    .click();
  await runStudioWorkflow(
    page,
    "literature",
    "Search & screen evidence",
  );

  await expect(page.locator(".screening-record")).not.toHaveCount(0);
  await page.getByRole("button", { name: "Extract", exact: true }).click();
  await expect(page.getByText("Extraction matrix").first()).toBeVisible();
  await expect(page.locator(".extraction-row")).not.toHaveCount(0);
  await expect(page.locator(".evidence-source-list article")).not.toHaveCount(0);
  await expect(page.getByText(/research-run-/).first()).toBeVisible();
});

test("[pw.operational-surfaces] [pw.run-detail] [pw.connector-test] Library, Runs, and connector settings contain operational data", async ({
  page,
}) => {
  await waitForWorkspace(page);

  await page.getByRole("button", { name: /^Library \d+$/ }).click();
  await expect(page.getByRole("heading", { name: "Library" })).toBeVisible();
  expect(
    await page.locator(".library-row:not(.library-head)").count(),
  ).toBeGreaterThanOrEqual(9);
  await expect(page.getByRole("button", { name: "Ingest source" })).toBeVisible();

  await page
    .getByRole("button", { name: /Runs & approvals \d+/i })
    .first()
    .click();
  await expect(
    page.getByRole("heading", { name: "Runs & Approvals" }),
  ).toBeVisible();
  await page.getByText("Open infrastructure application").first().click();
  await expect(page.getByText("Exact gated action")).toBeVisible();
  await expect(
    page.getByText("research-run-grant-001", { exact: true }),
  ).toBeVisible();

  await page.getByLabel("Open project settings").click();
  await page.getByRole("button", { name: /Connectors 12/i }).click();
  await expect(page.locator(".connector-card")).toHaveCount(12);
  await expect(page.getByText("Foundry Web Search")).toBeVisible();
  await expect(page.getByText("Assigned specialists").first()).toBeVisible();
});

test("[pw.library-ingest] Library ingestion creates a governed item and durable run", async ({
  page,
}) => {
  const title = `New reproducibility protocol ${Date.now()}`;
  await waitForWorkspace(page);
  await page.getByRole("button", { name: /^Library \d+$/ }).click();
  await page.getByRole("button", { name: "Ingest source" }).click();

  const dialog = page.getByRole("dialog", { name: "Add source to Library" });
  await dialog.getByLabel("Title").fill(title);
  await dialog.getByLabel("Source file").setInputFiles({
    name: "protocol.txt",
    mimeType: "text/plain",
    buffer: Buffer.from(
      "Protocol version 1.0\n\nInclude primary studies with explicit methods and limitations.",
    ),
  });
  await dialog.getByLabel("Description").fill(
    "A project-supplied protocol queued for governed extraction and indexing.",
  );
  const responsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().includes("/api/library/upload"),
  );
  await dialog.getByRole("button", { name: "Start ingestion" }).click();
  const response = await responsePromise;
  const payload = (await response.json()) as {
    item: { title: string; status: string };
    run: { durable_instance_id: string };
  };

  expect(response.status()).toBe(200);
  expect(payload.item).toMatchObject({ title, status: "processing" });
  expect(payload.run.durable_instance_id).toMatch(/^research-run-ingest-/);
  await expect(page.getByText(title, { exact: true })).toBeVisible();
});

test("[pw.library-oversize] BFF rejects oversized uploads before API processing", async ({
  page,
  releaseDiagnostics,
}) => {
  await waitForWorkspace(page);
  await page.getByRole("button", { name: /^Library \d+$/ }).click();
  await page.getByRole("button", { name: "Ingest source" }).click();

  const dialog = page.getByRole("dialog", { name: "Add source to Library" });
  await dialog.getByLabel("Title").fill("Oversized protocol");
  await dialog.getByLabel("Description").fill(
    "This upload must be rejected at the BFF boundary.",
  );
  await dialog.getByLabel("Source file").setInputFiles({
    name: "oversized.txt",
    mimeType: "text/plain",
    buffer: Buffer.alloc(21_000_000, "A"),
  });
  releaseDiagnostics.expectConsoleError(
    /status of 413 \(Payload Too Large\)/,
  );
  await dialog.getByRole("button", { name: "Start ingestion" }).click();

  await expect(dialog.getByRole("alert")).toContainText(
    "Request body exceeds",
  );
});

test("[pw.mobile-navigation] mobile navigation opens, closes, and preserves the selected view", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await waitForWorkspace(page);
  await page.getByLabel("Open navigation").click();
  await expect(page.getByLabel("Project navigation")).toHaveAttribute(
    "data-open",
    "true",
  );
  await page
    .getByRole("button", { name: "Dataset Lab", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Dataset Lab", level: 1 }),
  ).toBeVisible();
  await expect(page.getByLabel("Project navigation")).toHaveAttribute(
    "data-open",
    "false",
  );
});

test("all static workbench surfaces pass automated WCAG checks", async ({
  page,
}) => {
  await waitForWorkspace(page);
  const audit = async () => {
    const results = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
      .analyze();
    expect(results.violations).toEqual([]);
  };

  await audit();
  for (const view of [
    "Literature Studio",
    "Grant Studio",
    "Matching Explorer",
    "Dataset Lab",
    "Institutional Q&A",
    "Workflow Automation",
  ]) {
    await page.getByRole("button", { name: view, exact: true }).first().click();
    await audit();
  }
  await page.getByRole("button", { name: /^Library \d+$/ }).click();
  await audit();
  await page
    .getByRole("button", { name: /Runs & approvals \d+/i })
    .first()
    .click();
  await audit();
  await page.getByLabel("Open project settings").click();
  await audit();

  await page.setViewportSize({ width: 390, height: 844 });
  const navigationButton = page.getByLabel("Open navigation");
  await expect(navigationButton).toHaveAttribute("aria-expanded", "false");
  await navigationButton.click();
  await expect(navigationButton).toHaveAttribute("aria-expanded", "true");
  await audit();
});

test("capture the V3 UI foundation at desktop and mobile", async ({ page }) => {
  const outputDirectory = process.env.UX_SCREENSHOT_DIR;
  test.skip(!outputDirectory, "Screenshot directory not requested.");

  const capture = async (name: string, fullPage = true) => {
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.waitForTimeout(100);
    await page.screenshot({
      path: path.join(outputDirectory!, name),
      fullPage,
    });
  };

  await page.setViewportSize({ width: 1536, height: 1000 });
  await waitForWorkspace(page);
  await capture("01-overview-v3-m1.png");

  await page
    .getByRole("button", { name: /literature review synthesis/i })
    .click();
  await capture("02-literature-protocol-v3-m1.png");
  await runStudioWorkflow(
    page,
    "literature",
    "Search & screen evidence",
  );
  await expect(page.locator(".screening-record")).not.toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Search & screen evidence" }),
  ).toBeEnabled();
  await capture("03-literature-results-v3-m1.png");

  await page
    .getByRole("button", { name: "Grant Studio", exact: true })
    .first()
    .click();
  await page
    .getByRole("button", { name: "Parse notice & build package" })
    .click();
  await expect(page.locator(".requirement-done")).not.toHaveCount(0);
  await capture("04-grant-studio-v3-m1.png");

  await page
    .getByRole("button", { name: "Matching Explorer", exact: true })
    .first()
    .click();
  await page
    .getByRole("button", { name: "Build verified shortlist" })
    .click();
  await expect(page.locator(".match-card")).not.toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Build verified shortlist" }),
  ).toBeEnabled();
  await capture("05-matching-explorer-v3-m1.png");

  await page
    .getByRole("button", { name: "Dataset Lab", exact: true })
    .first()
    .click();
  await page
    .getByLabel(
      /I approve sending this bounded dataset to the Foundry Dataset Agent/,
    )
    .check();
  await page
    .getByRole("button", { name: "Analyze with Foundry Code Interpreter" })
    .click();
  await expect(page.locator(".schema-row")).not.toHaveCount(0);
  await expect(
    page.getByRole("button", {
      name: "Analyze with Foundry Code Interpreter",
    }),
  ).toBeEnabled();
  await capture("06-dataset-lab-v3-m1.png");

  await page
    .getByRole("button", { name: "Institutional Q&A", exact: true })
    .first()
    .click();
  await page.getByRole("button", { name: "Resolve policy answer" }).click();
  await expect(page.locator(".answer-card")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Resolve policy answer" }),
  ).toBeEnabled();
  await capture("07-institutional-qa-v3-m1.png");

  await page
    .getByRole("button", { name: "Workflow Automation", exact: true })
    .first()
    .click();
  await page.getByRole("button", { name: "Validate & dry run" }).click();
  await expect(page.getByText("Dry run passed")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Validate & dry run" }),
  ).toBeEnabled();
  await capture("08-workflow-automation-v3-m1.png");

  await page.getByRole("button", { name: /^Library \d+$/ }).click();
  await capture("09-library-v3-m1.png");
  await page
    .getByRole("button", { name: /Runs & approvals \d+/i })
    .first()
    .click();
  await page.getByText("Open infrastructure application").first().click();
  await expect(page.getByText("Exact gated action")).toBeVisible();
  await capture("10-runs-approvals-v3-m1.png");
  await page.getByLabel("Open project settings").click();
  await page.getByRole("button", { name: /Connectors 12/i }).click();
  await capture("11-connectors-settings-v3-m1.png");

  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByLabel("Open navigation").click();
  await capture("12-mobile-navigation-v3-m1.png", false);
});
