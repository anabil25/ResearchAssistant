import AxeBuilder from "@axe-core/playwright";
import type { Page, Route, TestInfo } from "@playwright/test";

import type { ConnectorSetting, ProjectSettings } from "@/lib/types";

import { completeWorkspaceRequests, expect, test } from "./fixtures";

const FIXED_TIME = "2026-07-23T18:00:00Z";

const DEFAULT_SETTINGS: ProjectSettings = {
  project_id: "atlas-observatory",
  name: "Atlas Observatory",
  description: "Independent state-coverage fixture for project settings.",
  default_classification: "confidential",
  online_research_default: false,
  retention_days: 1825,
  citation_coverage_threshold: 1,
  require_human_approval: true,
  allowed_export_destinations: ["Workspace Library"],
  model_profile: "High precision",
  evaluation_policy: "Block release on unresolved citations",
};

const DATACITE_FIXTURE: ConnectorSetting = {
  id: "datacite",
  name: "DataCite",
  category: "Datasets",
  description: "Dataset DOI metadata and bounded repository resolution.",
  auth_kind: "Managed identity",
  credential_kind: "none",
  credential_required: false,
  secret_status: "Deployment managed",
  enabled: true,
  test_status: "ready",
  last_tested_at: null,
  assigned_agents: ["dataset", "matching"],
  terms_url: "https://support.datacite.org/docs/terms-and-conditions",
  data_boundary: "Public metadata only; provider responses remain outside the tenant boundary.",
  capabilities: ["Dataset discovery", "DOI resolution"],
};

const WORKSPACE_SUMMARY = {
  project: DEFAULT_SETTINGS,
  library_items: 0,
  active_runs: 0,
  pending_approvals: 0,
  connector_ready: 1,
  connector_total: 1,
  last_activity_at: FIXED_TIME,
  persistence: "independently-authored playwright registry",
};

type RoutePlan = (route: Route, lab: SettingsRouteLab) => Promise<void>;

function cloneSettings(): ProjectSettings {
  return {
    ...DEFAULT_SETTINGS,
    allowed_export_destinations: [...DEFAULT_SETTINGS.allowed_export_destinations],
  };
}

function cloneConnector(): ConnectorSetting {
  return {
    ...DATACITE_FIXTURE,
    assigned_agents: [...DATACITE_FIXTURE.assigned_agents],
    capabilities: [...DATACITE_FIXTURE.capabilities],
  };
}

function createGate() {
  let release!: () => void;
  const ready = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { ready, release };
}

class RouteQueue {
  private queue: RoutePlan[] = [];

  enqueue(plan: RoutePlan) {
    this.queue.push(plan);
  }

  async run(
    route: Route,
    lab: SettingsRouteLab,
    fallback: () => Promise<void>,
  ) {
    const next = this.queue.shift();
    if (next) {
      await next(route, lab);
      return;
    }
    await fallback();
  }
}

class SettingsRouteLab {
  readonly settingsRead = new RouteQueue();
  readonly settingsWrite = new RouteQueue();
  readonly connectorWrite = new RouteQueue();
  readonly connectorProbe = new RouteQueue();

  private settingsState = cloneSettings();
  private connectorState = cloneConnector();

  constructor(private readonly page: Page) {}

  async install() {
    await this.page.route("**/api/backend/api/workspace", (route) =>
      this.fulfillJson(route, this.workspacePayload()),
    );
    await this.page.route("**/api/backend/api/library", (route) =>
      this.fulfillJson(route, []),
    );
    await this.page.route("**/api/backend/api/runs", (route) =>
      this.fulfillJson(route, []),
    );
    await this.page.route("**/api/backend/api/approvals", (route) =>
      this.fulfillJson(route, []),
    );
    await this.page.route("**/api/backend/api/agents", (route) =>
      this.fulfillJson(route, []),
    );
    await this.page.route("**/api/backend/api/workflows", (route) =>
      this.fulfillJson(route, []),
    );
    await this.page.route("**/api/backend/api/settings", (route) =>
      this.handleSettings(route),
    );
    await this.page.route("**/api/backend/api/connectors", (route) =>
      this.handleConnectorList(route),
    );
    await this.page.route("**/api/backend/api/connectors/datacite", (route) =>
      this.handleConnectorUpdate(route),
    );
    await this.page.route(
      "**/api/backend/api/connectors/datacite/test",
      (route) => this.handleConnectorProbe(route),
    );
  }

