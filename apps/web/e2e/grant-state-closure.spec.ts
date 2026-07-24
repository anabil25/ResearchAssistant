import AxeBuilder from "@axe-core/playwright";
import type { Page, TestInfo } from "@playwright/test";

import { expect, test } from "./fixtures";

/**
 * `playwright.config.ts` allocates a fresh OS-assigned port per invocation
 * (`resolveLocalPorts`/`resolvePort`) rather than a fixed 3000, precisely so
 * two concurrent worktrees/sessions running this suite on one shared
 * machine never collide on the same port. That means the web app's real
 * origin can't be a literal here -- it has to be read back from the env var
 * the config memoized it into (`PLAYWRIGHT_WEB_PORT`, inherited by every
 * worker process this file runs in). Falling back to a hardcoded port would
 * leave every `page.route`/`waitForRequest` below matching nothing and
 * hanging until the test timeout on any run where the app isn't, in fact,
 * on port 3000.
 */
function resolveWebOrigin(): string {
  if (process.env.PLAYWRIGHT_BASE_URL) {
    return process.env.PLAYWRIGHT_BASE_URL;
  }
  return `http://127.0.0.1:${process.env.PLAYWRIGHT_WEB_PORT ?? "3000"}`;
}

const WEB_ORIGIN = resolveWebOrigin();

function backendRoutePattern(pathname: string): RegExp {
  const escapedOrigin = WEB_ORIGIN.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`^${escapedOrigin}${pathname}$`);
}

const ROUTES = {
  workspace: backendRoutePattern("/api/backend/api/workspace"),
  library: backendRoutePattern("/api/backend/api/library"),
  runs: backendRoutePattern("/api/backend/api/runs"),
  approvals: backendRoutePattern("/api/backend/api/approvals"),
  connectors: backendRoutePattern("/api/backend/api/connectors"),
  settings: backendRoutePattern("/api/backend/api/settings"),
  agents: backendRoutePattern("/api/backend/api/agents"),
  workflows: backendRoutePattern("/api/backend/api/workflows"),
  grantRun: backendRoutePattern("/api/backend/api/studios/grant/run"),
} as const;

async function openWorkspaceView(page: Page, view: string) {
  await page.goto(`/?view=${view}`);
  await expect(page.locator(".workbench-shell")).toHaveAttribute(
    "data-workspace-ready",
    "true",
  );
}

async function snapshotState(page: Page, testInfo: TestInfo, id: string) {
  const filename = `${id}-${testInfo.project.name}.png`;
  const path = testInfo.outputPath(filename);
  await page.screenshot({ path, fullPage: true });
  await testInfo.attach(id, { path, contentType: "image/png" });
}

async function assertNoAxeViolations(page: Page) {
  const { violations } = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(violations).toEqual([]);
}

function deepClone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

/** A promise a test can hold open, then release on demand, to observe an
 * in-flight/loading UI state before letting a mocked request resolve. */
function deferredGate() {
  let open!: () => void;
  const held = new Promise<void>((resolveHeld) => {
    open = resolveHeld;
  });
  return { held, open };
}

const WORKSPACE_PROJECT = {
  allowed_export_destinations: ["SharePoint research site / Grant reviews"],
  citation_coverage_threshold: 1,
  default_classification: "Internal",
  description: "Deterministic grant workspace fixtures for Playwright coverage.",
  evaluation_policy: "Manual review required for release.",
  model_profile: "foundry-hosted",
  name: "Research Assistant Demo",
  online_research_default: false,
  project_id: "proj-demo",
  require_human_approval: true,
  retention_days: 30,
};

interface FundingConnectorFixture {
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
}

function fundingConnector(
  fields: Pick<FundingConnectorFixture, "id" | "name" | "description" | "terms_url" | "capabilities"> &
    Partial<FundingConnectorFixture>,
): FundingConnectorFixture {
  return {
    category: "Funding",
    auth_kind: "None",
    secret_status: "Not required",
    enabled: true,
    test_status: "ready",
    last_tested_at: null,
    assigned_agents: ["grant"],
    data_boundary: "Public metadata only.",
    ...fields,
  };
}

