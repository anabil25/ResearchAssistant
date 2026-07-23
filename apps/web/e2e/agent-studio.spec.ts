import path from "node:path";

import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

// The Agent Registry / Agent Workspace / Connections backend contract
// (agent health, evaluation, versions, manifest proposals, drafts, forks) is
// proposed but not yet implemented by the real API — see
// `src/lib/api.ts`'s "PENDING BACKEND" section. These specs run against the
// real dev backend (no route mocking) so the "Not available yet" banners
// asserted below are the genuine, honest response to a real 404 — exactly
// the state a researcher will see today, and the state that will
// automatically start showing real data once the backend ships.

async function waitForWorkspace(page: Page) {
  await page.goto("/");
  await expect(page.locator(".workbench-shell")).toHaveAttribute(
    "data-workspace-ready",
    "true",
  );
}

async function ensureNavOpen(page: Page) {
  const mobileMenuButton = page.getByLabel("Open navigation");
  if (await mobileMenuButton.isVisible().catch(() => false)) {
    await mobileMenuButton.click();
  }
}

async function openRegistry(page: Page) {
  await waitForWorkspace(page);
  await ensureNavOpen(page);
  await page.getByRole("button", { name: "Agent Registry" }).click();
  await expect(
    page.getByRole("heading", { name: "Agent Registry", level: 1 }),
  ).toBeVisible();
}

async function openConnections(page: Page) {
  await waitForWorkspace(page);
  await ensureNavOpen(page);
  await page.getByRole("button", { name: "Connections" }).click();
  await expect(
    page.getByRole("heading", { name: "Connections", level: 1 }),
  ).toBeVisible();
}

async function openAgentWorkspace(page: Page, cardName: RegExp | string) {
  await openRegistry(page);
  await page
    .locator(".agent-registry-card-main")
    .filter({ hasText: cardName })
    .first()
    .click();
  await expect(
    page.getByRole("heading", { name: "Agent Workspace" }).or(
      page.locator(".agent-workspace-header h1"),
    ).first(),
  ).toBeVisible();
}

test.describe("Agent Registry", () => {
  test("shows all nine system agents with the same behavioral contract fields", async ({
    page,
  }) => {
    await openRegistry(page);
    await expect(page.locator(".agent-registry-card")).toHaveCount(9);

    const first = page.locator(".agent-registry-card").first();
    await expect(first.getByText(/Platform-owned|Researcher-owned/)).toBeVisible();
    await expect(first.locator(".agent-registry-lifecycle")).toBeVisible();
    await expect(first.getByText("Discovered project model")).toBeVisible();
    await expect(first.getByText("Studio usage")).toBeVisible();
    await expect(first.getByText("Workflow usage")).toBeVisible();
    await expect(
      first.getByRole("button", { name: /Live evaluation, health & versions/ }),
    ).toBeVisible();

    await expect(page.getByRole("heading", { name: "Your agents" })).toBeVisible();
    await expect(page.getByText("No custom agents yet")).toBeVisible();
  });

  test("search narrows results and the owner filter empties the grid honestly", async ({
    page,
  }) => {
    await openRegistry(page);
    await page
      .getByPlaceholder("Search agents by name or purpose")
      .fill("literature");
    await expect(page.locator(".agent-registry-card")).toHaveCount(2);

    await page.getByPlaceholder("Search agents by name or purpose").fill("");
    await page
      .locator(".filter-pills")
      .getByRole("button", { name: "Researcher", exact: true })
      .click();
    await expect(
      page.getByText("No agents match this filter"),
    ).toBeVisible();

    await page
      .locator(".filter-pills")
      .getByRole("button", { name: "All", exact: true })
      .click();
    await expect(page.locator(".agent-registry-card")).toHaveCount(9);
  });

  test("New agent panel supports both creation paths and surfaces the pending-backend state honestly", async ({
    page,
  }) => {
    await openRegistry(page);
    await page.getByRole("button", { name: "New agent" }).first().click();
    const panel = page.getByRole("region", { name: "Create agent" });
    await expect(panel.getByText("Start from a task template or a blank")).toBeVisible();

    // Template path: switch the template capability, then submit.
    await expect(panel.getByRole("button", { name: "Task template" })).toHaveAttribute(
      "data-active",
      "true",
    );
    await panel.locator("select").selectOption("grant");
    await panel
      .getByPlaceholder(/Summarize weekly IRB submissions/)
      .fill("Draft a grant-matching assistant for our lab.");
    await panel.getByRole("button", { name: /Propose|Create/ }).click();
    await expect(
      panel.getByText("Agent creation isn't available yet"),
    ).toBeVisible({ timeout: 10_000 });

    // Blank conversational intent path.
    await panel.getByRole("button", { name: "Blank conversational intent" }).click();
    await expect(
      panel.getByText("Describe what you want this agent to do"),
    ).toBeVisible();
    await panel
      .getByPlaceholder("e.g. Summarize weekly IRB submissions and flag missing consent language.")
      .fill("Build an assistant that reconciles consent language across sites.");

    await panel.getByRole("button", { name: "Close" }).click();
    await expect(panel).not.toBeVisible();
  });

  test("expanding live evaluation, health, and versions on a system agent shows the honest not-yet-available state", async ({
    page,
  }) => {
    await openRegistry(page);
    const card = page.locator(".agent-registry-card").first();
    await card
      .getByRole("button", { name: /Live evaluation, health & versions/ })
      .click();
    await expect(card.getByText("Checking live evaluation")).toBeVisible();
    await expect(card.getByText("Not available yet")).toBeVisible({
      timeout: 10_000,
    });
  });

  test("forking a platform agent surfaces the pending-backend state honestly", async ({
    page,
  }) => {
    await openRegistry(page);
    const card = page.locator(".agent-registry-card").first();
    await card.getByRole("button", { name: "Fork for my workspace" }).click();
    await expect(card.locator(".agent-registry-fork-result")).toContainText(
      /isn't implemented yet|Not available/i,
      { timeout: 10_000 },
    );
  });

  test("clicking an agent card opens its workspace and Back returns to the registry", async ({
    page,
  }) => {
    await openAgentWorkspace(page, "literature-agent");
    await expect(page).toHaveURL(/view=agent&agentId=literature\b/);
    await page.getByRole("button", { name: "Registry", exact: true }).click();
    await expect(
      page.getByRole("heading", { name: "Agent Registry", level: 1 }),
    ).toBeVisible();
  });
});

