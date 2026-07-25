import { expect, test } from "./fixtures";

type StudioRunPayload = {
  objective: string;
  online_research: boolean;
  inputs: Record<string, unknown>;
};

/** Navigate to the workbench shell and wait for the readiness flag Playwright
 * fixtures use to know hydration/data bootstrap has finished. */
async function openWorkbench(page: import("@playwright/test").Page) {
  await page.goto("/");
  await expect(page.locator(".workbench-shell")).toHaveAttribute(
    "data-workspace-ready",
    "true",
  );
}

/** Open the workbench and switch to a named studio card in one step, since
 * every scenario below needs this exact pair of actions before anything
 * studio-specific can happen. */
async function openStudio(
  page: import("@playwright/test").Page,
  studioName: string | RegExp,
) {
  await openWorkbench(page);
  const exact = typeof studioName === "string";
  await page
    .getByRole("button", { name: studioName, exact })
    .first()
    .click();
}

/** Click a studio's run trigger and capture the exact JSON body the BFF
 * received, asserting the round trip itself succeeded (200) before handing
 * the parsed payload back for scenario-specific assertions. */
async function submitStudioRun(
  page: import("@playwright/test").Page,
  capability: string,
  triggerLabel: string,
): Promise<StudioRunPayload> {
  const pendingRequest = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      request.url().includes(`/api/studios/${capability}/run`),
  );
  const pendingResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().includes(`/api/studios/${capability}/run`),
  );
  await page.getByRole("button", { name: triggerLabel }).click();
  const [request, response] = await Promise.all([pendingRequest, pendingResponse]);
  expect(response.status()).toBe(200);
  return request.postDataJSON() as StudioRunPayload;
}

test.describe("Library and Settings interactions", () => {
  test("[pw.library-detail] a library row opens a detail dialog [pw.library.item.open:ready]", async ({
    page,
  }) => {
    await openWorkbench(page);
    await page.getByRole("button", { name: /^Library \d+$/ }).click();

    await page.locator(".library-row:not(.library-head)").first().click();
    const detailDialog = page.getByRole("dialog", { name: /.+/ });
    await expect(detailDialog.locator("dl.library-detail-facts")).toBeVisible();

    await detailDialog.getByRole("button", { name: "Close", exact: true }).click();
    await expect(detailDialog).not.toBeVisible();
  });

  test("[pw.integration-readiness] Settings exposes a truthful integration readiness section [pw.settings.integrations.readiness:deployment-managed]", async ({
    page,
  }) => {
    await openWorkbench(page);
    await page.getByLabel("Open project settings").click();
    await page.getByRole("button", { name: "Readiness" }).click();

    await expect(page.getByText("APIM / Toolbox")).toBeVisible();
    await expect(page.getByText("Work IQ", { exact: true })).toBeVisible();
    await expect(page.getByText("GitHub Copilot connector authoring")).toBeVisible();
    await expect(page.getByText("Foundry Code Interpreter")).toBeVisible();
    await expect(page.getByText("Deployment managed")).toBeVisible();
    await expect(page.getByText(/project-scoped, not per-user/i)).toBeVisible();
  });

  test("[pw.connector-versions] Connectors tab shows a truthful, clearly disabled APIM/MCP/Toolbox version state with no fake promotion [pw.settings.connectors.versions:unconfigured]", async ({
    page,
  }) => {
    await openWorkbench(page);
    await page.getByLabel("Open project settings").click();
    await page.getByRole("button", { name: /Connectors \d+/i }).click();
    await expect(page.getByText("Gateway & tool versions")).toBeVisible();

    const apimReadinessCard = page
      .locator(".readiness-status-card")
      .filter({ hasText: "Azure API Management (APIM)" });
    await expect(apimReadinessCard.getByText("Not configured")).toBeVisible();
    await expect(
      apimReadinessCard.getByRole("button", { name: "Promote to default" }),
    ).toBeDisabled();
    await expect(
      apimReadinessCard.getByRole("button", { name: "Roll back" }),
    ).toBeDisabled();

    await expect(page.getByText("MCP tool registry")).toBeVisible();
    await expect(page.getByText("Toolbox")).toBeVisible();
  });
});