const CORE_FUNDING_CONNECTORS: FundingConnectorFixture[] = [
  fundingConnector({
    id: "grants_gov",
    name: "Grants.gov",
    description: "Authoritative U.S. federal opportunity records.",
    terms_url: "https://www.grants.gov/web/grants/legal-privacy.html",
    capabilities: ["Opportunities"],
  }),
  fundingConnector({
    id: "nih_reporter",
    name: "NIH Reporter",
    description: "Federal biomedical funding and award records.",
    terms_url: "https://reporter.nih.gov/",
    capabilities: ["Opportunities", "Awards"],
  }),
];

const GRANT_RUN_FIXTURE = {
  run: {
    capability: "grant",
    current_stage: "Package drafted",
    durable_instance_id: "fixture-grant-run",
    id: "fixture-grant-run",
    owner: "Dr. Maya Chen",
    progress: 100,
    started_at: "2026-07-16T12:00:00Z",
    status: "completed",
    title: "Fixture grant package",
  },
  opportunity: {
    canonical_url: "https://www.grants.gov/",
    deadline: "2026-10-15",
    identifier: "SORI-2026-01",
    sponsor: "Example Federal Research Office",
    status: "Open",
    title: "Open Research Infrastructure Opportunity",
  },
  requirements: [
    {
      id: "summary",
      text: "Two-page project summary",
      category: "Narrative",
      status: "mapped",
      evidence_ids: ["cite-1"],
    },
    {
      id: "budget",
      text: "Budget justification",
      category: "Budget",
      status: "needs_input",
      evidence_ids: [],
    },
  ],
  fact_gaps: [] as Array<{
    id: string;
    label: string;
    guidance: string;
    status: string;
  }>,
  specific_aims: ["Aim one."],
  sections: [
    {
      id: "significance",
      title: "Significance",
      status: "draft",
      word_count: 3,
      body: "Significance body text.",
      evidence_ids: ["cite-1"],
    },
    {
      id: "approach",
      title: "Approach",
      status: "draft",
      word_count: 3,
      body: "Approach body text.",
      evidence_ids: ["cite-1"],
    },
  ],
  readiness: 80,
  blockers: [] as string[],
  citations: [
    {
      id: "cite-1",
      title: "Open Research Infrastructure Opportunity",
      section: "Eligibility",
      quote: "Applicants must summarize the project in two pages.",
      source_id: "notice-1",
      checksum: "sha256:notice",
      license: "Public domain",
      chunk_id: "chunk-notice-1",
      page_start: 1,
    },
  ],
  insight: {
    agent_name: "Grant drafting",
    content: "Draft package reviewed.",
    evidence_state: "verified",
    online_research_used: false,
    referenced_source_ids: ["notice-1"],
    unresolved_source_ids: [],
  },
};

async function fulfillJson(page: Page, pattern: RegExp, body: unknown) {
  await page.route(pattern, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });
}

interface WorkspaceFixtureOverrides {
  connectors?: object[];
  workflows?: object[];
  runs?: object[];
  approvals?: object[];
  library?: object[];
  agents?: object[];
}

async function seedWorkspace(
  page: Page,
  overrides: WorkspaceFixtureOverrides = {},
) {
  const connectors = overrides.connectors ?? deepClone(CORE_FUNDING_CONNECTORS);
  const workflows = overrides.workflows ?? [];
  const runs = overrides.runs ?? [];
  const approvals = overrides.approvals ?? [];
  const library = overrides.library ?? [];
  const agents = overrides.agents ?? [];

  await fulfillJson(page, ROUTES.workspace, {
    project: WORKSPACE_PROJECT,
    persistence: "Fixture ready",
    library_items: library.length,
    active_runs: 0,
    pending_approvals: approvals.length,
    connector_ready: connectors.length,
    connector_total: connectors.length,
    last_activity_at: "2026-07-23T12:00:00Z",
  });
  await fulfillJson(page, ROUTES.library, library);
  await fulfillJson(page, ROUTES.runs, runs);
  await fulfillJson(page, ROUTES.approvals, approvals);
  await fulfillJson(page, ROUTES.connectors, connectors);
  await fulfillJson(page, ROUTES.settings, WORKSPACE_PROJECT);
  await fulfillJson(page, ROUTES.agents, agents);
  await fulfillJson(page, ROUTES.workflows, workflows);
}

async function seedGrantRunResponses(page: Page, results: object | object[]) {
  const remaining = Array.isArray(results) ? [...results] : [results];
  await page.route(ROUTES.grantRun, async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    const next = remaining.length > 1 ? remaining.shift() : remaining[0];
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(next),
    });
  });
}

