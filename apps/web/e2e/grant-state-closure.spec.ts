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

function escapeForRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function backendRoutePattern(pathname: string): RegExp {
  return new RegExp(`^${escapeForRegExp(WEB_ORIGIN)}${pathname}$`);
}

const API_PATHS = {
  workspace: "/api/backend/api/workspace",
  library: "/api/backend/api/library",
  runs: "/api/backend/api/runs",
  approvals: "/api/backend/api/approvals",
  connectors: "/api/backend/api/connectors",
  settings: "/api/backend/api/settings",
  agents: "/api/backend/api/agents",
  workflows: "/api/backend/api/workflows",
  grantRun: "/api/backend/api/studios/grant/run",
} as const;

type ApiRouteKey = keyof typeof API_PATHS;

const ROUTES: Record<ApiRouteKey, RegExp> = Object.fromEntries(
  (Object.entries(API_PATHS) as Array<[ApiRouteKey, string]>).map(
    ([key, pathname]) => [key, backendRoutePattern(pathname)],
  ),
) as Record<ApiRouteKey, RegExp>;

async function openWorkspaceView(page: Page, view: string): Promise<void> {
  await page.goto(`/?view=${view}`);
  await expect(page.locator(".workbench-shell")).toHaveAttribute(
    "data-workspace-ready",
    "true",
  );
}

/** Captures a full-page screenshot attachment and, for states that are a
 * final resting point of a test (rather than a fleeting in-flight frame),
 * also asserts the page has no detectable accessibility violations. */
async function captureAndAudit(
  page: Page,
  testInfo: TestInfo,
  id: string,
): Promise<void> {
  await recordScreenshot(page, testInfo, id);
  const { violations } = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(violations).toEqual([]);
}

/** Captures a screenshot attachment only -- used for transient loading/edit
 * frames where an axe scan of a moment-in-time state isn't the goal. */
async function recordScreenshot(
  page: Page,
  testInfo: TestInfo,
  id: string,
): Promise<void> {
  const path = testInfo.outputPath(`${id}-${testInfo.project.name}.png`);
  await page.screenshot({ path, fullPage: true });
  await testInfo.attach(id, { path, contentType: "image/png" });
}

