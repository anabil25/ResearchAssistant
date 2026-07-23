import AxeBuilder from "@axe-core/playwright";
import type { Page, Route, TestInfo } from "@playwright/test";

import { expect, test } from "./fixtures";

const DATASET_OBJECTIVE =
  "Profile the pilot outcome dataset and plan a descriptive group comparison.";

const API_ROUTES = {
  workspace: /\/api\/backend\/api\/workspace$/,
  library: /\/api\/backend\/api\/library$/,
  runs: /\/api\/backend\/api\/runs$/,
  approvals: /\/api\/backend\/api\/approvals$/,
  connectors: /\/api\/backend\/api\/connectors$/,
  settings: /\/api\/backend\/api\/settings$/,
  agents: /\/api\/backend\/api\/agents$/,
  workflows: /\/api\/backend\/api\/workflows$/,
  matchingRun: /\/api\/backend\/api\/studios\/matching\/run$/,
  datasetRun: /\/api\/backend\/api\/studios\/dataset\/run$/,
};

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function createDeferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
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

async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function baseRun(overrides: Record<string, unknown> = {}) {
  return {
    capability: "matching",
    current_stage: "Complete",
    durable_instance_id: "research-run-test",
    id: "run-test",
    owner: "Dr. Maya Chen",
    progress: 100,
    started_at: "2026-07-16T12:00:00Z",
    status: "completed",
    title: "Test run",
    ...overrides,
  };
}

function buildWorkspaceData() {
  const workflows = [
    "literature",
    "grant",
    "matching",
    "dataset",
    "institutional_qa",
    "orchestration",
  ].map((capability) => ({
    capability,
    title: `${capability} workflow`,
    purpose: "A distinct workflow.",
    primary_artifact: "Verified artifact",
    online_research_policy: "optional-public-only",
    stages: [
      {
        id: "plan",
        label: "Plan",
        description: "Plan this workflow.",
        owner: "researcher",
        human_checkpoint: false,
      },
      {
        id: "verify",
        label: "Verify",
        description: "Verify this workflow.",
        owner: "validator",
        human_checkpoint: false,
      },
    ],
  }));

  return {
    summary: {
      project: {
        project_id: "demo-project",
        name: "State coverage workspace",
        description: "Deterministic workspace fixture.",
        default_classification: "internal",
        online_research_default: false,
        retention_days: 2555,
        citation_coverage_threshold: 1,
        require_human_approval: true,
        allowed_export_destinations: ["Workspace Library"],
        model_profile: "Balanced quality",
        evaluation_policy: "Block unresolved citations",
      },
      library_items: 2,
      active_runs: 0,
      pending_approvals: 0,
      connector_ready: 2,
      connector_total: 3,
      last_activity_at: "2026-07-16T12:00:00Z",
      persistence: "fixture-backed",
    },
    library: [
      {
        id: "dataset-sample",
        title: "pilot-outcomes.csv",
        kind: "Dataset",
        source: "Workspace upload",
        status: "ready",
        access: "internal",
        version: "1.0",
        checksum: "sha256:dataset-sample",
        license: "Project supplied",
        added_at: "2026-07-16T12:00:00Z",
        evidence_count: 0,
        connector: "Workspace upload",
        provider: "Workspace upload",
        publication_year: 2026,
        description: "Bounded pilot outcome dataset.",
        tags: ["dataset"],
      },
      {
        id: "paper-1",
        title: "Evidence workflow study",
        kind: "Paper",
        source: "PubMed",
        status: "ready",
        access: "public",
        version: "1.0",
        checksum: "sha256:test",
        license: "CC BY 4.0",
        added_at: "2026-07-16T12:00:00Z",
        evidence_count: 4,
        connector: "PubMed",
        provider: "PubMed",
        publication_year: 2025,
        description: "Verified test paper.",
        tags: ["evidence"],
      },
    ],
    runs: [],
    approvals: [],
    connectors: [
      {
        id: "openalex",
        name: "OpenAlex",
        category: "Matching",
        description: "Public expert metadata.",
        auth_kind: "None",
        secret_status: "Not required",
        enabled: true,
        test_status: "ready",
        last_tested_at: null,
        assigned_agents: ["matching"],
        terms_url: "https://openalex.org/",
        data_boundary: "Public metadata only.",
        capabilities: ["Search", "Metadata"],
      },
      {
        id: "nih_reporter",
        name: "NIH Reporter",
        category: "Matching",
        description: "Grant and investigator metadata.",
        auth_kind: "None",
        secret_status: "Not required",
        enabled: false,
        test_status: "not_configured",
        last_tested_at: null,
        assigned_agents: ["matching"],
        terms_url: "https://reporter.nih.gov/",
        data_boundary: "Public metadata only.",
        capabilities: ["Search", "Metadata"],
      },
      {
        id: "pubmed",
        name: "PubMed",
        category: "Literature",
        description: "Biomedical citations and abstracts.",
        auth_kind: "None",
        secret_status: "Not required",
        enabled: true,
        test_status: "ready",
        last_tested_at: null,
        assigned_agents: ["literature"],
        terms_url: "https://www.ncbi.nlm.nih.gov/home/about/policies/",
        data_boundary: "Public metadata only.",
        capabilities: ["Search", "Metadata"],
      },
    ],
    settings: {
      project_id: "demo-project",
      name: "State coverage workspace",
      description: "Deterministic workspace fixture.",
      default_classification: "internal",
      online_research_default: false,
      retention_days: 2555,
      citation_coverage_threshold: 1,
      require_human_approval: true,
      allowed_export_destinations: ["Workspace Library"],
      model_profile: "Balanced quality",
      evaluation_policy: "Block unresolved citations",
    },
    agents: [
      {
        id: "matching",
        name: "Matching explorer",
        model_tier: "Primary",
        status: "Active",
        web_access: "Opt-in public only",
        workflow_steps: ["Criteria", "Resolve", "Score"],
        deployment: "Foundry Hosted Agent",
      },
      {
        id: "dataset",
        name: "Dataset analysis",
        model_tier: "Primary",
        status: "Active",
        web_access: "Off",
        workflow_steps: ["Approve", "Profile", "Interpret"],
        deployment: "Foundry Hosted Agent",
      },
    ],
    workflows,
  };
}