async function submitGrantRunAndCapturePayload(
  page: Page,
  trigger: () => Promise<void>,
) {
  const requestPromise = page.waitForRequest(
    (request) => request.method() === "POST" && ROUTES.grantRun.test(request.url()),
  );
  const responsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      ROUTES.grantRun.test(response.url()),
  );

  await trigger();

  const request = await requestPromise;
  const response = await responsePromise;
  return {
    request,
    response,
    payload: request.postDataJSON() as {
      objective: string;
      online_research: boolean;
      inputs: Record<string, unknown>;
    },
  };
}

test.describe("Grant Studio: funding source discovery", () => {
  test("[pw.grant-discovery] [pw.grant-connectors] unassigned funding connectors surface the empty source list and disable opportunity discovery [pw.grant.discovery.search:disabled][pw.grant.discovery.sources:empty]", async ({
    page,
  }, testInfo) => {
    await seedWorkspace(page, { connectors: [] });
    await openWorkspaceView(page, "grant");

    const fundingPanel = page.getByLabel("Funding source discovery");
    await expect(
      fundingPanel.getByText(/no funding connectors are assigned yet/i),
    ).toBeVisible();

    const searchField = page.getByLabel("Search funding opportunities");
    await expect(searchField).toBeDisabled();
    await expect(
      page.getByText(
        /select at least one funding connector above to discover opportunities/i,
      ),
    ).toBeVisible();

    await snapshotState(page, testInfo, "grant-connectors-empty-discovery-disabled");
    await assertNoAxeViolations(page);
  });

  test("[pw.grant-discovery] [pw.grant-connectors] [pw.grant-opportunity] keyboard search, source selection, and opportunity id edits flow into the draft payload [pw.grant.discovery.search:keyboard][pw.grant.discovery.sources:selected][pw.grant.opportunity.id:keyboard]", async ({
    page,
  }, testInfo) => {
    await seedWorkspace(page, { connectors: deepClone(CORE_FUNDING_CONNECTORS) });
    await seedGrantRunResponses(page, deepClone(GRANT_RUN_FIXTURE));
    await openWorkspaceView(page, "grant");

    const discoveryPanel = page.getByLabel("Opportunity discovery");
    const searchField = page.getByLabel("Search funding opportunities");
    await searchField.focus();
    await page.keyboard.type("nih");
    await expect(searchField).toHaveValue("nih");
    await expect(discoveryPanel.getByText("NIH Reporter")).toBeVisible();
    await expect(discoveryPanel.getByText("Grants.gov")).toHaveCount(0);

    const nihReporterSource = page.getByRole("checkbox", { name: "NIH Reporter" });
    await expect(nihReporterSource).toBeChecked();
    await nihReporterSource.uncheck();
    await expect(nihReporterSource).not.toBeChecked();

    const opportunityIdField = page.getByLabel("Opportunity ID");
    await opportunityIdField.focus();
    await page.keyboard.press("Control+A");
    await page.keyboard.type("RFA-TRANS-77");
    await expect(opportunityIdField).toHaveValue("RFA-TRANS-77");

    await snapshotState(page, testInfo, "grant-discovery-keyboard-selected-sources");
    await assertNoAxeViolations(page);

    const { payload, response } = await submitGrantRunAndCapturePayload(
      page,
      async () => {
        await page
          .getByRole("button", { name: "Parse notice & build package" })
          .click();
      },
    );
    expect(response.status()).toBe(200);
    expect(payload.inputs.opportunity_id).toBe("RFA-TRANS-77");
    expect(payload.inputs.funding_sources).toEqual(["grants_gov"]);
  });

  test("[pw.grant.discovery.sources:needs-connection][pw.grant.discovery.sources:unavailable][pw.grant.discovery.sources:disabled] disables non-ready funding connectors with distinct captions and excludes them from the run payload", async ({
    page,
  }, testInfo) => {
    const connectors = [
      ...deepClone(CORE_FUNDING_CONNECTORS),
      fundingConnector({
        id: "foundation_dir",
        name: "Foundation Directory",
        description: "Private foundation opportunity records.",
        terms_url: "https://foundationdirectory.example.test/",
        capabilities: ["Awards"],
        test_status: "configuration_required",
      }),
      fundingConnector({
        id: "crossref",
        name: "Crossref",
        description: "DOI metadata and scholarly work resolution.",
        terms_url: "https://www.crossref.org/services/metadata-delivery/rest-api/",
        capabilities: ["DOI resolution"],
        test_status: "unavailable",
      }),
      fundingConnector({
        id: "nsf_award",
        name: "NSF Award Search",
        description: "Federal science award records.",
        terms_url: "https://www.nsf.gov/awardsearch/",
        capabilities: ["Awards"],
        enabled: false,
      }),
    ];
    await seedWorkspace(page, { connectors });
    await seedGrantRunResponses(page, deepClone(GRANT_RUN_FIXTURE));
    await openWorkspaceView(page, "grant");

    const readySource = page.getByRole("checkbox", { name: "Grants.gov" });
    const needsConnectionSource = page.getByRole("checkbox", {
      name: /^Foundation Directory/,
    });
    const unavailableSource = page.getByRole("checkbox", { name: /^Crossref/ });
    const disabledSource = page.getByRole("checkbox", {
      name: /^NSF Award Search/,
    });

    await expect(readySource).toBeEnabled();
    await expect(readySource).toBeChecked();
    await expect(needsConnectionSource).toBeDisabled();
    await expect(needsConnectionSource).not.toBeChecked();
    await expect(unavailableSource).toBeDisabled();
    await expect(unavailableSource).not.toBeChecked();
    await expect(disabledSource).toBeDisabled();
    await expect(disabledSource).not.toBeChecked();
    await expect(page.getByText("Needs connection setup")).toBeVisible();
    await expect(page.getByText("Currently unavailable")).toBeVisible();
    await expect(page.getByText("Disabled in Settings")).toBeVisible();

    await snapshotState(page, testInfo, "grant-connectors-availability");
    await assertNoAxeViolations(page);

    const { payload } = await submitGrantRunAndCapturePayload(page, async () => {
      await page
        .getByRole("button", { name: "Parse notice & build package" })
        .click();
    });
    expect(payload.inputs.funding_sources).toContain("grants_gov");
    expect(payload.inputs.funding_sources).not.toContain("foundation_dir");
    expect(payload.inputs.funding_sources).not.toContain("crossref");
    expect(payload.inputs.funding_sources).not.toContain("nsf_award");
  });
});