  connector() {
    return this.connectorState;
  }

  settings() {
    return this.settingsState;
  }

  replaceSettings(next: ProjectSettings) {
    this.settingsState = next;
  }

  mergeConnectorUpdate(
    next: Pick<ConnectorSetting, "enabled" | "assigned_agents">,
  ) {
    this.connectorState = { ...this.connectorState, ...next };
  }

  setConnectorStatus(testStatus: ConnectorSetting["test_status"]) {
    this.connectorState = {
      ...this.connectorState,
      test_status: testStatus,
      last_tested_at: FIXED_TIME,
    };
  }

  private workspacePayload() {
    return {
      ...WORKSPACE_SUMMARY,
      project: this.settingsState,
      connector_ready: ["ready", "ready_with_key"].includes(
        this.connectorState.test_status,
      )
        ? 1
        : 0,
    };
  }

  private async fulfillJson(route: Route, body: unknown, status = 200) {
    await route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  }

  private async handleSettings(route: Route) {
    if (route.request().method() === "GET") {
      await this.settingsRead.run(route, this, () =>
        this.fulfillJson(route, this.settingsState),
      );
      return;
    }

    expect(route.request().method()).toBe("PUT");
    await this.settingsWrite.run(route, this, async () => {
      this.settingsState = route.request().postDataJSON() as ProjectSettings;
      await this.fulfillJson(route, this.settingsState);
    });
  }

  private async handleConnectorList(route: Route) {
    expect(route.request().method()).toBe("GET");
    await this.fulfillJson(route, [this.connectorState]);
  }

  private async handleConnectorUpdate(route: Route) {
    expect(route.request().method()).toBe("PUT");
    await this.connectorWrite.run(route, this, async () => {
      const payload = route.request().postDataJSON() as Pick<
        ConnectorSetting,
        "enabled" | "assigned_agents"
      >;
      this.connectorState = { ...this.connectorState, ...payload };
      await this.fulfillJson(route, this.connectorState);
    });
  }

  private async handleConnectorProbe(route: Route) {
    expect(route.request().method()).toBe("POST");
    expect(route.request().postData()).toBeNull();
    await this.connectorProbe.run(route, this, () =>
      this.fulfillJson(route, this.connectorState),
    );
  }
}

async function createLab(page: Page) {
  const lab = new SettingsRouteLab(page);
  await lab.install();
  return lab;
}

async function visitSettings(page: Page) {
  await completeWorkspaceRequests(page, () => page.goto("/?view=settings"));
  await expect(page.locator(".workbench-shell")).toHaveAttribute(
    "data-workspace-ready",
    "true",
  );
}

async function switchToSettingsSection(
  page: Page,
  section: "Connectors" | "Evaluation" | "Readiness",
) {
  await visitSettings(page);
  const targetName =
    section === "Connectors" ? /Connectors 1/i : new RegExp(`^${section}$`);
  await page.getByRole("button", { name: targetName }).click();
}

async function auditVisualState(
  page: Page,
  testInfo: TestInfo,
  screenshotKey: string,
) {
  const shotPath = testInfo.outputPath(
    `${screenshotKey}-${testInfo.project.name}.png`,
  );
  await page.screenshot({ path: shotPath, fullPage: true });
  await testInfo.attach(screenshotKey, {
    path: shotPath,
    contentType: "image/png",
  });
  const axe = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(axe.violations).toEqual([]);
}

async function expectConnectorFeedback(
  page: Page,
  expectation: {
    label: string;
    detail: string;
  },
) {
  await expect(page.locator(".connector-health-badge")).toContainText(
    expectation.label,
  );
  await expect(page.locator(".connector-diagnostic strong")).toHaveText(
    expectation.label,
  );
  await expect(page.locator(".connector-diagnostic p")).toHaveText(
    expectation.detail,
  );
  await expect(page.getByRole("status")).toContainText(
    `DataCite: ${expectation.label}.`,
  );
}

