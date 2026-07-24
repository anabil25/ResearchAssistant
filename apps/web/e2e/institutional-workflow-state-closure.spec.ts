import AxeBuilder from "@axe-core/playwright";
import type { Locator, Page, TestInfo } from "@playwright/test";

import { expect, test } from "./fixtures";

const API_ROOT = "/api/backend/api";
const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] as const;
const WORKSPACE_READY_SELECTOR = ".workbench-shell";
const POLICY_SCOPE_ORDER = ["irb", "records", "governance"] as const;
const RUN_CONTROL_NAMES = ["Pause", "Resume", "Retry", "Cancel"] as const;

type StudioName = "institutional_qa" | "orchestration";
type WorkspaceSlice =
  | "/agents"
  | "/approvals"
  | "/connectors"
  | "/library"
  | "/runs"
  | "/settings"
  | "/workspace"
  | "/workflows";
type RunState = "blocked" | "cancelled" | "completed" | "failed" | "running";
type WorkspaceOverrides = {
  agents?: unknown[];
  approvals?: unknown[];
  connectors?: unknown[];
  library?: unknown[];
  runs?: unknown[];
  settings?: Record<string, unknown>;
  summary?: Record<string, unknown>;
  workflows?: unknown[];
};
type StudioRequestPayload = {
  objective: string;
  online_research: boolean;
  inputs: Record<string, unknown>;
};

function escapeExpressionSegment(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function backendMatcher(route: string) {
  return new RegExp(`${escapeExpressionSegment(`${API_ROOT}${route}`)}$`);
}

function createGate<T = void>() {
  let release!: (value: T | PromiseLike<T>) => void;
  const wait = new Promise<T>((resolve, reject) => {
    release = resolve;
    void reject;
  });
  return { release, wait };
}

async function openView(page: Page, viewName: string) {
  await page.goto(`/?view=${viewName}`);
  await expect(page.locator(WORKSPACE_READY_SELECTOR)).toHaveAttribute(
    "data-workspace-ready",
    "true",
  );
}

async function saveEvidence(page: Page, testInfo: TestInfo, label: string) {
  const imageName = `${label}-${testInfo.project.name}.png`;
  const imagePath = testInfo.outputPath(imageName);
  await page.screenshot({ fullPage: true, path: imagePath });
  await testInfo.attach(label, {
    contentType: "image/png",
    path: imagePath,
  });
}

async function expectNoAccessibilityViolations(page: Page) {
  const scan = await new AxeBuilder({ page }).withTags([...WCAG_TAGS]).analyze();
  expect(scan.violations).toEqual([]);
}

async function fulfillJson(
  route: Parameters<Parameters<Page["route"]>[1]>[0],
  payload: unknown,
  status = 200,
) {
  await route.fulfill({
    body: JSON.stringify(payload),
    contentType: "application/json",
    status,
  });
}

async function captureRunRequest(
  page: Page,
  studio: StudioName,
  actionLabel: string,
) {
  const requestStarted = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      request.url().includes(`/api/studios/${studio}/run`),
  );
  const responseArrived = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().includes(`/api/studios/${studio}/run`),
  );

  await page.getByRole("button", { name: actionLabel }).click();

  const outboundRequest = await requestStarted;
  const inboundResponse = await responseArrived;
  expect(inboundResponse.status()).toBe(200);
  return outboundRequest.postDataJSON() as StudioRequestPayload;
}

function defineStage(
  id: string,
  label: string,
  description: string,
  owner: string,
  humanCheckpoint: boolean,
) {
  return {
    description,
    human_checkpoint: humanCheckpoint,
    id,
    label,
    owner,
  };
}

function defineWorkflow(
  capability: string,
  title: string,
  purpose: string,
  primaryArtifact: string,
  onlineResearchPolicy: string,
  stages: ReturnType<typeof defineStage>[],
) {
  return {
    capability,
    online_research_policy: onlineResearchPolicy,
    primary_artifact: primaryArtifact,
    purpose,
    stages,
    title,
  };
}

