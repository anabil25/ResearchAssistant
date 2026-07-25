import AxeBuilder from "@axe-core/playwright";
import type { Page, Route, TestInfo } from "@playwright/test";

import { expect, test } from "./fixtures";

const DATASET_DEFAULT_OBJECTIVE =
  "Profile the pilot outcome dataset and plan a descriptive group comparison.";

const API_PATTERNS = {
  bootstrap: {
    workspace: /\/api\/backend\/api\/workspace$/,
    library: /\/api\/backend\/api\/library$/,
    runs: /\/api\/backend\/api\/runs$/,
    approvals: /\/api\/backend\/api\/approvals$/,
    connectors: /\/api\/backend\/api\/connectors$/,
    settings: /\/api\/backend\/api\/settings$/,
    agents: /\/api\/backend\/api\/agents$/,
    workflows: /\/api\/backend\/api\/workflows$/,
  },
  matchingRun: /\/api\/backend\/api\/studios\/matching\/run$/,
  datasetRun: /\/api\/backend\/api\/studios\/dataset\/run$/,
} as const;

const WORKFLOW_CAPABILITIES = [
  "literature",
  "grant",
  "matching",
  "dataset",
  "institutional_qa",
  "orchestration",
] as const;

const BASE_PROJECT_SETTINGS = {
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
};

const BASE_WORKSPACE_SUMMARY = {
  project: BASE_PROJECT_SETTINGS,
  library_items: 2,
  active_runs: 0,
  pending_approvals: 0,
  connector_ready: 2,
  connector_total: 3,
  last_activity_at: "2026-07-16T12:00:00Z",
  persistence: "fixture-backed",
};

const LIBRARY_FIXTURES = [
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
] as const;

const AGENT_FIXTURES = [
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
] as const;

type WorkspaceFixture = {
  summary: typeof BASE_WORKSPACE_SUMMARY;
  library: unknown[];
  runs: unknown[];
  approvals: unknown[];
  connectors: unknown[];
  settings: typeof BASE_PROJECT_SETTINGS;
  agents: unknown[];
  workflows: unknown[];
};
type BootstrapKey = keyof typeof API_PATTERNS.bootstrap;
type MatchingRunPayload = { inputs: { sources: string[] } };

function copyFixture<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function makeGate<T = void>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((complete) => {
    resolve = complete;
  });
  return { promise, resolve };
}

function createMatchingConnector(overrides: Record<string, unknown>) {
  return {
    category: "Matching",
    auth_kind: "None",
    secret_status: "Not required",
    enabled: true,
    last_tested_at: null,
    assigned_agents: ["matching"],
    data_boundary: "Public metadata only.",
    ...overrides,
  };
}

function createLiteratureConnector(overrides: Record<string, unknown>) {
  return {
    category: "Literature",
    auth_kind: "None",
    secret_status: "Not required",
    enabled: true,
    last_tested_at: null,
    assigned_agents: ["literature"],
    data_boundary: "Public metadata only.",
    ...overrides,
  };
}

function createDefaultConnectors() {
  return [
    createMatchingConnector({
      id: "openalex",
      name: "OpenAlex",
      description: "Public expert metadata.",
      test_status: "ready",
      terms_url: "https://openalex.org/",
      capabilities: ["Search", "Metadata"],
    }),
    createMatchingConnector({
      id: "nih_reporter",
      name: "NIH Reporter",
      description: "Grant and investigator metadata.",
      enabled: false,
      test_status: "not_configured",
      terms_url: "https://reporter.nih.gov/",
      capabilities: ["Search", "Metadata"],
    }),
    createLiteratureConnector({
      id: "pubmed",
      name: "PubMed",
      description: "Biomedical citations and abstracts.",
      test_status: "ready",
      terms_url: "https://www.ncbi.nlm.nih.gov/home/about/policies/",
      capabilities: ["Search", "Metadata"],
    }),
  ];
}

