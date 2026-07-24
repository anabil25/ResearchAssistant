import AxeBuilder from "@axe-core/playwright";
import type { Page, Route, TestInfo } from "@playwright/test";

import { completeWorkspaceRequests, expect, test } from "./fixtures";

type ValidationMode = "passed" | "blocked" | "error";

type WorkflowStepPayload = {
  id: string;
  label: string;
  kind: string;
  depends_on: string[];
  retry_limit: number;
  approval_required: boolean;
};

type DryRunRequestBody = {
  objective: string;
  online_research: boolean;
  inputs: {
    template_id: string;
    trigger: string;
    steps: WorkflowStepPayload[];
  };
};

type BackendOptions = {
  catalogStall?: Promise<void>;
  validationStall?: Promise<void>;
  validationMode?: ValidationMode;
  seedRuns?: Array<Record<string, unknown>>;
};

const FROZEN_AT = "2026-05-06T15:16:17Z";
const VALIDATED_GRAPH_VERSION = "7.4";
const VALIDATED_GRAPH_HASH = "delta-workflow-fingerprint-abcdef123456";

const WORKSPACE_PAYLOAD = {
  project: {
    project_id: "automation-proving-ground",
    name: "Automation proving ground",
    description: "Deterministic workflow studio coverage.",
    default_classification: "internal",
    online_research_default: false,
    retention_days: 120,
    citation_coverage_threshold: 1,
    require_human_approval: true,
    allowed_export_destinations: ["Workspace Library"],
    model_profile: "Balanced quality",
    evaluation_policy: "Block unresolved citations",
  },
  library_items: 2,
  active_runs: 1,
  pending_approvals: 1,
  connector_ready: 1,
  connector_total: 3,
  last_activity_at: FROZEN_AT,
  persistence: "deterministic playwright fixture",
};

const SETTINGS_PAYLOAD = {
  project_id: "automation-proving-ground",
  name: "Automation proving ground",
  description: "Deterministic workflow studio coverage.",
  default_classification: "internal",
  online_research_default: false,
  retention_days: 120,
  citation_coverage_threshold: 1,
  require_human_approval: true,
  allowed_export_destinations: ["Workspace Library"],
  model_profile: "Balanced quality",
  evaluation_policy: "Block unresolved citations",
};

const AGENT_FIXTURES = [
  {
    id: "triage-agent",
    name: "Signal triage",
    model_tier: "Primary",
    status: "Active",
    web_access: "Workspace approved",
    workflow_steps: ["Collect", "Review"],
    deployment: "Foundry Hosted Agent",
  },
  {
    id: "paused-agent",
    name: "Paused reviewer",
    model_tier: "Secondary",
    status: "Disabled",
    web_access: "Disabled",
    workflow_steps: [],
    deployment: "Foundry Hosted Agent",
  },
];

const CONNECTOR_FIXTURES = [
  {
    id: "archive-export",
    name: "Archive export",
    category: "Storage",
    description: "Pushes validated artifacts into a bounded archive.",
    auth_kind: "OAuth",
    secret_status: "Configured",
    enabled: true,
    test_status: "ready",
    last_tested_at: FROZEN_AT,
    assigned_agents: ["orchestration"],
    terms_url: "https://example.test/archive-export",
    data_boundary: "Validated workflow outputs only.",
    capabilities: ["Export"],
  },
  {
    id: "failing-export",
    name: "Failing export",
    category: "Storage",
    description: "Readiness checks still fail.",
    auth_kind: "OAuth",
    secret_status: "Configured",
    enabled: true,
    test_status: "error",
    last_tested_at: FROZEN_AT,
    assigned_agents: ["orchestration"],
    terms_url: "https://example.test/failing-export",
    data_boundary: "Validated workflow outputs only.",
    capabilities: ["Export"],
  },
  {
    id: "grant-export",
    name: "Grant export",
    category: "Storage",
    description: "Ready, but assigned only to grant workflows.",
    auth_kind: "OAuth",
    secret_status: "Configured",
    enabled: true,
    test_status: "ready",
    last_tested_at: FROZEN_AT,
    assigned_agents: ["grant"],
    terms_url: "https://example.test/grant-export",
    data_boundary: "Grant-only outputs.",
    capabilities: ["Export"],
  },
];

