import AxeBuilder from "@axe-core/playwright";
import type { Page, TestInfo } from "@playwright/test";

import {
  CORE_SCREENSHOT_CONTRACTS,
  STATE_SCREENSHOT_IDS,
} from "../src/testing/interaction-manifest";
import { completeWorkspaceRequests, expect, test } from "./fixtures";

test.setTimeout(120_000);

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

async function selectWorkspaceRoute(page: Page, route: string) {
  await page.evaluate((nextRoute) => {
    window.history.pushState({}, "", nextRoute);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }, route);
}

test("[pw.literature-run] captures core and critical states [pw.literature.protocol.run:loading][pw.literature.protocol.run:error][pw.literature.screen.tab:empty]", async ({
  page,
  releaseDiagnostics,
}, testInfo) => {
  await completeWorkspaceRequests(page, () =>
    page.goto(CORE_SCREENSHOT_CONTRACTS[0].route),
  );
  for (const [index, contract] of CORE_SCREENSHOT_CONTRACTS.entries()) {
    if (index > 0) {
      await selectWorkspaceRoute(page, contract.route);
    }
    await expect(page.locator(".workbench-shell")).toHaveAttribute(
      "data-workspace-ready",
      "true",
    );
    await expect(
      page.getByRole("heading", { name: contract.heading, level: 1 }),
    ).toBeVisible();
    await expectAccessible(page);
    await capture(page, testInfo, contract.id);
  }

  await selectWorkspaceRoute(page, "/?view=literature");
  await expect(page.locator(".workbench-shell")).toHaveAttribute(
    "data-workspace-ready",
    "true",
  );
  await expect(page.getByText("No screening run yet")).toBeVisible();
  await capture(page, testInfo, STATE_SCREENSHOT_IDS[0]);

  let releaseFailure: (() => void) | undefined;
  const failureReleased = new Promise<void>((resolve) => {
    releaseFailure = resolve;
  });
  await page.route(
    "**/api/backend/api/studios/literature/run",
    async (route) => {
      await failureReleased;
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({
          detail: "The bounded literature service is unavailable.",
        }),
      });
    },
  );
  releaseDiagnostics.expectConsoleError(
    /status of 503 \(Service Unavailable\)/,
  );

  const runButton = page.getByRole("button", {
    name: "Search & screen evidence",
  });
  await runButton.click();
  await expect(
    page.getByRole("button", { name: "Running workflow..." }),
  ).toBeDisabled();
  await capture(page, testInfo, STATE_SCREENSHOT_IDS[1]);

  releaseFailure?.();
  await expect(page.locator(".error-banner[role='alert']")).toContainText(
    "The bounded literature service is unavailable.",
  );
  await expect(runButton).toBeEnabled();
  await expectAccessible(page);
  await capture(page, testInfo, STATE_SCREENSHOT_IDS[2]);

  await selectWorkspaceRoute(page, "/?view=institutional_qa");
  await expect(page.locator(".workbench-shell")).toHaveAttribute(
    "data-workspace-ready",
    "true",
  );
  await expect(
    page.getByRole("heading", { name: "Work IQ", level: 1 }),
  ).toBeVisible();
  await expect(page.getByText("Plugin coming soon")).toBeVisible();
  await expectAccessible(page);
  await capture(page, testInfo, STATE_SCREENSHOT_IDS[3]);
});