test.describe("settings.general.form request states", () => {
  test("[pw.settings-general] withholds the form until the live GET resolves [pw.settings.general.form:loading]", async ({
    page,
  }, testInfo) => {
    const lab = await createLab(page);
    const gate = createGate();
    lab.settingsRead.enqueue(async (route, registry) => {
      await gate.ready;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(registry.settings()),
      });
    });

    await page.goto("/?view=settings");
    await expect(page.getByText("Loading project settings…")).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Save project settings" }),
    ).toHaveCount(0);
    await auditVisualState(page, testInfo, "general-form-load");

    gate.release();
    await expect(
      page.getByRole("button", { name: "Save project settings" }),
    ).toBeVisible();
  });

  test("[pw.settings-general] keeps the submit button in the real saving state until the PUT returns [pw.settings.general.form:saving]", async ({
    page,
  }, testInfo) => {
    const lab = await createLab(page);
    const gate = createGate();
    let payload: ProjectSettings | null = null;
    lab.settingsWrite.enqueue(async (route, registry) => {
      payload = route.request().postDataJSON() as ProjectSettings;
      await gate.ready;
      registry.replaceSettings(payload);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(registry.settings()),
      });
    });

    await visitSettings(page);
    await page
      .getByRole("textbox", { name: "Project name" })
      .fill("Atlas Observatory Controls");
    await page.getByRole("button", { name: "Save project settings" }).click();

    await expect(page.getByRole("button", { name: "Saving…" })).toBeDisabled();
    expect(payload).toMatchObject({
      name: "Atlas Observatory Controls",
      online_research_default: false,
    });
    await auditVisualState(page, testInfo, "general-form-save-pending");

    gate.release();
    await expect(page.getByText("Project settings saved.")).toBeVisible();
  });

  test("[pw.settings-general] renders a server-side settings save rejection [pw.settings.general.form:error]", async ({
    page,
    releaseDiagnostics,
  }, testInfo) => {
    const lab = await createLab(page);
    releaseDiagnostics.expectConsoleError(
      /Failed to load resource: the server responded with a status of 409/,
    );
    lab.settingsWrite.enqueue((route) =>
      route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({
          detail: "Retention policy blocked this settings update.",
        }),
      }),
    );

    await visitSettings(page);
    await page.getByRole("button", { name: "Save project settings" }).click();

    await expect(page.getByRole("status")).toContainText(
      "Retention policy blocked this settings update.",
    );
    await auditVisualState(page, testInfo, "general-form-save-error");
  });

  test("[pw.settings-general] reports an initial authorization failure instead of the form [pw.settings.general.form:unauthorized]", async ({
    page,
    releaseDiagnostics,
  }, testInfo) => {
    const lab = await createLab(page);
    releaseDiagnostics.expectConsoleError(
      /Failed to load resource: the server responded with a status of 401/,
    );
    lab.settingsRead.enqueue((route) =>
      route.fulfill({
        status: 401,
        contentType: "application/json",
        body: JSON.stringify({
          detail: "Authentication is required for project settings.",
        }),
      }),
    );

    await page.goto("/?view=settings");
    await expect(page.getByRole("status")).toContainText(
      "Authentication is required for project settings.",
    );
    await expect(
      page.getByRole("textbox", { name: "Project name" }),
    ).toHaveCount(0);
    await auditVisualState(page, testInfo, "general-form-auth-required");
  });
});

