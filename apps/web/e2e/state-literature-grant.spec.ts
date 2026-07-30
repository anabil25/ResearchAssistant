import fs from "node:fs";
import path from "node:path";

import AxeBuilder from "@axe-core/playwright";
import type { Page, Route, TestInfo } from "@playwright/test";
import ts from "typescript";

import type {
  ConnectorSetting,
  GrantStudioResult,
  LiteratureStudioResult,
} from "../src/lib/types";
import { UI_COVERAGE_MANIFEST } from "../src/testing/interaction-manifest";
import { expect, test } from "./fixtures";

test.setTimeout(180_000);

const FIXED_RUN = {
  capability: "literature" as const,
  current_stage: "Complete",
  durable_instance_id: "research-run-state-coverage",
  id: "run-state-coverage",
  owner: "State coverage reviewer",
  progress: 100,
  started_at: "2026-07-23T12:00:00Z",
  status: "completed" as const,
  title: "State coverage run",
};

const LITERATURE_WARNING_RESULT: LiteratureStudioResult = {
  run: FIXED_RUN,
  protocol: {
    research_question: "Which methods preserve auditable evidence?",
    date_from: 2019,
    date_to: 2026,
    sources: ["PubMed"],
    inclusion_criteria: ["Primary study"],
    exclusion_criteria: ["Duplicate"],
  },
  search_queries: ["auditable evidence"],
  candidate_count: 2,
  screening: [
    {
      source_id: "source-ready",
      title: "Verified retrieval study",
      decision: "include",
      reason: "Matches the locked protocol.",
      duplicate_group: null,
    },
    {
      source_id: "source-unresolved",
      title: "Unresolved retrieval study",
      decision: "include",
      reason: "Requires source resolution.",
      duplicate_group: null,
    },
  ],
  extraction_matrix: [
    {
      source_id: "source-ready",
      method: "Controlled benchmark",
      population: "Research workflows",
      outcome: "Traceable claims",
      limitation: "Single institution",
      citation_ids: ["citation-ready"],
    },
    {
      source_id: "source-unresolved",
      method: "Observational review",
      population: "Evidence systems",
      outcome: "Unresolved source",
      limitation: "Source unavailable",
      citation_ids: ["citation-unresolved"],
    },
  ],
  synthesis: [],
  citations: [
    {
      id: "citation-ready",
      title: "Verified retrieval study",
      section: "Results",
      quote: "Every claim retained a source pointer.",
      source_id: "source-ready",
      checksum: "sha256:ready",
      license: "CC BY",
      chunk_id: "chunk-ready",
      page_start: 2,
    },
    {
      id: "citation-unresolved",
      title: "Unresolved retrieval study",
      section: "Methods",
      quote: "The source record could not be resolved.",
      source_id: "source-unresolved",
      checksum: "sha256:unresolved",
      license: "CC BY",
      chunk_id: "chunk-unresolved",
      page_start: 4,
    },
  ],
  insight: {
    agent_name: "Literature synthesis",
    content: "One claim remains unresolved.",
    evidence_state: "verified",
    online_research_used: false,
    referenced_source_ids: ["source-ready"],
    unresolved_source_ids: ["source-unresolved"],
  },
};

const GRANT_RESULT: GrantStudioResult = {
  run: { ...FIXED_RUN, capability: "grant" },
  opportunity: {
    canonical_url: "https://grants.example.test/notices/SORI-2026-01",
    deadline: "2026-10-15",
    identifier: "SORI-2026-01",
    sponsor: "Example Federal Research Office",
    status: "Open",
    title: "Open Research Infrastructure Opportunity",
  },
  requirements: [
    {
      id: "mapped",
      text: "Mapped project summary",
      category: "Narrative",
      status: "mapped",
      evidence_ids: ["grant-citation"],
    },
    {
      id: "unmapped",
      text: "Unmapped facilities plan",
      category: "Attachment",
      status: "unmapped",
      evidence_ids: [],
    },
    {
      id: "needs-input",
      text: "Budget needs input",
      category: "Budget",
      status: "needs_input",
      evidence_ids: [],
    },
    {
      id: "blocked",
      text: "Blocked export approval",
      category: "Approval",
      status: "blocked",
      evidence_ids: [],
    },
  ],
  fact_gaps: [],
  specific_aims: ["Preserve source-linked infrastructure evidence."],
  sections: [
    {
      id: "significance",
      title: "Significance",
      status: "draft",
      word_count: 5,
      body: "A citation-backed significance section.",
      evidence_ids: ["grant-citation"],
    },
  ],
  readiness: 92,
  blockers: [],
  citations: [
    {
      id: "grant-citation",
      title: "Infrastructure opportunity notice",
      section: "Eligibility",
      quote: "Applicants must submit a project summary.",
      source_id: "notice-source",
      checksum: "sha256:notice",
      license: "Public domain",
      chunk_id: "notice-chunk",
      page_start: 1,
    },
  ],
};