function createDefaultWorkflows() {
  return [
    defineWorkflow(
      "institutional_qa",
      "Institutional policy answer",
      "Resolve policy answers from authorized corpora.",
      "Grounded answer",
      "disabled",
      [
        defineStage(
          "retrieve",
          "Retrieve",
          "Resolve effective policy versions.",
          "institutional-agent",
          false,
        ),
        defineStage(
          "answer",
          "Answer",
          "Return an answer or abstain.",
          "institutional-agent",
          false,
        ),
      ],
    ),
    defineWorkflow(
      "orchestration",
      "Workflow automation",
      "Validate and activate a bounded orchestration graph.",
      "Versioned workflow",
      "disabled",
      [
        defineStage(
          "draft",
          "Draft",
          "Edit the workflow graph.",
          "workflow-builder",
          false,
        ),
        defineStage(
          "validate",
          "Validate",
          "Dry-run the exact graph without side effects.",
          "workflow-validator",
          false,
        ),
      ],
    ),
  ];
}

function createWorkspaceFixture(overrides: WorkspaceOverrides = {}) {
  const libraryEntries = overrides.library ?? [];
  const approvalEntries = overrides.approvals ?? [];
  const connectorEntries =
    overrides.connectors ??
    [
      {
        auth_kind: "apiKey",
        assigned_agents: ["lit-agent"],
        capabilities: ["literature"],
        category: "Search",
        data_boundary: "Project",
        description: "Searches internal research sources.",
        enabled: true,
        id: "connector-lit-search",
        last_tested_at: "2026-07-22T15:00:00Z",
        name: "Literature Search",
        secret_status: "configured",
        terms_url: "https://example.test/terms/literature-search",
        test_status: "passed",
      },
    ];
  const agentEntries =
    overrides.agents ??
    [
      {
        deployment: "Hosted agent",
        id: "lit-agent",
        model_tier: "standard",
        name: "Literature synthesis",
        status: "Active",
        web_access: "Disabled",
        workflow_steps: ["retrieve", "screen", "synthesize"],
      },
    ];
  const runEntries = overrides.runs ?? [];
  const projectSettings = {
    allowed_export_destinations: ["SharePoint research site"],
    citation_coverage_threshold: 0.9,
    default_classification: "Internal",
    description: "Deterministic research workspace",
    evaluation_policy: "Citations required",
    model_profile: "Balanced",
    name: "AI for equitable clinical research",
    online_research_default: false,
    project_id: "proj-demo",
    require_human_approval: true,
    retention_days: 30,
    ...overrides.settings,
  };
  const pendingApprovals = approvalEntries.filter(
    (item) => (item as { state?: string }).state === "pending",
  ).length;
  const enabledConnectors = connectorEntries.filter(
    (item) => Boolean((item as { enabled?: boolean }).enabled),
  ).length;
  const activeRuns = runEntries.filter((item) =>
    ["planned", "running", "waiting_for_approval"].includes(
      String((item as { status?: string }).status ?? ""),
    ),
  ).length;

  return {
    agents: agentEntries,
    approvals: approvalEntries,
    connectors: connectorEntries,
    library: libraryEntries,
    runs: runEntries,
    settings: projectSettings,
    summary: {
      active_runs: activeRuns,
      connector_ready: enabledConnectors,
      connector_total: connectorEntries.length,
      last_activity_at: "2026-07-23T12:00:00Z",
      library_items: libraryEntries.length,
      pending_approvals: pendingApprovals,
      persistence: "Connected",
      project: projectSettings,
      ...overrides.summary,
    },
    workflows: overrides.workflows ?? createDefaultWorkflows(),
  };
}

type WorkspaceFixture = ReturnType<typeof createWorkspaceFixture>;

async function installWorkspaceFixture(
  page: Page,
  fixture: WorkspaceFixture,
  delayedSlice?: WorkspaceSlice,
) {
  const gate = createGate<void>();
  const slices: Array<[WorkspaceSlice, unknown]> = [
    ["/workspace", fixture.summary],
    ["/library", fixture.library],
    ["/runs", fixture.runs],
    ["/approvals", fixture.approvals],
    ["/connectors", fixture.connectors],
    ["/settings", fixture.settings],
    ["/agents", fixture.agents],
    ["/workflows", fixture.workflows],
  ];

  for (const [slice, payload] of slices) {
    await page.route(backendMatcher(slice), async (route) => {
      if (slice === delayedSlice) {
        await gate.wait;
      }
      await fulfillJson(route, payload);
    });
  }

  return gate;
}