test.describe("connector configuration persistence", () => {
  test("[pw.connector-enable] disables duplicate enable saves while the PUT is pending [pw.settings.connectors.enable:saving]", async ({
    page,
  }, testInfo) => {
    const lab = await createLab(page);
    const gate = createGate();
    let payload: Pick<ConnectorSetting, "enabled" | "assigned_agents"> | null =
      null;
    lab.connectorWrite.enqueue(async (route, registry) => {
      payload = route.request().postDataJSON() as Pick<
        ConnectorSetting,
        "enabled" | "assigned_agents"
      >;
      await gate.ready;
      registry.mergeConnectorUpdate(payload);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(registry.connector()),
      });
    });

    await switchToSettingsSection(page, "Connectors");
    await page.getByLabel("Enable DataCite").uncheck();
    await page.getByRole("button", { name: "Save configuration" }).click();

    await expect(page.getByRole("button", { name: "Saving…" })).toBeDisabled();
    expect(payload).toEqual({
      enabled: false,
      assigned_agents: ["dataset", "matching"],
    });
    await auditVisualState(page, testInfo, "connector-enable-save-pending");

    gate.release();
    await expect(page.getByText("DataCite configuration saved.")).toBeVisible();
  });

  test("[pw.connector-enable] keeps the connector editor visible when the enable PUT fails [pw.settings.connectors.enable:error]", async ({
    page,
    releaseDiagnostics,
  }, testInfo) => {
    const lab = await createLab(page);
    releaseDiagnostics.expectConsoleError(
      /Failed to load resource: the server responded with a status of 503/,
    );
    lab.connectorWrite.enqueue((route) =>
      route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({
          detail: "Connector update is unavailable.",
        }),
      }),
    );

    await switchToSettingsSection(page, "Connectors");
    await page.getByLabel("Enable DataCite").uncheck();
    await page.getByRole("button", { name: "Save configuration" }).click();

    await expect(page.getByRole("status")).toContainText(
      "Connector update is unavailable.",
    );
    await expect(page.getByLabel("Enable DataCite")).not.toBeChecked();
    await auditVisualState(page, testInfo, "connector-enable-save-error");
  });

  test("[pw.connector-assign] holds the assignment controls in the busy saving state [pw.settings.connectors.assign:saving]", async ({
    page,
  }, testInfo) => {
    const lab = await createLab(page);
    const gate = createGate();
    let payload: Pick<ConnectorSetting, "enabled" | "assigned_agents"> | null =
      null;
    lab.connectorWrite.enqueue(async (route, registry) => {
      payload = route.request().postDataJSON() as Pick<
        ConnectorSetting,
        "enabled" | "assigned_agents"
      >;
      await gate.ready;
      registry.mergeConnectorUpdate(payload);
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(registry.connector()),
      });
    });

    await switchToSettingsSection(page, "Connectors");
    await page.getByLabel("Assign literature to DataCite").check();
    await page.getByRole("button", { name: "Save configuration" }).click();

    await expect(page.getByRole("button", { name: "Saving…" })).toBeDisabled();
    await expect(page.getByLabel("Assign dataset to DataCite")).toBeDisabled();
    expect(payload).toEqual({
      enabled: true,
      assigned_agents: ["dataset", "matching", "literature"],
    });
    await auditVisualState(page, testInfo, "connector-assign-save-pending");

    gate.release();
    await expect(page.getByText("DataCite configuration saved.")).toBeVisible();
  });

  test("[pw.connector-assign] blocks an enabled connector with no specialist before any network request [pw.settings.connectors.assign:invalid]", async ({
    page,
  }, testInfo) => {
    const lab = await createLab(page);
    let connectorPutCount = 0;
    lab.connectorWrite.enqueue(async (route) => {
      connectorPutCount += 1;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(DATACITE_FIXTURE),
      });
    });

    await switchToSettingsSection(page, "Connectors");
    await page.getByLabel("Assign dataset to DataCite").uncheck();
    await page.getByLabel("Assign matching to DataCite").uncheck();
    await page.getByRole("button", { name: "Save configuration" }).click();

    await expect(page.getByRole("status")).toContainText(
      "Enabled connectors must be assigned to at least one specialist.",
    );
    expect(connectorPutCount).toBe(0);
    await auditVisualState(page, testInfo, "connector-assign-client-error");
  });

  test("[pw.connector-assign] preserves the unsaved draft when assignment persistence fails [pw.settings.connectors.assign:error]", async ({
    page,
    releaseDiagnostics,
  }, testInfo) => {
    const lab = await createLab(page);
    releaseDiagnostics.expectConsoleError(
      /Failed to load resource: the server responded with a status of 422/,
    );
    lab.connectorWrite.enqueue((route) =>
      route.fulfill({
        status: 422,
        contentType: "application/json",
        body: JSON.stringify({
          detail: "Assignment policy rejected the update.",
        }),
      }),
    );

    await switchToSettingsSection(page, "Connectors");
    await page.getByLabel("Assign literature to DataCite").check();
    await page.getByRole("button", { name: "Save configuration" }).click();

    await expect(page.getByRole("status")).toContainText(
      "Assignment policy rejected the update.",
    );
    await expect(page.getByLabel("Assign literature to DataCite")).toBeChecked();
    await auditVisualState(page, testInfo, "connector-assign-save-error");
  });
});

async function exerciseConnectorProbeState(
  page: Page,
  testInfo: TestInfo,
  scenario: {
    apiStatus: ConnectorSetting["test_status"];
    label: string;
    detail: string;
    screenshot: string;
  },
) {
  const lab = await createLab(page);
  lab.connectorProbe.enqueue(async (route, registry) => {
    registry.setConnectorStatus(scenario.apiStatus);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(registry.connector()),
    });
  });

  await switchToSettingsSection(page, "Connectors");
  await page.getByRole("button", { name: "Test connection" }).click();

  await expectConnectorFeedback(page, {
    label: scenario.label,
    detail: scenario.detail,
  });
  await auditVisualState(page, testInfo, scenario.screenshot);
}