test.describe("Agent Workspace", () => {
  test("renders the always-visible behavioral contract and progressively discloses Advanced", async ({
    page,
  }) => {
    await openAgentWorkspace(page, "literature-agent");
    const contract = page.getByLabel("Behavioral contract");
    await expect(contract.getByText("Purpose", { exact: true })).toBeVisible();
    await expect(
      contract.getByText("Input & artifact", { exact: true }),
    ).toBeVisible();
    await expect(
      contract.getByText("Instructions", { exact: true }),
    ).toBeVisible();
    await expect(
      contract.getByText("Evidence & citations", { exact: true }),
    ).toBeVisible();
    await expect(contract.getByText("Knowledge", { exact: true })).toBeVisible();
    await expect(contract.getByText("Tools", { exact: true })).toBeVisible();
    await expect(contract.getByText("Memory", { exact: true })).toBeVisible();
    await expect(contract.getByText("Connections", { exact: true })).toBeVisible();
    await expect(
      contract.getByText("Specialists", { exact: true }),
    ).toBeVisible();
    await expect(contract.getByText("Safety", { exact: true })).toBeVisible();
    await expect(contract.getByText("Tests", { exact: true })).toBeVisible();
    await expect(
      contract.getByText("Deployment", { exact: true }),
    ).toBeVisible();

    const advancedToggle = page.getByRole("button", { name: "Advanced" });
    await expect(advancedToggle).toHaveAttribute("aria-expanded", "false");
    await expect(
      contract.getByText("Output schema", { exact: true }),
    ).not.toBeVisible();
    await advancedToggle.click();
    await expect(advancedToggle).toHaveAttribute("aria-expanded", "true");
    await expect(
      contract.getByText("Output schema", { exact: true }),
    ).toBeVisible();
    await expect(contract.getByText("Runtime", { exact: true })).toBeVisible();
    await expect(contract.getByText("Identity", { exact: true })).toBeVisible();
  });

  test("switches across all six tabs", async ({ page }) => {
    await openAgentWorkspace(page, "literature-agent");
    for (const label of [
      "Build",
      "Test",
      "Evaluate",
      "Deploy",
      "Monitor",
      "Versions",
    ]) {
      await page.getByRole("tab", { name: label }).click();
      await expect(page.getByRole("tab", { name: label })).toHaveAttribute(
        "aria-selected",
        "true",
      );
    }
  });

  test("Build tab proposes a typed manifest change and shows the honest pending-backend state", async ({
    page,
  }) => {
    await openAgentWorkspace(page, "literature-agent");
    await page.getByRole("tab", { name: "Build" }).click();
    await page
      .getByLabel("Describe the change you want")
      .fill("Only cite passages published in the last five years.");
    await page.getByRole("button", { name: /Propose change/ }).click();
    await expect(
      page.getByText(/isn't available yet|proposal/i).first(),
    ).toBeVisible({ timeout: 10_000 });
  });

  test("Test tab runs a real studio request end to end", async ({ page }) => {
    await openAgentWorkspace(page, "literature-agent");
    await page.getByRole("tab", { name: "Test" }).click();
    const responsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().includes("/api/studios/literature/run"),
    );
    await page.getByRole("button", { name: "Search & screen evidence" }).click();
    const response = await responsePromise;
    expect(response.status()).toBe(200);
    await expect(page.locator(".screening-record").first()).toBeVisible({
      timeout: 10_000,
    });
  });

  test("Evaluate tab honestly reports the advisory evaluation backend isn't available yet", async ({
    page,
  }) => {
    await openAgentWorkspace(page, "literature-agent");
    await page.getByRole("tab", { name: "Evaluate" }).click();
    await expect(page.getByText("Not available yet")).toBeVisible({
      timeout: 10_000,
    });
  });

  test("Deploy tab requests deployment and honestly reports the pending-backend state", async ({
    page,
  }) => {
    await openAgentWorkspace(page, "literature-agent");
    await page.getByRole("tab", { name: "Deploy" }).click();
    await expect(page.getByText("Current status")).toBeVisible();
    await page.getByRole("button", { name: /Request deployment/ }).click();
    await expect(page.getByText("Not available yet")).toBeVisible({
      timeout: 10_000,
    });
  });

  test("Monitor tab shows real usage counts alongside the honest health-check state", async ({
    page,
  }) => {
    await openAgentWorkspace(page, "literature-agent");
    await page.getByRole("tab", { name: "Monitor" }).click();
    await expect(page.getByText("Not available yet")).toBeVisible({
      timeout: 10_000,
    });
    await expect(page.getByText("Studio usage", { exact: true })).toBeVisible();
    await expect(
      page.getByText("Workflow usage", { exact: true }),
    ).toBeVisible();
    await expect(page.getByText("Last used", { exact: true })).toBeVisible();
  });

  test("Versions tab shows immutable-baseline copy for platform agents and the honest pending-backend state", async ({
    page,
  }) => {
    await openAgentWorkspace(page, "literature-agent");
    await page.getByRole("tab", { name: "Versions" }).click();
    await expect(
      page.getByText(/This agent's baseline is immutable/),
    ).toBeVisible();
    await expect(page.getByText("Not available yet")).toBeVisible({
      timeout: 10_000,
    });
  });

  test("direct links to a specific agent workspace survive reload and back/forward", async ({
    page,
  }) => {
    await page.goto("/?view=agent&agentId=literature");
    await expect(page.locator(".workbench-shell")).toHaveAttribute(
      "data-workspace-ready",
      "true",
    );
    await expect(page.locator(".agent-workspace-header h1")).toContainText(
      "literature-agent",
    );

    await page.getByRole("button", { name: "Registry", exact: true }).click();
    await expect(page).toHaveURL(/view=registry/);
    await page.goBack();
    await expect(page.locator(".agent-workspace-header h1")).toContainText(
      "literature-agent",
    );
  });
});

test.describe("Connections", () => {
  test("lists workspace connectors grouped by category with assigned-specialist context", async ({
    page,
  }) => {
    await openConnections(page);
    await expect(page.locator(".connector-card")).not.toHaveCount(0);
    await expect(page.getByText("Assigned specialists").first()).toBeVisible();
  });

  test("selecting a connector reveals the manager panel with enable, specialist assignment, and test controls", async ({
    page,
  }) => {
    await openConnections(page);
    await page.locator(".connector-card").first().click();
    await expect(page.getByText("Enable connection")).toBeVisible();
    await expect(page.getByLabel("Connection to manage")).toBeVisible();
    await expect(page.getByText("Assigned agents")).toBeVisible();
  });

  test("toggling enable and saving persists through the real connectors API", async ({
    page,
  }) => {
    await openConnections(page);
    // Crossref (unlike PubMed/Grants.gov) is not a required baseline
    // connection, so its enable checkbox isn't disabled.
    await page
      .locator(".connector-card")
      .filter({ hasText: "Crossref" })
      .click();

    const enableToggle = page.getByLabel("Enable Crossref");
    const wasEnabled = await enableToggle.isChecked();
    await enableToggle.click();
    expect(await enableToggle.isChecked()).toBe(!wasEnabled);

    const putPromise = page.waitForResponse(
      (response) =>
        response.request().method() === "PUT" &&
        response.url().includes("/api/connectors/crossref"),
    );
    await page.getByRole("button", { name: "Save configuration" }).click();
    const putResponse = await putPromise;
    expect(putResponse.status()).toBe(200);

    // Restore original state so the suite is idempotent across reruns.
    await enableToggle.click();
    const restorePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "PUT" &&
        response.url().includes("/api/connectors/crossref"),
    );
    await page.getByRole("button", { name: "Save configuration" }).click();
    await restorePromise;
    expect(await enableToggle.isChecked()).toBe(wasEnabled);
  });

  test("testing a connection calls the real test endpoint and returns a concrete status", async ({
    page,
  }) => {
    await openConnections(page);
    await page.locator(".connector-card").first().click();
    const testPromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        /\/api\/connectors\/.+\/test$/.test(response.url()),
    );
    await page.getByRole("button", { name: "Test connection" }).click();
    const response = await testPromise;
    expect(response.status()).toBe(200);
    await expect(
      page.getByRole("button", { name: "Test connection" }),
    ).toBeEnabled({ timeout: 10_000 });
  });
});

