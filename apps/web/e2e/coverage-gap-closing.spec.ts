import AxeBuilder from "@axe-core/playwright";
import type { Page, TestInfo } from "@playwright/test";

import { expect, test } from "./fixtures";

const HANDHELD = { width: 390, height: 844 };

async function openWorkbenchShell(page: Page, view?: string) {
  await page.goto(view ? `/?view=${view}` : "/");
  await expect(page.locator(".workbench-shell")).toHaveAttribute(
    "data-workspace-ready",
    "true",
    { timeout: 15_000 },
  );
}

async function completeNavigationWithWorkflowRefresh(
  page: Page,
  navigate: () => Promise<void>,
) {
  const workflowsResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "GET" &&
      response.url().endsWith("/api/backend/api/workflows"),
  );
  await navigate();
  expect((await workflowsResponse).ok()).toBe(true);
}

async function recordWorkbenchShot(page: Page, testInfo: TestInfo, id: string) {
  const filename = `${id}-${testInfo.project.name}.png`;
  const path = testInfo.outputPath(filename);
  await page.screenshot({ path, fullPage: true });
  await testInfo.attach(id, { path, contentType: "image/png" });
}

async function expectAccessibleExperience(page: Page) {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(results.violations).toEqual([]);
}

async function readFocusedAriaLabel(page: Page) {
  return page.evaluate(
    () => document.activeElement?.getAttribute("aria-label") ?? null,
  );
}

function settingsSectionButton(page: Page, label: string) {
  return page.locator(
    '.settings-nav[aria-label="Settings sections"] button',
    { hasText: label },
  );
}

async function openConnectorsSection(page: Page) {
  await openWorkbenchShell(page, "settings");
  await settingsSectionButton(page, "Connectors").click();
}

const RUN_FIXTURE_PENDING = {
  id: "fixture-run-waiting",
  durable_instance_id: "research-fixture-run-waiting",
  project_id: "proj-demo",
  capability: "grant",
  title: "Fixture grant package awaiting review",
  status: "waiting_for_approval",
  progress: 86,
  current_stage: "Reviewer approval",
  owner: "Dr. Maya Chen",
  started_at: new Date().toISOString(),
  completed_at: null,
  artifact_count: 4,
  scheduler_managed: false,
  approval_id: "fixture-approval-1",
};
const RUN_FIXTURE_FINISHED = {
  ...RUN_FIXTURE_PENDING,
  id: "fixture-run-completed",
  durable_instance_id: "research-fixture-run-completed",
  capability: "literature",
  title: "Fixture completed literature run",
  status: "completed",
  current_stage: "Citation audit complete",
  completed_at: new Date().toISOString(),
  approval_id: null,
};
const APPROVAL_FIXTURE_PENDING = {
  id: "fixture-approval-1",
  run_id: "fixture-run-waiting",
  title: "Release fixture package for institutional review",
  state: "pending",
  risk: "High",
  gated_action: "Export the fixture package and notify the assigned reviewer.",
  destination: "SharePoint research site / Grant reviews",
  requested_by: "grant-agent",
  requested_at: new Date().toISOString(),
  evidence_summary: "Fixture evidence summary.",
  idempotency_key: "fixture-approval-1-key",
};

async function stubRunAndApprovalEndpoints(
  page: Page,
  runs: unknown[],
  approvals: unknown[],
) {
  await page.route("**/api/backend/api/runs", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(runs),
    });
  });
  await page.route("**/api/backend/api/approvals", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(approvals),
    });
  });
}