function cloneFixture<T>(value: T): T {
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

type FundingConnectorSpec = Pick<
  FundingConnectorFixture,
  "id" | "name" | "description" | "terms_url" | "capabilities"
> &
  Partial<FundingConnectorFixture>;

function fundingConnector(spec: FundingConnectorSpec): FundingConnectorFixture {
  return {
    category: "Funding",
    auth_kind: "None",
    secret_status: "Not required",
    enabled: true,
    test_status: "ready",
    last_tested_at: null,
    assigned_agents: ["grant"],
    data_boundary: "Public metadata only.",
    ...spec,
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

interface GrantRequirement {
  id: string;
  text: string;
  category: string;
  status: string;
  evidence_ids: string[];
}

interface GrantFactGap {
  id: string;
  label: string;
  guidance: string;
  status: string;
}

interface GrantSection {
  id: string;
  title: string;
  status: string;
  word_count: number;
  body: string;
  evidence_ids: string[];
}

interface GrantCitation {
  id: string;
  title: string;
  section: string;
  quote: string;
  source_id: string;
  checksum: string;
  license: string;
  chunk_id: string;
  page_start: number;
}

interface GrantRunResult {
  run: {
    capability: string;
    current_stage: string;
    durable_instance_id: string;
    id: string;
    owner: string;
    progress: number;
    started_at: string;
    status: string;
    title: string;
  };
  opportunity: {
    canonical_url: string;
    deadline: string;
    identifier: string;
    sponsor: string;
    status: string;
    title: string;
  };
  requirements: GrantRequirement[];
  fact_gaps: GrantFactGap[];
  specific_aims: string[];
  sections: GrantSection[];
  readiness: number;
  blockers: string[];
  citations: GrantCitation[];
  insight: {
    agent_name: string;
    content: string;
    evidence_state: string;
    online_research_used: boolean;
    referenced_source_ids: string[];
    unresolved_source_ids: string[];
  };
}

const DEFAULT_REQUIREMENTS: GrantRequirement[] = [
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
];

const DEFAULT_SECTIONS: GrantSection[] = [
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
];

const DEFAULT_CITATIONS: GrantCitation[] = [
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
];

/** Builds a complete grant run result from defaults, applying only the
 * fields a given test actually needs to vary. Replaces the previous
 * deep-clone-then-mutate pattern with an explicit, typed overrides
 * contract so each test's fixture intent is visible at the call site. */
function buildGrantResult(
  overrides: {
    run?: Partial<GrantRunResult["run"]>;
    opportunity?: Partial<GrantRunResult["opportunity"]>;
    requirements?: GrantRequirement[];
    fact_gaps?: GrantFactGap[];
    readiness?: number;
    blockers?: string[];
    insight?: Partial<GrantRunResult["insight"]>;
  } = {},
): GrantRunResult {
  return {
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
      ...overrides.run,
    },
    opportunity: {
      canonical_url: "https://www.grants.gov/",
      deadline: "2026-10-15",
      identifier: "SORI-2026-01",
      sponsor: "Example Federal Research Office",
      status: "Open",
      title: "Open Research Infrastructure Opportunity",
      ...overrides.opportunity,
    },
    requirements: overrides.requirements ?? DEFAULT_REQUIREMENTS,
    fact_gaps: overrides.fact_gaps ?? [],
    specific_aims: ["Aim one."],
    sections: DEFAULT_SECTIONS,
    readiness: overrides.readiness ?? 80,
    blockers: overrides.blockers ?? [],
    citations: DEFAULT_CITATIONS,
    insight: {
      agent_name: "Grant drafting",
      content: "Draft package reviewed.",
      evidence_state: "verified",
      online_research_used: false,
      referenced_source_ids: ["notice-1"],
      unresolved_source_ids: [],
      ...overrides.insight,
    },
  };
}

interface WorkspaceSeed {
  connectors?: FundingConnectorFixture[];
  workflows?: object[];
  runs?: object[];
  approvals?: object[];
  library?: object[];
  agents?: object[];
}

async function seedGrantWorkspace(
  page: Page,
  seed: WorkspaceSeed = {},
): Promise<void> {
  const connectors = seed.connectors ?? cloneFixture(CORE_FUNDING_CONNECTORS);
  const library = seed.library ?? [];
  const approvals = seed.approvals ?? [];

  const routeBodies: Array<[RegExp, unknown]> = [
    [
      ROUTES.workspace,
      {
        project: WORKSPACE_PROJECT,
        persistence: "Fixture ready",
        library_items: library.length,
        active_runs: 0,
        pending_approvals: approvals.length,
        connector_ready: connectors.length,
        connector_total: connectors.length,
        last_activity_at: "2026-07-23T12:00:00Z",
      },
    ],
    [ROUTES.library, library],
    [ROUTES.runs, seed.runs ?? []],
    [ROUTES.approvals, approvals],
    [ROUTES.connectors, connectors],
    [ROUTES.settings, WORKSPACE_PROJECT],
    [ROUTES.agents, seed.agents ?? []],
    [ROUTES.workflows, seed.workflows ?? []],
  ];

  for (const [pattern, body] of routeBodies) {
    await page.route(pattern, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(body),
      });
    });
  }
}

/** Stubs the grant-run POST endpoint with one or more results, repeating
 * the final result for any request beyond the supplied list -- a variadic
 * replacement for the previous single-or-array argument shape. */