const CONNECTORS: ConnectorSetting[] = [
  connector("pubmed", "PubMed", ["literature"]),
  connector("europe_pmc", "Europe PMC", ["literature"]),
  connector("crossref", "Crossref", ["literature"], { enabled: false }),
  connector("openalex", "OpenAlex", ["literature"], { test_status: "failed" }),
  connector("arxiv", "arXiv", ["literature"], {
    test_status: "unauthorized",
  }),
  connector("grants_gov", "Grants.gov", ["grant"], {
    capabilities: ["Opportunities", "Requirements"],
  }),
  connector("nih_reporter", "NIH RePORTER", ["grant"], {
    capabilities: ["Awards"],
  }),
  connector("foundation_dir", "Foundation Directory", ["grant"], {
    test_status: "degraded",
  }),
  connector("private_funder", "Private Funder", ["grant"], {
    test_status: "authorization required",
  }),
];

function connector(
  id: string,
  name: string,
  assignedAgents: string[],
  overrides: Partial<ConnectorSetting> = {},
): ConnectorSetting {
  return {
    id,
    name,
    category: assignedAgents.includes("grant") ? "Funding" : "Literature",
    description: `${name} deterministic state fixture.`,
    auth_kind: "None",
    credential_kind: "none",
    credential_required: false,
    secret_status: "Not required",
    enabled: true,
    test_status: "ready",
    last_tested_at: "2026-07-23T12:00:00Z",
    assigned_agents: assignedAgents,
    terms_url: `https://${id}.example.test/terms`,
    data_boundary: "Public metadata only.",
    capabilities: ["Search"],
    ...overrides,
  };
}

async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function routeConnectors(
  page: Page,
  getConnectors: () => ConnectorSetting[] = () => CONNECTORS,
) {
  const settings = {
    allowed_export_destinations: ["SharePoint research site"],
    citation_coverage_threshold: 0.95,
    default_classification: "Internal",
    description: "Deterministic state-coverage workspace.",
    evaluation_policy: "Every claim requires stored evidence.",
    model_profile: "Bounded research",
    name: "State coverage project",
    online_research_default: false,
    project_id: "project-state-coverage",
    require_human_approval: true,
    retention_days: 365,
  };
  const workspace = {
    active_runs: 0,
    connector_ready: getConnectors().filter(
      (connector) => connector.test_status === "ready",
    ).length,
    connector_total: getConnectors().length,
    last_activity_at: "2026-07-23T12:00:00Z",
    library_items: 0,
    pending_approvals: 0,
    persistence: "deterministic fixture",
    project: settings,
  };
  for (const [endpoint, body] of [
    ["workspace", workspace],
    ["library", []],
    ["runs", []],
    ["approvals", []],
    ["settings", settings],
    ["agents", []],
    ["workflows", []],
  ] as const) {
    await page.route(`**/api/backend/api/${endpoint}`, (route) =>
      fulfillJson(route, body),
    );
  }
  await page.route("**/api/backend/api/connectors", (route) =>
    fulfillJson(route, getConnectors()),
  );
}

async function gotoStudio(page: Page, view: "literature" | "grant") {
  await page.goto(`/?view=${view}`);
  await expect(page.locator(".workbench-shell")).toHaveAttribute(
    "data-workspace-ready",
    "true",
  );
  await expect(
    page.getByRole("heading", {
      name: view === "literature" ? "Literature Studio" : "Grant Studio",
      level: 1,
    }),
  ).toBeVisible();
}

async function expectAccessible(page: Page) {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(results.violations).toEqual([]);
}