test.describe("settings administration coverage", () => {
  test.describe("[pw.connector-filter] connector search and category filters", () => {
    test("[pw.connector-filter] search text and category pills filter the connector catalog and show a no-results state [pw.settings.connectors.search-filter:ready][pw.settings.connectors.search-filter:filtered][pw.settings.connectors.search-filter:empty]", async ({
      page,
    }, testInfo) => {
      await openConnectorsSection(page);

      const cards = page.locator(".connector-grid .connector-card");
      const totalCount = await cards.count();
      expect(totalCount).toBe(12);

      await page.getByPlaceholder("Search connectors").fill("arxiv");
      await expect(cards).toHaveCount(1);
      await expect(cards.first()).toContainText("arXiv");
      await recordWorkbenchShot(page, testInfo, "connector-filter-search");

      await page.getByPlaceholder("Search connectors").fill("zzzznonexistentzzzz");
      await expect(page.getByText("No connectors match this filter.")).toBeVisible();
      await recordWorkbenchShot(page, testInfo, "connector-filter-empty");

      await page.getByPlaceholder("Search connectors").fill("");
      await page.locator(".filter-pills button", { hasText: "Identity" }).click();
      await expect(cards).toHaveCount(2);
      await recordWorkbenchShot(page, testInfo, "connector-filter-identity");

      await page.locator(".filter-pills button", { hasText: "All" }).click();
      await expect(cards).toHaveCount(totalCount);
    });

    test("[pw.connector-filter] search box supports real keyboard typing [pw.settings.connectors.search-filter:keyboard]", async ({
      page,
    }) => {
      await openConnectorsSection(page);
      const cards = page.locator(".connector-grid .connector-card");
      const search = page.getByPlaceholder("Search connectors");
      await search.focus();
      await page.keyboard.type("arxiv");
      await expect(cards).toHaveCount(1);
      await expect(cards.first()).toContainText("arXiv");
      await expectAccessibleExperience(page);
    });
  });

  test.describe("[pw.settings-tabs] settings section navigation", () => {
    test("[pw.settings-tabs] every settings tab opens a distinct, non-blank panel [pw.settings.tabs:ready][pw.settings.tabs:selected]", async ({
      page,
    }, testInfo) => {
      await openWorkbenchShell(page, "settings");
      await expect(
        page.getByRole("heading", { name: "Project Settings", level: 1 }),
      ).toBeVisible();

      const expectations: Array<[string, string]> = [
        ["General", "Workspace profile"],
        ["Agents & Models", "Hosted Agent topology"],
        ["Connectors", "Research data connectors"],
        ["Retrieval & Evidence", "Retrieval & evidence"],
        ["Governance", "Governance & approvals"],
        ["Evaluation", "Release evaluation"],
      ];
      for (const [tab, heading] of expectations) {
        await settingsSectionButton(page, tab).click();
        await expect(page.locator(".settings-content h2").first()).toHaveText(
          heading,
        );
        await expect(page.locator(".settings-content")).not.toBeEmpty();
      }
      await recordWorkbenchShot(page, testInfo, "settings-tabs-evaluation");

      await settingsSectionButton(page, "Readiness").click();
      await expect(page.getByText("APIM / Toolbox")).toBeVisible();
      await recordWorkbenchShot(page, testInfo, "settings-tabs-readiness");
    });

    test("[pw.settings-tabs] tabs are keyboard-activatable and render at mobile viewport [pw.settings.tabs:keyboard][pw.settings.tabs:mobile]", async ({
      page,
    }) => {
      await openWorkbenchShell(page, "settings");
      const connectorsTab = page.locator(
        '.settings-nav[aria-label="Settings sections"] button',
        { hasText: "Connectors" },
      );
      await connectorsTab.focus();
      await page.keyboard.press("Enter");
      await expect(page.locator(".settings-content h2").first()).toHaveText(
        "Research data connectors",
      );

      await page.setViewportSize(HANDHELD);
      await expect(
        page.getByRole("heading", { name: "Project Settings", level: 1 }),
      ).toBeVisible();
      await expect(page.locator(".settings-content")).not.toBeEmpty();
      await expectAccessibleExperience(page);
    });
  });

  test.describe("[pw.connector-enable] connector enable/disable", () => {
    test("[pw.connector-enable] required connectors are locked; optional connectors can be disabled and saved [pw.settings.connectors.enable:enabled][pw.settings.connectors.enable:disabled][pw.settings.connectors.enable:locked]", async ({
      page,
    }, testInfo) => {
      await openConnectorsSection(page);

      await page.locator(".connector-card", { hasText: "PubMed" }).click();
      const pubmedEnable = page.getByLabel("Enable PubMed");
      await expect(pubmedEnable).toBeChecked();
      await expect(pubmedEnable).toBeDisabled();
      await expect(
        page.getByText("Required baseline connectors cannot be disabled."),
      ).toBeVisible();

      await page.locator(".connector-card", { hasText: "arXiv" }).click();
      const arxivEnable = page.getByLabel("Enable arXiv");
      await expect(arxivEnable).toBeEnabled();
      await expect(arxivEnable).toBeChecked();

      let capturedBody: Record<string, unknown> | null = null;
      await page.route("**/api/backend/api/connectors/arxiv", async (route) => {
        capturedBody = route.request().postDataJSON();
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: "arxiv",
            name: "arXiv",
            category: "Literature",
            auth_kind: "none",
            enabled: false,
            assigned_agents: ["literature"],
            capabilities: [],
            data_boundary: "Public metadata",
            secret_status: "not_required",
            last_tested_at: new Date().toISOString(),
            terms_url: "https://arxiv.org/help/api/tou",
          }),
        });
      });
      await arxivEnable.uncheck();
      await page.getByRole("button", { name: "Save configuration" }).click();
      await expect(page.getByText("arXiv configuration saved.")).toBeVisible();
      await recordWorkbenchShot(page, testInfo, "connector-enable-arxiv-disabled");
      expect(capturedBody).toMatchObject({ enabled: false });
    });
  });

  test.describe("[pw.settings-general] settings general project profile form", () => {
    test("[pw.settings-general] validates, persists the full settings payload, and reports success [pw.settings.general.form:ready][pw.settings.general.form:success]", async ({
      page,
    }, testInfo) => {
      await openWorkbenchShell(page, "settings");
      await expect(page.getByRole("textbox", { name: "Project name" })).toHaveValue(
        "AI for equitable clinical research",
      );

      await page.getByRole("textbox", { name: "Project name" }).fill("Equitable clinical research v2");
      await page
        .getByRole("combobox", { name: "Default classification" })
        .selectOption("confidential");
      await page.getByRole("spinbutton", { name: "Retention (days)" }).fill("1000");

      let capturedBody: Record<string, unknown> | null = null;
      await page.route("**/api/backend/api/settings", async (route) => {
        if (route.request().method() !== "PUT") {
          await route.continue();
          return;
        }
        capturedBody = route.request().postDataJSON();
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: route.request().postData() ?? "{}",
        });
      });

      await page.getByRole("button", { name: "Save project settings" }).click();
      await expect(page.getByText("Project settings saved.")).toBeVisible();
      await recordWorkbenchShot(page, testInfo, "settings-general-saved");

      expect(capturedBody).toMatchObject({
        name: "Equitable clinical research v2",
        default_classification: "confidential",
        retention_days: 1000,
      });
      await expect(
        page.getByText("Online research is always opt-in per run"),
      ).toBeVisible();
    });

    test("[pw.settings-general] project name field supports real keyboard typing [pw.settings.general.form:keyboard]", async ({
      page,
    }) => {
      await openWorkbenchShell(page, "settings");
      const nameField = page.getByRole("textbox", { name: "Project name" });
      await nameField.fill("");
      await nameField.focus();
      await page.keyboard.type("Keyboard-typed project name");
      await expect(nameField).toHaveValue("Keyboard-typed project name");
      await expectAccessibleExperience(page);
    });
  });

  test.describe("[pw.connector-assign] connector specialist assignment", () => {
    test("[pw.connector-assign] toggling a specialist checkbox persists the assigned_agents list [pw.settings.connectors.assign:selected][pw.settings.connectors.assign:unselected]", async ({
      page,
    }) => {
      await openConnectorsSection(page);
      await page.locator(".connector-card", { hasText: "Crossref" }).click();

      const grantAssign = page.getByLabel("Assign grant to Crossref");
      const literatureAssign = page.getByLabel("Assign literature to Crossref");
      await expect(literatureAssign).toBeChecked();
      await expect(grantAssign).toBeChecked();

      await grantAssign.uncheck();
      await expect(grantAssign).not.toBeChecked();

      let capturedBody: Record<string, unknown> | null = null;
      await page.route("**/api/backend/api/connectors/crossref", async (route) => {
        capturedBody = route.request().postDataJSON();
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: "crossref",
            name: "Crossref",
            category: "Literature",
            auth_kind: "none",
            enabled: true,
            assigned_agents: ["literature"],
            capabilities: [],
            data_boundary: "Public metadata",
            secret_status: "not_required",
            last_tested_at: new Date().toISOString(),
            terms_url: "https://www.crossref.org/documentation/retrieve-metadata/rest-api/tos/",
          }),
        });
      });
      await page.getByRole("button", { name: "Save configuration" }).click();
      await expect(page.getByText("Crossref configuration saved.")).toBeVisible();
      expect(capturedBody).toMatchObject({ assigned_agents: ["literature"] });
    });
  });

  test.describe("[pw.connector-terms] connector provider terms link", () => {
    const APPROVED_TERMS_FIXTURE_URL = "https://arxiv.org/help/api/tou";

    function stubConnectorCatalog(page: Page, termsUrl: string) {
      return page.route("**/api/backend/api/connectors", async (route) => {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([
            {
              id: "pubmed",
              name: "PubMed",
              category: "Literature",
              auth_kind: "none",
              enabled: true,
              assigned_agents: ["literature"],
              capabilities: ["search"],
              data_boundary: "Public metadata",
              secret_status: "not_required",
              last_tested_at: new Date().toISOString(),
              terms_url: termsUrl,
            },
          ]),
        });
      });
    }

    test("[pw.connector-terms] opens an approved terms URL in a new tab against a controlled, intercepted destination [pw.settings.connectors.terms:ready]", async ({
      page,
    }, testInfo) => {
      await stubConnectorCatalog(page, APPROVED_TERMS_FIXTURE_URL);
      await page.context().route(APPROVED_TERMS_FIXTURE_URL, async (route) => {
        await route.fulfill({
          status: 200,
          contentType: "text/html",
          body: "<!doctype html><title>Intercepted terms fixture</title><body>Intercepted terms fixture: no real third-party network call was made.</body>",
        });
      });

      await openConnectorsSection(page);
      await page.locator(".connector-card", { hasText: "PubMed" }).click();

      const termsLink = page.getByRole("link", { name: /Provider terms/ });
      await expect(termsLink).toHaveAttribute("target", "_blank");
      await expect(termsLink).toHaveAttribute("rel", "noopener noreferrer");
      await expect(termsLink).toHaveAttribute("href", APPROVED_TERMS_FIXTURE_URL);
      await expectAccessibleExperience(page);
      await recordWorkbenchShot(page, testInfo, "connector-terms-ready");

      const [popup] = await Promise.all([
        page.context().waitForEvent("page"),
        termsLink.click(),
      ]);
      let loadError: unknown = null;
      try {
        await popup.waitForLoadState("load");
      } catch (error) {
        loadError = error;
      }
      expect(loadError).toBeNull();
      expect(popup.url()).toBe(APPROVED_TERMS_FIXTURE_URL);
      await expect(popup.getByText("Intercepted terms fixture")).toBeVisible();
      await popup.close();
    });

    test("[pw.connector-terms] blocks an unapproved-host terms URL and shows a visible unavailable state with no clickable link [pw.settings.connectors.terms:blocked-url]", async ({
      page,
    }, testInfo) => {
      await stubConnectorCatalog(page, "https://evil.example.com/terms");

      await openConnectorsSection(page);
      await page.locator(".connector-card", { hasText: "PubMed" }).click();

      const blockedStatus = page.locator('[data-terms-state="blocked-url"]');
      await expect(blockedStatus).toBeVisible();
      await expect(blockedStatus).toHaveAttribute(
        "aria-label",
        "This link targets a host that is not on the approved list.",
      );
      await expect(
        page.getByRole("link", { name: /Provider terms/ }),
      ).toHaveCount(0);

      const pagesBefore = page.context().pages().length;
      await expectAccessibleExperience(page);
      await recordWorkbenchShot(page, testInfo, "connector-terms-blocked-url");
      expect(page.context().pages().length).toBe(pagesBefore);
    });
  });

});