function createLatch<T = void>() {
  let release!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((resolve) => {
    release = resolve;
  });
  return { promise, release };
}

function workflowRun(status: string, sequence: number) {
  return {
    id: `seeded-run-${status}`,
    durable_instance_id: `instance-${sequence}`,
    project_id: "automation-proving-ground",
    capability: "orchestration",
    title: `Scenario ${status.replaceAll("_", " ")}`,
    status,
    progress: status === "completed" ? 100 : sequence * 9,
    current_stage: "Deterministic coverage",
    owner: "Workflow operator",
    started_at: FROZEN_AT,
    completed_at: status === "completed" ? FROZEN_AT : null,
    artifact_count: status === "completed" ? 2 : 0,
    approval_id: null,
    estimated_cost_usd: 0,
    scheduler_managed: false,
    scheduling_state: "not_managed",
    orchestration_input: null,
    stages: [],
  };
}

class WorkflowApiDouble {
  readonly dryRunBodies: DryRunRequestBody[] = [];
  readonly unexpectedRequests: string[] = [];
  readonly firstDryRun = createLatch<DryRunRequestBody>();

  constructor(
    private readonly page: Page,
    private readonly options: BackendOptions = {},
  ) {}

  async install() {
    await this.page.route("**/api/backend/api/**", async (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname.replace(
        /^.*\/api\/backend\/api/,
        "",
      );
      this.unexpectedRequests.push(`${request.method()} ${path}`);
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ detail: `Unexpected deterministic route: ${path}` }),
      });
    });

    await this.registerGet("/workspace", async () => WORKSPACE_PAYLOAD);
    await this.registerGet("/library", async () => []);
    await this.registerGet("/runs", async () => this.options.seedRuns ?? []);
    await this.registerGet("/approvals", async () => []);
    await this.registerGet("/connectors", async () => CONNECTOR_FIXTURES);
    await this.registerGet("/settings", async () => SETTINGS_PAYLOAD);
    await this.registerGet("/workflows", async () => []);
    await this.registerGet("/agents", async () => {
      if (this.options.catalogStall) {
        await this.options.catalogStall;
      }
      return AGENT_FIXTURES;
    });
    await this.registerPost("/studios/orchestration/run", async (route) => {
      const body = route.request().postDataJSON() as DryRunRequestBody;
      this.dryRunBodies.push(body);
      this.firstDryRun.release(body);
      if (this.options.validationStall) {
        await this.options.validationStall;
      }

      if (this.options.validationMode === "error") {
        await route.fulfill({
          status: 503,
          contentType: "application/json",
          body: JSON.stringify({
            detail: "Deterministic workflow validation failed.",
          }),
        });
        return;
      }

      const blocked = this.options.validationMode === "blocked";
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          run: workflowRun(blocked ? "blocked" : "completed", 1),
          template_id: body.inputs.template_id,
          trigger: body.inputs.trigger,
          steps: body.inputs.steps,
          validation_errors: blocked
            ? ["Approval policy blocks the external action."]
            : [],
          dry_run_status: blocked ? "failed" : "passed",
          graph_version: VALIDATED_GRAPH_VERSION,
          graph_hash: VALIDATED_GRAPH_HASH,
          citations: [],
        }),
      });
    });
  }

  assertNothingUnexpected() {
    expect(this.unexpectedRequests).toEqual([]);
  }

  private async registerGet(path: string, responder: () => Promise<unknown>) {
    await this.page.route(`**/api/backend/api${path}`, async (route) => {
      if (route.request().method() !== "GET") {
        await route.fallback();
        return;
      }
      await this.fulfillJson(route, 200, await responder());
    });
  }

  private async registerPost(
    path: string,
    responder: (route: Route) => Promise<void>,
  ) {
    await this.page.route(`**/api/backend/api${path}`, async (route) => {
      if (route.request().method() !== "POST") {
        await route.fallback();
        return;
      }
      await responder(route);
    });
  }

  private async fulfillJson(route: Route, status: number, body: unknown) {
    await route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  }
}