test.describe("Grant Studio: draft package build", () => {
  test("[pw.grant-draft] [pw.grant-build] project framing is ready for keyboard editing and submits via keyboard while the package run is loading [pw.grant.editor.framing:ready][pw.grant.editor.framing:keyboard][pw.grant.editor.framing:success][pw.grant.package.build:keyboard][pw.grant.package.build:loading]", async ({
    page,
  }, testInfo) => {
    const runGate = deferredGate();
    await seedWorkspace(page, { connectors: deepClone(CORE_FUNDING_CONNECTORS) });
    await page.route(ROUTES.grantRun, async (route) => {
      await runGate.held;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(deepClone(GRANT_RUN_FIXTURE)),
      });
    });
    await openWorkspaceView(page, "grant");

    const framingField = page.getByLabel("Project framing");
    await expect(framingField).toHaveValue(
      "Develop a competitive application for an open research infrastructure program.",
    );
    await framingField.focus();
    await page.keyboard.press("Control+A");
    await page.keyboard.type(
      "Develop a citation-backed infrastructure package for an auditable pilot program.",
    );
    await expect(framingField).toHaveValue(
      "Develop a citation-backed infrastructure package for an auditable pilot program.",
    );

    await snapshotState(page, testInfo, "grant-framing-keyboard");

    const requestPromise = page.waitForRequest(
      (request) =>
        request.method() === "POST" && ROUTES.grantRun.test(request.url()),
    );
    const responsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        ROUTES.grantRun.test(response.url()),
    );

    const buildButton = page.getByRole("button", {
      name: "Parse notice & build package",
    });
    await buildButton.focus();
    await page.keyboard.press("Enter");

    const loadingButton = page.getByRole("button", {
      name: "Running workflow...",
    });
    await expect(loadingButton).toBeDisabled();
    await snapshotState(page, testInfo, "grant-package-build-loading");

    runGate.open();
    const request = await requestPromise;
    const response = await responsePromise;
    expect(response.status()).toBe(200);
    const payload = request.postDataJSON() as {
      objective: string;
      inputs: Record<string, unknown>;
    };
    expect(payload.objective).toBe(
      "Develop a citation-backed infrastructure package for an auditable pilot program.",
    );
    await expect(page.locator(".requirement-matrix .subtle-chip")).toHaveText(
      "2 mapped",
    );

    await snapshotState(page, testInfo, "grant-framing-success");
    await assertNoAxeViolations(page);
  });

  test("[pw.grant-fit] [pw.grant-build] verified facts remain checked and explicit when the unrelated package build request returns an error [pw.grant.package.build:error]", async ({
    page,
    releaseDiagnostics,
  }, testInfo) => {
    releaseDiagnostics.expectConsoleError(
      /Failed to load resource: the server responded with a status of 500/i,
    );
    await seedWorkspace(page, { connectors: deepClone(CORE_FUNDING_CONNECTORS) });
    await page.route(ROUTES.grantRun, async (route) => {
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({
          detail:
            "Verified project facts could not be incorporated into the grant build.",
        }),
      });
    });
    await openWorkspaceView(page, "grant");

    const verifiedFactsCheckbox = page.getByRole("checkbox", {
      name: /core project facts verified/i,
    });
    await verifiedFactsCheckbox.check();
    await expect(verifiedFactsCheckbox).toBeChecked();

    const { payload, response } = await submitGrantRunAndCapturePayload(
      page,
      async () => {
        await page
          .getByRole("button", { name: "Parse notice & build package" })
          .click();
      },
    );
    expect(response.status()).toBe(500);
    expect(payload.inputs.project_facts).toEqual([
      "Research office sponsor confirmed",
      "PI role confirmed",
    ]);

    await expect(page.locator(".error-banner")).toContainText(
      "Verified project facts could not be incorporated into the grant build.",
    );
    await expect(verifiedFactsCheckbox).toBeChecked();
    await expect(
      page.getByRole("button", { name: "Parse notice & build package" }),
    ).toBeEnabled();

    await snapshotState(page, testInfo, "grant-build-error-facts-confirmed");
    await assertNoAxeViolations(page);
  });
});