async function mockWorkspaceApis(page: Page, workspace = buildWorkspaceData()) {
  const data = clone(workspace);
  await page.route(API_ROUTES.workspace, async (route) => {
    await fulfillJson(route, data.summary);
  });
  await page.route(API_ROUTES.library, async (route) => {
    await fulfillJson(route, data.library);
  });
  await page.route(API_ROUTES.runs, async (route) => {
    await fulfillJson(route, data.runs);
  });
  await page.route(API_ROUTES.approvals, async (route) => {
    await fulfillJson(route, data.approvals);
  });
  await page.route(API_ROUTES.connectors, async (route) => {
    await fulfillJson(route, data.connectors);
  });
  await page.route(API_ROUTES.settings, async (route) => {
    await fulfillJson(route, data.settings);
  });
  await page.route(API_ROUTES.agents, async (route) => {
    await fulfillJson(route, data.agents);
  });
  await page.route(API_ROUTES.workflows, async (route) => {
    await fulfillJson(route, data.workflows);
  });
}

function buildMatchingResult() {
  return {
    run: baseRun({
      capability: "matching",
      id: "run-matching-1",
      durable_instance_id: "research-run-matching-1",
      title: "Verified shortlist",
    }),
    criteria: [],
    matches: [
      {
        id: "match-1",
        name: "Dr. Amara Osei",
        kind: "person",
        score: 88,
        freshness: "Updated 2 days ago",
        strengths: ["Genomics", "Reproducibility"],
        gaps: [],
        hard_filters_passed: true,
        components: [
          {
            criterion_id: "expertise",
            label: "Expertise match",
            weight: 0.6,
            match: 0.9,
            contribution: 54,
            evidence_id: "cite-1",
          },
        ],
      },
      {
        id: "match-2",
        name: "Core Genomics Facility",
        kind: "facility",
        score: 71,
        freshness: "stale",
        strengths: ["Sequencing", "Core services"],
        gaps: ["Availability review pending"],
        hard_filters_passed: true,
        components: [
          {
            criterion_id: "capacity",
            label: "Capacity match",
            weight: 0.4,
            match: 0.7,
            contribution: 28,
            evidence_id: "cite-2",
          },
        ],
      },
    ],
    shortlist_ids: [],
    citations: [],
  };
}