async function capture(page: Page, testInfo: TestInfo, id: string) {
  const screenshotPath = testInfo.outputPath(`${id}.png`);
  await page.screenshot({ path: screenshotPath, fullPage: true });
  await testInfo.attach(id, {
    path: screenshotPath,
    contentType: "image/png",
  });
}

async function assertVisibleState(
  page: Page,
  testInfo: TestInfo,
  screenshotId: string,
) {
  await expectAccessible(page);
  await capture(page, testInfo, screenshotId);
}

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((release) => {
    resolve = release;
  });
  return { promise, resolve };
}

test("Literature and Grant state-token contract has zero owned gaps", async (
  {},
  testInfo,
) => {
  const statePattern =
    /\[pw\.([a-z][a-z0-9-]*(?:\.[a-z0-9-]+)*):([a-z][a-z0-9-]*)\]/g;
  const implemented = new Set<string>();

  for (const filename of fs
    .readdirSync(__dirname)
    .filter((name) => name.endsWith(".spec.ts"))) {
    const source = fs.readFileSync(path.join(__dirname, filename), "utf8");
    const sourceFile = ts.createSourceFile(
      filename,
      source,
      ts.ScriptTarget.Latest,
      true,
      ts.ScriptKind.TS,
    );
    const visit = (node: ts.Node) => {
      if (
        ts.isCallExpression(node) &&
        ts.isIdentifier(node.expression) &&
        node.expression.text === "test"
      ) {
        const title = node.arguments[0];
        if (title && ts.isStringLiteralLike(title)) {
          for (const match of title.text.matchAll(statePattern)) {
            if (
              match[1].startsWith("literature.") ||
              match[1].startsWith("grant.")
            ) {
              implemented.add(`${match[1]}:${match[2]}`);
            }
          }
        }
      }
      ts.forEachChild(node, visit);
    };
    visit(sourceFile);
  }

  const required = new Set(
    UI_COVERAGE_MANIFEST.filter(
      (interaction) =>
        interaction.surface === "Literature" ||
        interaction.surface === "Grant",
    ).flatMap((interaction) =>
      interaction.states.map((state) => `${interaction.id}:${state}`),
    ),
  );
  const report = {
    requiredStateCount: required.size,
    implementedStateCount: [...implemented].filter((pair) => required.has(pair))
      .length,
    missingStates: [...required].filter((pair) => !implemented.has(pair)).sort(),
    orphanedStates: [...implemented]
      .filter((pair) => !required.has(pair))
      .sort(),
  };
  await testInfo.attach("literature-grant-state-contract.json", {
    body: JSON.stringify(report, null, 2),
    contentType: "application/json",
  });
  expect(report).toEqual({
    requiredStateCount: required.size,
    implementedStateCount: required.size,
    missingStates: [],
    orphanedStates: [],
  });
});