test.describe("Grant Studio: requirement detail states", () => {
  test("[pw.grant-requirements] parsed requirement rows surface blocked, needs-input, and unmapped states [pw.grant.requirements.open:blocked][pw.grant.requirements.open:needs-input][pw.grant.requirements.open:unmapped]", async ({
    page,
  }, testInfo) => {
    const requirementsResult = deepClone(GRANT_RUN_FIXTURE);
    requirementsResult.requirements = [
      {
        id: "blocked-requirement",
        text: "Institutional sign-off letter",
        category: "Approvals",
        status: "blocked",
        evidence_ids: [],
      },
      {
        id: "needs-input-requirement",
        text: "Budget justification",
        category: "Budget",
        status: "needs_input",
        evidence_ids: [],
      },
      {
        id: "unmapped-requirement",
        text: "Facilities and other resources",
        category: "Narrative",
        status: "unmapped",
        evidence_ids: [],
      },
    ];
    requirementsResult.blockers = ["budget sign-off"];

    await seedWorkspace(page, { connectors: deepClone(CORE_FUNDING_CONNECTORS) });
    await seedGrantRunResponses(page, requirementsResult);
    await openWorkspaceView(page, "grant");

    const { response } = await submitGrantRunAndCapturePayload(page, async () => {
      await page
        .getByRole("button", { name: "Parse notice & build package" })
        .click();
    });
    expect(response.status()).toBe(200);
    await expect(page.getByText(/export blocked by/i)).toContainText(
      "budget sign-off",
    );

    await page
      .getByRole("button", { name: /institutional sign-off letter/i })
      .click();
    const blockedDialog = page.getByRole("dialog", {
      name: "Institutional sign-off letter",
    });
    await expect(blockedDialog).toContainText("Status: blocked");
    await expect(blockedDialog).toContainText(
      "No source evidence is linked to this requirement yet.",
    );
    await snapshotState(page, testInfo, "grant-requirements-blocked");
    await assertNoAxeViolations(page);
    await blockedDialog.getByLabel("Close requirement detail").click();

    await page.getByRole("button", { name: /budget justification/i }).click();
    const needsInputDialog = page.getByRole("dialog", {
      name: "Budget justification",
    });
    await expect(needsInputDialog).toContainText("Status: needs input");
    await expect(needsInputDialog).toContainText(
      "No source evidence is linked to this requirement yet.",
    );
    await snapshotState(page, testInfo, "grant-requirements-needs-input");
    await assertNoAxeViolations(page);
    await needsInputDialog.getByLabel("Close requirement detail").click();

    await page
      .getByRole("button", { name: /facilities and other resources/i })
      .click();
    const unmappedDialog = page.getByRole("dialog", {
      name: "Facilities and other resources",
    });
    await expect(unmappedDialog).toContainText("Status: unmapped");
    await expect(unmappedDialog).toContainText(
      "No source evidence is linked to this requirement yet.",
    );
    await snapshotState(page, testInfo, "grant-requirements-unmapped");
    await assertNoAxeViolations(page);
  });
});