async function queueGrantRunResponses(
  page: Page,
  ...results: object[]
): Promise<void> {
  const queue = [...results];
  await page.route(ROUTES.grantRun, async (route) => {
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

interface GrantRunPayload {
  objective: string;
  online_research: boolean;
  inputs: Record<string, unknown>;
}

function watchGrantRunPost(page: Page) {
  return {
    request: page.waitForRequest(
      (request) =>
        request.method() === "POST" && ROUTES.grantRun.test(request.url()),
    ),
    response: page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        ROUTES.grantRun.test(response.url()),
    ),
  };
}

async function settleGrantRunPost(
  pending: ReturnType<typeof watchGrantRunPost>,
) {
  const [request, response] = await Promise.all([
    pending.request,
    pending.response,
  ]);
  return {
    request,
    response,
    payload: request.postDataJSON() as GrantRunPayload,
  };
}

async function submitGrantRun(page: Page, trigger: () => Promise<void>) {
  const pending = watchGrantRunPost(page);
  await trigger();
  return settleGrantRunPost(pending);
}

test.describe("Grant Studio: draft package build", () => {
  test("[pw.grant-fit] [pw.grant-build] verified facts remain checked and explicit when the unrelated package build request returns an error [pw.grant.package.build:error]", async ({
    page,
    releaseDiagnostics,
  }, testInfo) => {
    releaseDiagnostics.expectConsoleError(
      /Failed to load resource: the server responded with a status of 500/i,
    );
    await seedGrantWorkspace(page, {
      connectors: cloneFixture(CORE_FUNDING_CONNECTORS),
    });
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

    await test.step("confirm verified project facts before submitting", async () => {
      const verifiedFactsCheckbox = page.getByRole("checkbox", {
        name: /core project facts verified/i,
      });
      await verifiedFactsCheckbox.check();
      await expect(verifiedFactsCheckbox).toBeChecked();
    });

    const { payload, response } = await submitGrantRun(page, async () => {
      await page
        .getByRole("button", { name: "Parse notice & build package" })
        .click();
    });
    expect(response.status()).toBe(500);
    expect(payload.inputs.project_facts).toEqual([
      "Research office sponsor confirmed",
      "PI role confirmed",
    ]);

    await test.step("verified facts and the build action recover from the error", async () => {
      await expect(page.locator(".error-banner")).toContainText(
        "Verified project facts could not be incorporated into the grant build.",
      );
      await expect(
        page.getByRole("checkbox", { name: /core project facts verified/i }),
      ).toBeChecked();
      await expect(
        page.getByRole("button", { name: "Parse notice & build package" }),
      ).toBeEnabled();
    });

    await captureAndAudit(page, testInfo, "grant-build-error-facts-confirmed");
  });

  test("[pw.grant-draft] [pw.grant-build] project framing is ready for keyboard editing and submits via keyboard while the package run is loading [pw.grant.editor.framing:ready][pw.grant.editor.framing:keyboard][pw.grant.editor.framing:success][pw.grant.package.build:keyboard][pw.grant.package.build:loading]", async ({
    page,
  }, testInfo) => {
    const runGate = deferredGate();
    await seedGrantWorkspace(page, {
      connectors: cloneFixture(CORE_FUNDING_CONNECTORS),
    });
    await page.route(ROUTES.grantRun, async (route) => {
      await runGate.held;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(buildGrantResult()),
      });
    });
    await openWorkspaceView(page, "grant");

    const editedFraming =
      "Develop a citation-backed infrastructure package for an auditable pilot program.";
    await test.step("edit project framing via keyboard", async () => {
      const framingField = page.getByLabel("Project framing");
      await expect(framingField).toHaveValue(
        "Develop a competitive application for an open research infrastructure program.",
      );
      await framingField.focus();
      await page.keyboard.press("Control+A");
      await page.keyboard.type(editedFraming);
      await expect(framingField).toHaveValue(editedFraming);
    });
    await recordScreenshot(page, testInfo, "grant-framing-keyboard");

    const pendingRun = watchGrantRunPost(page);
    await test.step("submit the build via keyboard and observe the loading state", async () => {
      const buildButton = page.getByRole("button", {
        name: "Parse notice & build package",
      });
      await buildButton.focus();
      await page.keyboard.press("Enter");

      const loadingButton = page.getByRole("button", {
        name: "Running workflow...",
      });
      await expect(loadingButton).toBeDisabled();
    });
    await recordScreenshot(page, testInfo, "grant-package-build-loading");

    runGate.open();
    const { request, response } = await settleGrantRunPost(pendingRun);
    expect(response.status()).toBe(200);
    const payload = request.postDataJSON() as Pick<
      GrantRunPayload,
      "objective" | "inputs"
    >;
    expect(payload.objective).toBe(editedFraming);
    await expect(page.locator(".requirement-matrix .subtle-chip")).toHaveText(
      "2 mapped",
    );

    await captureAndAudit(page, testInfo, "grant-framing-success");
  });
});