test("[pw.literature-protocol] validates protocol fields, criteria, connector policy, and request payload [pw.literature.protocol.question:keyboard][pw.literature.protocol.question:success][pw.literature.protocol.date-window:ready][pw.literature.protocol.date-window:keyboard][pw.literature.protocol.date-window:success][pw.literature.protocol.date-window:error][pw.literature.protocol.criteria:empty][pw.literature.protocol.criteria:duplicate][pw.literature.protocol.criteria:error][pw.literature.protocol.sources:selected][pw.literature.protocol.sources:disabled][pw.literature.protocol.sources:unhealthy][pw.literature.protocol.sources:unauthorized]", async ({
  page,
}, testInfo) => {
  await routeConnectors(page);
  let submittedPayload: Record<string, unknown> | undefined;
  await page.route("**/api/backend/api/studios/literature/run", async (route) => {
    submittedPayload = route.request().postDataJSON() as Record<string, unknown>;
    await fulfillJson(route, LITERATURE_WARNING_RESULT);
  });
  await gotoStudio(page, "literature");

  const from = page.getByLabel("Published from");
  const through = page.getByLabel("Through");
  await expect(from).toHaveValue("2020");
  await expect(through).toHaveValue("2026");
  await assertVisibleState(page, testInfo, "literature-date-ready");

  await from.fill("2030");
  await from.focus();
  await page.keyboard.press("ArrowUp");
  await expect(from).toHaveValue("2031");
  await page.keyboard.press("ArrowDown");
  await through.fill("2020");
  await page.getByRole("button", { name: "Search & screen evidence" }).click();
  await expect(page.locator("#literature-date-error")).toHaveText(
    "Published from must be earlier than or equal to Through.",
  );
  expect(submittedPayload).toBeUndefined();
  await assertVisibleState(page, testInfo, "literature-date-error");

  const inclusionInput = page.getByRole("textbox", {
    name: "Add inclusion criterion",
  });
  await inclusionInput.fill("Methods available");
  await page
    .getByRole("button", { name: "Add inclusion criterion" })
    .click();
  await expect(page.getByRole("status")).toContainText("already in");
  await expect(
    page.getByRole("button", {
      name: "Remove inclusion criterion: Methods available",
    }),
  ).toHaveCount(1);
  await assertVisibleState(page, testInfo, "literature-criterion-duplicate");

  await inclusionInput.fill("   ");
  await page
    .getByRole("button", { name: "Add inclusion criterion" })
    .click();
  await expect(
    page.getByText("Enter a criterion before adding it.", { exact: true }),
  ).toBeVisible();
  await assertVisibleState(page, testInfo, "literature-criterion-error");

  const inclusionRemovers = page.getByRole("button", {
    name: /^Remove inclusion criterion:/,
  });
  while ((await inclusionRemovers.count()) > 0) {
    await inclusionRemovers.first().click();
  }
  const exclusionRemovers = page.getByRole("button", {
    name: /^Remove exclusion criterion:/,
  });
  while ((await exclusionRemovers.count()) > 0) {
    await exclusionRemovers.first().click();
  }
  await expect(page.getByText("No inclusion criteria.")).toBeVisible();
  await expect(page.getByText("No exclusion criteria.")).toBeVisible();
  await assertVisibleState(page, testInfo, "literature-criteria-empty");

  await expect(page.getByRole("checkbox", { name: "Crossref" })).toBeDisabled();
  await expect(
    page.getByRole("checkbox", { name: "OpenAlex" }),
  ).toBeDisabled();
  await expect(page.getByRole("checkbox", { name: "arXiv" })).toBeDisabled();
  await expect(page.getByText("Disabled by an administrator")).toBeVisible();
  await expect(page.getByText("Connector health check failed")).toBeVisible();
  await expect(page.getByText("Authorization required").first()).toBeVisible();
  await assertVisibleState(page, testInfo, "literature-source-policy");

  const question = page.getByLabel("Research question");
  await question.focus();
  await page.keyboard.press("Control+A");
  await page.keyboard.type("Which workflows retain source-linked claims?");
  await from.fill("2018");
  await through.fill("2028");
  const europePmc = page.getByRole("checkbox", { name: "Europe PMC" });
  await europePmc.uncheck();
  await europePmc.focus();
  await page.keyboard.press("Space");
  await expect(europePmc).toBeChecked();

  const response = page.waitForResponse(
    (candidate) =>
      candidate.request().method() === "POST" &&
      candidate.url().endsWith("/api/backend/api/studios/literature/run"),
  );
  await page.getByRole("button", { name: "Search & screen evidence" }).focus();
  await page.keyboard.press("Enter");
  expect((await response).status()).toBe(200);
  expect(submittedPayload).toMatchObject({
    objective: "Which workflows retain source-linked claims?",
    inputs: {
      date_from: 2018,
      date_to: 2028,
      sources: ["PubMed", "Europe PMC"],
      inclusion_criteria: [],
      exclusion_criteria: [],
    },
  });
  await expect(page.locator(".screening-record")).toHaveCount(2);
  await assertVisibleState(page, testInfo, "literature-protocol-success");
});

