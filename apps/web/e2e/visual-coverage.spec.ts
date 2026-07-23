import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type TestInfo } from "@playwright/test";

import {
  CORE_SCREENSHOT_CONTRACTS,
  STATE_SCREENSHOT_IDS,
} from "../src/testing/interaction-manifest";

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

test("[pw.visual-states] captures core, empty, loading, and error states", async ({
  page,
}, testInfo) => {
  for (const contract of CORE_SCREENSHOT_CONTRACTS) {
    await page.goto(contract.route);
    await expect(page.locator(".workbench-shell")).toHaveAttribute(
      "data-workspace-ready",
      "true",
    );
    await expect(
      page.getByRole("heading", { name: contract.heading, level: 1 }),
    ).toBeVisible();
    await capture(page, testInfo, contract.id);
  }

  await page.goto("/?view=literature");
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
  await expectAccessible(page);
  await capture(page, testInfo, STATE_SCREENSHOT_IDS[2]);
});