function createStudioRunRecord(
  capability: StudioName,
  status: RunState,
  title: string,
) {
  const stageByStatus: Record<RunState, string> = {
    blocked: "Blocked",
    cancelled: "Cancelled",
    completed: "Completed",
    failed: "Validation failed",
    running: "Executing",
  };
  const progressByStatus: Record<RunState, number> = {
    blocked: 20,
    cancelled: 31,
    completed: 100,
    failed: 65,
    running: 54,
  };

  return {
    capability,
    current_stage: stageByStatus[status],
    durable_instance_id: `${capability}-${status}-instance`,
    id: `${capability}-${status}-run`,
    owner: "Dr. Maya Chen",
    progress: progressByStatus[status],
    started_at: "2026-07-23T12:05:00Z",
    status,
    title,
  };
}

function createInstitutionalAnswer(answer: string) {
  return {
    abstained: false,
    answer,
    citations: [],
    conflicts: [],
    escalation: null,
    insight: null,
    run: createStudioRunRecord(
      "institutional_qa",
      "completed",
      "Institutional policy answer",
    ),
    scope: "IRB and research compliance",
    versions: [
      {
        effective_date: "2026-01-01",
        source_id: "policy-irb-2026",
        status: "effective",
        title: "IRB protocol guidance",
        version: "2026.1",
      },
    ],
  };
}

function defineAutomationStep(
  id: string,
  label: string,
  kind: string,
  dependsOn: string[],
  retryLimit: number,
  approvalRequired: boolean,
) {
  return {
    approval_required: approvalRequired,
    depends_on: dependsOn,
    id,
    kind,
    label,
    retry_limit: retryLimit,
  };
}

function createAutomationResult(overrides?: {
  dry_run_status?: string;
  graph_hash?: string;
  graph_version?: string;
  run?: ReturnType<typeof createStudioRunRecord>;
  trigger?: string;
  validation_errors?: string[];
}) {
  return {
    citations: [],
    dry_run_status: overrides?.dry_run_status ?? "passed",
    graph_hash: overrides?.graph_hash ?? "graphhash1234567890",
    graph_version: overrides?.graph_version ?? "2.0",
    insight: null,
    run:
      overrides?.run ??
      createStudioRunRecord(
        "orchestration",
        "completed",
        "Validated evidence workflow",
      ),
    steps: [
      defineAutomationStep("ingest", "Ingest & verify", "activity", [], 3, false),
      defineAutomationStep(
        "retrieve",
        "Retrieve evidence",
        "fan_out",
        ["ingest"],
        2,
        false,
      ),
      defineAutomationStep(
        "synthesize",
        "Synthesize",
        "agent",
        ["retrieve"],
        1,
        false,
      ),
      defineAutomationStep(
        "review",
        "Human review",
        "approval",
        ["synthesize"],
        0,
        true,
      ),
      defineAutomationStep(
        "export",
        "Export",
        "external_action",
        ["review"],
        2,
        false,
      ),
    ],
    template_id: "evidence-review-v2",
    trigger: overrides?.trigger ?? "Manual",
    validation_errors: overrides?.validation_errors ?? [],
  };
}

async function replaceInstitutionalQuestion(page: Page, prompt: string) {
  const field = page.getByRole("textbox", { name: "Institutional question" });
  await field.focus();
  await page.keyboard.press("Control+a");
  await page.keyboard.type(prompt);
  return field;
}

async function activateWithKeyboard(page: Page, control: Locator, times = 1) {
  await control.focus();
  for (let pass = 0; pass < times; pass += 1) {
    await page.keyboard.press("Enter");
  }
}

async function expectControlPlaneDisabled(scope: Locator) {
  for (const actionName of RUN_CONTROL_NAMES) {
    await expect(scope.getByRole("button", { name: actionName })).toBeDisabled();
  }
}

