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

test.describe("Copilot connector authoring certification", () => {
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
    await expect(page.getByText(/cannot merge, deploy, or promote/i)).toBeVisible();
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
    await expect(page.getByText("OpenAlex configuration saved.")).toBeVisible();

    await page.getByLabel("Connector to manage").selectOption("pubmed");
    await expect(page.getByLabel("Enable PubMed")).toBeDisabled();

    await page.getByLabel("Connector to manage").selectOption("grants_gov");
    await page.getByRole("button", { name: "Test connection" }).click();
    await expect(page.getByText(/Setup required.*provider is not down/i)).toBeVisible();
    await page.getByRole("button", { name: "Test connection" }).click();
    await expect(page.getByText("Bounded connector probe timed out.")).toBeVisible();
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