function buildDatasetComputedResult() {
  return {
    asset_name: "pilot-outcomes.csv",
    run: baseRun({
      capability: "dataset",
      id: "run-dataset-1",
      durable_instance_id: "research-run-dataset-1",
      title: "Pilot dataset profile",
    }),
    profile_status: "computed",
    row_count: 1200,
    column_count: 4,
    fields: [
      {
        name: "participant_id",
        data_type: "string",
        missing: 0,
        range_or_values: "1,200 unique IDs",
        unique: 1200,
      },
      {
        name: "response_score",
        data_type: "number",
        missing: 3,
        range_or_values: "0-100",
        unique: 98,
      },
    ],
    quality_findings: ["3 missing response scores"],
    profile_note: "Ready for bounded computation.",
    analysis_plan: [
      {
        id: "profile",
        question: "What are the core field ranges?",
        method: "Deterministic profile",
        status: "ready",
        deterministic: true,
      },
    ],
    interpretation: ["Scores trend higher in the intervention cohort."],
    compute_proposal: {
      adapter: "Foundry Code Interpreter",
      estimated_bytes: 2_500_000_000,
      estimated_cost_usd: 1.2,
      estimated_minutes: 4,
      stages: ["Validate schema", "Profile columns"],
      approval_required: false,
    },
    citations: [],
    insight: {
      agent_name: "Dataset analysis",
      content: "Computation remained within the approved boundary.",
      evidence_state: "verified",
      online_research_used: false,
      referenced_source_ids: [],
      unresolved_source_ids: [],
    },
  };
}

function buildDatasetBlockedResult() {
  return {
    ...buildDatasetComputedResult(),
    asset_name: "clinical-events-archive.parquet",
    run: baseRun({
      capability: "dataset",
      id: "run-dataset-approval",
      durable_instance_id: "research-run-dataset-approval",
      title: "Large dataset estimate",
      progress: 42,
      current_stage: "Estimate ready",
      status: "waiting_for_approval",
    }),
    profile_status: "estimated",
    row_count: 0,
    column_count: 0,
    fields: [],
    quality_findings: [],
    profile_note: "Await plan approval before profiling.",
    interpretation: [],
    compute_proposal: {
      adapter: "Foundry Code Interpreter",
      estimated_bytes: 1_200_000_000_000,
      estimated_cost_usd: 88.5,
      estimated_minutes: 35,
      stages: ["Estimate data movement", "Queue approval gate"],
      approval_required: true,
    },
  };
}