test("[pw.literature-run] [pw.literature-screen] [pw.literature-extract] [pw.literature-synthesize] [pw.literature-audit] covers run disablement and deterministic downstream states [pw.literature.protocol.run:loading][pw.literature.screen.decision:keyboard][pw.literature.extract.tab:empty][pw.literature.extract.edit-export:keyboard][pw.literature.extract.edit-export:disabled][pw.literature.synthesize.tab:empty][pw.literature.audit.tab:empty][pw.literature.audit.tab:warning][pw.literature.audit.tab:blocked]", async ({
  page,
}, testInfo) => {
  await routeConnectors(page);
  const firstRun = deferred();
  let runNumber = 0;
  await page.route("**/api/backend/api/studios/literature/run", async (route) => {
    runNumber += 1;
    if (runNumber === 1) await firstRun.promise;
    const result =
      runNumber === 1
        ? LITERATURE_WARNING_RESULT
        : runNumber === 2
          ? {
              ...LITERATURE_WARNING_RESULT,
              citations: [LITERATURE_WARNING_RESULT.citations[1]],
              insight: {
                ...LITERATURE_WARNING_RESULT.insight!,
                referenced_source_ids: [],
                unresolved_source_ids: ["source-unresolved"],
              },
            }
          : {
              ...LITERATURE_WARNING_RESULT,
              citations: [],
              insight: {
                ...LITERATURE_WARNING_RESULT.insight!,
                referenced_source_ids: [],
                unresolved_source_ids: [],
              },
            };
    await fulfillJson(route, result);
  });
  await gotoStudio(page, "literature");

  await page.getByRole("button", { name: "Search & screen evidence" }).click();
  const runningButton = page.getByRole("button", { name: "Running workflow..." });
  await expect(runningButton).toBeDisabled();
  await assertVisibleState(page, testInfo, "literature-run-disabled");
  firstRun.resolve();
  await expect(page.locator(".screening-record")).toHaveCount(2);

  const firstDecision = page
    .getByRole("group", {
      name: "Screening decision for Verified retrieval study",
    })
    .getByRole("button", { name: "Maybe" });
  await firstDecision.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator(".metric-line").first()).toContainText("1 maybe");
  await assertVisibleState(page, testInfo, "literature-decision-keyboard");

  await page.getByRole("button", { name: "Extract", exact: true }).click();
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export CSV" }).focus();
  await page.keyboard.press("Enter");
  expect((await download).suggestedFilename()).toBe(
    "extraction-matrix-run-state-coverage.csv",
  );

  await page.getByRole("button", { name: "Screen", exact: true }).click();
  for (const group of await page
    .getByRole("group", { name: /^Screening decision for / })
    .all()) {
    await group.getByRole("button", { name: "Exclude" }).click();
  }
  await page.getByRole("button", { name: "Extract", exact: true }).click();
  await expect(
    page.getByText(/no included study currently has extractable fields/i),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Export CSV" })).toBeDisabled();
  await assertVisibleState(page, testInfo, "literature-extraction-empty");

  await page.getByRole("button", { name: "Synthesize" }).click();
  await expect(
    page.getByText("No supported synthesis is available for this run."),
  ).toBeVisible();
  await assertVisibleState(page, testInfo, "literature-synthesis-empty");

  await page.getByRole("button", { name: "Audit" }).click();
  await expect(page.getByText("Audit warning")).toBeVisible();
  await expect(page.locator(".audit-flag.unresolved")).toHaveCount(1);
  await assertVisibleState(page, testInfo, "literature-audit-warning");

  await page.getByRole("button", { name: "Search & screen evidence" }).click();
  await expect(page.getByText("Audit blocked")).toBeVisible();
  await expect(page.locator(".audit-flag.unresolved")).toHaveCount(1);
  await assertVisibleState(page, testInfo, "literature-audit-blocked");

  await page.getByRole("button", { name: "Search & screen evidence" }).click();
  await expect(page.getByText("No citations")).toBeVisible();
  await expect(page.locator(".audit-citation-row")).toHaveCount(0);
  await assertVisibleState(page, testInfo, "literature-audit-empty");
});