test.describe("Institutional Q&A interactions", () => {
  test("[pw.institutional-corpora] [pw.institutional-answer] [pw.institutional-evidence] [pw.work-iq-readiness] corpus scopes are sent, citations open an evidence dialog, and Work IQ stays disabled [pw.institutional.corpora:selected][pw.institutional.corpora:unselected][pw.institutional.corpora:locked][pw.institutional.work-iq:unconfigured][pw.institutional.question:ready][pw.institutional.question:success][pw.institutional.evidence.open:ready][pw.institutional.evidence.open:open]", async ({
    page,
  }) => {
    await openStudio(page, "Institutional Q&A");

    const legalHoldScope = page.getByRole("checkbox", { name: /legal hold/i });
    await expect(legalHoldScope).toBeDisabled();
    await page.getByRole("checkbox", { name: /research records/i }).uncheck();

    const payload = await submitStudioRun(page, "institutional_qa", "Resolve policy answer");
    expect(payload.inputs.corpus_scopes).not.toContain("records");
    expect(payload.inputs.corpus_scopes).not.toContain("legal_hold");

    await page.locator(".inline-citation").first().click();
    const evidenceDialog = page.getByRole("dialog", { name: /.+/ });
    await expect(evidenceDialog.locator("dl.citation-detail-facts")).toBeVisible();
    await page.getByLabel("Close evidence detail").click();

    const workIqSignalsToggle = page.getByRole("checkbox", {
      name: /enable work iq readiness signals/i,
    });
    await expect(workIqSignalsToggle).toBeDisabled();
    await expect(workIqSignalsToggle).not.toBeChecked();
  });
});

test.describe("Dataset Lab interactions", () => {
  test("[pw.dataset-upload] [pw.dataset-plan] [pw.dataset-profile] uploads a real bounded CSV file and requires plan approval before profiling [pw.dataset.upload:uploading][pw.dataset.upload:validated][pw.dataset.profile:ready][pw.dataset.profile:disabled][pw.dataset.profile:success][pw.dataset.plan.approve:approved][pw.dataset.execution:waiting-for-approval][pw.dataset.execution:completed]", async ({
    page,
  }) => {
    await openStudio(page, "Dataset Lab");

    const runButton = page.getByRole("button", {
      name: "Analyze with Foundry Code Interpreter",
    });
    await expect(runButton).toBeDisabled();

    const objectiveField = page.getByRole("textbox", { name: "Analysis objective" });
    await expect(objectiveField).toHaveValue(
      "Profile the pilot outcome dataset and plan a descriptive group comparison.",
    );
    await objectiveField.focus();
    await page.keyboard.press("Control+a");
    await page.keyboard.type("Keyboard-typed dataset objective.");
    await expect(objectiveField).toHaveValue("Keyboard-typed dataset objective.");

    await page.getByLabel("Upload a dataset file").setInputFiles({
      name: "pilot.csv",
      mimeType: "text/csv",
      buffer: Buffer.from("id,outcome\n1,improved\n2,stable\n"),
    });
    await expect(page.getByText("pilot.csv")).toBeVisible();

    const uploadResponse = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().includes("/api/library/upload"),
    );
    await page.getByRole("button", { name: "Upload to Library" }).click();
    expect((await uploadResponse).status()).toBe(200);

    // [pw.dataset.execution:waiting-for-approval] The asset is now uploaded
    // and read-ready (uploadedFile set, csvReadStatus === "ready") and the
    // objective is filled in, so the RunButton's only remaining disabled
    // reason is `!planApproved`. Deliberately isolated from the earlier
    // `toBeDisabled()` assertion above, which is caused by several unmet
    // preconditions at once (missing objective/upload) and does not
    // truthfully exercise this state alone. This is a distinct gate from
    // `dataset.execution:blocked` (the separate large-asset "Human approval
    // required before submit" banner, which only appears after a run
    // attempt on an estimate-required asset).
    await expect(runButton).toBeDisabled();

    await page
      .getByLabel(/I approve sending this bounded dataset to the Foundry Dataset Agent/)
      .check();
    await expect(runButton).toBeEnabled();

    const payload = await submitStudioRun(page, "dataset", "Analyze with Foundry Code Interpreter");
    expect(payload.inputs.filename).toBe("pilot.csv");
    expect(String(payload.inputs.csv_text)).toContain("id,outcome");
    expect(payload.objective).toBe("Keyboard-typed dataset objective.");
    await expect(page.locator(".schema-row").first()).toBeVisible();
  });
});