test.describe("connector diagnostic probes", () => {
  test("[pw.connector-test] labels the live POST as testing until the connector probe resolves [pw.settings.connectors.test:testing]", async ({
    page,
  }, testInfo) => {
    const lab = await createLab(page);
    const gate = createGate();
    lab.connectorProbe.enqueue(async (route) => {
      await gate.ready;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(cloneConnector()),
      });
    });

    await switchToSettingsSection(page, "Connectors");
    await page.getByRole("button", { name: "Test connection" }).click();

    await expect(page.getByRole("button", { name: "Testing…" })).toBeDisabled();
    await auditVisualState(page, testInfo, "connector-probe-pending");

    gate.release();
    await expect(page.getByRole("status")).toContainText("DataCite: Ready.");
  });

  test("[pw.connector-test] renders the configuration-required connector diagnostic [pw.settings.connectors.test:configuration-required]", async ({
    page,
  }, testInfo) => {
    await exerciseConnectorProbeState(page, testInfo, {
      apiStatus: "configuration_required",
      label: "Setup required",
      detail:
        "The provider is not down. An administrator must configure the connector gateway URL and managed identity before tests can reach it.",
      screenshot: "connector-probe-needs-setup",
    });
  });

  test("[pw.connector-test] renders the healthy connector diagnostic [pw.settings.connectors.test:healthy]", async ({
    page,
  }, testInfo) => {
    await exerciseConnectorProbeState(page, testInfo, {
      apiStatus: "ready",
      label: "Ready",
      detail:
        "The latest bounded probe succeeded and this connector can serve its assigned specialists.",
      screenshot: "connector-probe-ready",
    });
  });

  test("[pw.connector-test] renders the degraded connector diagnostic [pw.settings.connectors.test:degraded]", async ({
    page,
  }, testInfo) => {
    await exerciseConnectorProbeState(page, testInfo, {
      apiStatus: "ready_with_key",
      label: "Ready, key recommended",
      detail:
        "The connector is reachable with limited anonymous quota. Add the optional deployment-managed key for more reliable capacity.",
      screenshot: "connector-probe-key-recommended",
    });
  });

  test("[pw.connector-test] renders the failed connector diagnostic [pw.settings.connectors.test:failed]", async ({
    page,
  }, testInfo) => {
    await exerciseConnectorProbeState(page, testInfo, {
      apiStatus: "unavailable",
      label: "Connection failed",
      detail:
        "The gateway is configured, but the latest bounded provider probe failed. Retry the test or inspect gateway logs before using this source.",
      screenshot: "connector-probe-failed",
    });
  });
});

test.describe("settings readiness truth states", () => {
  test("[pw.integration-readiness] shows consent, repository setup, and ready boundaries without overstating permissions [pw.settings.integrations.readiness:needs-consent][pw.settings.integrations.readiness:blocked][pw.settings.integrations.readiness:ready]", async ({
    page,
  }, testInfo) => {
    await createLab(page);
    await switchToSettingsSection(page, "Readiness");

    await expect(
      page.locator('[data-readiness-state="needs-consent"]'),
    ).toHaveText("Needs tenant consent");
    await expect(page.locator('[data-readiness-state="blocked"]')).toHaveText(
      "Repository setup required",
    );
    await expect(page.locator('[data-readiness-state="ready"]')).toHaveText(
      "Ready: dataset toolbox",
    );
    await expect(page.getByText(/cannot merge, deploy, or promote/i)).toBeVisible();
    await auditVisualState(page, testInfo, "integration-readiness-cards");
  });

  test("[pw.evaluation-readiness] keeps release dimensions independently ready, blocked, and degraded [pw.settings.evaluations.release:ready][pw.settings.evaluations.release:blocked][pw.settings.evaluations.release:degraded]", async ({
    page,
  }, testInfo) => {
    await createLab(page);
    await switchToSettingsSection(page, "Evaluation");

    await expect(page.locator('[data-evaluation-state="ready"]')).toHaveCount(4);
    await expect(page.locator('[data-evaluation-state="blocked"]')).toContainText(
      "Blocked · Blocking",
    );
    await expect(
      page.locator('[data-evaluation-state="degraded"]'),
    ).toContainText("Degraded · Warning");
    await expect(
      page.locator(".evaluation-card").filter({ hasText: "Claim entailment" }),
    ).toContainText("96%");
    await auditVisualState(page, testInfo, "evaluation-readiness-cards");
  });
});