test("[pw.grant-discovery] [pw.grant-connectors] [pw.grant-opportunity] [pw.grant-draft] covers governed discovery, source policy, framing, and empty sources [pw.grant.discovery.search:keyboard][pw.grant.discovery.search:disabled][pw.grant.discovery.sources:selected][pw.grant.discovery.sources:unhealthy][pw.grant.discovery.sources:unauthorized][pw.grant.discovery.sources:empty][pw.grant.opportunity.id:keyboard][pw.grant.editor.framing:ready][pw.grant.editor.framing:keyboard][pw.grant.editor.framing:success]", async ({
  page,
}, testInfo) => {
  let emptyGrantSources = false;
  await routeConnectors(page, () =>
    emptyGrantSources
      ? CONNECTORS.filter(
          (connector) => !connector.assigned_agents.includes("grant"),
        )
      : CONNECTORS,
  );
  let submittedPayload: Record<string, unknown> | undefined;
  await page.route("**/api/backend/api/studios/grant/run", async (route) => {
    submittedPayload = route.request().postDataJSON() as Record<string, unknown>;
    await fulfillJson(route, GRANT_RESULT);
  });
  await gotoStudio(page, "grant");

  const framing = page.getByLabel("Project framing");
  await expect(framing).toHaveValue(/competitive application/i);
  await framing.focus();
  await page.keyboard.press("ControlOrMeta+A");
  await page.keyboard.type(
    "Build a source-linked package without unsupported institutional facts.",
  );

  const discovery = page.getByLabel("Opportunity discovery");
  const search = discovery.getByLabel("Search funding opportunities");
  await search.focus();
  await page.keyboard.type("Grants.gov");
  await expect(discovery.getByText("Grants.gov")).toBeVisible();
  await search.fill("");
  const useSource = discovery
    .getByRole("button", { name: "Use as opportunity source" })
    .first();
  await useSource.focus();
  await page.keyboard.press("Enter");
  const opportunityId = page.getByLabel("Opportunity ID");
  await expect(opportunityId).toHaveValue(/GRANTS_GOV-LEAD-/);
  await opportunityId.focus();
  await page.keyboard.press("Control+A");
  await page.keyboard.type("RFA-KEYBOARD-2026");

  await expect(
    page.getByRole("checkbox", { name: "Foundation Directory" }),
  ).toBeDisabled();
  await expect(
    page
      .locator('label[data-availability]:has(input[aria-label="Foundation Directory"])'),
  ).toHaveAttribute("data-availability", "unhealthy");
  await expect(
    page.getByRole("checkbox", { name: "Private Funder" }),
  ).toBeDisabled();
  await expect(page.getByText("Connector health check failed")).toBeVisible();
  await expect(page.getByText("Authorization required")).toBeVisible();
  await assertVisibleState(page, testInfo, "grant-source-policy");

  const grantsGov = page.getByRole("checkbox", { name: "Grants.gov" });
  const nihReporter = page.getByRole("checkbox", { name: "NIH RePORTER" });
  await grantsGov.uncheck();
  await nihReporter.uncheck();
  await expect(search).toBeDisabled();
  await expect(
    discovery.getByText(/select at least one funding connector/i),
  ).toBeVisible();
  await assertVisibleState(page, testInfo, "grant-discovery-disabled");

  await grantsGov.focus();
  await page.keyboard.press("Space");
  await expect(grantsGov).toBeChecked();
  await expect(search).toBeEnabled();
  await page.getByRole("button", { name: "Parse notice & build package" }).click();
  expect(submittedPayload).toMatchObject({
    objective:
      "Build a source-linked package without unsupported institutional facts.",
    inputs: {
      opportunity_id: "RFA-KEYBOARD-2026",
      funding_sources: ["grants_gov"],
    },
  });
  await assertVisibleState(page, testInfo, "grant-framing-success");

  emptyGrantSources = true;
  await page.reload();
  await expect(page.locator(".workbench-shell")).toHaveAttribute(
    "data-workspace-ready",
    "true",
  );
  await expect(
    page.getByText(/no funding connectors are assigned yet/i),
  ).toBeVisible();
  await expect(
    page.getByLabel("Search funding opportunities"),
  ).toBeDisabled();
  await assertVisibleState(page, testInfo, "grant-sources-empty");
});

test("[pw.grant-requirements] opens each deterministic requirement status [pw.grant.requirements.open:unmapped][pw.grant.requirements.open:needs-input][pw.grant.requirements.open:blocked]", async ({
  page,
}, testInfo) => {
  await routeConnectors(page);
  await page.route("**/api/backend/api/studios/grant/run", (route) =>
    fulfillJson(route, GRANT_RESULT),
  );
  await gotoStudio(page, "grant");
  await page.getByRole("button", { name: "Parse notice & build package" }).click();
  await expect(
    page.getByRole("button", { name: "Unmapped facilities plan" }),
  ).toBeEnabled();

  for (const state of [
    {
      button: "Unmapped facilities plan",
      dialog: "Unmapped facilities plan",
      status: "unmapped",
      screenshot: "grant-requirement-unmapped",
    },
    {
      button: "Budget needs input",
      dialog: "Budget needs input",
      status: "needs input",
      screenshot: "grant-requirement-needs-input",
    },
    {
      button: "Blocked export approval",
      dialog: "Blocked export approval",
      status: "blocked",
      screenshot: "grant-requirement-blocked",
    },
  ]) {
    const button = page.getByRole("button", { name: state.button });
    await button.focus();
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog", { name: state.dialog });
    await expect(dialog).toContainText(`Status: ${state.status}`);
    await assertVisibleState(page, testInfo, state.screenshot);
    await dialog.getByLabel("Close requirement detail").click();
  }
});

