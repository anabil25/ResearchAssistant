import { expect, test } from "@playwright/test";

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

test.describe("Institutional Q&A interactions", () => {
  test("shows the Work IQ plugin coming-soon page without retired controls", async ({
    page,
  }) => {
    await waitForWorkspace(page);
    await page
      .getByRole("button", { name: "Institutional Q&A", exact: true })
      .first()
      .click();

    await expect(
      page.getByRole("heading", { name: "Work IQ", level: 1 }),
    ).toBeVisible();
    await expect(page.getByText("Plugin coming soon")).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Resolve policy answer" }),
    ).toHaveCount(0);
  });
});

test.describe("Workflow Automation interactions", () => {
  test("zoom, step editing, and activation are wired with real gating", async ({
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

  test("capability catalog adds an authorized agent to the graph, and run management inspects a real run", async ({
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
  test("[pw.library-detail] a ready library row opens a detail dialog [pw.library.item.open:ready]", async ({ page }) => {
    await waitForWorkspace(page);
    await page.getByRole("button", { name: /^Library \d+$/ }).click();
    await page.locator(".library-row:not(.library-head)").first().click();
    const dialog = page.getByRole("dialog", { name: /.+/ });
    await expect(dialog.locator("dl.library-detail-facts")).toBeVisible();
    await dialog.getByRole("button", { name: "Close", exact: true }).click();
    await expect(dialog).not.toBeVisible();
  });

  test("[pw.integration-readiness] Settings exposes a truthful integration readiness section [pw.settings.integrations.readiness:deployment-managed]", async ({
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
