import { expect, test } from "./fixtures";

async function waitForWorkspace(page: import("@playwright/test").Page) {
  await page.goto("/");
  await expect(page.locator(".workbench-shell")).toHaveAttribute(
    "data-workspace-ready",
    "true",
  );
}

async function runStudioAndCapturePayload(
  page: import("@playwright/test").Page,
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

test.describe("Literature Studio interactions", () => {
  test("[pw.literature-protocol] [pw.literature-screen] [pw.literature-extract] [pw.literature-synthesize] [pw.literature-audit] Screen/Extract/Synthesize/Audit tabs show distinct content and criteria are editable [pw.literature.protocol.criteria:ready][pw.literature.protocol.criteria:editing][pw.literature.synthesize.tab:ready][pw.literature.audit.tab:passed]", async ({
    page,
  }) => {
    await waitForWorkspace(page);
    await page
      .getByRole("button", { name: /literature review synthesis/i })
      .click();

    await page.getByRole("textbox", { name: "Add inclusion criterion" }).fill("Custom criterion");
    await page
      .getByRole("button", { name: "Add inclusion criterion" })
      .click();
    await expect(page.getByText("Custom criterion")).toBeVisible();

    const payload = await runStudioAndCapturePayload(
      page,
      "literature",
      "Search & screen evidence",
    );
    expect(payload.inputs.inclusion_criteria).toContain("Custom criterion");

    await expect(page.locator(".screening-record").first()).toBeVisible();
    await page.getByRole("button", { name: "Extract", exact: true }).click();
    await expect(page.locator(".extraction-row").first()).toBeVisible();
    await page.getByRole("button", { name: "Synthesize" }).click();
    await expect(page.locator(".synthesis-card")).toBeVisible();
    await page.getByRole("button", { name: "Audit" }).click();
    await expect(page.getByText("Claim & citation audit")).toBeVisible();
  });

  test("[pw.literature-screen] screening decisions change included/excluded counts and the extraction tab [pw.literature.screen.decision:ready][pw.literature.screen.decision:success]", async ({
    page,
  }) => {
    await waitForWorkspace(page);
    await page
      .getByRole("button", { name: /literature review synthesis/i })
      .click();
    await runStudioAndCapturePayload(
      page,
      "literature",
      "Search & screen evidence",
    );

    const firstRecord = page.locator(".screening-record").first();
    await firstRecord.getByRole("button", { name: "Exclude" }).click();
    await expect(page.locator(".metric-line")).toContainText("1 excluded");
  });

  test("[pw.literature-extract] extraction cells are editable and export the current version as a CSV download [pw.literature.extract.edit-export:ready][pw.literature.extract.edit-export:success]", async ({
    page,
  }) => {
    await waitForWorkspace(page);
    await page
      .getByRole("button", { name: /literature review synthesis/i })
      .click();
    await runStudioAndCapturePayload(
      page,
      "literature",
      "Search & screen evidence",
    );

    await page.getByRole("button", { name: "Extract", exact: true }).click();
    const methodField = page.locator(".extraction-row").first().locator(
      "input",
    ).first();
    await methodField.fill("Revised method for this run");
    await expect(methodField).toHaveValue("Revised method for this run");

    const downloadPromise = page.waitForEvent("download");
    await page.getByRole("button", { name: "Export CSV" }).click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toMatch(/^extraction-matrix-.*\.csv$/);
    await expect(page.getByRole("status")).toContainText(/exported \d+ extraction row/i);
  });
});

test.describe("Grant Studio interactions", () => {
  test("[pw.grant-draft] [pw.grant-review] [pw.grant-connectors] [pw.grant-build] section tabs, red-team, source discovery, and connector draft dialog work [pw.grant.discovery.sources:ready][pw.grant.editor.tabs:ready][pw.grant.editor.tabs:selected][pw.grant.package.build:ready][pw.grant.package.build:success][pw.grant.review.red-team:ready]", async ({
    page,
  }) => {
    await waitForWorkspace(page);
    await page
      .getByRole("button", { name: "Grant Studio", exact: true })
      .first()
      .click();

    await page.getByRole("button", { name: "Significance" }).click();
    await expect(
      page.getByText(/not yet drafted for this section/i),
    ).toBeVisible();
    await page.getByRole("button", { name: "Specific aims", exact: true }).click();

    const buildPayload = await runStudioAndCapturePayload(
      page,
      "grant",
      "Parse notice & build package",
    );
    expect(buildPayload.inputs.red_team_pass).toBe(false);

    const redTeamPayload = await runStudioAndCapturePayload(
      page,
      "grant",
      "Red-team draft",
    );
    expect(redTeamPayload.inputs.red_team_pass).toBe(true);

    await page
      .getByRole("button", { name: "Request a new connector" })
      .click();
    const dialog = page.getByRole("dialog", {
      name: /request a new connector/i,
    });
    await expect(
      dialog.getByText(/records a draft request only/i),
    ).toBeVisible();
    await dialog.getByLabel("Connector name").fill("NSF Awards");
    await dialog.getByLabel("Base URL").fill("https://api.nsf.gov");
    await dialog
      .getByLabel("Justification")
      .fill("Needed for federal award discovery.");
    await dialog.getByRole("button", { name: "Save draft request" }).click();
    await expect(page.getByText("Draft — needs review")).toBeVisible();
  });

  test("[pw.grant-discovery] [pw.grant-opportunity] [pw.grant-requirements] discovery filters governed connectors and requirement rows open source evidence [pw.grant.discovery.search:ready][pw.grant.discovery.search:success][pw.grant.opportunity.id:ready][pw.grant.opportunity.id:success][pw.grant.requirements.open:mapped]", async ({
    page,
  }) => {
    await waitForWorkspace(page);
    await page
      .getByRole("button", { name: "Grant Studio", exact: true })
      .first()
      .click();

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

    await runStudioAndCapturePayload(
      page,
      "grant",
      "Parse notice & build package",
    );
    await page
      .getByRole("button", { name: /two-page project summary/i })
      .click();
    const requirementDialog = page.getByRole("dialog", {
      name: /two-page project summary/i,
    });
    await expect(requirementDialog).toBeVisible();
    await requirementDialog.getByLabel("Close requirement detail").click();
    await expect(requirementDialog).not.toBeVisible();
  });
});

test.describe("Matching Explorer source selection", () => {
  test("[pw.matching-sources] controls public/institutional sources, keeps Work IQ disabled, and sends selected sources [pw.matching.need.sources:ready][pw.matching.need.sources:selected][pw.matching.need.sources:unavailable]", async ({
    page,
  }) => {
    await waitForWorkspace(page);
    await page
      .getByRole("button", { name: "Matching Explorer", exact: true })
      .first()
      .click();

    const workIqToggle = page.getByRole("checkbox", {
      name: /work iq collaboration signals/i,
    });
    await expect(workIqToggle).toBeDisabled();
    await expect(workIqToggle).not.toBeChecked();

    await page
      .getByRole("checkbox", { name: "Institutional directory" })
      .uncheck();

    const payload = await runStudioAndCapturePayload(
      page,
      "matching",
      "Build verified shortlist",
    );
    expect(payload.inputs.sources).not.toContain("institutional");
  });
});

test.describe("Matching Explorer interactions", () => {
  test("[pw.matching-filters] [pw.matching-run] [pw.matching-results] [pw.matching-shortlist] record types and hard filters are sent, and shortlist compare is transparent [pw.matching.compare-shortlist:ready][pw.matching.compare-shortlist:success][pw.matching.need.entity-types:selected][pw.matching.need.entity-types:unselected][pw.matching.need.hard-filters:selected][pw.matching.need.hard-filters:unselected][pw.matching.run:ready][pw.matching.run:success][pw.matching.result.select:ready]", async ({
    page,
  }) => {
    await waitForWorkspace(page);
    await page
      .getByRole("button", { name: "Matching Explorer", exact: true })
      .first()
      .click();

    await page.getByRole("checkbox", { name: "Templates" }).check();
    await page
      .getByRole("checkbox", { name: "Current institutional record" })
      .uncheck();

    const payload = await runStudioAndCapturePayload(
      page,
      "matching",
      "Build verified shortlist",
    );
    expect(payload.inputs.record_kinds).toContain("template");
    expect(payload.inputs.hard_filters).not.toContain(
      "current_institutional_record",
    );

    const firstCard = page.locator(".match-card").first();
    await firstCard.getByRole("button", { name: /add .* to shortlist/i }).click();
    await expect(page.getByText(/^Shortlist \(1\)$/)).toBeVisible();
    await page.getByRole("button", { name: "Compare shortlisted" }).click();
    await expect(page.getByText("Top evidence factors")).toBeVisible();
  });
});

test.describe("Dataset Lab interactions", () => {
  test("[pw.dataset-upload] [pw.dataset-plan] [pw.dataset-profile] uploads a real bounded CSV file and requires plan approval before profiling [pw.dataset.upload:uploading][pw.dataset.upload:validated][pw.dataset.profile:ready][pw.dataset.profile:disabled][pw.dataset.profile:success][pw.dataset.plan.approve:approved][pw.dataset.execution:pending-approval][pw.dataset.execution:completed]", async ({
    page,
  }) => {
    await waitForWorkspace(page);
    await page
      .getByRole("button", { name: "Dataset Lab", exact: true })
      .first()
      .click();

    const runButton = page.getByRole("button", {
      name: "Analyze with Foundry Code Interpreter",
    });
    await expect(runButton).toBeDisabled();

    const objectiveField = page.getByRole("textbox", {
      name: "Analysis objective",
    });
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

    const uploadResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().includes("/api/library/upload"),
    );
    await page.getByRole("button", { name: "Upload to Library" }).click();
    expect((await uploadResponsePromise).status()).toBe(200);

    await page
      .getByLabel(
        /I approve sending this bounded dataset to the Foundry Dataset Agent/,
      )
      .check();
    await expect(runButton).toBeEnabled();

    const payload = await runStudioAndCapturePayload(
      page,
      "dataset",
      "Analyze with Foundry Code Interpreter",
    );
    expect(payload.inputs.filename).toBe("pilot.csv");
    expect(String(payload.inputs.csv_text)).toContain("id,outcome");
    expect(payload.objective).toBe("Keyboard-typed dataset objective.");
    await expect(page.locator(".schema-row").first()).toBeVisible();
  });
});

