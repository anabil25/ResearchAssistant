import { Buffer } from "node:buffer";

import AxeBuilder from "@axe-core/playwright";
import type { Page, TestInfo } from "@playwright/test";

import { expect, test } from "./fixtures";

test.describe.configure({ mode: "serial" });

const API_PREFIX = "/api/backend/api";

function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function apiMatcher(path: string) {
  return new RegExp(`${escapeRegExp(`${API_PREFIX}${path}`)}(?:\\?.*)?$`);
}

function jsonResponse(body: unknown, status = 200) {
  return {
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  };
}

function createDeferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

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

async function gotoSettingsConnectors(page: Page) {
  await gotoView(page, "settings");
  await page
    .locator('.settings-nav[aria-label="Settings sections"] button', {
      hasText: "Connectors",
    })
    .click();
  await expect(page.locator(".connector-manager")).toBeVisible();
}

async function routeJsonGet(
  page: Page,
  path: string,
  getBody: () => unknown,
) {
  await page.route(apiMatcher(path), async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    await route.fulfill(jsonResponse(getBody()));
  });
}

const FIXED_RUN_WAITING = {
  id: "fixture-run-waiting",
  durable_instance_id: "research-fixture-run-waiting",
  project_id: "proj-demo",
  capability: "grant",
  title: "Fixture grant package awaiting review",
  status: "waiting_for_approval",
  progress: 86,
  current_stage: "Reviewer approval",
  owner: "Dr. Maya Chen",
  started_at: "2026-07-20T12:00:00Z",
  completed_at: null,
  artifact_count: 4,
  estimated_cost_usd: 0,
  scheduler_managed: false,
  scheduling_state: "not_managed",
  orchestration_input: null,
  stages: [],
  approval_id: "fixture-approval-1" as string | null,
};

const FIXED_APPROVAL = {
  id: "fixture-approval-1",
  run_id: "fixture-run-waiting",
  title: "Release fixture package for institutional review",
  state: "pending",
  risk: "High",
  gated_action: "Export the fixture package and notify the assigned reviewer.",
  destination: "SharePoint research site / Grant reviews",
  requested_by: "grant-agent",
  requested_at: "2026-07-20T12:00:00Z",
  evidence_summary: "Fixture evidence summary.",
  idempotency_key: "fixture-approval-1-key",
  approver_id: null,
  approver_name: null,
  decided_at: null,
  rationale: null,
  event_delivery: "not_requested",
  decision_event_id: null,
};

const BASE_LIBRARY_ITEM = {
  id: "library-item-ready",
  title: "Evidence workflow study",
  kind: "Paper",
  source: "PubMed",
  status: "ready",
  access: "public",
  version: "1.0",
  checksum: "sha256:ready",
  license: "CC BY 4.0",
  added_at: "2026-07-16T12:00:00Z",
  evidence_count: 4,
  connector: "PubMed",
  provider: "PubMed",
  publication_year: 2025,
  description: "Verified test paper.",
  tags: ["evidence", "review"],
  size_bytes: 2_400_000,
  content_type: "application/pdf",
};

const DEFAULT_SETTINGS = {
  project_id: "demo-project",
  name: "Test workspace",
  description: "A governed test workspace.",
  default_classification: "internal",
  online_research_default: false,
  retention_days: 2555,
  citation_coverage_threshold: 1,
  require_human_approval: true,
  allowed_export_destinations: ["Workspace Library"],
  model_profile: "Balanced quality",
  evaluation_policy: "Block unresolved citations",
};

const BASE_AGENT = {
  id: "literature",
  name: "Literature synthesis",
  model_tier: "Primary",
  status: "Active",
  web_access: "Opt-in public only",
  workflow_steps: ["Protocol", "Search", "Screen", "Audit"],
  deployment: "Foundry Hosted Agent",
};

function buildConnector(
  overrides: Partial<{
    id: string;
    name: string;
    category: string;
    description: string;
    auth_kind: string;
    secret_status: string;
    enabled: boolean;
    test_status: string;
    last_tested_at: string | null;
    assigned_agents: string[];
    terms_url: string;
    data_boundary: string;
    capabilities: string[];
  }> = {},
) {
  return {
    id: "pubmed",
    name: "PubMed",
    category: "Literature",
    description: "Biomedical citations and abstracts.",
    auth_kind: "None",
    secret_status: "Not required",
    enabled: true,
    test_status: "ready",
    last_tested_at: "2026-07-16T12:00:00Z",
    assigned_agents: ["literature"],
    terms_url: "https://www.ncbi.nlm.nih.gov/home/about/policies/",
    data_boundary: "Public metadata only.",
    capabilities: ["Search", "Metadata"],
    ...overrides,
  };
}