test.describe("[pw.library-filter] library search and type filters", () => {
  test("[pw.library-filter] search text and type pills filter real library records and show a no-results state [pw.library.search-filter:ready][pw.library.search-filter:filtered][pw.library.search-filter:empty]", async ({
    page,
  }, testInfo) => {
    await openWorkbenchShell(page, "library");
    const rows = page.locator(".library-table .library-row:not(.library-head)");
    const totalCount = await rows.count();
    expect(totalCount).toBeGreaterThan(0);

    const search = page.getByPlaceholder("Search title, source, or tag");
    await search.fill("zzzznonexistentzzzz");
    await expect(page.getByText("No sources match this view")).toBeVisible();
    await recordWorkbenchShot(page, testInfo, "library-filter-empty");

    await search.fill("");
    await expect(rows).toHaveCount(totalCount);

    const firstKindPill = page
      .locator('.filter-pills[aria-label="Filter library by type"] button')
      .nth(1);
    const kindName = (await firstKindPill.innerText()).trim();
    await firstKindPill.click();
    await expect(firstKindPill).toHaveAttribute("data-active", "true");
    const filteredCount = await rows.count();
    expect(filteredCount).toBeGreaterThan(0);
    expect(filteredCount).toBeLessThanOrEqual(totalCount);
    await recordWorkbenchShot(page, testInfo, `library-filter-${kindName.toLowerCase()}`);

    await page
      .locator('.filter-pills[aria-label="Filter library by type"] button')
      .first()
      .click();
    await expect(rows).toHaveCount(totalCount);
  });

  test("[pw.library-filter] search box supports real keyboard typing [pw.library.search-filter:keyboard]", async ({
    page,
  }) => {
    await openWorkbenchShell(page, "library");
    const rows = page.locator(".library-table .library-row:not(.library-head)");
    const totalCount = await rows.count();
    expect(totalCount).toBeGreaterThan(0);

    const search = page.getByPlaceholder("Search title, source, or tag");
    await search.focus();
    await page.keyboard.type("zzzznonexistentzzzz");
    await expect(page.getByText("No sources match this view")).toBeVisible();
    await expectAccessibleExperience(page);
  });
});