test.describe("Grant Studio: red-team review", () => {
  test("[pw.grant-review] red-team errors surface an alert and restore the action [pw.grant.review.red-team:error]", async ({
    page,
    releaseDiagnostics,
  }, testInfo) => {
    releaseDiagnostics.expectConsoleError(
      /Failed to load resource: the server responded with a status of 500/i,
    );
    await seedGrantWorkspace(page, {
      connectors: cloneFixture(CORE_FUNDING_CONNECTORS),
    });
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

    const { response } = await submitGrantRun(page, async () => {
      await page.getByRole("button", { name: "Red-team draft" }).click();
    });
    expect(response.status()).toBe(500);
    await expect(page.locator(".error-banner")).toContainText(
      "Red-team review failed before findings could be generated.",
    );
    await expect(
      page.getByRole("button", { name: "Red-team draft" }),
    ).toBeEnabled();

    await captureAndAudit(page, testInfo, "grant-red-team-error");
  });

  test("[pw.grant-review] red-team can resolve all findings [pw.grant.review.red-team:resolved]", async ({
    page,
  }, testInfo) => {
    await seedGrantWorkspace(page, {
      connectors: cloneFixture(CORE_FUNDING_CONNECTORS),
    });
    await queueGrantRunResponses(
      page,
      buildGrantResult({ readiness: 100, fact_gaps: [], blockers: [] }),
    );
    await openWorkspaceView(page, "grant");

    const { response } = await submitGrantRun(page, async () => {
      await page.getByRole("button", { name: "Red-team draft" }).click();
    });
    expect(response.status()).toBe(200);
    await expect(page.getByText("100% ready · red-team pass")).toBeVisible();
    await expect(page.locator(".fact-gap")).toHaveCount(0);
    await expect(page.locator(".blocker-callout")).toHaveCount(0);

    await captureAndAudit(page, testInfo, "grant-red-team-resolved");
  });

  test("[pw.grant-review] red-team runs show the running state and surface findings [pw.grant.review.red-team:running][pw.grant.review.red-team:findings]", async ({
    page,
  }, testInfo) => {
    const findingsResult = buildGrantResult({
      fact_gaps: [
        {
          id: "gap-1",
          label: "Biosketch missing",
          guidance: "Upload the verified PI biosketch before export.",
          status: "missing",
        },
      ],
      blockers: ["budget sign-off"],
    });

    const reviewGate = deferredGate();
    await seedGrantWorkspace(page, {
      connectors: cloneFixture(CORE_FUNDING_CONNECTORS),
    });
    await page.route(ROUTES.grantRun, async (route) => {
      await reviewGate.held;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(findingsResult),
      });
    });
    await openWorkspaceView(page, "grant");

    const pendingResponse = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        ROUTES.grantRun.test(response.url()),
    );

    await test.step("start the red-team run and observe the running state", async () => {
      await page.getByRole("button", { name: "Red-team draft" }).click();
      await expect(
        page.getByRole("button", { name: "Red-teaming..." }),
      ).toBeDisabled();
    });
    await recordScreenshot(page, testInfo, "grant-red-team-running");

    reviewGate.open();
    const response = await pendingResponse;
    expect(response.status()).toBe(200);
    await expect(page.getByText("Biosketch missing")).toBeVisible();
    await expect(page.getByText(/export blocked by/i)).toContainText(
      "budget sign-off",
    );

    await captureAndAudit(page, testInfo, "grant-red-team-findings");
  });
});