test.describe("Agent Studio accessibility and responsive layout", () => {
  test("Agent Registry, Agent Workspace, and Connections pass automated WCAG checks", async ({
    page,
  }) => {
    const audit = async () => {
      const results = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
        .analyze();
      expect(results.violations).toEqual([]);
    };

    await openRegistry(page);
    await audit();

    await page.locator(".agent-registry-card-main").first().click();
    await audit();

    await page.getByRole("button", { name: "Registry", exact: true }).click();
    await page.getByRole("button", { name: "Connections" }).click();
    await audit();
  });

  test("Agent Registry and Agent Workspace remain usable on tablet and mobile viewports", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 768, height: 1024 });
    await openRegistry(page);
    await expect(page.locator(".agent-registry-card")).toHaveCount(9);

    await page.setViewportSize({ width: 390, height: 844 });
    await openRegistry(page);
    await expect(page.locator(".agent-registry-card")).toHaveCount(9);
    await page.locator(".agent-registry-card-main").first().click();
    await expect(page.locator(".agent-workspace-header h1")).toBeVisible();
  });

  test("capture Agent Studio surfaces at desktop, tablet, and mobile", async ({
    page,
  }) => {
    const outputDirectory = process.env.UX_SCREENSHOT_DIR;
    test.skip(!outputDirectory, "Screenshot directory not requested.");

    const capture = async (name: string) => {
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.waitForTimeout(100);
      await page.screenshot({
        path: path.join(outputDirectory!, name),
        fullPage: true,
      });
    };

    for (const [tag, size] of [
      ["desktop", { width: 1536, height: 1000 }],
      ["tablet", { width: 768, height: 1024 }],
      ["mobile", { width: 390, height: 844 }],
    ] as const) {
      await page.setViewportSize(size);
      await openRegistry(page);
      await capture(`agent-registry-${tag}.png`);

      const card = page.locator(".agent-registry-card").first();
      await card
        .getByRole("button", { name: /Live evaluation, health & versions/ })
        .click();
      await expect(card.getByText("Not available yet")).toBeVisible({
        timeout: 10_000,
      });
      await capture(`agent-registry-live-error-${tag}.png`);

      await page.getByRole("button", { name: "New agent" }).first().click();
      await capture(`agent-registry-create-panel-${tag}.png`);
      await page.getByRole("button", { name: "Close", exact: true }).click();

      await card.click();
      await capture(`agent-workspace-build-${tag}.png`);

      await page.getByRole("tab", { name: "Evaluate" }).click();
      await expect(page.getByText("Not available yet")).toBeVisible({
        timeout: 10_000,
      });
      await capture(`agent-workspace-evaluate-error-${tag}.png`);

      await page.getByRole("button", { name: "Registry", exact: true }).click();
      await ensureNavOpen(page);
      await page.getByRole("button", { name: "Connections" }).click();
      await capture(`connections-${tag}.png`);
    }
  });
});