function createWorkflowCatalog() {
  return WORKFLOW_CAPABILITIES.map((capability) => ({
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
}

function createWorkspaceFixture(overrides: Partial<WorkspaceFixture> = {}) {
  return {
    summary: copyFixture(BASE_WORKSPACE_SUMMARY),
    library: copyFixture(LIBRARY_FIXTURES),
    runs: [],
    approvals: [],
    connectors: createDefaultConnectors(),
    settings: copyFixture(BASE_PROJECT_SETTINGS),
    agents: copyFixture(AGENT_FIXTURES),
    workflows: createWorkflowCatalog(),
    ...overrides,
  };
}

async function openWorkbenchView(page: Page, view: string) {
  await page.goto(`/?view=${view}`);
  await expect(page.locator(".workbench-shell")).toHaveAttribute(
    "data-workspace-ready",
    "true",
  );
}

async function attachScreenshot(page: Page, testInfo: TestInfo, artifactId: string) {
  const filename = `${artifactId}-${testInfo.project.name}.png`;
  const path = testInfo.outputPath(filename);
  await page.screenshot({ path, fullPage: true });
  await testInfo.attach(artifactId, { path, contentType: "image/png" });
}

async function assertNoAxeViolations(page: Page) {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(results.violations).toEqual([]);
}

async function recordStateArtifact(
  page: Page,
  testInfo: TestInfo,
  artifactId: string,
) {
  await attachScreenshot(page, testInfo, artifactId);
  await assertNoAxeViolations(page);
}

async function replyWithJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function createRunRecord(overrides: Record<string, unknown> = {}) {
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

async function stubWorkspace(page: Page, fixture = createWorkspaceFixture()) {
  const workspace = copyFixture(fixture);
  const responses: Record<BootstrapKey, unknown> = {
    workspace: workspace.summary,
    library: workspace.library,
    runs: workspace.runs,
    approvals: workspace.approvals,
    connectors: workspace.connectors,
    settings: workspace.settings,
    agents: workspace.agents,
    workflows: workspace.workflows,
  };

  for (const [key, pattern] of Object.entries(
    API_PATTERNS.bootstrap,
  ) as Array<[BootstrapKey, RegExp]>) {
    await page.route(pattern, async (route) => {
      await replyWithJson(route, responses[key]);
    });
  }
}

function createMatchingResult() {
  return {
    run: createRunRecord({
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

function createDatasetComputedResult() {
  return {
    asset_name: "pilot-outcomes.csv",
    run: createRunRecord({
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

function createEstimatedDatasetResult(config: {
  assetName: string;
  runOverrides: Record<string, unknown>;
  profileNote: string;
  computeProposal: {
    adapter: string;
    estimated_bytes: number;
    estimated_cost_usd: number;
    estimated_minutes: number;
    stages: string[];
    approval_required: boolean;
  };
}) {
  return {
    ...createDatasetComputedResult(),
    asset_name: config.assetName,
    run: createRunRecord({ capability: "dataset", ...config.runOverrides }),
    profile_status: "estimated",
    row_count: 0,
    column_count: 0,
    fields: [],
    quality_findings: [],
    profile_note: config.profileNote,
    interpretation: [],
    compute_proposal: config.computeProposal,
  };
}

function createDatasetWaitingForApprovalResult() {
  return createEstimatedDatasetResult({
    assetName: "clinical-events-archive.parquet",
    runOverrides: {
      id: "run-dataset-approval",
      durable_instance_id: "research-run-dataset-approval",
      title: "Large dataset estimate",
      progress: 42,
      current_stage: "Estimate ready",
      status: "waiting_for_approval",
    },
    profileNote: "Await plan approval before profiling.",
    computeProposal: {
      adapter: "Foundry Code Interpreter",
      estimated_bytes: 1_200_000_000_000,
      estimated_cost_usd: 88.5,
      estimated_minutes: 35,
      stages: ["Estimate data movement", "Queue approval gate"],
      approval_required: true,
    },
  });
}

function createDatasetBlockedResult() {
  return createEstimatedDatasetResult({
    assetName: "restricted-phi-dataset.parquet",
    runOverrides: {
      id: "run-dataset-blocked",
      durable_instance_id: "research-run-dataset-blocked",
      title: "Restricted dataset access",
      progress: 0,
      current_stage: "Blocked by data-governance policy",
      status: "blocked",
    },
    profileNote:
      "This asset is blocked from analysis by data-governance policy and cannot proceed.",
    computeProposal: {
      adapter: "Foundry Code Interpreter",
      estimated_bytes: 0,
      estimated_cost_usd: 0,
      estimated_minutes: 0,
      stages: [],
      approval_required: false,
    },
  });
}

function matchingRunButton(page: Page) {
  return page.getByRole("button", { name: "Build verified shortlist" });
}

function datasetApprovalCheckbox(page: Page) {
  return page.getByRole("checkbox", {
    name: /I approve sending this bounded dataset/i,
  });
}

function datasetRunButton(page: Page) {
  return page.getByRole("button", {
    name: "Analyze with Foundry Code Interpreter",
  });
}

async function chooseDatasetAsset(page: Page, assetName: string) {
  await page
    .locator(".asset-picker button", { hasText: assetName })
    .click();
}

test.describe("Matching state closure", () => {
  test("[pw.matching.need.sources:consent-required] surfaces Work IQ as unavailable until tenant consent exists", async ({
    page,
  }, testInfo) => {
    await stubWorkspace(page);
    await openWorkbenchView(page, "matching");

    const workIqCheckbox = page.getByRole("checkbox", {
      name: /work iq collaboration signals/i,
    });
    await expect(workIqCheckbox).toBeDisabled();
    await expect(workIqCheckbox).not.toBeChecked();
    await expect(
      page.getByText(/requires tenant microsoft graph consent/i),
    ).toBeVisible();
    await expect(page.locator(".work-iq-toggle")).toHaveAttribute(
      "title",
      "Work IQ requires tenant-level Microsoft Graph consent that has not been granted.",
    );

    await recordStateArtifact(page, testInfo, "matching-sources-consent-required");
  });

  test("[pw.matching.need.sources:needs-connection][pw.matching.need.sources:unavailable][pw.matching.need.sources:disabled] keeps non-ready public sources non-interactive and omits them from the run payload", async ({
    page,
  }, testInfo) => {
    const workspace = createWorkspaceFixture({
      connectors: [
        ...createDefaultConnectors(),
        createMatchingConnector({
          id: "ror",
          name: "ROR",
          description: "Open identifiers for research organizations.",
          test_status: "configuration_required",
          terms_url: "https://ror.org/terms/",
          capabilities: ["Organization resolution"],
        }),
        createMatchingConnector({
          id: "orcid",
          name: "ORCID",
          description: "Public researcher identifier records.",
          test_status: "unavailable",
          terms_url: "https://info.orcid.org/terms-of-use/",
          capabilities: ["Identity resolution"],
        }),
      ],
    });
    await stubWorkspace(page, workspace);

    let capturedRequest: MatchingRunPayload | null = null;
    await page.route(API_PATTERNS.matchingRun, async (route) => {
      capturedRequest = route.request().postDataJSON() as MatchingRunPayload;
      await replyWithJson(route, createMatchingResult());
    });
    await openWorkbenchView(page, "matching");

    const sourceCheckboxes = {
      ready: page.getByRole("checkbox", { name: "OpenAlex" }),
      setupRequired: page.getByRole("checkbox", { name: /^ROR/ }),
      unavailable: page.getByRole("checkbox", { name: /^ORCID/ }),
      settingsDisabled: page.getByRole("checkbox", { name: /^NIH Reporter/ }),
    };

    await expect(sourceCheckboxes.ready).toBeEnabled();
    await expect(sourceCheckboxes.ready).toBeChecked();
    await expect(sourceCheckboxes.setupRequired).toBeDisabled();
    await expect(sourceCheckboxes.setupRequired).not.toBeChecked();
    await expect(sourceCheckboxes.unavailable).toBeDisabled();
    await expect(sourceCheckboxes.unavailable).not.toBeChecked();
    await expect(sourceCheckboxes.settingsDisabled).toBeDisabled();
    await expect(sourceCheckboxes.settingsDisabled).not.toBeChecked();
    await expect(page.getByText("Needs connection setup")).toBeVisible();
    await expect(page.getByText("Currently unavailable")).toBeVisible();
    await expect(page.getByText("Disabled in Settings")).toBeVisible();

    await recordStateArtifact(page, testInfo, "matching-sources-availability");

    await matchingRunButton(page).click();
    await expect.poll(() => capturedRequest).not.toBeNull();
    const { inputs } = capturedRequest!;
    expect(inputs.sources).toContain("openalex");
    expect(inputs.sources).not.toContain("ror");
    expect(inputs.sources).not.toContain("orcid");
    expect(inputs.sources).not.toContain("nih_reporter");
  });

  test("[pw.matching.run:keyboard][pw.matching.run:loading] starts shortlist generation from the keyboard and exposes the in-flight running state", async ({
    page,
  }, testInfo) => {
    await stubWorkspace(page);
    const runGate = makeGate<void>();
    await page.route(API_PATTERNS.matchingRun, async (route) => {
      await runGate.promise;
      await replyWithJson(route, createMatchingResult());
    });
    await openWorkbenchView(page, "matching");

    const trigger = matchingRunButton(page);
    await trigger.focus();
    await page.keyboard.press("Enter");

    const runningButton = page.locator(".matching-layout .primary-button");
    await expect(runningButton).toBeDisabled();
    await expect(runningButton).toContainText("Running workflow...");
    await recordStateArtifact(page, testInfo, "matching-run-loading");

    runGate.resolve();
    await expect(page.locator(".match-card")).toHaveCount(2);
  });

  test("[pw.matching.run:error] reports a matching run failure without leaving the control spinning", async ({
    page,
    releaseDiagnostics,
  }, testInfo) => {
    await stubWorkspace(page);
    releaseDiagnostics.expectConsoleError(
      /Failed to load resource: the server responded with a status of 500/,
    );
    await page.route(API_PATTERNS.matchingRun, async (route) => {
      await replyWithJson(route, { detail: "Matching service failed." }, 500);
    });
    await openWorkbenchView(page, "matching");

    const trigger = matchingRunButton(page);
    await trigger.click();

    await expect(page.locator(".error-banner[role='alert']")).toContainText(
      "Matching service failed.",
    );
    await expect(trigger).toBeEnabled();
    await expect(trigger).toContainText("Build verified shortlist");

    await recordStateArtifact(page, testInfo, "matching-run-error");
  });

  test("[pw.matching.compare-shortlist:keyboard][pw.matching.result.select:keyboard][pw.matching.result.select:selected][pw.matching.result.select:stale] selects the stale result from the keyboard and opens shortlist comparison", async ({
    page,
  }, testInfo) => {
    await stubWorkspace(page);
    await page.route(API_PATTERNS.matchingRun, async (route) => {
      await replyWithJson(route, createMatchingResult());
    });
    await openWorkbenchView(page, "matching");

    await matchingRunButton(page).click();
    const cards = page.locator(".match-card");
    await expect(cards).toHaveCount(2);

    const staleCard = cards.last();
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
    const shortlistPanel = page.locator(".shortlist-compare");
    await expect(shortlistPanel).toBeVisible();
    await expect(shortlistPanel).toContainText("Core Genomics Facility");

    await recordStateArtifact(page, testInfo, "matching-result-stale-keyboard");
  });
});

test.describe("Dataset state closure", () => {
  test("[pw.dataset.upload:empty][pw.dataset.objective:ready][pw.dataset.objective:keyboard][pw.dataset.plan.approve:draft] shows the idle upload surface, a ready objective field, and a draft approval plan", async ({
    page,
  }, testInfo) => {
    await stubWorkspace(page);
    await openWorkbenchView(page, "dataset");

    const uploadTile = page.locator(".asset-upload-tile");
    const objectiveField = page.getByRole("textbox", {
      name: "Analysis objective",
    });
    const approvalCheckbox = datasetApprovalCheckbox(page);

    await expect(uploadTile).toHaveAttribute("data-read-status", "idle");
    await expect(uploadTile).toContainText("Upload a dataset");
    await expect(objectiveField).toHaveValue(DATASET_DEFAULT_OBJECTIVE);
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

    await recordStateArtifact(page, testInfo, "dataset-empty-draft");
  });

  test("[pw.dataset.execution:running][pw.dataset.profile:keyboard][pw.dataset.profile:loading] starts bounded dataset analysis from the keyboard and exposes the shared running state", async ({
    page,
  }, testInfo) => {
    await stubWorkspace(page);
    const runGate = makeGate<void>();
    await page.route(API_PATTERNS.datasetRun, async (route) => {
      await runGate.promise;
      await replyWithJson(route, createDatasetComputedResult());
    });
    await openWorkbenchView(page, "dataset");

    const approvalCheckbox = datasetApprovalCheckbox(page);
    const trigger = datasetRunButton(page);
    await approvalCheckbox.check();
    await trigger.focus();
    await page.keyboard.press("Enter");

    const runningButton = page.locator(".dataset-studio .primary-button");
    await expect(runningButton).toBeDisabled();
    await expect(runningButton).toContainText("Running workflow...");
    await recordStateArtifact(page, testInfo, "dataset-profile-loading");

    runGate.resolve();
    await expect(page.locator(".schema-row").nth(1)).toBeVisible();
  });

  test("[pw.dataset.execution:failed][pw.dataset.profile:error] keeps the dataset control truthful when the run API rejects", async ({
    page,
    releaseDiagnostics,
  }, testInfo) => {
    await stubWorkspace(page);
    releaseDiagnostics.expectConsoleError(
      /Failed to load resource: the server responded with a status of 500/,
    );
    await page.route(API_PATTERNS.datasetRun, async (route) => {
      await replyWithJson(route, { detail: "Dataset analysis failed." }, 500);
    });
    await openWorkbenchView(page, "dataset");

    const approvalCheckbox = datasetApprovalCheckbox(page);
    const trigger = datasetRunButton(page);
    await approvalCheckbox.check();
    await trigger.click();

    await expect(page.locator(".error-banner[role='alert']")).toContainText(
      "Dataset analysis failed.",
    );
    await expect(trigger).toBeEnabled();
    await expect(trigger).toContainText(
      "Analyze with Foundry Code Interpreter",
    );

    await recordStateArtifact(page, testInfo, "dataset-profile-error");
  });

  test("[pw.dataset.execution:waiting-for-approval] shows the estimate-only human-approval gate for large dataset execution", async ({
    page,
  }, testInfo) => {
    // This fixture uses the backend queue state `waiting_for_approval`.
    // It remains distinguishable from both a locally unchecked approval box
    // and a genuine backend `blocked` run status.
    await stubWorkspace(page);
    await page.route(API_PATTERNS.datasetRun, async (route) => {
      await replyWithJson(route, createDatasetWaitingForApprovalResult());
    });
    await openWorkbenchView(page, "dataset");

    await chooseDatasetAsset(page, "clinical-events-archive.parquet");
    const approvalCheckbox = datasetApprovalCheckbox(page);
    await approvalCheckbox.check();
    await datasetRunButton(page).click();

    await expect(
      page.getByText("Human approval required before submit"),
    ).toBeVisible();
    await expect(page.getByText("Asset not profiled")).toBeVisible();
    await expect(page.locator(".schema-browser .subtle-chip")).toHaveText(
      "Estimate only · no profile",
    );
    await expect(page.locator(".status-chip")).toHaveText(
      "waiting for approval",
    );

    await recordStateArtifact(
      page,
      testInfo,
      "dataset-execution-waiting-for-approval",
    );
  });

  test("[pw.dataset.execution:blocked] truthfully renders a data-governance policy block distinct from waiting-for-approval", async ({
    page,
  }, testInfo) => {
    // This fixture returns the backend literal `blocked` run status.
    // Unlike the approval queue, it is not cleared by checking the local
    // consent box or by waiting for a reviewer.
    await stubWorkspace(page);
    await page.route(API_PATTERNS.datasetRun, async (route) => {
      await replyWithJson(route, createDatasetBlockedResult());
    });
    await openWorkbenchView(page, "dataset");

    await chooseDatasetAsset(page, "clinical-events-archive.parquet");
    const approvalCheckbox = datasetApprovalCheckbox(page);
    await approvalCheckbox.check();
    await datasetRunButton(page).click();

    await expect(page.locator(".status-chip")).toHaveText("blocked");
    await expect(
      page.getByText(
        "This asset is blocked from analysis by data-governance policy and cannot proceed.",
      ),
    ).toBeVisible();
    await expect(
      page.getByText("Human approval required before submit"),
    ).toHaveCount(0);

    await recordStateArtifact(page, testInfo, "dataset-execution-blocked");
  });
});