class WorkflowStudioHarness {
  constructor(
    private readonly page: Page,
    private readonly testInfo: TestInfo,
  ) {}

  async openReady() {
    await completeWorkspaceRequests(this.page, async () => {
      await this.page.goto("/?view=orchestration");
    });
    await expect(this.heading).toBeVisible();
    await expect(this.shell).toHaveAttribute("data-workspace-ready", "true");
  }

  async openUntilShellVisible() {
    await this.page.goto("/?view=orchestration");
    await expect(this.heading).toBeVisible();
  }

  get heading() {
    return this.page.getByRole("heading", {
      level: 1,
      name: "Workflow Automation",
    });
  }

  get shell() {
    return this.page.locator(".workbench-shell");
  }

  get triggerSelect() {
    return this.page.getByRole("combobox", { name: "Trigger" });
  }

  get activateButton() {
    return this.page.getByRole("button", { name: /Activate|Activated/ });
  }

  templateButton(title: string) {
    return this.page.locator(".template-strip button").filter({ hasText: title });
  }

  catalogRow(label: string) {
    return this.page.locator(".workflow-catalog .step-editor-row").filter({
      hasText: label,
    });
  }

  runRow(label: string) {
    return this.page.locator(".workflow-run-manager .step-editor-row").filter({
      hasText: label,
    });
  }

  async capture(name: string) {
    const image = this.testInfo.outputPath(`${name}.png`);
    await this.page.screenshot({ path: image, fullPage: true });
    await this.testInfo.attach(name, {
      path: image,
      contentType: "image/png",
    });
  }

  async audit() {
    const report = await new AxeBuilder({ page: this.page })
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
      .analyze();
    expect(report.violations).toEqual([]);
  }
}

