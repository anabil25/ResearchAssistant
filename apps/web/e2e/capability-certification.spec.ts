import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type TestInfo } from "@playwright/test";

async function openWorkspace(page: Page) {
  await page.goto("/");
  await expect(page.locator(".workbench-shell")).toHaveAttribute(
    "data-workspace-ready",
    "true",
  );
}

async function capture(page: Page, testInfo: TestInfo, name: string) {
  await page.evaluate(() => window.scrollTo(0, 0));
  const path = testInfo.outputPath(name);
  await page.screenshot({ path, fullPage: true });
  await testInfo.attach(name, { path, contentType: "image/png" });
}

async function expectAccessible(page: Page) {
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations).toEqual([]);
}

test.describe("Dataset Code Interpreter certification", () => {
  test("covers unsupported, JSON-only, approved CSV, upload, and success states", async ({
    page,
  }, testInfo) => {
    await openWorkspace(page);
    await page
      .getByRole("button", { name: "Dataset Lab", exact: true })
      .first()
      .click();

    const analyze = page.getByRole("button", {
      name: "Analyze with Foundry Code Interpreter",
    });
    const approval = page.getByLabel(
      /I approve sending this bounded dataset to the Foundry Dataset Agent/,
    );
    await expect(analyze).toBeDisabled();

    await page.getByLabel("Upload a dataset file").setInputFiles({
      name: "unsupported.xlsx",
      mimeType: "application/octet-stream",
      buffer: Buffer.from("not a supported dataset"),
    });
    await expect(
      page.getByRole("alert").filter({ hasText: /only .csv or .json/i }),
    ).toBeVisible();

    await page.getByLabel("Upload a dataset file").setInputFiles({
      name: "metadata.json",
      mimeType: "application/json",
      buffer: Buffer.from('{"id": 1}'),
    });
    await expect(page.getByText(/JSON preview only uploads to Library/i)).toBeVisible();
    await expect(approval).toBeDisabled();
    await expect(analyze).toBeDisabled();

    await page.getByLabel("Upload a dataset file").setInputFiles({
      name: "approved.csv",
      mimeType: "text/csv",
      buffer: Buffer.from(
        "group,value\ncontrol,10\ncontrol,12\nintervention,14\n",
      ),
    });
    await expect(approval).toBeEnabled();

    const uploadResponse = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().includes("/api/library/upload"),
    );
    await page.getByRole("button", { name: "Upload to Library" }).click();
    expect((await uploadResponse).status()).toBe(200);
    await expect(
      page.getByRole("button", { name: "Uploaded to Library" }),
    ).toBeVisible();

    await approval.check();
    const requestPromise = page.waitForRequest(
      (request) =>
        request.method() === "POST" &&
        request.url().includes("/api/studios/dataset/run"),
    );
    await analyze.click();
    const payload = (await requestPromise).postDataJSON() as {
      inputs: Record<string, unknown>;
    };
    expect(payload.inputs).toEqual(
      expect.objectContaining({
        filename: "approved.csv",
        analysis_approved: true,
        data_classification: "public_or_synthetic",
      }),
    );
    expect(String(payload.inputs.csv_text)).toContain("intervention,14");
    await expect(page.getByText(/Plan approved/i)).toBeVisible();
    await expect(page.getByRole("table")).toBeVisible();
    await expectAccessible(page);
    await capture(page, testInfo, "dataset-approved-success.png");
  });

  test("shows bounded-size and backend failure states without getting stuck", async ({
    page,
  }, testInfo) => {
    await page.route("**/api/backend/api/studios/dataset/run", async (route) => {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({
          detail:
            "Dataset Agent is temporarily unavailable after bounded readiness retries.",
        }),
      });
    });
    await openWorkspace(page);
    await page
      .getByRole("button", { name: "Dataset Lab", exact: true })
      .first()
      .click();

    await page.getByLabel("Upload a dataset file").setInputFiles({
      name: "too-large.csv",
      mimeType: "text/csv",
      buffer: Buffer.alloc(100_001, "a"),
    });
    await expect(
      page.getByRole("alert").filter({ hasText: /limited to 100 KB/i }),
    ).toBeVisible();

    const approval = page.getByLabel(
      /I approve sending this bounded dataset to the Foundry Dataset Agent/,
    );
    await page
      .getByRole("button", { name: "pilot-outcomes.csv", exact: false })
      .click();
    await approval.check();
    const analyze = page.getByRole("button", {
      name: "Analyze with Foundry Code Interpreter",
    });
    await analyze.click();
    await expect(
      page
        .getByRole("alert")
        .filter({
          hasText: /temporarily unavailable after bounded readiness retries/i,
        }),
    ).toBeVisible();
    await expect(analyze).toBeEnabled();
    await capture(page, testInfo, "dataset-bounded-failure.png");
  });
});