test.describe("Matching and Dataset state closure", () => {
  test("[pw.matching.need.sources:consent-required] keeps Work IQ visibly locked behind tenant consent", async ({
    page,
  }, testInfo) => {
    await mockWorkspaceApis(page);
    await gotoView(page, "matching");

    const workIqToggle = page.getByRole("checkbox", {
      name: /work iq collaboration signals/i,
    });
    await expect(workIqToggle).toBeDisabled();
    await expect(workIqToggle).not.toBeChecked();
    await expect(
      page.getByText(/requires tenant microsoft graph consent/i),
    ).toBeVisible();
    await expect(page.locator(".work-iq-toggle")).toHaveAttribute(
      "title",
      "Work IQ requires tenant-level Microsoft Graph consent that has not been granted.",
    );

    await capture(page, testInfo, "matching-sources-consent-required");
    await expectAccessible(page);
  });

  test("[pw.matching.compare-shortlist:keyboard][pw.matching.result.select:keyboard][pw.matching.result.select:selected][pw.matching.result.select:stale] selects a stale result and opens shortlist comparison from the keyboard", async ({
    page,
  }, testInfo) => {
    await mockWorkspaceApis(page);
    await page.route(API_ROUTES.matchingRun, async (route) => {
      await fulfillJson(route, buildMatchingResult());
    });
    await gotoView(page, "matching");

    await page.getByRole("button", { name: "Build verified shortlist" }).click();
    const cards = page.locator(".match-card");
    await expect(cards).toHaveCount(2);

    const staleCard = cards.nth(1);
    await expect(staleCard.locator(".freshness")).toContainText("stale");

    const staleSelect = staleCard.locator(".match-select");
    await staleSelect.focus();
    await page.keyboard.press("Enter");
    await expect(staleCard).toHaveAttribute("data-active", "true");
    await expect(staleSelect).toHaveAttribute("aria-pressed", "true");
    await expect(page.locator(".score-explainer h2")).toHaveText(
      "Core Genomics Facility",
    );

    const shortlistToggle = staleCard.locator(".shortlist-toggle");
    await shortlistToggle.focus();
    await page.keyboard.press("Enter");
    await expect(shortlistToggle).toHaveAttribute("data-active", "true");

    const compareButton = page.getByRole("button", { name: "Compare shortlisted" });
    await compareButton.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator(".shortlist-compare")).toBeVisible();
    await expect(page.locator(".shortlist-compare")).toContainText(
      "Core Genomics Facility",
    );

    await capture(page, testInfo, "matching-result-stale-keyboard");
    await expectAccessible(page);
  });

  test("[pw.matching.run:keyboard][pw.matching.run:loading] starts shortlist generation from the keyboard and exposes the in-flight running state", async ({
    page,
  }, testInfo) => {
    await mockWorkspaceApis(page);
    const gate = createDeferred<void>();
    await page.route(API_ROUTES.matchingRun, async (route) => {
      await gate.promise;
      await fulfillJson(route, buildMatchingResult());
    });
    await gotoView(page, "matching");

    const runButton = page.getByRole("button", {
      name: "Build verified shortlist",
    });
    await runButton.focus();
    await page.keyboard.press("Enter");

    const runningButton = page.locator(".matching-layout .primary-button");
    await expect(runningButton).toBeDisabled();
    await expect(runningButton).toContainText("Running workflow...");
    await capture(page, testInfo, "matching-run-loading");
    await expectAccessible(page);

    gate.resolve();
    await expect(page.locator(".match-card")).toHaveCount(2);
  });

  test("[pw.matching.run:error] surfaces a truthful matching run failure without leaving the control spinning", async ({
    page,
    releaseDiagnostics,
  }, testInfo) => {
    await mockWorkspaceApis(page);
    releaseDiagnostics.expectConsoleError(
      /Failed to load resource: the server responded with a status of 500/,
    );
    await page.route(API_ROUTES.matchingRun, async (route) => {
      await fulfillJson(route, { detail: "Matching service failed." }, 500);
    });
    await gotoView(page, "matching");

    const runButton = page.getByRole("button", {
      name: "Build verified shortlist",
    });
    await runButton.click();

    await expect(page.locator(".error-banner[role='alert']")).toContainText(
      "Matching service failed.",
    );
    await expect(runButton).toBeEnabled();
    await expect(runButton).toContainText("Build verified shortlist");

    await capture(page, testInfo, "matching-run-error");
    await expectAccessible(page);
  });

  test("[pw.dataset.upload:empty][pw.dataset.objective:ready][pw.dataset.objective:keyboard][pw.dataset.plan.approve:draft] shows the empty upload tile, a ready objective field, and a draft approval plan", async ({
    page,
  }, testInfo) => {
    await mockWorkspaceApis(page);
    await gotoView(page, "dataset");

    const uploadTile = page.locator(".asset-upload-tile");
    const objectiveField = page.getByRole("textbox", { name: "Analysis objective" });
    const approvalCheckbox = page.getByRole("checkbox", {
      name: /I approve sending this bounded dataset/i,
    });

    await expect(uploadTile).toHaveAttribute("data-read-status", "idle");
    await expect(uploadTile).toContainText("Upload a dataset");
    await expect(objectiveField).toHaveValue(DATASET_OBJECTIVE);
    await expect(approvalCheckbox).not.toBeChecked();
    await expect(page.locator(".analysis-notebook .subtle-chip")).toHaveText(
      "Pending approval",
    );

    await objectiveField.focus();
    await page.keyboard.press("Control+a");
    await page.keyboard.type("Keyboard-driven cohort drift analysis.");
    await expect(objectiveField).toHaveValue(
      "Keyboard-driven cohort drift analysis.",
    );

    await capture(page, testInfo, "dataset-empty-draft");
    await expectAccessible(page);
  });

  test("[pw.dataset.execution:running][pw.dataset.profile:keyboard][pw.dataset.profile:loading] starts bounded dataset analysis from the keyboard and exposes the shared running state", async ({
    page,
  }, testInfo) => {
    await mockWorkspaceApis(page);
    const gate = createDeferred<void>();
    await page.route(API_ROUTES.datasetRun, async (route) => {
      await gate.promise;
      await fulfillJson(route, buildDatasetComputedResult());
    });
    await gotoView(page, "dataset");

    const approvalCheckbox = page.getByRole("checkbox", {
      name: /I approve sending this bounded dataset/i,
    });
    const runButton = page.getByRole("button", {
      name: "Analyze with Foundry Code Interpreter",
    });
    await approvalCheckbox.check();
    await runButton.focus();
    await page.keyboard.press("Enter");

    const runningButton = page.locator(".dataset-studio .primary-button");
    await expect(runningButton).toBeDisabled();
    await expect(runningButton).toContainText("Running workflow...");
    await capture(page, testInfo, "dataset-profile-loading");
    await expectAccessible(page);

    gate.resolve();
    await expect(page.locator(".schema-row").nth(1)).toBeVisible();
  });

  test("[pw.dataset.execution:failed][pw.dataset.profile:error] keeps the dataset control truthful when the run API rejects", async ({
    page,
    releaseDiagnostics,
  }, testInfo) => {
    await mockWorkspaceApis(page);
    releaseDiagnostics.expectConsoleError(
      /Failed to load resource: the server responded with a status of 500/,
    );
    await page.route(API_ROUTES.datasetRun, async (route) => {
      await fulfillJson(route, { detail: "Dataset analysis failed." }, 500);
    });
    await gotoView(page, "dataset");

    const approvalCheckbox = page.getByRole("checkbox", {
      name: /I approve sending this bounded dataset/i,
    });
    const runButton = page.getByRole("button", {
      name: "Analyze with Foundry Code Interpreter",
    });
    await approvalCheckbox.check();
    await runButton.click();

    await expect(page.locator(".error-banner[role='alert']")).toContainText(
      "Dataset analysis failed.",
    );
    await expect(runButton).toBeEnabled();
    await expect(runButton).toContainText(
      "Analyze with Foundry Code Interpreter",
    );

    await capture(page, testInfo, "dataset-profile-error");
    await expectAccessible(page);
  });

  test("[pw.dataset.execution:blocked] shows the estimate-only approval gate for large dataset execution", async ({
    page,
  }, testInfo) => {
    await mockWorkspaceApis(page);
    await page.route(API_ROUTES.datasetRun, async (route) => {
      await fulfillJson(route, buildDatasetBlockedResult());
    });
    await gotoView(page, "dataset");

    await page
      .locator(".asset-picker button", {
        hasText: "clinical-events-archive.parquet",
      })
      .click();
    const approvalCheckbox = page.getByRole("checkbox", {
      name: /I approve sending this bounded dataset/i,
    });
    await approvalCheckbox.check();
    await page
      .getByRole("button", { name: "Analyze with Foundry Code Interpreter" })
      .click();

    await expect(page.getByText("Human approval required before submit")).toBeVisible();
    await expect(page.getByText("Asset not profiled")).toBeVisible();
    await expect(page.locator(".schema-browser .subtle-chip")).toHaveText(
      "Estimate only · no profile",
    );

    await capture(page, testInfo, "dataset-execution-blocked");
    await expectAccessible(page);
  });
});