test.describe("workflow state coverage", () => {
  test(
    "[pw.workflow-template] [pw.workflow-trigger] keyboard trigger selection and template changes submit the real template graph [pw.workflow.template:ready][pw.workflow.template:selected][pw.workflow.trigger:ready][pw.workflow.trigger:selected][pw.workflow.trigger:keyboard]",
    async ({ page }, testInfo) => {
      const backend = new WorkflowApiDouble(page);
      await backend.install();
      const studio = new WorkflowStudioHarness(page, testInfo);
      await studio.openReady();

      await expect(studio.templateButton("Evidence review")).toHaveAttribute(
        "aria-pressed",
        "true",
      );
      await expect(page.getByText("Ingest & verify").first()).toBeVisible();
      await expect(studio.triggerSelect.locator("option")).toHaveText([
        "Manual",
        "Schedule",
        "Webhook",
        "GitHub",
        "Library upload",
      ]);

      await studio.triggerSelect.focus();
      await page.keyboard.press("ArrowDown");
      await page.keyboard.press("ArrowDown");
      await page.keyboard.press("ArrowDown");
      await expect(studio.triggerSelect).toHaveValue("GitHub");

      await studio.templateButton("Grant red team").click();
      await expect(studio.templateButton("Grant red team")).toHaveAttribute(
        "aria-pressed",
        "true",
      );
      await expect(studio.templateButton("Evidence review")).toHaveAttribute(
        "aria-pressed",
        "false",
      );
      await expect(page.getByText("Parse notice").first()).toBeVisible();
      await expect(page.getByText("Ingest & verify")).toHaveCount(0);

      await page.getByRole("button", { name: "Validate & dry run" }).click();
      await expect(page.getByText("Dry run passed")).toBeVisible();
      expect(backend.dryRunBodies[0]?.inputs).toMatchObject({
        template_id: "grant-review-v2",
        trigger: "GitHub",
      });
      expect(backend.dryRunBodies[0]?.inputs.steps.map((step) => step.id)).toEqual([
        "parse-notice",
        "draft-response",
        "compliance-review",
        "approve-submission",
      ]);
      expect(
        backend.dryRunBodies[0]?.inputs.steps.at(-1)?.approval_required,
      ).toBe(true);

      await studio.capture("workflow-template-trigger-keyboard");
      await studio.audit();
      backend.assertNothingUnexpected();
    },
  );

  test(
    "[pw.workflow-catalog] catalog loading, unauthorized capability gating, ready actions, and preview all stay deterministic [pw.workflow.catalog:loading][pw.workflow.catalog:ready][pw.workflow.catalog:unauthorized][pw.workflow.catalog:preview]",
    async ({ page }, testInfo) => {
      const catalogRelease = createLatch();
      const backend = new WorkflowApiDouble(page, {
        catalogStall: catalogRelease.promise,
      });
      await backend.install();
      const studio = new WorkflowStudioHarness(page, testInfo);
      await studio.openUntilShellVisible();

      const catalog = page.getByRole("region", {
        name: "Workflow capability catalog",
      });
      await expect(
        catalog.getByText("Loading workspace catalog…"),
      ).toBeVisible();
      await studio.capture("workflow-catalog-loading-state");

      catalogRelease.release();
      await expect(studio.shell).toHaveAttribute("data-workspace-ready", "true");

      const activeAgent = studio.catalogRow("Signal triage");
      const disabledAgent = studio.catalogRow("Paused reviewer");
      const failingConnector = studio.catalogRow("Failing export");
      const grantConnector = studio.catalogRow("Grant export");

      await expect(
        activeAgent.getByRole("button", { name: "Add to graph" }),
      ).toBeEnabled();
      for (const row of [disabledAgent, failingConnector, grantConnector]) {
        await expect(row).toContainText("not authorized");
        await expect(row.getByRole("button", { name: "Add to graph" })).toBeDisabled();
        await expect(row.getByRole("button", { name: "Add to graph" })).toHaveAttribute(
          "title",
          "This capability is not authorized for this workspace yet.",
        );
      }

      await activeAgent
        .getByRole("button", { name: "Preview Signal triage" })
        .click();
      await expect(
        activeAgent.getByText(/Foundry Hosted Agent · Primary tier · Workspace approved/i),
      ).toBeVisible();
      await activeAgent.getByRole("button", { name: "Add to graph" }).click();
      await expect(
        page.getByRole("button", { name: "Remove Signal triage" }),
      ).toBeVisible();

      await studio.capture("workflow-catalog-authorized-preview");
      await studio.audit();
      backend.assertNothingUnexpected();
    },
  );

  test(
    "[pw.workflow-graph] adding and editing a workflow step covers draft, invalid, dirty, and valid payload states [pw.workflow.graph.edit:draft][pw.workflow.graph.edit:invalid][pw.workflow.graph.edit:dirty][pw.workflow.graph.edit:valid]",
    async ({ page }, testInfo) => {
      const backend = new WorkflowApiDouble(page);
      await backend.install();
      const studio = new WorkflowStudioHarness(page, testInfo);
      await studio.openReady();

      const editor = page.getByRole("region", { name: "Workflow step editor" });
      await expect(editor.getByRole("heading", { name: "Steps (5/8)" })).toBeVisible();
      await expect(
        editor.getByRole("button", { name: "Remove Ingest & verify" }),
      ).toBeDisabled();

      await editor.getByRole("button", { name: "Add step" }).click();
      const draftForm = editor.locator(".step-editor-form").last();
      const addButton = draftForm.getByRole("button", { name: "Add", exact: true });
      await expect(addButton).toBeDisabled();

      await draftForm.getByLabel("Step label").fill("Notify finance");
      await draftForm.getByLabel("Kind").selectOption("external_action");
      await draftForm.getByLabel("Retry limit (0-5)").fill("7");
      await draftForm.getByRole("checkbox", { name: "Human review" }).check();
      await draftForm.getByRole("checkbox", { name: "Approval required" }).check();
      await expect(addButton).toBeEnabled();
      await addButton.click();

      const newStepButton = editor.getByRole("button", {
        name: "Configure Notify finance",
      });
      await expect(newStepButton).toBeVisible();
      await newStepButton.click();
      const editForm = editor.locator(".step-editor-form").first();
      await editForm.getByLabel("Retry limit (0-5)").fill("5");
      await editForm.getByRole("button", { name: "Save" }).click();
      await expect(
        editor.getByText(
          /external action · depends on review · 5 retries · approval gate/i,
        ),
      ).toBeVisible();

      await page.getByRole("button", { name: "Validate & dry run" }).click();
      await expect(page.getByText("Dry run passed")).toBeVisible();
      expect(backend.dryRunBodies[0]?.inputs.steps).toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            label: "Notify finance",
            depends_on: ["review"],
            retry_limit: 5,
            approval_required: true,
          }),
        ]),
      );

      await studio.capture("workflow-graph-edited-valid");
      await studio.audit();
      backend.assertNothingUnexpected();
    },
  );

  test(
    "[pw.workflow-viewport] zoom controls hold at the real bounds and stay keyboard operable [pw.workflow.canvas.zoom:ready][pw.workflow.canvas.zoom:minimum][pw.workflow.canvas.zoom:maximum][pw.workflow.canvas.zoom:keyboard]",
    async ({ page }, testInfo) => {
      const backend = new WorkflowApiDouble(page);
      await backend.install();
      const studio = new WorkflowStudioHarness(page, testInfo);
      await studio.openReady();

      const zoomReadout = page.getByRole("status", { name: "Workflow zoom" });
      const zoomOut = page.getByRole("button", { name: "Zoom out" });
      const zoomIn = page.getByRole("button", { name: "Zoom in" });
      await expect(zoomReadout).toHaveText("100%");

      for (let index = 0; index < 5; index += 1) {
        await zoomIn.click();
      }
      await expect(zoomReadout).toHaveText("150%");
      await expect(zoomIn).toBeDisabled();
      await studio.capture("workflow-zoom-upper-bound");

      await zoomOut.focus();
      await page.keyboard.press("Space");
      await expect(zoomReadout).toHaveText("140%");
      for (let index = 0; index < 9; index += 1) {
        await zoomOut.click();
      }
      await expect(zoomReadout).toHaveText("50%");
      await expect(zoomOut).toBeDisabled();

      await studio.capture("workflow-zoom-lower-bound");
      await studio.audit();
      backend.assertNothingUnexpected();
    },
  );

  test(
    "[pw.workflow-dry-run] dry run moves from draft to validating to passed without side effects [pw.workflow.validate:draft][pw.workflow.validate:validating][pw.workflow.validate:passed]",
    async ({ page }, testInfo) => {
      const validationRelease = createLatch();
      const backend = new WorkflowApiDouble(page, {
        validationStall: validationRelease.promise,
      });
      await backend.install();
      const studio = new WorkflowStudioHarness(page, testInfo);
      await studio.openReady();

      await expect(
        page.getByText("Not run · external side effects disabled"),
      ).toBeVisible();
      await page.getByRole("button", { name: "Validate & dry run" }).click();
      const firstSubmission = await backend.firstDryRun.promise;
      await expect(
        page.getByRole("button", { name: "Running workflow..." }),
      ).toBeDisabled();
      await studio.capture("workflow-dry-run-validating");

      validationRelease.release();
      await expect(page.getByText("0 graph errors · passed")).toBeVisible();
      expect(firstSubmission).toMatchObject({
        objective: "Validate and dry run the configured evidence workflow.",
        online_research: false,
      });

      await studio.capture("workflow-dry-run-passed");
      await studio.audit();
      backend.assertNothingUnexpected();
    },
  );

  test(
    "[pw.workflow-dry-run] blocked validation leaves activation unavailable and surfaces graph errors [pw.workflow.validate:blocked]",
    async ({ page }, testInfo) => {
      const backend = new WorkflowApiDouble(page, {
        validationMode: "blocked",
      });
      await backend.install();
      const studio = new WorkflowStudioHarness(page, testInfo);
      await studio.openReady();

      await page.getByRole("button", { name: "Validate & dry run" }).click();
      await expect(page.getByText("1 graph errors · failed")).toBeVisible();
      await expect(
        page.getByText("Approval policy blocks the external action."),
      ).toBeVisible();
      await expect(studio.activateButton).toBeDisabled();

      await studio.capture("workflow-dry-run-blocked");
      await studio.audit();
      backend.assertNothingUnexpected();
    },
  );

  test(
    "[pw.workflow-dry-run] transport failures surface a visible validation error and keep activation blocked [pw.workflow.validate:error]",
    async ({ page, releaseDiagnostics }, testInfo) => {
      const backend = new WorkflowApiDouble(page, {
        validationMode: "error",
      });
      await backend.install();
      const studio = new WorkflowStudioHarness(page, testInfo);
      await studio.openReady();

      releaseDiagnostics.expectConsoleError(
        /Failed to load resource: the server responded with a status of 503/,
      );
      await page.getByRole("button", { name: "Validate & dry run" }).click();
      await expect(page.locator(".error-banner[role='alert']")).toHaveText(
        "Deterministic workflow validation failed.",
      );
      await expect(studio.activateButton).toBeDisabled();

      await studio.capture("workflow-dry-run-error");
      await studio.audit();
      backend.assertNothingUnexpected();
    },
  );

  test(
    "[pw.workflow-activation] activation stays disabled until a passing dry run, leaves activation unrecorded when the confirmation dialog is cancelled, and locks after confirmation [pw.workflow.activate:disabled][pw.workflow.activate:ready][pw.workflow.activate:waiting-for-approval][pw.workflow.activate:active]",
    async ({ page }, testInfo) => {
      // Note: there is no distinct "rejected" state. The confirmation
      // dialog's Cancel button (studio-components.tsx) only closes the
      // dialog with no persisted outcome — behaviorally indistinguishable
      // from never opening it. This test still exercises that Cancel path
      // to prove it doesn't activate and the control remains usable
      // afterward, but does not claim it as a separate manifest state.
      const backend = new WorkflowApiDouble(page);
      await backend.install();
      const studio = new WorkflowStudioHarness(page, testInfo);
      await studio.openReady();

      await expect(studio.activateButton).toBeDisabled();
      await page.getByRole("button", { name: "Validate & dry run" }).click();
      await expect(page.getByText("Dry run passed")).toBeVisible();
      await expect(studio.activateButton).toBeEnabled();

      await studio.activateButton.click();
      const dialog = page.getByRole("dialog", {
        name: `Activate graph ${VALIDATED_GRAPH_VERSION}`,
      });
      await expect(dialog).toContainText(
        `hash ${VALIDATED_GRAPH_HASH.slice(0, 12)}…`,
      );
      await studio.capture("workflow-activation-waiting-dialog");
      await dialog.getByRole("button", { name: "Cancel" }).click();
      await expect(dialog).not.toBeVisible();
      await expect(studio.activateButton).toBeEnabled();

      await studio.activateButton.click();
      await page
        .getByRole("dialog", { name: `Activate graph ${VALIDATED_GRAPH_VERSION}` })
        .getByRole("button", { name: "Confirm activation" })
        .click();
      await expect(
        page.getByRole("button", { name: "Activated (draft workspace)" }),
      ).toBeDisabled();

      await studio.capture("workflow-activation-confirmed");
      await studio.audit();
      backend.assertNothingUnexpected();
    },
  );

  test(
    "[pw.workflow-activation] an open confirmation dialog makes background controls inert and still refuses to activate a stale config forced through [pw.workflow.activate:disabled]",
    async ({ page }, testInfo) => {
      const backend = new WorkflowApiDouble(page);
      await backend.install();
      const studio = new WorkflowStudioHarness(page, testInfo);
      await studio.openReady();

      await page.getByRole("button", { name: "Validate & dry run" }).click();
      await expect(page.getByText("Dry run passed")).toBeVisible();
      await expect(studio.activateButton).toBeEnabled();

      await studio.activateButton.click();
      const dialog = page.getByRole("dialog", {
        name: `Activate graph ${VALIDATED_GRAPH_VERSION}`,
      });
      await expect(dialog).toBeVisible();

      // Defense layer 1: the background subtree is genuinely inert while
      // the dialog is open (studio-components.tsx renders the dialog via
      // a portal to document.body and marks the studio page `inert`).
      // A real browser refuses pointer/keyboard interaction with an inert
      // subtree, so a normal (non-forced) action never reaches the control.
      // Defense layer 1: the background subtree is genuinely inert while
      // the dialog is open (studio-components.tsx renders the dialog via
      // a portal to document.body and marks the studio page `inert`).
      // A real browser refuses pointer interaction with an inert subtree,
      // so a normal (non-forced) click never reaches the control.
      await expect(
        page.locator(".studio-page.automation-studio"),
      ).toHaveJSProperty("inert", true);
      await expect(
        page
          .getByRole("button", { name: "Validate & dry run" })
          .click({ timeout: 1000 }),
      ).rejects.toThrow();
      await expect(dialog).toBeVisible();

      // Defense layer 2 (belt-and-suspenders): even if a mutation reached
      // the background config while the dialog stayed open (e.g. via a
      // forced/bypassing event, simulating an assistive-tech or race
      // edge case that inert does not cover), the Confirm handler
      // re-derives canActivate from the *current* config/result at click
      // time and refuses to activate a stale fingerprint.
      const triggerSelect = studio.triggerSelect;
      await triggerSelect.selectOption("GitHub", { force: true });
      await expect(
        dialog.getByRole("button", { name: "Confirm activation" }),
      ).toBeDisabled();
      await dialog
        .getByRole("button", { name: "Confirm activation" })
        .click({ force: true });
      await expect(
        page.getByRole("button", { name: "Activated (draft workspace)" }),
      ).toHaveCount(0);
      await expect(dialog).toBeVisible();

      await studio.capture("workflow-activation-stale-dialog-blocked");
      backend.assertNothingUnexpected();
    },
  );

  test(
    "[pw.workflow-run] run management shows every durable status, keeps unavailable scheduler controls disabled, and clones drafts [pw.workflow.run.manage:planned][pw.workflow.run.manage:running][pw.workflow.run.manage:waiting-for-approval][pw.workflow.run.manage:partial][pw.workflow.run.manage:blocked][pw.workflow.run.manage:completed][pw.workflow.run.manage:cancelled][pw.workflow.run.manage:failed]",
    async ({ page }, testInfo) => {
      const allStatuses = [
        "planned",
        "running",
        "waiting_for_approval",
        "partial",
        "blocked",
        "completed",
        "cancelled",
        "failed",
      ];
      const backend = new WorkflowApiDouble(page, {
        seedRuns: allStatuses.map(workflowRun),
      });
      await backend.install();
      const studio = new WorkflowStudioHarness(page, testInfo);
      await studio.openReady();

      const manager = page.getByRole("region", {
        name: "Workflow run management",
      });
      for (const status of allStatuses) {
        await expect(
          manager.getByText(status.replaceAll("_", " "), { exact: true }),
        ).toBeVisible();
      }

      const runningRow = studio.runRow("Scenario running");
      for (const name of ["Pause", "Resume", "Retry", "Cancel"]) {
        await expect(runningRow.getByRole("button", { name })).toBeDisabled();
      }

      await runningRow.getByRole("button", { name: "Clone" }).click();
      await expect(manager.getByRole("status")).toHaveText(
        /Cloned Scenario running into a new draft \(v2\.0\)\. Validate and dry run before activating\./i,
      );
      await studio.capture("workflow-run-management-states");
      await studio.audit();

      await manager
        .locator(".step-editor-row")
        .filter({ hasText: "Scenario completed" })
        .getByRole("button", { name: "Inspect" })
        .click();
      await expect(
        page.getByRole("heading", { level: 1, name: "Runs & Approvals" }),
      ).toBeVisible();
      backend.assertNothingUnexpected();
    },
  );
});