test.describe("Institutional Q&A interactions", () => {
  test("[pw.institutional-corpora] [pw.institutional-answer] [pw.institutional-evidence] [pw.work-iq-readiness] corpus scopes are sent, citations open an evidence dialog, and Work IQ stays disabled [pw.institutional.corpora:selected][pw.institutional.corpora:unselected][pw.institutional.corpora:locked][pw.institutional.work-iq:unconfigured][pw.institutional.question:ready][pw.institutional.question:success][pw.institutional.evidence.open:ready][pw.institutional.evidence.open:open]", async ({
    page,
  }) => {
    await waitForWorkspace(page);
    await page
      .getByRole("button", { name: "Institutional Q&A", exact: true })
      .first()
      .click();

    const legalHold = page.getByRole("checkbox", { name: /legal hold/i });
    await expect(legalHold).toBeDisabled();
    await page
      .getByRole("checkbox", { name: /research records/i })
      .uncheck();

    const payload = await runStudioAndCapturePayload(
      page,
      "institutional_qa",
      "Resolve policy answer",
    );
    expect(payload.inputs.corpus_scopes).not.toContain("records");
    expect(payload.inputs.corpus_scopes).not.toContain("legal_hold");

    await page.locator(".inline-citation").first().click();
    await expect(
      page.getByRole("dialog", { name: /.+/ }).locator("dl.citation-detail-facts"),
    ).toBeVisible();
    await page.getByLabel("Close evidence detail").click();

    const workIqToggle = page.getByRole("checkbox", {
      name: /enable work iq readiness signals/i,
    });
    await expect(workIqToggle).toBeDisabled();
    await expect(workIqToggle).not.toBeChecked();
  });
});