test("[pw.grant-build] [pw.grant-review] covers package and red-team async, findings, resolution, and errors [pw.grant.package.build:keyboard][pw.grant.package.build:loading][pw.grant.package.build:error][pw.grant.review.red-team:disabled][pw.grant.review.red-team:running][pw.grant.review.red-team:findings][pw.grant.review.red-team:resolved][pw.grant.review.red-team:error]", async ({
  page,
  releaseDiagnostics,
}, testInfo) => {
  await routeConnectors(page);
  const buildFailure = deferred();
  const redTeamRunning = deferred();
  let redTeamCount = 0;
  await page.route("**/api/backend/api/studios/grant/run", async (route) => {
    const request = route.request().postDataJSON() as {
      inputs: { red_team_pass?: boolean };
    };
    if (!request.inputs.red_team_pass) {
      await buildFailure.promise;
      await fulfillJson(
        route,
        { detail: "Grant package validation failed deterministically." },
        503,
      );
      return;
    }

    redTeamCount += 1;
    if (redTeamCount === 1) {
      await redTeamRunning.promise;
      await fulfillJson(route, {
        ...GRANT_RESULT,
        readiness: 61,
        blockers: ["budget sign-off"],
        fact_gaps: [
          {
            id: "biosketch",
            label: "Biosketch missing",
            guidance: "Upload the verified biosketch.",
            status: "missing",
          },
        ],
      });
      return;
    }
    if (redTeamCount === 2) {
      await fulfillJson(route, GRANT_RESULT);
      return;
    }
    await fulfillJson(
      route,
      { detail: "Red-team review failed deterministically." },
      503,
    );
  });
  releaseDiagnostics.expectConsoleError(/status of 503 \(Service Unavailable\)/);
  releaseDiagnostics.expectConsoleError(/status of 503 \(Service Unavailable\)/);
  await gotoStudio(page, "grant");

  const buildButton = page.getByRole("button", {
    name: "Parse notice & build package",
  });
  await buildButton.focus();
  await page.keyboard.press("Enter");
  await expect(
    page.getByRole("button", { name: "Running workflow..." }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Red-team draft" }),
  ).toBeDisabled();
  await assertVisibleState(page, testInfo, "grant-package-loading");
  buildFailure.resolve();
  await expect(page.locator(".error-banner[role='alert']")).toHaveText(
    "Grant package validation failed deterministically.",
  );
  await assertVisibleState(page, testInfo, "grant-package-error");

  await page.getByRole("button", { name: "Red-team draft" }).click();
  await expect(
    page.getByRole("button", { name: "Red-teaming..." }),
  ).toBeDisabled();
  await assertVisibleState(page, testInfo, "grant-red-team-running");
  redTeamRunning.resolve();
  await expect(page.getByText("Biosketch missing")).toBeVisible();
  await expect(page.getByText(/export blocked by/i)).toContainText(
    "budget sign-off",
  );
  await assertVisibleState(page, testInfo, "grant-red-team-findings");

  await page.getByRole("button", { name: "Red-team draft" }).click();
  await expect(page.getByText(/92% ready · red-team pass/i)).toBeVisible();
  await expect(page.getByText(/export blocked by/i)).toHaveCount(0);
  await assertVisibleState(page, testInfo, "grant-red-team-resolved");

  await page.getByRole("button", { name: "Red-team draft" }).click();
  await expect(page.locator(".error-banner[role='alert']")).toHaveText(
    "Red-team review failed deterministically.",
  );
  await assertVisibleState(page, testInfo, "grant-red-team-error");
});