test.describe("Matching Explorer source selection", () => {
  test("[pw.matching-sources] controls public/institutional sources, keeps Work IQ disabled, and sends selected sources [pw.matching.need.sources:ready][pw.matching.need.sources:selected]", async ({
    page,
  }) => {
    await openStudio(page, "Matching Explorer");

    const workIqCollaborationToggle = page.getByRole("checkbox", {
      name: /work iq collaboration signals/i,
    });
    await expect(workIqCollaborationToggle).toBeDisabled();
    await expect(workIqCollaborationToggle).not.toBeChecked();

    await page.getByRole("checkbox", { name: "Institutional directory" }).uncheck();

    const payload = await submitStudioRun(page, "matching", "Build verified shortlist");
    expect(payload.inputs.sources).not.toContain("institutional");
  });
});

test.describe("Matching Explorer interactions", () => {
  test("[pw.matching-filters] [pw.matching-run] [pw.matching-results] [pw.matching-shortlist] record types and hard filters are sent, and shortlist compare is transparent [pw.matching.compare-shortlist:ready][pw.matching.compare-shortlist:success][pw.matching.need.entity-types:selected][pw.matching.need.entity-types:unselected][pw.matching.need.hard-filters:selected][pw.matching.need.hard-filters:unselected][pw.matching.run:ready][pw.matching.run:success][pw.matching.result.select:ready]", async ({
    page,
  }) => {
    await openStudio(page, "Matching Explorer");

    await page.getByRole("checkbox", { name: "Templates" }).check();
    await page.getByRole("checkbox", { name: "Current institutional record" }).uncheck();

    const payload = await submitStudioRun(page, "matching", "Build verified shortlist");
    expect(payload.inputs.record_kinds).toContain("template");
    expect(payload.inputs.hard_filters).not.toContain("current_institutional_record");

    const topMatchCard = page.locator(".match-card").first();
    await topMatchCard.getByRole("button", { name: /add .* to shortlist/i }).click();
    await expect(page.getByText(/^Shortlist \(1\)$/)).toBeVisible();

    await page.getByRole("button", { name: "Compare shortlisted" }).click();
    await expect(page.getByText("Top evidence factors")).toBeVisible();
  });
});

test.describe("Grant Studio interactions", () => {
  test("[pw.grant-draft] [pw.grant-review] [pw.grant-connectors] [pw.grant-build] section tabs, red-team, source discovery, and connector draft dialog work [pw.grant.discovery.sources:ready][pw.grant.editor.tabs:ready][pw.grant.editor.tabs:selected][pw.grant.package.build:ready][pw.grant.package.build:success][pw.grant.review.red-team:ready]", async ({
    page,
  }) => {
    await openStudio(page, "Grant Studio");

    await page.getByRole("button", { name: "Significance" }).click();
    await expect(page.getByText(/not yet drafted for this section/i)).toBeVisible();
    await page.getByRole("button", { name: "Specific aims", exact: true }).click();

    const firstBuild = await submitStudioRun(page, "grant", "Parse notice & build package");
    expect(firstBuild.inputs.red_team_pass).toBe(false);

    const redTeamedBuild = await submitStudioRun(page, "grant", "Red-team draft");
    expect(redTeamedBuild.inputs.red_team_pass).toBe(true);

    await page.getByRole("button", { name: "Request a new connector" }).click();
    const requestDialog = page.getByRole("dialog", { name: /request a new connector/i });
    await expect(requestDialog.getByText(/records a draft request only/i)).toBeVisible();
    await requestDialog.getByLabel("Connector name").fill("NSF Awards");
    await requestDialog.getByLabel("Base URL").fill("https://api.nsf.gov");
    await requestDialog
      .getByLabel("Justification")
      .fill("Needed for federal award discovery.");
    await requestDialog.getByRole("button", { name: "Save draft request" }).click();
    await expect(page.getByText("Draft — needs review")).toBeVisible();
  });

  test("[pw.grant-discovery] [pw.grant-opportunity] [pw.grant-requirements] discovery filters governed connectors and requirement rows open source evidence [pw.grant.discovery.search:ready][pw.grant.discovery.search:success][pw.grant.opportunity.id:ready][pw.grant.opportunity.id:success][pw.grant.requirements.open:mapped]", async ({
    page,
  }) => {
    await openStudio(page, "Grant Studio");

    const discoveryPanel = page.getByLabel("Opportunity discovery");
    await expect(discoveryPanel.getByText("Grants.gov")).toBeVisible();

    await discoveryPanel
      .getByLabel("Search funding opportunities")
      .fill("no such opportunity anywhere");
    await expect(
      discoveryPanel.getByText(/no net-new opportunities match this query/i),
    ).toBeVisible();

    await discoveryPanel.getByLabel("Search funding opportunities").fill("");
    await discoveryPanel
      .getByRole("button", { name: "Use as opportunity source" })
      .first()
      .click();
    await expect(page.getByLabel("Opportunity ID")).toHaveValue(/-LEAD-/);

    await submitStudioRun(page, "grant", "Parse notice & build package");
    await page.getByRole("button", { name: /two-page project summary/i }).click();
    const requirementDetailDialog = page.getByRole("dialog", {
      name: /two-page project summary/i,
    });
    await expect(requirementDetailDialog).toBeVisible();
    await requirementDetailDialog.getByLabel("Close requirement detail").click();
    await expect(requirementDetailDialog).not.toBeVisible();
  });
});