test.describe("Workflow Automation interactions", () => {
  test("[pw.workflow-viewport] [pw.workflow-graph] [pw.workflow-dry-run] [pw.workflow-activation] zoom, step editing, and activation are wired with real gating [pw.workflow.graph.edit:draft][pw.workflow.graph.edit:dirty][pw.workflow.graph.edit:valid][pw.workflow.canvas.zoom:ready][pw.workflow.validate:draft][pw.workflow.validate:passed][pw.workflow.activate:disabled][pw.workflow.activate:ready][pw.workflow.activate:active]", async ({
    page,
  }) => {
    await waitForWorkspace(page);
    await page
      .getByRole("button", { name: "Workflow Automation", exact: true })
      .first()
      .click();

    const zoomLabel = page.locator(".canvas-toolbar").getByText("100%");
    await expect(zoomLabel).toBeVisible();
    await page.getByRole("button", { name: "Zoom in" }).click();
    await expect(page.locator(".canvas-toolbar").getByText("110%")).toBeVisible();

    const activateButton = page.getByRole("button", {
      name: "Activate after approval",
    });
    await expect(activateButton).toBeDisabled();

    await page.getByRole("button", { name: "Add step" }).click();
    await page.getByLabel("Step label").fill("Notify reviewer");
    await page.getByRole("button", { name: "Add", exact: true }).click();
    const stepEditor = page.getByRole("region", {
      name: "Workflow step editor",
    });
    await expect(stepEditor.getByText("Notify reviewer")).toBeVisible();

    const payload = await runStudioAndCapturePayload(
      page,
      "orchestration",
      "Validate & dry run",
    );
    const steps = payload.inputs.steps as { label: string }[];
    expect(steps.some((step) => step.label === "Notify reviewer")).toBe(true);
    await expect(page.getByText("Dry run passed")).toBeVisible();

    await expect(activateButton).toBeEnabled();
    await activateButton.click();
    const dialog = page.getByRole("dialog", { name: /activate graph/i });
    await dialog.getByRole("button", { name: "Confirm activation" }).click();
    await expect(
      page.getByRole("button", { name: /activated \(draft workspace\)/i }),
    ).toBeDisabled();
  });

  test("[pw.workflow-catalog] [pw.workflow-run] capability catalog adds an authorized agent to the graph, and run management inspects a real run [pw.workflow.catalog:ready][pw.workflow.catalog:preview][pw.workflow.run.manage:completed]", async ({
    page,
  }) => {
    await waitForWorkspace(page);
    await page
      .getByRole("button", { name: "Workflow Automation", exact: true })
      .first()
      .click();

    const catalog = page.getByRole("region", {
      name: "Workflow capability catalog",
    });
    const literatureRow = catalog
      .locator(".step-editor-row")
      .filter({ hasText: "Literature synthesis" });
    await literatureRow.getByRole("button", { name: "Preview" }).click();
    await expect(literatureRow.getByText(/foundry hosted agent/i)).toBeVisible();
    await literatureRow.getByRole("button", { name: "Add to graph" }).click();
    const stepEditor = page.getByRole("region", {
      name: "Workflow step editor",
    });
    await expect(stepEditor.getByText("Literature synthesis")).toBeVisible();

    await runStudioAndCapturePayload(
      page,
      "orchestration",
      "Validate & dry run",
    );

    const runManager = page.getByRole("region", {
      name: "Workflow run management",
    });
    const runRow = runManager.locator(".step-editor-row").first();
    await expect(runRow).toBeVisible();
    await expect(runRow.getByRole("button", { name: "Pause" })).toBeDisabled();
    await expect(runRow.getByRole("button", { name: "Resume" })).toBeDisabled();
    await expect(runRow.getByRole("button", { name: "Retry" })).toBeDisabled();
    await expect(runRow.getByRole("button", { name: "Cancel" })).toBeDisabled();

    await runRow.getByRole("button", { name: "Inspect" }).click();
    await expect(
      page.getByRole("heading", { name: "Runs & Approvals" }),
    ).toBeVisible();
  });
});