test.describe("Grant Studio: requirement detail states", () => {
  const REQUIREMENT_DETAIL_CASES: Array<{
    requirement: GrantRequirement;
    buttonName: RegExp;
    dialogName: string;
    statusText: string;
    screenshotId: string;
  }> = [
    {
      requirement: {
        id: "unmapped-requirement",
        text: "Facilities and other resources",
        category: "Narrative",
        status: "unmapped",
        evidence_ids: [],
      },
      buttonName: /facilities and other resources/i,
      dialogName: "Facilities and other resources",
      statusText: "Status: unmapped",
      screenshotId: "grant-requirements-unmapped",
    },
    {
      requirement: {
        id: "blocked-requirement",
        text: "Institutional sign-off letter",
        category: "Approvals",
        status: "blocked",
        evidence_ids: [],
      },
      buttonName: /institutional sign-off letter/i,
      dialogName: "Institutional sign-off letter",
      statusText: "Status: blocked",
      screenshotId: "grant-requirements-blocked",
    },
    {
      requirement: {
        id: "needs-input-requirement",
        text: "Budget justification",
        category: "Budget",
        status: "needs_input",
        evidence_ids: [],
      },
      buttonName: /budget justification/i,
      dialogName: "Budget justification",
      statusText: "Status: needs input",
      screenshotId: "grant-requirements-needs-input",
    },
  ];

  test("[pw.grant-requirements] parsed requirement rows surface blocked, needs-input, and unmapped states [pw.grant.requirements.open:blocked][pw.grant.requirements.open:needs-input][pw.grant.requirements.open:unmapped]", async ({
    page,
  }, testInfo) => {
    const requirementsResult = buildGrantResult({
      requirements: REQUIREMENT_DETAIL_CASES.map((scenario) => scenario.requirement),
      blockers: ["budget sign-off"],
    });

    await seedGrantWorkspace(page, {
      connectors: cloneFixture(CORE_FUNDING_CONNECTORS),
    });
    await queueGrantRunResponses(page, requirementsResult);
    await openWorkspaceView(page, "grant");

    const { response } = await submitGrantRun(page, async () => {
      await page
        .getByRole("button", { name: "Parse notice & build package" })
        .click();
    });
    expect(response.status()).toBe(200);
    await expect(page.getByText(/export blocked by/i)).toContainText(
      "budget sign-off",
    );

    for (const scenario of REQUIREMENT_DETAIL_CASES) {
      await test.step(`requirement detail dialog: ${scenario.requirement.status}`, async () => {
        await page.getByRole("button", { name: scenario.buttonName }).click();
        const dialog = page.getByRole("dialog", { name: scenario.dialogName });
        await expect(dialog).toContainText(scenario.statusText);
        await expect(dialog).toContainText(
          "No source evidence is linked to this requirement yet.",
        );
        await captureAndAudit(page, testInfo, scenario.screenshotId);
        await dialog.getByLabel("Close requirement detail").click();
      });
    }
  });
});