test.describe("Literature Studio interactions", () => {
  test("[pw.literature-protocol] [pw.literature-screen] [pw.literature-extract] [pw.literature-synthesize] [pw.literature-audit] Screen/Extract/Synthesize/Audit tabs show distinct content and criteria are editable [pw.literature.protocol.criteria:ready][pw.literature.protocol.criteria:editing][pw.literature.synthesize.tab:ready][pw.literature.audit.tab:not-verified]", async ({
    page,
  }) => {
    await openStudio(page, /literature review synthesis/i);

    await page.getByRole("textbox", { name: "Add inclusion criterion" }).fill("Custom criterion");
    await page.getByRole("button", { name: "Add inclusion criterion" }).click();
    await expect(page.getByText("Custom criterion")).toBeVisible();

    const payload = await submitStudioRun(page, "literature", "Search & screen evidence");
    expect(payload.inputs.inclusion_criteria).toContain("Custom criterion");

    await expect(page.locator(".screening-record").first()).toBeVisible();
    await page.getByRole("button", { name: "Extract", exact: true }).click();
    await expect(page.locator(".extraction-row").first()).toBeVisible();
    await page.getByRole("button", { name: "Synthesize" }).click();
    await expect(page.locator(".synthesis-card")).toBeVisible();
    await page.getByRole("button", { name: "Audit" }).click();

    const auditStatus = page.locator("[data-audit-status]");
    await expect(page.getByText("Claim & citation audit")).toBeVisible();
    await expect(auditStatus).toHaveAttribute("data-audit-status", "not-verified");
    await expect(auditStatus).toContainText("Not verified");
  });

  test("[pw.literature-screen] screening decisions change included/excluded counts and the extraction tab [pw.literature.screen.decision:ready][pw.literature.screen.decision:success]", async ({
    page,
  }) => {
    await openStudio(page, /literature review synthesis/i);
    await submitStudioRun(page, "literature", "Search & screen evidence");

    const firstScreeningRecord = page.locator(".screening-record").first();
    await firstScreeningRecord.getByRole("button", { name: "Exclude" }).click();
    await expect(page.locator(".metric-line")).toContainText("1 excluded");
  });

  test("[pw.literature-extract] extraction cells are editable and export the current version as a CSV download [pw.literature.extract.edit-export:ready][pw.literature.extract.edit-export:success]", async ({
    page,
  }) => {
    await openStudio(page, /literature review synthesis/i);
    await submitStudioRun(page, "literature", "Search & screen evidence");

    await page.getByRole("button", { name: "Extract", exact: true }).click();
    const methodField = page.locator(".extraction-row").first().locator("input").first();
    await methodField.fill("Revised method for this run");
    await expect(methodField).toHaveValue("Revised method for this run");

    const downloadPromise = page.waitForEvent("download");
    await page.getByRole("button", { name: "Export CSV" }).click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toMatch(/^extraction-matrix-.*\.csv$/);
    await expect(page.getByRole("status")).toContainText(/exported \d+ extraction row/i);
  });
});