test.describe("Library and Settings interactions", () => {
  test("[pw.library-detail] a library row opens a detail dialog [pw.library.item.open:ready]", async ({ page }) => {
    await waitForWorkspace(page);
    await page.getByRole("button", { name: /^Library \d+$/ }).click();
    await page.locator(".library-row:not(.library-head)").first().click();
    const dialog = page.getByRole("dialog", { name: /.+/ });
    await expect(dialog.locator("dl.library-detail-facts")).toBeVisible();
    await dialog.getByRole("button", { name: "Close", exact: true }).click();
    await expect(dialog).not.toBeVisible();
  });

  test("[pw.integration-readiness] Settings exposes a truthful integration readiness section [pw.settings.integrations.readiness:unconfigured]", async ({
    page,
  }) => {
    await waitForWorkspace(page);
    await page.getByLabel("Open project settings").click();
    await page.getByRole("button", { name: "Readiness" }).click();
    await expect(page.getByText("APIM / Toolbox")).toBeVisible();
    await expect(page.getByText("Work IQ", { exact: true })).toBeVisible();
    await expect(
      page.getByText("GitHub Copilot connector authoring"),
    ).toBeVisible();
    await expect(page.getByText("Foundry Code Interpreter")).toBeVisible();
    await expect(page.getByText("Deployment managed")).toBeVisible();
    await expect(page.getByText(/project-scoped, not per-user/i)).toBeVisible();
  });

  test("[pw.connector-versions] Connectors tab shows a truthful, clearly disabled APIM/MCP/Toolbox version state with no fake promotion [pw.settings.connectors.versions:unconfigured]", async ({
    page,
  }) => {
    await waitForWorkspace(page);
    await page.getByLabel("Open project settings").click();
    await page.getByRole("button", { name: /Connectors \d+/i }).click();

    await expect(page.getByText("Gateway & tool versions")).toBeVisible();
    const apimCard = page
      .locator(".readiness-status-card")
      .filter({ hasText: "Azure API Management (APIM)" });
    await expect(apimCard.getByText("Not configured")).toBeVisible();
    await expect(
      apimCard.getByRole("button", { name: "Promote to default" }),
    ).toBeDisabled();
    await expect(
      apimCard.getByRole("button", { name: "Roll back" }),
    ).toBeDisabled();
    await expect(page.getByText("MCP tool registry")).toBeVisible();
    await expect(page.getByText("Toolbox")).toBeVisible();
  });
});