test.describe("shell interaction coverage", () => {
  test.describe("[pw.keyboard-shell] shell keyboard interactions", () => {
    test("[pw.keyboard-shell] Escape closes the mobile navigation, moves focus into the drawer on open, and restores focus to the trigger on close [pw.shell.navigation.close-mobile:ready][pw.shell.navigation.close-mobile:keyboard][pw.shell.navigation.close-mobile:mobile]", async ({
      page,
    }, testInfo) => {
      await page.setViewportSize(HANDHELD);
      await openWorkbenchShell(page);
      const openNavButton = page.getByLabel("Open navigation");
      await openNavButton.click();
      await expect(page.getByLabel("Project navigation")).toHaveAttribute(
        "data-open",
        "true",
      );
      await expect(openNavButton).toHaveAttribute("aria-expanded", "true");
      await recordWorkbenchShot(page, testInfo, "keyboard-shell-mobile-nav-open");

      const openActiveLabel = await readFocusedAriaLabel(page);
      expect(openActiveLabel).toBe("Close navigation");

      await page.keyboard.press("Escape");
      await expect(page.getByLabel("Project navigation")).toHaveAttribute(
        "data-open",
        "false",
      );
      await expect(openNavButton).toHaveAttribute("aria-expanded", "false");
      await recordWorkbenchShot(page, testInfo, "keyboard-shell-mobile-nav-closed");

      const closedActiveLabel = await readFocusedAriaLabel(page);
      expect(closedActiveLabel).toBe("Open navigation");
      await expectAccessibleExperience(page);
    });
  });

});

