import AxeBuilder from "@axe-core/playwright";
import type { Page, TestInfo } from "@playwright/test";

import { expect, test } from "./fixtures";

const BASE_URL = process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:3000";

function escapeRegex(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

const WORKSPACE_URL = new RegExp(
  `^${escapeRegex(BASE_URL)}/api/backend/api/workspace$`,
);
const LIBRARY_URL = new RegExp(
  `^${escapeRegex(BASE_URL)}/api/backend/api/library$`,
);
const RUNS_URL = new RegExp(`^${escapeRegex(BASE_URL)}/api/backend/api/runs$`);
const APPROVALS_URL = new RegExp(
  `^${escapeRegex(BASE_URL)}/api/backend/api/approvals$`,
);
const CONNECTORS_URL = new RegExp(
  `^${escapeRegex(BASE_URL)}/api/backend/api/connectors$`,
);
const SETTINGS_URL = new RegExp(
  `^${escapeRegex(BASE_URL)}/api/backend/api/settings$`,
);
const AGENTS_URL = new RegExp(
  `^${escapeRegex(BASE_URL)}/api/backend/api/agents$`,
);
const WORKFLOWS_URL = new RegExp(
  `^${escapeRegex(BASE_URL)}/api/backend/api/workflows$`,
);
const GRANT_RUN_URL = new RegExp(
  `^${escapeRegex(BASE_URL)}/api/backend/api/studios/grant/run$`,
);

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

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function createDeferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((resolver) => {
    resolve = resolver;
  });
  return { promise, resolve };
}

const PROJECT_SETTINGS = {
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

const BASE_CONNECTORS = [
  {
    id: "grants_gov",
    name: "Grants.gov",
    category: "Funding",
    description: "Authoritative U.S. federal opportunity records.",
    auth_kind: "None",
    secret_status: "Not required",
    enabled: true,
    test_status: "ready",
    last_tested_at: null,
    assigned_agents: ["grant"],
    terms_url: "https://www.grants.gov/web/grants/legal-privacy.html",
    data_boundary: "Public metadata only.",
    capabilities: ["Opportunities"],
  },
  {
    id: "nih_reporter",
    name: "NIH Reporter",
    category: "Funding",
    description: "Federal biomedical funding and award records.",
    auth_kind: "None",
    secret_status: "Not required",
    enabled: true,
    test_status: "ready",
    last_tested_at: null,
    assigned_agents: ["grant"],
    terms_url: "https://reporter.nih.gov/",
    data_boundary: "Public metadata only.",
    capabilities: ["Opportunities", "Awards"],
  },
];

const BASE_GRANT_RESULT = {
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

async function mockJsonRoute(page: Page, pattern: RegExp, body: unknown) {
  await page.route(pattern, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });
}

async function mockWorkspace(
  page: Page,
  options: {
    connectors?: object[];
    workflows?: object[];
    runs?: object[];
    approvals?: object[];
    library?: object[];
    agents?: object[];
  } = {},
) {
  const connectors = options.connectors ?? clone(BASE_CONNECTORS);
  const workflows = options.workflows ?? [];
  const runs = options.runs ?? [];
  const approvals = options.approvals ?? [];
  const library = options.library ?? [];
  const agents = options.agents ?? [];

  await mockJsonRoute(page, WORKSPACE_URL, {
    active_runs: 0,
    connector_ready: connectors.length,
    connector_total: connectors.length,
    last_activity_at: "2026-07-23T12:00:00Z",
    library_items: library.length,
    pending_approvals: approvals.length,
    persistence: "Fixture ready",
    project: PROJECT_SETTINGS,
  });
  await mockJsonRoute(page, LIBRARY_URL, library);
  await mockJsonRoute(page, RUNS_URL, runs);
  await mockJsonRoute(page, APPROVALS_URL, approvals);
  await mockJsonRoute(page, CONNECTORS_URL, connectors);
  await mockJsonRoute(page, SETTINGS_URL, PROJECT_SETTINGS);
  await mockJsonRoute(page, AGENTS_URL, agents);
  await mockJsonRoute(page, WORKFLOWS_URL, workflows);
}

async function mockGrantRun(page: Page, results: object | object[]) {
  const queue = Array.isArray(results) ? [...results] : [results];
  await page.route(GRANT_RUN_URL, async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue();
      return;
    }

    const next = queue.length > 1 ? queue.shift() : queue[0];
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(next),
    });
  });
}