test.describe("Grant Studio: red-team review", () => {
  test("[pw.grant-review] red-team runs show the running state and surface findings [pw.grant.review.red-team:running][pw.grant.review.red-team:findings]", async ({
    page,
  }, testInfo) => {
    const findingsResult = deepClone(GRANT_RUN_FIXTURE);
    findingsResult.fact_gaps = [
      {
        id: "gap-1",
        label: "Biosketch missing",
        guidance: "Upload the verified PI biosketch before export.",
        status: "missing",
      },
    ];
    findingsResult.blockers = ["budget sign-off"];

    const reviewGate = deferredGate();
    await seedWorkspace(page, { connectors: deepClone(CORE_FUNDING_CONNECTORS) });
    await page.route(ROUTES.grantRun, async (route) => {
      await reviewGate.held;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(findingsResult),
      });
    });
    await openWorkspaceView(page, "grant");

    const responsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        ROUTES.grantRun.test(response.url()),
    );

    await page.getByRole("button", { name: "Red-team draft" }).click();
    const loadingButton = page.getByRole("button", { name: "Red-teaming..." });
    await expect(loadingButton).toBeDisabled();
    await snapshotState(page, testInfo, "grant-red-team-running");

    reviewGate.open();
    const response = await responsePromise;
    expect(response.status()).toBe(200);
    await expect(page.getByText("Biosketch missing")).toBeVisible();
    await expect(page.getByText(/export blocked by/i)).toContainText(
      "budget sign-off",
    );

    await snapshotState(page, testInfo, "grant-red-team-findings");
    await assertNoAxeViolations(page);
  });

  test("[pw.grant-review] red-team can resolve all findings [pw.grant.review.red-team:resolved]", async ({
    page,
  }, testInfo) => {
    const resolvedResult = deepClone(GRANT_RUN_FIXTURE);
    resolvedResult.readiness = 100;
    resolvedResult.fact_gaps = [];
    resolvedResult.blockers = [];

    await seedWorkspace(page, { connectors: deepClone(CORE_FUNDING_CONNECTORS) });
    await seedGrantRunResponses(page, resolvedResult);
    await openWorkspaceView(page, "grant");

    const { response } = await submitGrantRunAndCapturePayload(page, async () => {
      await page.getByRole("button", { name: "Red-team draft" }).click();
    });
    expect(response.status()).toBe(200);
    await expect(page.getByText("100% ready · red-team pass")).toBeVisible();
    await expect(page.locator(".fact-gap")).toHaveCount(0);
    await expect(page.locator(".blocker-callout")).toHaveCount(0);

    await snapshotState(page, testInfo, "grant-red-team-resolved");
    await assertNoAxeViolations(page);
  });

  test("[pw.grant-review] red-team errors surface an alert and restore the action [pw.grant.review.red-team:error]", async ({
    page,
    releaseDiagnostics,
  }, testInfo) => {
    releaseDiagnostics.expectConsoleError(
      /Failed to load resource: the server responded with a status of 500/i,
    );
    await seedWorkspace(page, { connectors: deepClone(CORE_FUNDING_CONNECTORS) });
    await page.route(ROUTES.grantRun, async (route) => {
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({
          detail: "Red-team review failed before findings could be generated.",
        }),
      });
    });
    await openWorkspaceView(page, "grant");

    const { response } = await submitGrantRunAndCapturePayload(page, async () => {
      await page.getByRole("button", { name: "Red-team draft" }).click();
    });
    expect(response.status()).toBe(500);
    await expect(page.locator(".error-banner")).toContainText(
      "Red-team review failed before findings could be generated.",
    );
    await expect(
      page.getByRole("button", { name: "Red-team draft" }),
    ).toBeEnabled();

    await snapshotState(page, testInfo, "grant-red-team-error");
    await assertNoAxeViolations(page);
  });
});