test.describe("Grant Studio: funding source discovery", () => {
  const AVAILABILITY_CASES: Array<{
    label: string;
    accessibleName: string | RegExp;
    enabled: boolean;
    checked: boolean;
    caption: string | null;
  }> = [
    {
      label: "Grants.gov (ready)",
      accessibleName: "Grants.gov",
      enabled: true,
      checked: true,
      caption: null,
    },
    {
      label: "Foundation Directory (needs connection)",
      accessibleName: /^Foundation Directory/,
      enabled: false,
      checked: false,
      caption: "Needs connection setup",
    },
    {
      label: "Crossref (unavailable)",
      accessibleName: /^Crossref/,
      enabled: false,
      checked: false,
      caption: "Currently unavailable",
    },
    {
      label: "NSF Award Search (disabled in settings)",
      accessibleName: /^NSF Award Search/,
      enabled: false,
      checked: false,
      caption: "Disabled in Settings",
    },
  ];

  test("[pw.grant.discovery.sources:needs-connection][pw.grant.discovery.sources:unavailable][pw.grant.discovery.sources:disabled] disables non-ready funding connectors with distinct captions and excludes them from the run payload", async ({
    page,
  }, testInfo) => {
    const connectors = [
      ...cloneFixture(CORE_FUNDING_CONNECTORS),
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
    await seedGrantWorkspace(page, { connectors });
    await queueGrantRunResponses(page, buildGrantResult());
    await openWorkspaceView(page, "grant");

    for (const scenario of AVAILABILITY_CASES) {
      await test.step(`connector availability: ${scenario.label}`, async () => {
        const checkbox = page.getByRole("checkbox", {
          name: scenario.accessibleName,
        });
        if (scenario.enabled) {
          await expect(checkbox).toBeEnabled();
        } else {
          await expect(checkbox).toBeDisabled();
        }
        if (scenario.checked) {
          await expect(checkbox).toBeChecked();
        } else {
          await expect(checkbox).not.toBeChecked();
        }
        if (scenario.caption) {
          await expect(page.getByText(scenario.caption)).toBeVisible();
        }
      });
    }

    await captureAndAudit(page, testInfo, "grant-connectors-availability");

    const { payload } = await submitGrantRun(page, async () => {
      await page
        .getByRole("button", { name: "Parse notice & build package" })
        .click();
    });
    expect(payload.inputs.sources).toContain("grants_gov");
    expect(payload.inputs.sources).not.toContain("foundation_dir");
    expect(payload.inputs.sources).not.toContain("crossref");
    expect(payload.inputs.sources).not.toContain("nsf_award");
  });

  test("[pw.grant-discovery] [pw.grant-connectors] unassigned funding connectors surface the empty source list and disable opportunity discovery [pw.grant.discovery.search:disabled][pw.grant.discovery.sources:empty]", async ({
    page,
  }, testInfo) => {
    await seedGrantWorkspace(page, { connectors: [] });
    await openWorkspaceView(page, "grant");

    await test.step("no connectors assigned disables discovery", async () => {
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
    });

    await captureAndAudit(
      page,
      testInfo,
      "grant-connectors-empty-discovery-disabled",
    );
  });

  test("[pw.grant-discovery] [pw.grant-connectors] [pw.grant-opportunity] keyboard search, source selection, and opportunity id edits flow into the draft payload [pw.grant.discovery.search:keyboard][pw.grant.discovery.sources:selected][pw.grant.opportunity.id:keyboard]", async ({
    page,
  }, testInfo) => {
    await seedGrantWorkspace(page, {
      connectors: cloneFixture(CORE_FUNDING_CONNECTORS),
    });
    await queueGrantRunResponses(page, buildGrantResult());
    await openWorkspaceView(page, "grant");

    await test.step("search narrows the discovery list via keyboard", async () => {
      const discoveryPanel = page.getByLabel("Opportunity discovery");
      const searchField = page.getByLabel("Search funding opportunities");
      await searchField.focus();
      await page.keyboard.type("nih");
      await expect(searchField).toHaveValue("nih");
      await expect(discoveryPanel.getByText("NIH Reporter")).toBeVisible();
      await expect(discoveryPanel.getByText("Grants.gov")).toHaveCount(0);
    });

    await test.step("deselect a funding source", async () => {
      const nihReporterSource = page.getByRole("checkbox", {
        name: "NIH Reporter",
      });
      await expect(nihReporterSource).toBeChecked();
      await nihReporterSource.uncheck();
      await expect(nihReporterSource).not.toBeChecked();
    });

    await test.step("edit the opportunity id via keyboard", async () => {
      const opportunityIdField = page.getByLabel("Opportunity ID");
      await opportunityIdField.focus();
      await page.keyboard.press("Control+A");
      await page.keyboard.type("RFA-TRANS-77");
      await expect(opportunityIdField).toHaveValue("RFA-TRANS-77");
    });

    await captureAndAudit(page, testInfo, "grant-discovery-keyboard-selected-sources");

    const { payload, response } = await submitGrantRun(page, async () => {
      await page
        .getByRole("button", { name: "Parse notice & build package" })
        .click();
    });
    expect(response.status()).toBe(200);
    expect(payload.inputs.opportunity_id).toBe("RFA-TRANS-77");
    expect(payload.inputs.sources).toEqual(["grants_gov"]);
  });
});
