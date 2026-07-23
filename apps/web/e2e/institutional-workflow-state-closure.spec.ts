import AxeBuilder from "@axe-core/playwright";
import type { Page, TestInfo } from "@playwright/test";

import { expect, test } from "./fixtures";

function escapeRegex(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function backendApi(path: string) {
  return new RegExp(`${escapeRegex(`/api/backend/api${path}`)}$`);
}

function deferred<T = void>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    void rejectPromise;
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

async function runStudioAndCapturePayload(
  page: Page,
  capability: string,
  buttonName: string,
) {
  const requestPromise = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      request.url().includes(`/api/studios/${capability}/run`),
  );
  const responsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().includes(`/api/studios/${capability}/run`),
  );
  await page.getByRole("button", { name: buttonName }).click();
  const request = await requestPromise;
  const response = await responsePromise;
  expect(response.status()).toBe(200);
  return request.postDataJSON() as {
    objective: string;
    online_research: boolean;
    inputs: Record<string, unknown>;
  };
}

function createWorkspaceData(overrides?: {
  agents?: unknown[];
  approvals?: unknown[];
  connectors?: unknown[];
  library?: unknown[];
  runs?: unknown[];
  settings?: Record<string, unknown>;
  summary?: Record<string, unknown>;
  workflows?: unknown[];
}) {
  const library = overrides?.library ?? [];
  const approvals = overrides?.approvals ?? [];
  const connectors =
    overrides?.connectors ??
    [
      {
        assigned_agents: ["lit-agent"],
        auth_kind: "apiKey",
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
  const agents =
    overrides?.agents ??
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
  const settings = {
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
    ...overrides?.settings,
  };
  const workflows =
    overrides?.workflows ??
    [
      {
        capability: "institutional_qa",
        title: "Institutional policy answer",
        purpose: "Resolve policy answers from authorized corpora.",
        primary_artifact: "Grounded answer",
        online_research_policy: "disabled",
        stages: [
          {
            id: "retrieve",
            label: "Retrieve",
            description: "Resolve effective policy versions.",
            owner: "institutional-agent",
            human_checkpoint: false,
          },
          {
            id: "answer",
            label: "Answer",
            description: "Return an answer or abstain.",
            owner: "institutional-agent",
            human_checkpoint: false,
          },
        ],
      },
      {
        capability: "orchestration",
        title: "Workflow automation",
        purpose: "Validate and activate a bounded orchestration graph.",
        primary_artifact: "Versioned workflow",
        online_research_policy: "disabled",
        stages: [
          {
            id: "draft",
            label: "Draft",
            description: "Edit the workflow graph.",
            owner: "workflow-builder",
            human_checkpoint: false,
          },
          {
            id: "validate",
            label: "Validate",
            description: "Dry-run the exact graph without side effects.",
            owner: "workflow-validator",
            human_checkpoint: false,
          },
        ],
      },
    ];
  const runs = overrides?.runs ?? [];
  const summary = {
    active_runs: runs.filter((run) =>
      ["planned", "running", "waiting_for_approval"].includes(
        String((run as { status?: string }).status ?? ""),
      ),
    ).length,
    connector_ready: connectors.filter(
      (connector) => Boolean((connector as { enabled?: boolean }).enabled),
    ).length,
    connector_total: connectors.length,
    last_activity_at: "2026-07-23T12:00:00Z",
    library_items: library.length,
    pending_approvals: approvals.filter(
      (approval) => (approval as { state?: string }).state === "pending",
    ).length,
    persistence: "Connected",
    project: settings,
    ...overrides?.summary,
  };
  return {
    summary,
    library,
    runs,
    approvals,
    connectors,
    settings,
    agents,
    workflows,
  };
}

async function mockWorkspace(
  page: Page,
  workspace: ReturnType<typeof createWorkspaceData>,
  pendingPath?: "/agents" | "/approvals" | "/connectors" | "/library" | "/runs" | "/settings" | "/workspace" | "/workflows",
) {
  const releasePending = deferred<void>();
  const routes = [
    ["/workspace", workspace.summary],
    ["/library", workspace.library],
    ["/runs", workspace.runs],
    ["/approvals", workspace.approvals],
    ["/connectors", workspace.connectors],
    ["/settings", workspace.settings],
    ["/agents", workspace.agents],
    ["/workflows", workspace.workflows],
  ] as const;

  for (const [path, body] of routes) {
    await page.route(backendApi(path), async (route) => {
      if (path === pendingPath) {
        await releasePending.promise;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(body),
      });
    });
  }

  return releasePending;
}

function createStudioRun(
  capability: "institutional_qa" | "orchestration",
  status: "completed" | "blocked" | "running" | "cancelled" | "failed",
  title: string,
) {
  return {
    capability,
    current_stage:
      status === "running"
        ? "Executing"
        : status === "blocked"
          ? "Blocked"
          : status === "failed"
            ? "Validation failed"
            : status === "cancelled"
              ? "Cancelled"
              : "Completed",
    durable_instance_id: `${capability}-${status}-instance`,
    id: `${capability}-${status}-run`,
    owner: "Dr. Maya Chen",
    progress:
      status === "running"
        ? 54
        : status === "blocked"
          ? 20
          : status === "failed"
            ? 65
            : status === "cancelled"
              ? 31
              : 100,
    started_at: "2026-07-23T12:05:00Z",
    status,
    title,
  };
}

function createInstitutionalResult(answer: string) {
  return {
    abstained: false,
    answer,
    citations: [],
    conflicts: [],
    escalation: null,
    insight: null,
    run: createStudioRun(
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

function createAutomationResult(overrides?: {
  dry_run_status?: string;
  graph_hash?: string;
  graph_version?: string;
  run?: ReturnType<typeof createStudioRun>;
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
      createStudioRun("orchestration", "completed", "Validated evidence workflow"),
    steps: [
      {
        id: "ingest",
        label: "Ingest & verify",
        kind: "activity",
        depends_on: [],
        retry_limit: 3,
        approval_required: false,
      },
      {
        id: "retrieve",
        label: "Retrieve evidence",
        kind: "fan_out",
        depends_on: ["ingest"],
        retry_limit: 2,
        approval_required: false,
      },
      {
        id: "synthesize",
        label: "Synthesize",
        kind: "agent",
        depends_on: ["retrieve"],
        retry_limit: 1,
        approval_required: false,
      },
      {
        id: "review",
        label: "Human review",
        kind: "approval",
        depends_on: ["synthesize"],
        retry_limit: 0,
        approval_required: true,
      },
      {
        id: "export",
        label: "Export",
        kind: "external_action",
        depends_on: ["review"],
        retry_limit: 2,
        approval_required: false,
      },
    ],
    template_id: "evidence-review-v2",
    trigger: overrides?.trigger ?? "Manual",
    validation_errors: overrides?.validation_errors ?? [],
  };
}

test.describe("Institutional and workflow state closure", () => {
  test("[pw.institutional-answer] keyboard submission enters a loading state before rendering the grounded answer [pw.institutional.question:keyboard][pw.institutional.question:loading]", async ({
    page,
  }, testInfo) => {
    await mockWorkspace(page, createWorkspaceData());
    const releaseRun = deferred<void>();
    await page.route(backendApi("/studios/institutional_qa/run"), async (route) => {
      await releaseRun.promise;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          createInstitutionalResult(
            "Disclose generative AI assistance in the protocol narrative whenever it shapes study materials or analysis outputs.",
          ),
        ),
      });
    });

    await gotoView(page, "institutional_qa");
    const question = page.getByRole("textbox", {
      name: "Institutional question",
    });
    await question.focus();
    await page.keyboard.press("Control+a");
    await page.keyboard.type(
      "When does an IRB protocol need a disclosure about generative AI assistance?",
    );

    const requestPromise = page.waitForRequest(
      (request) =>
        request.method() === "POST" &&
        request.url().includes("/api/studios/institutional_qa/run"),
    );
    await page.keyboard.press("Tab");
    await expect(
      page.getByRole("button", { name: "Resolve policy answer" }),
    ).toBeFocused();
    await page.keyboard.press("Enter");

    const payload = (await requestPromise).postDataJSON() as {
      objective: string;
      inputs: { corpus_scopes: string[] };
    };
    expect(payload.objective).toContain("disclosure about generative AI assistance");
    expect(payload.inputs.corpus_scopes).toEqual([
      "irb",
      "records",
      "governance",
    ]);

    await expect(
      page.getByRole("button", { name: "Running workflow..." }),
    ).toBeDisabled();
    await capture(page, testInfo, "institutional-question-loading");
    await expectAccessible(page);

    releaseRun.resolve();
    await expect(page.getByText("Grounded answer")).toBeVisible();
    await expect(
      page.getByRole("heading", {
        name: /IRB protocol need a disclosure about generative AI assistance/i,
      }),
    ).toBeVisible();
    await expect(
      page.getByText(/Disclose generative AI assistance in the protocol narrative/i),
    ).toBeVisible();
  });

  test("[pw.institutional-answer] failed submission shows the scoped error banner without clearing the typed prompt [pw.institutional.question:error]", async ({
    page,
    releaseDiagnostics,
  }, testInfo) => {
    await mockWorkspace(page, createWorkspaceData());
    await page.route(backendApi("/studios/institutional_qa/run"), async (route) => {
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({
          detail: "Institutional answer generation failed for the selected policy scope.",
        }),
      });
    });

    await gotoView(page, "institutional_qa");
    releaseDiagnostics.expectConsoleError(/500 \(Internal Server Error\)/);
    const question = page.getByRole("textbox", {
      name: "Institutional question",
    });
    await question.fill(
      "What approval language must be disclosed for automated participant messaging?",
    );
    await page.getByRole("button", { name: "Resolve policy answer" }).click();

    await expect(page.locator(".error-banner")).toContainText(
      "Institutional answer generation failed for the selected policy scope.",
    );
    await expect(question).toHaveValue(
      "What approval language must be disclosed for automated participant messaging?",
    );
    await expect(page.getByText("Ask from authorized policy")).toBeVisible();
    await capture(page, testInfo, "institutional-question-error");
    await expectAccessible(page);
  });

  test("[pw.workflow-catalog] the catalog exposes both loading and unauthorized capability states truthfully [pw.workflow.catalog:loading][pw.workflow.catalog:unauthorized]", async ({
    page,
  }, testInfo) => {
    const workspace = createWorkspaceData({
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
    const releasePending = await mockWorkspace(page, workspace, "/agents");

    await page.goto("/?view=orchestration");
    await expect(
      page.getByRole("heading", { name: "Workflow Automation" }),
    ).toBeVisible();
    await expect(page.getByText("Loading workspace catalog…")).toBeVisible();
    await capture(page, testInfo, "workflow-catalog-loading");
    await expectAccessible(page);

    releasePending.resolve();
    await expect(page.locator(".workbench-shell")).toHaveAttribute(
      "data-workspace-ready",
      "true",
    );
    const catalog = page.getByRole("region", {
      name: "Workflow capability catalog",
    });
    const unauthorizedRow = catalog
      .locator(".step-editor-row")
      .filter({ hasText: "Governed export" });
    await expect(unauthorizedRow.getByText("Tool · not authorized")).toBeVisible();
    await expect(
      unauthorizedRow.getByRole("button", { name: "Add to graph" }),
    ).toBeDisabled();
    await expect(
      unauthorizedRow.getByRole("button", { name: "Add to graph" }),
    ).toHaveAttribute(
      "title",
      "This capability is not authorized for this workspace yet.",
    );
    await capture(page, testInfo, "workflow-catalog-unauthorized");
    await expectAccessible(page);
  });

  test("[pw.workflow-viewport] zoom controls honor keyboard activation and clamp the graph between minimum and maximum scales [pw.workflow.canvas.zoom:keyboard][pw.workflow.canvas.zoom:maximum][pw.workflow.canvas.zoom:minimum]", async ({
    page,
  }, testInfo) => {
    await mockWorkspace(page, createWorkspaceData());
    await gotoView(page, "orchestration");

    const zoomOut = page.getByRole("button", { name: "Zoom out" });
    const zoomIn = page.getByRole("button", { name: "Zoom in" });
    const graph = page.locator(".workflow-graph");

    await expect(page.locator(".canvas-toolbar").getByText("100%")).toBeVisible();
    await zoomIn.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator(".canvas-toolbar").getByText("110%")).toBeVisible();
    await expect(graph).toHaveAttribute("style", /scale\(1\.1\)/);

    await zoomIn.focus();
    for (let index = 0; index < 4; index += 1) {
      await page.keyboard.press("Enter");
    }
    await expect(page.locator(".canvas-toolbar").getByText("150%")).toBeVisible();
    await expect(zoomIn).toBeDisabled();
    await expect(graph).toHaveAttribute("style", /scale\(1\.5\)/);
    await capture(page, testInfo, "workflow-zoom-maximum");

    await zoomOut.focus();
    for (let index = 0; index < 10; index += 1) {
      await page.keyboard.press("Enter");
    }
    await expect(page.locator(".canvas-toolbar").getByText("50%")).toBeVisible();
    await expect(zoomOut).toBeDisabled();
    await expect(graph).toHaveAttribute("style", /scale\(0\.5\)/);
    await capture(page, testInfo, "workflow-zoom-minimum");
    await expectAccessible(page);
  });

  test("[pw.workflow-trigger] keyboard selection changes the submitted trigger field before validation runs [pw.workflow.trigger:keyboard]", async ({
    page,
  }, testInfo) => {
    await mockWorkspace(page, createWorkspaceData());
    await page.route(backendApi("/studios/orchestration/run"), async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          createAutomationResult({ dry_run_status: "passed", trigger: "Schedule" }),
        ),
      });
    });

    await gotoView(page, "orchestration");
    const trigger = page.getByRole("combobox", { name: "Trigger" });
    await expect(trigger).toHaveValue("Manual");

    await trigger.focus();
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("ArrowDown");
    await expect(trigger).toHaveValue("Schedule");
    await capture(page, testInfo, "workflow-trigger-keyboard");
    await expectAccessible(page);

    const payload = await runStudioAndCapturePayload(
      page,
      "orchestration",
      "Validate & dry run",
    );
    expect(payload.inputs.trigger).toBe("Schedule");
    await expect(page.getByText("Dry run passed")).toBeVisible();
  });

  test("[pw.workflow-graph] adding a new step stays invalid until the label is non-empty [pw.workflow.graph.edit:invalid]", async ({
    page,
  }, testInfo) => {
    await mockWorkspace(page, createWorkspaceData());
    await gotoView(page, "orchestration");

    await page.getByRole("button", { name: "Add step" }).click();
    const commitButton = page.getByRole("button", { name: "Add", exact: true });
    const labelInput = page.getByLabel("Step label");

    await expect(commitButton).toBeDisabled();
    await labelInput.fill("Notify reviewer");
    await expect(commitButton).toBeEnabled();
    await labelInput.fill("");
    await expect(commitButton).toBeDisabled();
    await capture(page, testInfo, "workflow-graph-invalid");
    await expectAccessible(page);
  });

  test("[pw.workflow-dry-run] validation shows the in-flight state, then reports a blocked dry run with explicit graph errors [pw.workflow.validate:validating][pw.workflow.validate:blocked]", async ({
    page,
  }, testInfo) => {
    await mockWorkspace(page, createWorkspaceData());
    const releaseRun = deferred<void>();
    await page.route(backendApi("/studios/orchestration/run"), async (route) => {
      await releaseRun.promise;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          createAutomationResult({
            dry_run_status: "blocked",
            run: createStudioRun(
              "orchestration",
              "blocked",
              "Blocked evidence workflow",
            ),
            validation_errors: [
              "Approval gates cannot be the first executable step in the graph.",
            ],
          }),
        ),
      });
    });

    await gotoView(page, "orchestration");
    const requestPromise = page.waitForRequest(
      (request) =>
        request.method() === "POST" &&
        request.url().includes("/api/studios/orchestration/run"),
    );
    await page.getByRole("button", { name: "Validate & dry run" }).click();

    await requestPromise;
    await expect(
      page.getByRole("button", { name: "Running workflow..." }),
    ).toBeDisabled();
    await expect(
      page.getByText("Not run · external side effects disabled"),
    ).toBeVisible();
    await capture(page, testInfo, "workflow-validate-validating");
    await expectAccessible(page);

    releaseRun.resolve();
    await expect(page.getByText("1 graph errors · blocked")).toBeVisible();
    await expect(
      page.getByText(
        "Approval gates cannot be the first executable step in the graph.",
      ),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Activate after approval" }),
    ).toBeDisabled();
    await capture(page, testInfo, "workflow-validate-blocked");
    await expectAccessible(page);
  });

  test("[pw.workflow-dry-run] validation failures surface the backend error without leaving the form stuck in a running state [pw.workflow.validate:error]", async ({
    page,
    releaseDiagnostics,
  }, testInfo) => {
    await mockWorkspace(page, createWorkspaceData());
    await page.route(backendApi("/studios/orchestration/run"), async (route) => {
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({
          detail: "Workflow validation failed before the dry run could start.",
        }),
      });
    });

    await gotoView(page, "orchestration");
    releaseDiagnostics.expectConsoleError(/500 \(Internal Server Error\)/);
    await page.getByRole("button", { name: "Validate & dry run" }).click();

    await expect(page.locator(".error-banner")).toContainText(
      "Workflow validation failed before the dry run could start.",
    );
    await expect(
      page.getByRole("button", { name: "Validate & dry run" }),
    ).toBeEnabled();
    await expect(page.getByText("Not run · external side effects disabled")).toBeVisible();
    await capture(page, testInfo, "workflow-validate-error");
    await expectAccessible(page);
  });

  test("[pw.workflow-run] run management renders running, failed, and cancelled orchestration runs without pretending the control plane exists [pw.workflow.run.manage:running][pw.workflow.run.manage:failed][pw.workflow.run.manage:cancelled]", async ({
    page,
  }, testInfo) => {
    const workspace = createWorkspaceData({
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
    await mockWorkspace(page, workspace);

    await gotoView(page, "orchestration");
    const runManager = page.getByRole("region", {
      name: "Workflow run management",
    });
    await expect(runManager.getByText("3 runs")).toBeVisible();

    for (const [title, status] of [
      ["Active evidence workflow", "running"],
      ["Failed evidence workflow", "failed"],
      ["Cancelled evidence workflow", "cancelled"],
    ] as const) {
      const row = runManager.locator(".step-editor-row").filter({ hasText: title });
      await expect(row.locator(`.table-status.${status}`)).toHaveText(status);
      await expect(row.getByRole("button", { name: "Pause" })).toBeDisabled();
      await expect(row.getByRole("button", { name: "Resume" })).toBeDisabled();
      await expect(row.getByRole("button", { name: "Retry" })).toBeDisabled();
      await expect(row.getByRole("button", { name: "Cancel" })).toBeDisabled();
    }

    await capture(page, testInfo, "workflow-run-statuses");
    await expectAccessible(page);
  });
});