type WorkspaceFixtureState = {
  library: typeof BASE_LIBRARY_ITEM[];
  runs: typeof FIXED_RUN_WAITING[];
  approvals: typeof FIXED_APPROVAL[];
  connectors: ReturnType<typeof buildConnector>[];
  settings: typeof DEFAULT_SETTINGS;
  agents: typeof BASE_AGENT[];
  workflows: unknown[];
};

function buildWorkspaceState(
  overrides: Partial<WorkspaceFixtureState> = {},
): WorkspaceFixtureState {
  return {
    library: [{ ...BASE_LIBRARY_ITEM }],
    runs: [{ ...FIXED_RUN_WAITING }],
    approvals: [{ ...FIXED_APPROVAL }],
    connectors: [buildConnector()],
    settings: { ...DEFAULT_SETTINGS },
    agents: [{ ...BASE_AGENT }],
    workflows: [],
    ...overrides,
  };
}

async function mockWorkspaceData(
  page: Page,
  overrides: Partial<WorkspaceFixtureState> = {},
) {
  const state = buildWorkspaceState(overrides);
  await routeJsonGet(page, "/workspace", () => ({
    project: {
      ...state.settings,
    },
    library_items: state.library.length,
    active_runs: state.runs.length,
    pending_approvals: state.approvals.filter((item) => item.state === "pending")
      .length,
    connector_ready: state.connectors.filter(
      (item) => item.enabled && item.test_status === "ready",
    ).length,
    connector_total: state.connectors.length,
    last_activity_at: "2026-07-23T12:00:00Z",
    persistence: "in-memory demo",
  }));
  await routeJsonGet(page, "/library", () => state.library);
  await routeJsonGet(page, "/runs", () => state.runs);
  await routeJsonGet(page, "/approvals", () => state.approvals);
  await routeJsonGet(page, "/connectors", () => state.connectors);
  await routeJsonGet(page, "/settings", () => state.settings);
  await routeJsonGet(page, "/agents", () => state.agents);
  await routeJsonGet(page, "/workflows", () => state.workflows);
  return state;
}

test.describe("[pw.approval-decision] approvals state closure", () => {
  test("[pw.approval-decision] rejecting a pending approval disables rationale while the decision is in flight and records the rejected decision path [pw.approvals.decide:submitting][pw.approvals.rationale:disabled][pw.approvals.decide:rejected]", async ({
    page,
  }, testInfo) => {
    const workspace = await mockWorkspaceData(page, {
      runs: [{ ...FIXED_RUN_WAITING }],
      approvals: [{ ...FIXED_APPROVAL }],
    });

    const decisionDeferred = createDeferred<{
      decision: string;
      rationale: string;
    }>();
    let capturedBody: { decision: string; rationale: string } | null = null;
    await page.route(
      apiMatcher("/approvals/fixture-approval-1/decision"),
      async (route) => {
        capturedBody = route.request().postDataJSON() as {
          decision: string;
          rationale: string;
        };
        const responseBody = await decisionDeferred.promise;
        await route.fulfill(
          jsonResponse({
            ...FIXED_APPROVAL,
            state: responseBody.decision,
            rationale: responseBody.rationale,
          }),
        );
      },
    );

    await gotoView(page, "runs");
    const rationaleField = page.getByRole("textbox", {
      name: "Reviewer rationale",
    });
    await rationaleField.fill(
      "Rejecting export until the destination policy exception is documented.",
    );

    await page.getByRole("button", { name: "Reject action" }).click();
    await expect(page.getByRole("button", { name: "Reject action" })).toBeDisabled();
    await expect(
      page.getByRole("button", { name: "Approve exact action" }),
    ).toBeDisabled();
    await expect(rationaleField).toBeDisabled();
    await capture(page, testInfo, "approvals-rejected-submitting");
    await expectAccessible(page);

    workspace.approvals = [];
    decisionDeferred.resolve({
      decision: "rejected",
      rationale:
        "Rejecting export until the destination policy exception is documented.",
    });

    await expect(page.getByText("No pending decision")).toBeVisible();
    await expect(page.locator(".approval-card")).toHaveCount(0);
    await expect.poll(() => capturedBody).toEqual({
      decision: "rejected",
      rationale:
        "Rejecting export until the destination policy exception is documented.",
    });
  });

  test("[pw.approval-decision] approval decision errors surface a visible failure without hiding the pending review card [pw.approvals.decide:error]", async ({
    page,
    releaseDiagnostics,
  }, testInfo) => {
    await mockWorkspaceData(page, {
      runs: [{ ...FIXED_RUN_WAITING }],
      approvals: [{ ...FIXED_APPROVAL }],
    });
    await page.route(
      apiMatcher("/approvals/fixture-approval-1/decision"),
      async (route) => {
        await route.fulfill(
          jsonResponse({ detail: "Decision denied by policy." }, 500),
        );
      },
    );

    await gotoView(page, "runs");
    releaseDiagnostics.expectConsoleError(/500 \(Internal Server Error\)/);
    await page
      .getByRole("textbox", { name: "Reviewer rationale" })
      .fill("Approving would violate the release boundary.");
    await page
      .getByRole("button", { name: "Approve exact action" })
      .click();

    await expect(page.locator(".error-banner[role='alert']")).toHaveText(
      "Decision denied by policy.",
    );
    await expect(page.getByRole("button", { name: "Approve exact action" })).toBeEnabled();
    await expect(page.getByText(FIXED_APPROVAL.title)).toBeVisible();
    await capture(page, testInfo, "approvals-decision-error");
    await expectAccessible(page);
  });
});