test.describe("Institutional and workflow state closure", () => {
  test.describe(() => {
    test("[pw.workflow-graph] adding a new step stays invalid until the label is non-empty [pw.workflow.graph.edit:invalid]", async ({
      page,
    }, testInfo) => {
      await installWorkspaceFixture(page, createWorkspaceFixture());
      await openView(page, "orchestration");

      await page.getByRole("button", { name: "Add step" }).click();
      const confirmAdd = page.getByRole("button", { exact: true, name: "Add" });
      const stepName = page.getByLabel("Step label");

      await expect(confirmAdd).toBeDisabled();
      await stepName.fill("Notify reviewer");
      await expect(confirmAdd).toBeEnabled();
      await stepName.fill("");
      await expect(confirmAdd).toBeDisabled();
      await saveEvidence(page, testInfo, "workflow-graph-invalid");
      await expectNoAccessibilityViolations(page);
    });

    test("[pw.workflow-catalog] the catalog exposes both loading and unauthorized capability states truthfully [pw.workflow.catalog:loading][pw.workflow.catalog:unauthorized]", async ({
      page,
    }, testInfo) => {
      const fixture = createWorkspaceFixture({
        agents: [
          {
            deployment: "Hosted agent",
            id: "restricted-agent",
            model_tier: "standard",
            name: "Restricted reviewer",
            status: "Inactive",
            web_access: "Disabled",
            workflow_steps: ["review"],
          },
        ],
        connectors: [
          {
            assigned_agents: [],
            auth_kind: "oauth2",
            capabilities: ["workflow"],
            category: "Export",
            data_boundary: "Tenant",
            description: "Exports approved artifacts to a governed system.",
            enabled: false,
            id: "governed-export",
            last_tested_at: "2026-07-22T15:00:00Z",
            name: "Governed export",
            secret_status: "missing",
            terms_url: "https://example.test/terms/governed-export",
            test_status: "not_configured",
          },
        ],
      });
      const catalogGate = await installWorkspaceFixture(page, fixture, "/agents");

      await page.goto("/?view=orchestration");
      await expect(
        page.getByRole("heading", { name: "Workflow Automation" }),
      ).toBeVisible();
      await expect(
        page.getByText(`Loading workspace catalog\u2026`),
      ).toBeVisible();
      await saveEvidence(page, testInfo, "workflow-catalog-loading");
      await expectNoAccessibilityViolations(page);

      catalogGate.release();
      await expect(page.locator(WORKSPACE_READY_SELECTOR)).toHaveAttribute(
        "data-workspace-ready",
        "true",
      );

      const capabilityCatalog = page.getByRole("region", {
        name: "Workflow capability catalog",
      });
      const blockedCapability = capabilityCatalog
        .locator(".step-editor-row")
        .filter({ hasText: "Governed export" });
      const addToGraph = blockedCapability.getByRole("button", {
        name: "Add to graph",
      });

      await expect(
        blockedCapability.getByText(`Tool \u00b7 not authorized`),
      ).toBeVisible();
      await expect(addToGraph).toBeDisabled();
      await expect(addToGraph).toHaveAttribute(
        "title",
        "This capability is not authorized for this workspace yet.",
      );
      await saveEvidence(page, testInfo, "workflow-catalog-unauthorized");
      await expectNoAccessibilityViolations(page);
    });

    test("[pw.workflow-viewport] zoom controls honor keyboard activation and clamp the graph between minimum and maximum scales [pw.workflow.canvas.zoom:keyboard][pw.workflow.canvas.zoom:maximum][pw.workflow.canvas.zoom:minimum]", async ({
      page,
    }, testInfo) => {
      await installWorkspaceFixture(page, createWorkspaceFixture());
      await openView(page, "orchestration");

      const shrinkGraph = page.getByRole("button", { name: "Zoom out" });
      const enlargeGraph = page.getByRole("button", { name: "Zoom in" });
      const canvas = page.locator(".workflow-graph");
      const toolbar = page.locator(".canvas-toolbar");

      await expect(toolbar.getByText("100%")).toBeVisible();
      await activateWithKeyboard(page, enlargeGraph);
      await expect(toolbar.getByText("110%")).toBeVisible();
      await expect(canvas).toHaveAttribute("style", /scale\(1\.1\)/);

      await activateWithKeyboard(page, enlargeGraph, 4);
      await expect(toolbar.getByText("150%")).toBeVisible();
      await expect(enlargeGraph).toBeDisabled();
      await expect(canvas).toHaveAttribute("style", /scale\(1\.5\)/);
      await saveEvidence(page, testInfo, "workflow-zoom-maximum");

      await activateWithKeyboard(page, shrinkGraph, 10);
      await expect(toolbar.getByText("50%")).toBeVisible();
      await expect(shrinkGraph).toBeDisabled();
      await expect(canvas).toHaveAttribute("style", /scale\(0\.5\)/);
      await saveEvidence(page, testInfo, "workflow-zoom-minimum");
      await expectNoAccessibilityViolations(page);
    });
  });

  test.describe(() => {
    test("[pw.institutional-answer] keyboard submission enters a loading state before rendering the grounded answer [pw.institutional.question:keyboard][pw.institutional.question:loading]", async ({
      page,
    }, testInfo) => {
      await installWorkspaceFixture(page, createWorkspaceFixture());
      const runGate = createGate<void>();
      await page.route(backendMatcher("/studios/institutional_qa/run"), async (route) => {
        await runGate.wait;
        await fulfillJson(
          route,
          createInstitutionalAnswer(
            "Disclose generative AI assistance in the protocol narrative whenever it shapes study materials or analysis outputs.",
          ),
        );
      });

      await openView(page, "institutional_qa");
      await replaceInstitutionalQuestion(
        page,
        "When does an IRB protocol need a disclosure about generative AI assistance?",
      );

      const postStarted = page.waitForRequest(
        (request) =>
          request.method() === "POST" &&
          request.url().includes("/api/studios/institutional_qa/run"),
      );
      await page.keyboard.press("Tab");
      const resolvePolicyAnswer = page.getByRole("button", {
        name: "Resolve policy answer",
      });
      await expect(resolvePolicyAnswer).toBeFocused();
      await page.keyboard.press("Enter");

      const submission = (await postStarted).postDataJSON() as {
        objective: string;
        inputs: { corpus_scopes: string[] };
      };
      expect(submission.objective).toContain(
        "disclosure about generative AI assistance",
      );
      expect(submission.inputs.corpus_scopes).toEqual([...POLICY_SCOPE_ORDER]);

      await expect(
        page.getByRole("button", { name: "Running workflow..." }),
      ).toBeDisabled();
      await saveEvidence(page, testInfo, "institutional-question-loading");
      await expectNoAccessibilityViolations(page);

      runGate.release();
      await expect(page.getByText("Grounded answer")).toBeVisible();
      await expect(
        page.getByRole("heading", {
          name: /IRB protocol need a disclosure about generative AI assistance/i,
        }),
      ).toBeVisible();
      await expect(
        page.getByText(
          /Disclose generative AI assistance in the protocol narrative/i,
        ),
      ).toBeVisible();
    });

    test("[pw.workflow-trigger] keyboard selection changes the submitted trigger field before validation runs [pw.workflow.trigger:keyboard]", async ({
      page,
    }, testInfo) => {
      await installWorkspaceFixture(page, createWorkspaceFixture());
      await page.route(backendMatcher("/studios/orchestration/run"), async (route) => {
        await fulfillJson(
          route,
          createAutomationResult({
            dry_run_status: "passed",
            trigger: "Schedule",
          }),
        );
      });

      await openView(page, "orchestration");
      const triggerField = page.getByRole("combobox", { name: "Trigger" });
      await expect(triggerField).toHaveValue("Manual");

      await triggerField.focus();
      await page.keyboard.press("ArrowDown");
      await expect(triggerField).toHaveValue("Schedule");
      await saveEvidence(page, testInfo, "workflow-trigger-keyboard");
      await expectNoAccessibilityViolations(page);

      const submission = await captureRunRequest(
        page,
        "orchestration",
        "Validate & dry run",
      );
      expect(submission.inputs.trigger).toBe("Schedule");
      await expect(page.getByText("Dry run passed")).toBeVisible();
    });

    test("[pw.workflow-dry-run] validation shows the in-flight state, then reports a blocked dry run with explicit graph errors [pw.workflow.validate:validating][pw.workflow.validate:blocked]", async ({
      page,
    }, testInfo) => {
      await installWorkspaceFixture(page, createWorkspaceFixture());
      const validationGate = createGate<void>();
      await page.route(backendMatcher("/studios/orchestration/run"), async (route) => {
        await validationGate.wait;
        await fulfillJson(
          route,
          createAutomationResult({
            dry_run_status: "blocked",
            run: createStudioRunRecord(
              "orchestration",
              "blocked",
              "Blocked evidence workflow",
            ),
            validation_errors: [
              "Approval gates cannot be the first executable step in the graph.",
            ],
          }),
        );
      });

      await openView(page, "orchestration");
      const outboundValidation = page.waitForRequest(
        (request) =>
          request.method() === "POST" &&
          request.url().includes("/api/studios/orchestration/run"),
      );
      await page.getByRole("button", { name: "Validate & dry run" }).click();

      await outboundValidation;
      await expect(
        page.getByRole("button", { name: "Running workflow..." }),
      ).toBeDisabled();
      await expect(
        page.getByText(`Not run \u00b7 external side effects disabled`),
      ).toBeVisible();
      await saveEvidence(page, testInfo, "workflow-validate-validating");
      await expectNoAccessibilityViolations(page);

      validationGate.release();
      await expect(page.getByText(`1 graph errors \u00b7 blocked`)).toBeVisible();
      await expect(
        page.getByText(
          "Approval gates cannot be the first executable step in the graph.",
        ),
      ).toBeVisible();
      await expect(
        page.getByRole("button", { name: "Activate after approval" }),
      ).toBeDisabled();
      await saveEvidence(page, testInfo, "workflow-validate-blocked");
      await expectNoAccessibilityViolations(page);
    });
  });

  test.describe(() => {
    test("[pw.institutional-answer] failed submission shows the scoped error banner without clearing the typed prompt [pw.institutional.question:error]", async ({
      page,
      releaseDiagnostics,
    }, testInfo) => {
      await installWorkspaceFixture(page, createWorkspaceFixture());
      await page.route(backendMatcher("/studios/institutional_qa/run"), async (route) => {
        await fulfillJson(
          route,
          {
            detail:
              "Institutional answer generation failed for the selected policy scope.",
          },
          500,
        );
      });

      await openView(page, "institutional_qa");
      releaseDiagnostics.expectConsoleError(/500 \(Internal Server Error\)/);
      const promptField = page.getByRole("textbox", {
        name: "Institutional question",
      });
      await promptField.fill(
        "What approval language must be disclosed for automated participant messaging?",
      );
      await page.getByRole("button", { name: "Resolve policy answer" }).click();

      await expect(page.locator(".error-banner")).toContainText(
        "Institutional answer generation failed for the selected policy scope.",
      );
      await expect(promptField).toHaveValue(
        "What approval language must be disclosed for automated participant messaging?",
      );
      await expect(page.getByText("Ask from authorized policy")).toBeVisible();
      await saveEvidence(page, testInfo, "institutional-question-error");
      await expectNoAccessibilityViolations(page);
    });

    test("[pw.workflow-dry-run] validation failures surface the backend error without leaving the form stuck in a running state [pw.workflow.validate:error]", async ({
      page,
      releaseDiagnostics,
    }, testInfo) => {
      await installWorkspaceFixture(page, createWorkspaceFixture());
      await page.route(backendMatcher("/studios/orchestration/run"), async (route) => {
        await fulfillJson(
          route,
          {
            detail: "Workflow validation failed before the dry run could start.",
          },
          500,
        );
      });

      await openView(page, "orchestration");
      releaseDiagnostics.expectConsoleError(/500 \(Internal Server Error\)/);
      await page.getByRole("button", { name: "Validate & dry run" }).click();

      await expect(page.locator(".error-banner")).toContainText(
        "Workflow validation failed before the dry run could start.",
      );
      await expect(
        page.getByRole("button", { name: "Validate & dry run" }),
      ).toBeEnabled();
      await expect(
        page.getByText(`Not run \u00b7 external side effects disabled`),
      ).toBeVisible();
      await saveEvidence(page, testInfo, "workflow-validate-error");
      await expectNoAccessibilityViolations(page);
    });

    test("[pw.workflow-run] run management renders running, failed, and cancelled orchestration runs without pretending the control plane exists", async ({
      page,
    }, testInfo) => {
      const fixture = createWorkspaceFixture({
        runs: [
          {
            approval_id: null,
            artifact_count: 2,
            capability: "orchestration",
            completed_at: null,
            current_stage: "Executing",
            durable_instance_id: "orch-running-01",
            estimated_cost_usd: 1.25,
            id: "orch-running-01",
            orchestration_input: null,
            owner: "Dr. Maya Chen",
            progress: 54,
            project_id: "proj-demo",
            scheduler_managed: true,
            scheduling_state: "managed",
            stages: [],
            started_at: "2026-07-23T11:45:00Z",
            status: "running",
            title: "Active evidence workflow",
          },
          {
            approval_id: null,
            artifact_count: 1,
            capability: "orchestration",
            completed_at: "2026-07-23T11:05:00Z",
            current_stage: "Validation failed",
            durable_instance_id: "orch-failed-01",
            estimated_cost_usd: 0.9,
            id: "orch-failed-01",
            orchestration_input: null,
            owner: "Dr. Maya Chen",
            progress: 65,
            project_id: "proj-demo",
            scheduler_managed: false,
            scheduling_state: "not_managed",
            stages: [],
            started_at: "2026-07-23T10:55:00Z",
            status: "failed",
            title: "Failed evidence workflow",
          },
          {
            approval_id: null,
            artifact_count: 0,
            capability: "orchestration",
            completed_at: "2026-07-23T09:25:00Z",
            current_stage: "Cancelled",
            durable_instance_id: "orch-cancelled-01",
            estimated_cost_usd: 0.4,
            id: "orch-cancelled-01",
            orchestration_input: null,
            owner: "Dr. Maya Chen",
            progress: 31,
            project_id: "proj-demo",
            scheduler_managed: false,
            scheduling_state: "not_managed",
            stages: [],
            started_at: "2026-07-23T09:10:00Z",
            status: "cancelled",
            title: "Cancelled evidence workflow",
          },
          {
            approval_id: null,
            artifact_count: 5,
            capability: "grant",
            completed_at: "2026-07-23T08:05:00Z",
            current_stage: "Completed",
            durable_instance_id: "grant-completed-01",
            estimated_cost_usd: 2.4,
            id: "grant-completed-01",
            orchestration_input: null,
            owner: "Dr. Maya Chen",
            progress: 100,
            project_id: "proj-demo",
            scheduler_managed: false,
            scheduling_state: "not_managed",
            stages: [],
            started_at: "2026-07-23T07:30:00Z",
            status: "completed",
            title: "Grant package build",
          },
        ],
      });
      await installWorkspaceFixture(page, fixture);

      await openView(page, "orchestration");
      const runPanel = page.getByRole("region", {
        name: "Workflow run management",
      });
      await expect(runPanel.getByText("3 runs")).toBeVisible();

      for (const [rowTitle, stateName] of [
        ["Active evidence workflow", "running"],
        ["Failed evidence workflow", "failed"],
        ["Cancelled evidence workflow", "cancelled"],
      ] as const) {
        const matchingRow = runPanel
          .locator(".step-editor-row")
          .filter({ hasText: rowTitle });
        await expect(matchingRow.locator(`.table-status.${stateName}`)).toHaveText(
          stateName,
        );
        await expectControlPlaneDisabled(matchingRow);
      }

      await saveEvidence(page, testInfo, "workflow-run-statuses");
      await expectNoAccessibilityViolations(page);
    });
  });
});