async function runGrantAndCapturePayload(
  page: Page,
  trigger: () => Promise<void>,
) {
  const requestPromise = page.waitForRequest(
    (request) => request.method() === "POST" && GRANT_RUN_URL.test(request.url()),
  );
  const responsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" && GRANT_RUN_URL.test(response.url()),
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

test.describe("Grant state closure", () => {
  test("[pw.grant-discovery] [pw.grant-connectors] unassigned funding connectors surface the empty source list and disable opportunity discovery [pw.grant.discovery.search:disabled][pw.grant.discovery.sources:empty]", async ({
    page,
  }, testInfo) => {
    await mockWorkspace(page, { connectors: [] });
    await gotoView(page, "grant");

    const fundingPanel = page.getByLabel("Funding source discovery");
    await expect(
      fundingPanel.getByText(/no funding connectors are assigned yet/i),
    ).toBeVisible();

    const search = page.getByLabel("Search funding opportunities");
    await expect(search).toBeDisabled();
    await expect(
      page.getByText(/select at least one funding connector above to discover opportunities/i),
    ).toBeVisible();

    await capture(page, testInfo, "grant-connectors-empty-discovery-disabled");
    await expectAccessible(page);
  });

  test("[pw.grant-discovery] [pw.grant-connectors] [pw.grant-opportunity] keyboard search, source selection, and opportunity id edits flow into the draft payload [pw.grant.discovery.search:keyboard][pw.grant.discovery.sources:selected][pw.grant.opportunity.id:keyboard]", async ({
    page,
  }, testInfo) => {
    await mockWorkspace(page, { connectors: clone(BASE_CONNECTORS) });
    await mockGrantRun(page, clone(BASE_GRANT_RESULT));
    await gotoView(page, "grant");

    const discoveryPanel = page.getByLabel("Opportunity discovery");
    const search = page.getByLabel("Search funding opportunities");
    await search.focus();
    await page.keyboard.type("nih");
    await expect(search).toHaveValue("nih");
    await expect(discoveryPanel.getByText("NIH Reporter")).toBeVisible();
    await expect(discoveryPanel.getByText("Grants.gov")).toHaveCount(0);

    const nihReporter = page.getByRole("checkbox", { name: "NIH Reporter" });
    await expect(nihReporter).toBeChecked();
    await nihReporter.uncheck();
    await expect(nihReporter).not.toBeChecked();

    const opportunityId = page.getByLabel("Opportunity ID");
    await opportunityId.focus();
    await page.keyboard.press("Control+A");
    await page.keyboard.type("RFA-TRANS-77");
    await expect(opportunityId).toHaveValue("RFA-TRANS-77");

    await capture(page, testInfo, "grant-discovery-keyboard-selected-sources");
    await expectAccessible(page);

    const { payload, response } = await runGrantAndCapturePayload(page, async () => {
      await page.getByRole("button", { name: "Parse notice & build package" }).click();
    });
    expect(response.status()).toBe(200);
    expect(payload.inputs.opportunity_id).toBe("RFA-TRANS-77");
    expect(payload.inputs.funding_sources).toEqual(["grants_gov"]);
  });

  test("[pw.grant-draft] [pw.grant-build] project framing is ready for keyboard editing and submits via keyboard while the package run is loading [pw.grant.editor.framing:ready][pw.grant.editor.framing:keyboard][pw.grant.editor.framing:success][pw.grant.package.build:keyboard][pw.grant.package.build:loading]", async ({
    page,
  }, testInfo) => {
    const gate = createDeferred();
    await mockWorkspace(page, { connectors: clone(BASE_CONNECTORS) });
    await page.route(GRANT_RUN_URL, async (route) => {
      await gate.promise;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(clone(BASE_GRANT_RESULT)),
      });
    });
    await gotoView(page, "grant");

    const framing = page.getByLabel("Project framing");
    await expect(framing).toHaveValue(
      "Develop a competitive application for an open research infrastructure program.",
    );
    await framing.focus();
    await page.keyboard.press("Control+A");
    await page.keyboard.type(
      "Develop a citation-backed infrastructure package for an auditable pilot program.",
    );
    await expect(framing).toHaveValue(
      "Develop a citation-backed infrastructure package for an auditable pilot program.",
    );

    await capture(page, testInfo, "grant-framing-keyboard");

    const requestPromise = page.waitForRequest(
      (request) =>
        request.method() === "POST" && GRANT_RUN_URL.test(request.url()),
    );
    const responsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" && GRANT_RUN_URL.test(response.url()),
    );

    const buildButton = page.getByRole("button", {
      name: "Parse notice & build package",
    });
    await buildButton.focus();
    await page.keyboard.press("Enter");

    const runningButton = page.getByRole("button", { name: "Running workflow..." });
    await expect(runningButton).toBeDisabled();
    await capture(page, testInfo, "grant-package-build-loading");

    gate.resolve();
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
    await expect(page.locator(".requirement-matrix .subtle-chip")).toHaveText("2 mapped");

    await capture(page, testInfo, "grant-framing-success");
    await expectAccessible(page);
  });

  test("[pw.grant-fit] [pw.grant-build] verified facts remain explicit when package build returns an error [pw.grant.facts.confirm:error][pw.grant.package.build:error]", async ({
    page,
    releaseDiagnostics,
  }, testInfo) => {
    releaseDiagnostics.expectConsoleError(
      /Failed to load resource: the server responded with a status of 500/i,
    );
    await mockWorkspace(page, { connectors: clone(BASE_CONNECTORS) });
    await page.route(GRANT_RUN_URL, async (route) => {
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({
          detail: "Verified project facts could not be incorporated into the grant build.",
        }),
      });
    });
    await gotoView(page, "grant");

    const facts = page.getByRole("checkbox", {
      name: /core project facts verified/i,
    });
    await facts.check();
    await expect(facts).toBeChecked();

    const { payload, response } = await runGrantAndCapturePayload(page, async () => {
      await page.getByRole("button", { name: "Parse notice & build package" }).click();
    });
    expect(response.status()).toBe(500);
    expect(payload.inputs.project_facts).toEqual([
      "Research office sponsor confirmed",
      "PI role confirmed",
    ]);

    await expect(page.locator(".error-banner")).toContainText(
      "Verified project facts could not be incorporated into the grant build.",
    );
    await expect(facts).toBeChecked();
    await expect(
      page.getByRole("button", { name: "Parse notice & build package" }),
    ).toBeEnabled();

    await capture(page, testInfo, "grant-build-error-facts-confirmed");
    await expectAccessible(page);
  });

  test("[pw.grant-requirements] parsed requirement rows surface blocked, needs-input, and unmapped states [pw.grant.requirements.open:blocked][pw.grant.requirements.open:needs-input][pw.grant.requirements.open:unmapped]", async ({
    page,
  }, testInfo) => {
    const requirementsResult = clone(BASE_GRANT_RESULT);
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

    await mockWorkspace(page, { connectors: clone(BASE_CONNECTORS) });
    await mockGrantRun(page, requirementsResult);
    await gotoView(page, "grant");

    const { response } = await runGrantAndCapturePayload(page, async () => {
      await page.getByRole("button", { name: "Parse notice & build package" }).click();
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
    await capture(page, testInfo, "grant-requirements-blocked");
    await expectAccessible(page);
    await blockedDialog.getByLabel("Close requirement detail").click();

    await page.getByRole("button", { name: /budget justification/i }).click();
    const needsInputDialog = page.getByRole("dialog", {
      name: "Budget justification",
    });
    await expect(needsInputDialog).toContainText("Status: needs input");
    await expect(needsInputDialog).toContainText(
      "No source evidence is linked to this requirement yet.",
    );
    await capture(page, testInfo, "grant-requirements-needs-input");
    await expectAccessible(page);
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
    await capture(page, testInfo, "grant-requirements-unmapped");
    await expectAccessible(page);
  });

  test("[pw.grant-review] red-team runs show the running state and surface findings [pw.grant.review.red-team:running][pw.grant.review.red-team:findings]", async ({
    page,
  }, testInfo) => {
    const findingsResult = clone(BASE_GRANT_RESULT);
    findingsResult.fact_gaps = [
      {
        id: "gap-1",
        label: "Biosketch missing",
        guidance: "Upload the verified PI biosketch before export.",
        status: "missing",
      },
    ];
    findingsResult.blockers = ["budget sign-off"];

    const gate = createDeferred();
    await mockWorkspace(page, { connectors: clone(BASE_CONNECTORS) });
    await page.route(GRANT_RUN_URL, async (route) => {
      await gate.promise;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(findingsResult),
      });
    });
    await gotoView(page, "grant");

    const responsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" && GRANT_RUN_URL.test(response.url()),
    );

    await page.getByRole("button", { name: "Red-team draft" }).click();
    const runningButton = page.getByRole("button", { name: "Red-teaming..." });
    await expect(runningButton).toBeDisabled();
    await capture(page, testInfo, "grant-red-team-running");

    gate.resolve();
    const response = await responsePromise;
    expect(response.status()).toBe(200);
    await expect(page.getByText("Biosketch missing")).toBeVisible();
    await expect(page.getByText(/export blocked by/i)).toContainText(
      "budget sign-off",
    );

    await capture(page, testInfo, "grant-red-team-findings");
    await expectAccessible(page);
  });

  test("[pw.grant-review] red-team can resolve all findings [pw.grant.review.red-team:resolved]", async ({
    page,
  }, testInfo) => {
    const resolvedResult = clone(BASE_GRANT_RESULT);
    resolvedResult.readiness = 100;
    resolvedResult.fact_gaps = [];
    resolvedResult.blockers = [];

    await mockWorkspace(page, { connectors: clone(BASE_CONNECTORS) });
    await mockGrantRun(page, resolvedResult);
    await gotoView(page, "grant");

    const { response } = await runGrantAndCapturePayload(page, async () => {
      await page.getByRole("button", { name: "Red-team draft" }).click();
    });
    expect(response.status()).toBe(200);
    await expect(page.getByText("100% ready · red-team pass")).toBeVisible();
    await expect(page.locator(".fact-gap")).toHaveCount(0);
    await expect(page.locator(".blocker-callout")).toHaveCount(0);

    await capture(page, testInfo, "grant-red-team-resolved");
    await expectAccessible(page);
  });

  test("[pw.grant-review] red-team errors surface an alert and restore the action [pw.grant.review.red-team:error]", async ({
    page,
    releaseDiagnostics,
  }, testInfo) => {
    releaseDiagnostics.expectConsoleError(
      /Failed to load resource: the server responded with a status of 500/i,
    );
    await mockWorkspace(page, { connectors: clone(BASE_CONNECTORS) });
    await page.route(GRANT_RUN_URL, async (route) => {
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({
          detail: "Red-team review failed before findings could be generated.",
        }),
      });
    });
    await gotoView(page, "grant");

    const { response } = await runGrantAndCapturePayload(page, async () => {
      await page.getByRole("button", { name: "Red-team draft" }).click();
    });
    expect(response.status()).toBe(500);
    await expect(page.locator(".error-banner")).toContainText(
      "Red-team review failed before findings could be generated.",
    );
    await expect(page.getByRole("button", { name: "Red-team draft" })).toBeEnabled();

    await capture(page, testInfo, "grant-red-team-error");
    await expectAccessible(page);
  });

});