test.describe("[pw.library-ingest] library state closure", () => {
  test("[pw.library-ingest] queued ingestion shows a submitting state and the resulting processing item opens with authoritative detail [pw.library.ingest.form:submitting][pw.library.item.open:processing]", async ({
    page,
  }, testInfo) => {
    const workspace = await mockWorkspaceData(page, {
      library: [{ ...BASE_LIBRARY_ITEM }],
    });

    const processingItem = {
      ...BASE_LIBRARY_ITEM,
      id: "library-item-processing",
      title: "Queued longitudinal cohort protocol",
      status: "processing",
      checksum: "sha256:processing",
      description: "Queued for checksum, license, and indexing steps.",
      source: "Workspace upload",
      connector: "Workspace upload",
      provider: "Workspace upload",
      content_type: "text/plain",
      access: "internal",
    };
    const uploadDeferred = createDeferred<void>();
    await page.route(apiMatcher("/library/upload"), async (route) => {
      await uploadDeferred.promise;
      await route.fulfill(
        jsonResponse({
          item: processingItem,
          run: {
            id: "ingest-run-1",
            durable_instance_id: "research-run-ingest-1",
          },
        }),
      );
    });

    await gotoView(page, "library");
    await page.getByRole("button", { name: "Ingest source" }).click();

    const dialog = page.getByRole("dialog", { name: "Add source to Library" });
    await dialog.getByLabel("Title").fill(processingItem.title);
    await dialog.getByLabel("Source file").setInputFiles({
      name: "protocol.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("Protocol\n\nThis source is queued for governed indexing."),
    });
    await dialog.getByLabel("Description").fill(processingItem.description);

    await dialog.getByRole("button", { name: "Start ingestion" }).click();
    await expect(dialog.getByRole("button", { name: "Queuing…" })).toBeDisabled();
    await capture(page, testInfo, "library-ingest-submitting");
    await expectAccessible(page);

    workspace.library = [processingItem, ...workspace.library];
    uploadDeferred.resolve();

    await expect(dialog).toBeHidden();
    const processingRow = page.getByRole("button", {
      name: new RegExp(processingItem.title.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")),
    });
    await expect(processingRow).toBeVisible();
    await processingRow.click();

    const detailDialog = page.getByRole("dialog", {
      name: processingItem.title,
    });
    await expect(detailDialog.getByText("processing", { exact: true })).toBeVisible();
    await expect(detailDialog.locator("dl.library-detail-facts")).toContainText(
      "Workspace upload",
    );
    await capture(page, testInfo, "library-item-processing-detail");
    await expectAccessible(page);
  });
});

test.describe("[pw.run-detail] runs state closure", () => {
  test("[pw.run-detail] selecting a partial run keeps the detail panel aligned with the chosen row [pw.runs.select:partial]", async ({
    page,
  }, testInfo) => {
    const partialRun = {
      ...FIXED_RUN_WAITING,
      id: "fixture-run-partial",
      durable_instance_id: "research-fixture-run-partial",
      title: "Partial literature extraction rerun",
      capability: "literature",
      status: "partial",
      current_stage: "Extraction evidence missing for one source",
      artifact_count: 2,
      approval_id: null,
    };
    await mockWorkspaceData(page, {
      runs: [
        {
          ...FIXED_RUN_WAITING,
          id: "fixture-run-completed",
          durable_instance_id: "research-fixture-run-completed",
          title: "Completed baseline grant review",
          status: "completed",
          current_stage: "Release package exported",
          approval_id: null,
        },
        partialRun,
      ],
      approvals: [],
    });

    await gotoView(page, "runs");
    await page.getByRole("button", { name: /Partial literature extraction rerun/i }).click();

    await expect(
      page.locator('.detailed-run-list button[data-active="true"]'),
    ).toContainText("Partial literature extraction rerun");
    await expect(page.locator(".run-overview .table-status")).toHaveText("partial");
    await expect(
      page.getByText("Extraction evidence missing for one source"),
    ).toBeVisible();
    await capture(page, testInfo, "runs-select-partial");
    await expectAccessible(page);
  });
});

test.describe("[pw.settings-general] settings general state closure", () => {
  test("[pw.settings-general] the settings view renders a truthful loading fallback before workspace settings arrive [pw.settings.general.form:loading]", async ({
    page,
  }, testInfo) => {
    const workspace = buildWorkspaceState();
    await routeJsonGet(page, "/workspace", () => ({
      project: { ...workspace.settings },
      library_items: workspace.library.length,
      active_runs: workspace.runs.length,
      pending_approvals: workspace.approvals.filter(
        (item) => item.state === "pending",
      ).length,
      connector_ready: workspace.connectors.filter(
        (item) => item.enabled && item.test_status === "ready",
      ).length,
      connector_total: workspace.connectors.length,
      last_activity_at: "2026-07-23T12:00:00Z",
      persistence: "in-memory demo",
    }));
    await routeJsonGet(page, "/library", () => workspace.library);
    await routeJsonGet(page, "/runs", () => workspace.runs);
    await routeJsonGet(page, "/approvals", () => workspace.approvals);
    await routeJsonGet(page, "/connectors", () => workspace.connectors);
    await routeJsonGet(page, "/agents", () => workspace.agents);
    await routeJsonGet(page, "/workflows", () => workspace.workflows);
    const settingsDeferred = createDeferred<typeof DEFAULT_SETTINGS>();
    await page.route(apiMatcher("/settings"), async (route) => {
      if (route.request().method() !== "GET") {
        await route.continue();
        return;
      }
      const settings = await settingsDeferred.promise;
      await route.fulfill(jsonResponse(settings));
    });

    await page.goto("/?view=settings");
    await expect(page.locator(".workbench-shell")).toHaveAttribute(
      "data-workspace-ready",
      "false",
    );
    await expect(
      page.getByRole("heading", { name: "Project Settings", level: 1 }),
    ).toBeVisible();
    await expect(page.getByText("Loading project settings…")).toBeVisible();
    await capture(page, testInfo, "settings-general-loading");
    await expectAccessible(page);

    settingsDeferred.resolve({ ...DEFAULT_SETTINGS });
    await expect(page.locator(".workbench-shell")).toHaveAttribute(
      "data-workspace-ready",
      "true",
    );
  });

  test("[pw.settings-general] saving disables the form action and reports API failures inline [pw.settings.general.form:disabled][pw.settings.general.form:error]", async ({
    page,
    releaseDiagnostics,
  }, testInfo) => {
    await mockWorkspaceData(page, {
      settings: { ...DEFAULT_SETTINGS },
    });
    const saveDeferred = createDeferred<{ status: number; body: unknown }>();
    let capturedBody: Record<string, unknown> | null = null;
    await page.route(apiMatcher("/settings"), async (route) => {
      if (route.request().method() !== "PUT") {
        await route.continue();
        return;
      }
      capturedBody = route.request().postDataJSON() as Record<string, unknown>;
      const response = await saveDeferred.promise;
      await route.fulfill(jsonResponse(response.body, response.status));
    });

    await gotoView(page, "settings");
    await page.getByRole("textbox", { name: "Project name" }).fill("Blocked workspace update");
    releaseDiagnostics.expectConsoleError(/500 \(Internal Server Error\)/);
    await page.getByRole("button", { name: "Save project settings" }).click();

    await expect(page.getByRole("button", { name: "Saving…" })).toBeDisabled();
    await capture(page, testInfo, "settings-general-disabled");
    await expectAccessible(page);

    saveDeferred.resolve({
      status: 500,
      body: { detail: "Retention policy blocked the update." },
    });

    await expect(page.locator(".save-status")).toHaveText(
      "Retention policy blocked the update.",
    );
    await expect(page.getByRole("button", { name: "Save project settings" })).toBeEnabled();
    expect(capturedBody).toMatchObject({ name: "Blocked workspace update" });
    await capture(page, testInfo, "settings-general-error");
    await expectAccessible(page);
  });
});

test.describe("[pw.connector-enable] connector enable state closure", () => {
  test("[pw.connector-enable] saving a connector enable change disables the control and surfaces update errors [pw.settings.connectors.enable:saving][pw.settings.connectors.enable:error]", async ({
    page,
    releaseDiagnostics,
  }, testInfo) => {
    const connectors = [
      buildConnector({
        id: "arxiv",
        name: "arXiv",
        description: "Preprints and public metadata.",
      }),
    ];
    await mockWorkspaceData(page, { connectors });

    const saveDeferred = createDeferred<{ status: number; body: unknown }>();
    await page.route(apiMatcher("/connectors/arxiv"), async (route) => {
      if (route.request().method() !== "PUT") {
        await route.continue();
        return;
      }
      const response = await saveDeferred.promise;
      await route.fulfill(jsonResponse(response.body, response.status));
    });

    await gotoSettingsConnectors(page);
    const enableCheckbox = page.getByLabel("Enable arXiv");
    await expect(enableCheckbox).toBeChecked();
    await enableCheckbox.uncheck();
    releaseDiagnostics.expectConsoleError(/500 \(Internal Server Error\)/);
    await page.getByRole("button", { name: "Save configuration" }).click();

    await expect(enableCheckbox).toBeDisabled();
    await expect(page.getByRole("button", { name: "Save configuration" })).toBeDisabled();
    await capture(page, testInfo, "connector-enable-saving");
    await expectAccessible(page);

    saveDeferred.resolve({
      status: 500,
      body: { detail: "Connector update denied." },
    });

    await expect(page.locator(".save-status")).toHaveText(
      "Connector update denied.",
    );
    await expect(enableCheckbox).toBeEnabled();
    await capture(page, testInfo, "connector-enable-error");
    await expectAccessible(page);
  });
});

test.describe("[pw.connector-assign] connector assignment state closure", () => {
  test("[pw.connector-assign] assignment saves disable specialist checkboxes and preserve a visible error on failure [pw.settings.connectors.assign:saving][pw.settings.connectors.assign:error]", async ({
    page,
    releaseDiagnostics,
  }, testInfo) => {
    const connectors = [
      buildConnector({
        id: "crossref",
        name: "Crossref",
        assigned_agents: ["literature", "grant"],
      }),
    ];
    await mockWorkspaceData(page, { connectors });

    const saveDeferred = createDeferred<{ status: number; body: unknown }>();
    await page.route(apiMatcher("/connectors/crossref"), async (route) => {
      if (route.request().method() !== "PUT") {
        await route.continue();
        return;
      }
      const response = await saveDeferred.promise;
      await route.fulfill(jsonResponse(response.body, response.status));
    });

    await gotoSettingsConnectors(page);
    const grantCheckbox = page.getByLabel("Assign grant to Crossref");
    const literatureCheckbox = page.getByLabel("Assign literature to Crossref");
    await grantCheckbox.uncheck();
    releaseDiagnostics.expectConsoleError(/500 \(Internal Server Error\)/);
    await page.getByRole("button", { name: "Save configuration" }).click();

    await expect(grantCheckbox).toBeDisabled();
    await expect(literatureCheckbox).toBeDisabled();
    await capture(page, testInfo, "connector-assign-saving");
    await expectAccessible(page);

    saveDeferred.resolve({
      status: 500,
      body: { detail: "Connector assignment blocked by policy." },
    });

    await expect(page.locator(".save-status")).toHaveText(
      "Connector assignment blocked by policy.",
    );
    await expect(grantCheckbox).toBeEnabled();
    await capture(page, testInfo, "connector-assign-error");
    await expectAccessible(page);
  });
});

test.describe("[pw.connector-test] connector test state closure", () => {
  test("[pw.connector-test] test connection reports healthy, degraded, configuration-required, and failed outcomes while exposing the live testing state [pw.settings.connectors.test:testing][pw.settings.connectors.test:healthy][pw.settings.connectors.test:degraded][pw.settings.connectors.test:configuration-required][pw.settings.connectors.test:failed]", async ({
    page,
  }, testInfo) => {
    const workspace = await mockWorkspaceData(page, {
      connectors: [
        buildConnector({
          id: "datacite",
          name: "DataCite",
          category: "Datasets",
          test_status: "not_tested",
          assigned_agents: ["dataset"],
        }),
      ],
    });
    let connectors = workspace.connectors;

    const firstResponse = createDeferred<{
      test_status: string;
      last_tested_at: string;
    }>();
    let testCallCount = 0;
    await page.route(apiMatcher("/connectors/datacite/test"), async (route) => {
      testCallCount += 1;
      if (testCallCount === 1) {
        const next = await firstResponse.promise;
        const updated = {
          ...connectors[0],
          ...next,
        };
        connectors = [updated];
        workspace.connectors = connectors;
        await route.fulfill(jsonResponse(updated));
        return;
      }
      if (testCallCount === 2) {
        const updated = {
          ...connectors[0],
          test_status: "ready_with_key",
          last_tested_at: "2026-07-23T12:02:00Z",
        };
        connectors = [updated];
        workspace.connectors = connectors;
        await route.fulfill(jsonResponse(updated));
        return;
      }
      if (testCallCount === 3) {
        const updated = {
          ...connectors[0],
          test_status: "configuration_required",
          last_tested_at: "2026-07-23T12:03:00Z",
        };
        connectors = [updated];
        workspace.connectors = connectors;
        await route.fulfill(jsonResponse(updated));
        return;
      }
      const updated = {
        ...connectors[0],
        test_status: "unavailable",
        last_tested_at: "2026-07-23T12:04:00Z",
      };
      connectors = [updated];
      workspace.connectors = connectors;
      await route.fulfill(jsonResponse(updated));
    });

    await gotoSettingsConnectors(page);
    const testButton = page.getByRole("button", { name: "Test connection" });

    await testButton.click();
    await expect(page.getByRole("button", { name: "Testing…" })).toBeDisabled();
    await capture(page, testInfo, "connector-test-testing");
    await expectAccessible(page);

    firstResponse.resolve({
      test_status: "ready",
      last_tested_at: "2026-07-23T12:01:00Z",
    });

    await expect(page.locator(".save-status")).toContainText("DataCite: Ready.");
    await expect(page.locator(".connector-health-badge")).toHaveText("Ready");
    await capture(page, testInfo, "connector-test-healthy");
    await expectAccessible(page);

    await testButton.click();
    await expect(page.locator(".save-status")).toContainText(
      "DataCite: Ready, key recommended.",
    );
    await expect(page.locator(".connector-health-badge")).toHaveText(
      "Ready, key recommended",
    );
    await capture(page, testInfo, "connector-test-degraded");
    await expectAccessible(page);

    await testButton.click();
    await expect(page.locator(".save-status")).toContainText(
      "DataCite: Setup required.",
    );
    await expect(page.locator(".connector-health-badge")).toHaveText(
      "Setup required",
    );
    await capture(page, testInfo, "connector-test-configuration-required");
    await expectAccessible(page);

    await testButton.click();
    await expect(page.locator(".save-status")).toContainText(
      "DataCite: Connection failed.",
    );
    await expect(page.locator(".connector-health-badge")).toHaveText(
      "Connection failed",
    );
    await capture(page, testInfo, "connector-test-failed");
    await expectAccessible(page);
  });
});