test.describe("Copilot connector authoring certification", () => {
  test("covers required policy inputs, cancellation, and reviewed draft creation", async ({
    page,
  }, testInfo) => {
    await openWorkspace(page);
    await page
      .getByRole("button", { name: "Grant Studio", exact: true })
      .first()
      .click();
    await page
      .getByRole("button", { name: "Request a new connector" })
      .click();
    let dialog = page.getByRole("dialog", {
      name: /request a new connector/i,
    });
    await expect(dialog.getByText(/not a Copilot SDK container/i)).toBeVisible();
    await expect(
      dialog.getByRole("button", { name: "Save draft request" }),
    ).toBeVisible();
    expect(
      await dialog.getByLabel("Connector name").evaluate(
        (element) => (element as HTMLInputElement).checkValidity(),
      ),
    ).toBe(false);
    await dialog.getByRole("button", { name: "Cancel" }).click();
    await expect(dialog).not.toBeVisible();

    await page
      .getByRole("button", { name: "Request a new connector" })
      .click();
    dialog = page.getByRole("dialog", {
      name: /request a new connector/i,
    });
    await dialog.getByLabel("Connector name").fill("NSF Awards");
    await dialog.getByLabel("Base URL").fill("https://api.nsf.gov");
    await dialog
      .getByLabel("Authoritative API documentation")
      .fill("https://api.nsf.gov/docs");
    await dialog
      .getByLabel("Terms, license, and robots policy")
      .fill("https://api.nsf.gov/terms");
    await dialog
      .getByLabel("Allowed hosts and path prefixes")
      .fill("api.nsf.gov/v1/");
    await dialog.getByLabel("Authentication").selectOption("None");
    await dialog
      .getByLabel("Sample query and normalized fields")
      .fill("award search -> id,title,url,updated_at");
    await dialog
      .getByLabel("Justification")
      .fill("Needed for federal award discovery.");
    await dialog.getByLabel(/confirmed this use is permitted/i).check();
    await dialog.getByLabel(/generated code requires tests/i).check();
    await dialog.getByRole("button", { name: "Save draft request" }).click();

    await expect(page.getByText("NSF Awards")).toBeVisible();
    await expect(page.getByText("Funding · None")).toBeVisible();
    await expect(page.getByText("Draft — needs review")).toBeVisible();
    await expectAccessible(page);
    await capture(page, testInfo, "copilot-connector-draft.png");
  });

  test("covers every connector health state, filtering, mutation, and test failures", async ({
    page,
  }, testInfo) => {
    const base = {
      description: "Public metadata.",
      auth_kind: "None",
      secret_status: "Not required",
      last_tested_at: null,
      assigned_agents: ["literature"],
      terms_url: "https://example.org/terms",
      data_boundary: "Public metadata only.",
      capabilities: ["Search"],
    };
    const connectors = [
      {
        ...base,
        id: "pubmed",
        name: "PubMed",
        category: "Literature",
        enabled: true,
        test_status: "ready",
      },
      {
        ...base,
        id: "grants_gov",
        name: "Grants.gov",
        category: "Funding",
        enabled: true,
        test_status: "configuration_required",
      },
      {
        ...base,
        id: "openalex",
        name: "OpenAlex",
        category: "Literature",
        enabled: true,
        test_status: "unavailable",
      },
      {
        ...base,
        id: "crossref",
        name: "Crossref",
        category: "Literature",
        enabled: true,
        test_status: "ready_with_key",
      },
      {
        ...base,
        id: "datacite",
        name: "DataCite",
        category: "Datasets",
        enabled: true,
        test_status: "untested",
      },
      {
        ...base,
        id: "disabled-source",
        name: "Disabled Source",
        category: "Identity",
        enabled: false,
        test_status: "ready",
      },
    ];
    let connectorTests = 0;
    await page.route("**/api/backend/api/connectors**", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (request.method() === "GET" && url.pathname.endsWith("/connectors")) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(connectors),
        });
        return;
      }
      if (request.method() === "PUT") {
        const update = request.postDataJSON() as {
          enabled: boolean;
          assigned_agents: string[];
        };
        const selected = connectors.find((item) =>
          url.pathname.endsWith(`/${item.id}`),
        );
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ ...selected, ...update }),
        });
        return;
      }
      if (request.method() === "POST" && url.pathname.endsWith("/test")) {
        connectorTests += 1;
        if (connectorTests === 1) {
          await route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify(connectors[1]),
          });
        } else {
          await route.fulfill({
            status: 503,
            contentType: "application/json",
            body: JSON.stringify({ detail: "Bounded connector probe timed out." }),
          });
        }
        return;
      }
      await route.continue();
    });

    await openWorkspace(page);
    await page.getByLabel("Open project settings").click();
    await page.getByRole("button", { name: "Readiness" }).click();
    await expect(page.getByText(/not an SDK container/i)).toBeVisible();
    await page.getByRole("button", { name: /Connectors 6/i }).click();

    for (const label of [
      "Ready",
      "Setup required",
      "Connection failed",
      "Ready, key recommended",
      "Not tested",
      "Disabled",
    ]) {
      await expect(page.getByText(label, { exact: true }).first()).toBeVisible();
    }

    await page.getByPlaceholder("Search connectors").fill("nothing matches");
    await expect(page.getByText("No connectors match this filter.")).toBeVisible();
    await page.getByPlaceholder("Search connectors").fill("");
    await page.getByRole("button", { name: "Funding", exact: true }).click();
    await expect(page.getByRole("button", { name: /Grants.gov/ })).toBeVisible();
    await page.getByRole("button", { name: "All", exact: true }).click();

    await page.getByLabel("Connector to manage").selectOption("openalex");
    await page.getByLabel("Enable OpenAlex").uncheck();
    await page.getByLabel("Assign grant to OpenAlex").check();
    await page.getByRole("button", { name: "Save configuration" }).click();
    await expect(page.getByRole("status")).toContainText(
      "OpenAlex configuration saved.",
    );

    await page.getByLabel("Connector to manage").selectOption("pubmed");
    await expect(page.getByLabel("Enable PubMed")).toBeDisabled();

    await page.getByLabel("Connector to manage").selectOption("grants_gov");
    await page.getByRole("button", { name: "Test connection" }).click();
    await expect(page.getByRole("status")).toContainText(
      /Setup required.*provider is not down/i,
    );
    await page.getByRole("button", { name: "Test connection" }).click();
    await expect(page.getByRole("status")).toContainText(
      "Bounded connector probe timed out.",
    );
    await expect(page.getByRole("button", { name: "Test connection" })).toBeEnabled();

    await expectAccessible(page);
    await capture(page, testInfo, "connector-management-desktop.png");
    await page.setViewportSize({ width: 390, height: 844 });
    // The evidence rail used to sit off-viewport here. It no longer exists at
    // any width -- run evidence is rendered inline with the artifact instead.
    await expect(page.locator(".evidence-panel")).toHaveCount(0);
    await capture(page, testInfo, "connector-management-mobile.png");
  });
});