test.describe("run navigation coverage", () => {
  test.describe("[pw.approval-decision] approval rationale and decision recording", () => {
    test("[pw.approval-decision] requires a rationale and records an approval decision with the exact payload [pw.approvals.rationale:empty][pw.approvals.rationale:valid][pw.approvals.rationale:invalid][pw.approvals.decide:pending][pw.approvals.decide:approved]", async ({
      page,
    }) => {
      await stubRunAndApprovalEndpoints(page, [RUN_FIXTURE_PENDING], [APPROVAL_FIXTURE_PENDING]);
      await openWorkbenchShell(page, "runs");
      await page.locator(".detailed-run-list button").first().click();

      const approveButton = page.getByRole("button", {
        name: "Approve exact action",
      });
      await approveButton.click();
      await expect(
        page.getByText("Add a rationale before recording a decision."),
      ).toBeVisible();

      const rationaleField = page.getByRole("textbox", { name: /rationale/i }).or(
        page.locator("textarea"),
      );
      await rationaleField.first().fill("Reviewed compliance mapping; approved for release.");

      let capturedBody: { decision: string; rationale: string } | null = null;
      await page.route(
        "**/api/backend/api/approvals/fixture-approval-1/decision",
        async (route) => {
          capturedBody = route.request().postDataJSON();
          await route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({ ...APPROVAL_FIXTURE_PENDING, state: "approved" }),
          });
        },
      );
      await approveButton.click();
      await expect
        .poll(() => capturedBody)
        .toEqual({
          decision: "approved",
          rationale: "Reviewed compliance mapping; approved for release.",
        });
    });
  });

  test.describe("[pw.runs-filter] runs status filters", () => {
    test("[pw.runs-filter] filters runs by status and preserves a valid selected run when the filter excludes it [pw.runs.filter:all][pw.runs.filter:filtered][pw.runs.filter:empty]", async ({
      page,
    }, testInfo) => {
      await stubRunAndApprovalEndpoints(
        page,
        [RUN_FIXTURE_PENDING, RUN_FIXTURE_FINISHED],
        [APPROVAL_FIXTURE_PENDING],
      );
      await openWorkbenchShell(page, "runs");
      const tabs = page.locator('.runs-tabs[aria-label="Filter runs"] button');
      await expect(tabs).toHaveCount(4);

      await page.getByRole("button", { name: "Needs approval" }).click();
      await expect(page.locator(".detailed-run-list button")).toHaveCount(1);
      await page.locator(".detailed-run-list button").first().click();
      await expect(page.locator(".run-overview h2")).toHaveText(
        "Fixture grant package awaiting review",
      );
      await recordWorkbenchShot(page, testInfo, "runs-filter-needs-approval");

      await page.getByRole("button", { name: "Completed" }).click();
      await expect(page.locator(".detailed-run-list button")).toHaveCount(1);
      await expect(page.locator(".run-overview h2")).toHaveText(
        "Fixture completed literature run",
      );

      await page.getByRole("button", { name: "Running", exact: true }).click();
      await expect(page.locator(".detailed-run-list button")).toHaveCount(0);
      await expect(page.getByText("No durable runs available")).toBeVisible();
      await recordWorkbenchShot(page, testInfo, "runs-filter-running-empty");

      await page.getByRole("button", { name: "All", exact: true }).click();
      await expect(page.locator(".detailed-run-list button")).toHaveCount(2);
    });

    test("[pw.runs-filter] status filter tabs are keyboard-activatable [pw.runs.filter:keyboard]", async ({
      page,
    }) => {
      await stubRunAndApprovalEndpoints(
        page,
        [RUN_FIXTURE_PENDING, RUN_FIXTURE_FINISHED],
        [APPROVAL_FIXTURE_PENDING],
      );
      await openWorkbenchShell(page, "runs");
      const completedTab = page.getByRole("button", {
        name: "Completed",
        exact: true,
      });
      await completedTab.focus();
      await page.keyboard.press("Enter");
      await expect(completedTab).toHaveAttribute("data-active", "true");
      await expect(page.locator(".detailed-run-list button")).toHaveCount(1);
      await expectAccessibleExperience(page);
    });
  });

  test.describe("[pw.overview-runs] overview run list and preselection", () => {
    test("[pw.overview-runs] run rows and View all runs navigate to Runs but do not preselect the clicked run (documented defect) [pw.overview.open-runs:ready]", async ({
      page,
    }, testInfo) => {
      await stubRunAndApprovalEndpoints(
        page,
        [RUN_FIXTURE_PENDING, RUN_FIXTURE_FINISHED],
        [APPROVAL_FIXTURE_PENDING],
      );
      await openWorkbenchShell(page);
      const runRows = page.locator(".work-in-motion .run-list button");
      await expect(runRows).toHaveCount(2);
      const rowCount = await runRows.count();
      expect(rowCount).toBeGreaterThan(1);

      const firstRowTitle = (
        await runRows.nth(0).locator("strong").innerText()
      ).trim();
      const secondRowTitle = (
        await runRows.nth(1).locator("strong").innerText()
      ).trim();
      expect(secondRowTitle).not.toBe(firstRowTitle);
      await recordWorkbenchShot(page, testInfo, "overview-runs-list");

      await completeNavigationWithWorkflowRefresh(page, () =>
        runRows.nth(1).click(),
      );
      await expect(
        page.getByRole("heading", { name: "Runs & Approvals", level: 1 }),
      ).toBeVisible();
      const selectedHeading = await page
        .locator(".run-overview h2")
        .first()
        .innerText();
      expect(selectedHeading.trim()).toBe(firstRowTitle);
      expect(selectedHeading.trim()).not.toBe(secondRowTitle);

      await stubRunAndApprovalEndpoints(
        page,
        [RUN_FIXTURE_PENDING, RUN_FIXTURE_FINISHED],
        [APPROVAL_FIXTURE_PENDING],
      );
      await openWorkbenchShell(page);
      await completeNavigationWithWorkflowRefresh(page, () =>
        page
          .locator(".work-in-motion")
          .getByRole("button", { name: /view all runs/i })
          .click(),
      );
      await expect(
        page.getByRole("heading", { name: "Runs & Approvals", level: 1 }),
      ).toBeVisible();
      const selectedHeadingFromViewAll = await page
        .locator(".run-overview h2")
        .first()
        .innerText();
      expect(selectedHeadingFromViewAll.trim()).toBe(firstRowTitle);
    });

    test("[pw.overview-runs] a run row is keyboard-activatable [pw.overview.open-runs:keyboard]", async ({
      page,
    }) => {
      await stubRunAndApprovalEndpoints(
        page,
        [RUN_FIXTURE_PENDING, RUN_FIXTURE_FINISHED],
        [APPROVAL_FIXTURE_PENDING],
      );
      await openWorkbenchShell(page);
      const runRows = page.locator(".work-in-motion .run-list button");
      await expect(runRows).toHaveCount(2);
      await runRows.first().focus();
      await completeNavigationWithWorkflowRefresh(page, () =>
        page.keyboard.press("Enter"),
      );
      await expect(
        page.getByRole("heading", { name: "Runs & Approvals", level: 1 }),
      ).toBeVisible();
      await expectAccessibleExperience(page);
    });

    test("[pw.overview-runs] empty and loading run lists render identical placeholder copy (documented defect) [pw.overview.open-runs:empty]", async ({
      page,
    }, testInfo) => {
      await page.route("**/api/backend/api/runs", async (route) => {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: "[]",
        });
      });
      await openWorkbenchShell(page);
      await expect(
        page.locator(".work-in-motion .loading-block"),
      ).toHaveText("Loading durable runs…");
      await recordWorkbenchShot(page, testInfo, "overview-runs-empty");
    });
  });

});
