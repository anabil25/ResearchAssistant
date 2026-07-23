import fs from "node:fs";
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
//
// PRE-INTEGRATION NOTE: this file exercises the honest unavailable/error
// states for endpoints the backend hasn't shipped yet (create, propose/apply
// a builder proposal, fork, memory controls, deploy). That coverage is real
// and valid today, but it cannot substantiate the real happy-path behavior
// of those flows — it only proves the UI degrades honestly. Once the
// the `/v1/agent-studio/...` endpoints land, this suite must be extended with
// real happy-path specs for create/propose/apply/fork/memory/deploy; treat
// the current 404/unavailable-state specs as pre-integration coverage, not
// as a substitute for post-integration acceptance tests.

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
    // The loading state is exercised deterministically in the RTL unit
    // tests (agent-registry.test.tsx) via a controlled deferred promise —
    // against the real, fast local dev backend the 404 can resolve before
    // this transient state is observable, so only the honest final state is
    // asserted here.
    await expect(
      card.getByText("Not available yet", { exact: true }),
    ).toBeVisible({
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
    await openAgentWorkspace(page, "Literature synthesis");
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
    await openAgentWorkspace(page, "Literature synthesis");
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
    await expect(contract.getByText("Safety & public web boundary", { exact: true })).toBeVisible();
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
    await openAgentWorkspace(page, "Literature synthesis");
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

  test("Build tab honestly blocks submission until a real draft loads, showing the pending-backend state", async ({
    page,
  }) => {
    await openAgentWorkspace(page, "Literature synthesis");
    await page.getByRole("tab", { name: "Build" }).click();
    // The real Agent Studio draft endpoint isn't implemented on the backend
    // yet, so the draft fetch fails — submission must stay blocked rather
    // than silently falling back to a fabricated draftId/empty etag.
    await expect(page.getByText("Draft status: unavailable")).toBeVisible({
      timeout: 10_000,
    });
    await expect(
      page.getByText(/couldn't be loaded, so builder changes are disabled/),
    ).toBeVisible();
    await page
      .getByLabel("Describe the change you want")
      .fill("Only cite passages published in the last five years.");
    const submit = page.getByRole("button", { name: "Waiting for draft…" });
    await expect(submit).toBeDisabled();
  });

  test("Test tab runs a real studio request end to end", async ({ page }) => {
    await openAgentWorkspace(page, "Literature synthesis");
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
    await openAgentWorkspace(page, "Literature synthesis");
    await page.getByRole("tab", { name: "Evaluate" }).click();
    await expect(
      page
        .getByLabel("Advisory evaluation")
        .getByText("Not available yet", { exact: true }),
    ).toBeVisible({ timeout: 10_000 });
  });

  test("Deploy tab honestly reports the pending-backend deployment state", async ({
    page,
  }) => {
    await openAgentWorkspace(page, "Literature synthesis");
    await page.getByRole("tab", { name: "Deploy" }).click();
    await expect(
      page.getByLabel("Deployment").getByText("Not available yet", { exact: true }),
    ).toBeVisible({ timeout: 10_000 });
  });

  test("Monitor tab shows real usage counts alongside the honest health-check state", async ({
    page,
  }) => {
    await openAgentWorkspace(page, "Literature synthesis");
    await page.getByRole("tab", { name: "Monitor" }).click();
    await expect(
      page
        .getByLabel("Health and usage")
        .getByText("Not available yet", { exact: true }),
    ).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText("Studio usage", { exact: true })).toBeVisible();
    await expect(
      page.getByText("Workflow usage", { exact: true }),
    ).toBeVisible();
    await expect(page.getByText("Last used", { exact: true })).toBeVisible();
  });

  test("Versions tab shows immutable-baseline copy for platform agents and the honest pending-backend state", async ({
    page,
  }) => {
    await openAgentWorkspace(page, "Literature synthesis");
    await page.getByRole("tab", { name: "Versions" }).click();
    await expect(
      page.getByText(/Every release below is immutable/),
    ).toBeVisible();
    await expect(
      page.getByLabel("Versions").getByText("Not available yet", { exact: true }),
    ).toBeVisible({
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
      "Literature synthesis",
    );

    await page.getByRole("button", { name: "Registry", exact: true }).click();
    await expect(page).toHaveURL(/view=registry/);
    await page.goBack();
    await expect(page.locator(".agent-workspace-header h1")).toContainText(
      "Literature synthesis",
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

  test("renders a real, allowlisted provider-terms link as a ready clickable anchor", async ({
    page,
  }) => {
    await openConnections(page);
    await page.locator(".connector-card").first().click();
    const termsLink = page.getByRole("link", { name: /Provider terms/ });
    await expect(termsLink).toBeVisible();
    await expect(termsLink).toHaveAttribute("data-terms-state", "ready");
    const href = await termsLink.getAttribute("href");
    expect(href).toMatch(/^https:\/\//);
  });

  test("fails closed on an unapproved connector terms URL instead of exposing a raw anchor", async ({
    page,
  }) => {
    // The real backend's connector data is always allowlisted, so this one
    // deterministic edge case (an unapproved host slipping through) is
    // reproduced by rewriting the real `/connectors` response in place,
    // matching the `research-markdown.spec.ts` real-route-rewrite pattern.
    await page.route("**/api/connectors", async (route) => {
      const response = await route.fetch();
      const body = (await response.json()) as Array<Record<string, unknown>>;
      if (body.length > 0) {
        body[0] = { ...body[0], terms_url: "https://evil.example.com/terms" };
      }
      await route.fulfill({ response, json: body });
    });
    await openConnections(page);
    await page.locator(".connector-card").first().click();
    await expect(
      page.getByRole("link", { name: /Provider terms/ }),
    ).not.toBeVisible();
    const blocked = page.getByRole("status", {
      name: "This link targets a host that is not on the approved list.",
    });
    await expect(blocked).toBeVisible();
    await expect(blocked).toHaveAttribute("data-terms-state", "blocked-url");
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
    // Capturing 10 states x 3 viewports (30 mandatory screenshots),
    // including 3 real end-to-end studio runs, genuinely takes longer than
    // the suite's default 30s per-test timeout — this is not masking a
    // hang, just proportional to the larger honest-state surface covered.
    test.setTimeout(120_000);

    // Mandatory release-gate artifact capture: the env var may redirect
    // *where* the 30 screenshots (10 states x 3 viewports) land, but this
    // test must never be silently skipped — a missing UX_SCREENSHOT_DIR
    // falls back to a default in-repo test-results directory rather than
    // skipping.
    const outputDirectory =
      process.env.UX_SCREENSHOT_DIR ??
      path.join(process.cwd(), "test-results", "agent-studio-screenshots");
    fs.mkdirSync(outputDirectory, { recursive: true });

    const capture = async (name: string) => {
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.waitForTimeout(100);
      await page.screenshot({
        path: path.join(outputDirectory, name),
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

      const card = page
        .locator(".agent-registry-card")
        .filter({ hasText: "Literature synthesis" })
        .first();
      await card
        .getByRole("button", { name: /Live evaluation, health & versions/ })
        .click();
      await expect(
        card.getByText("Not available yet", { exact: true }),
      ).toBeVisible({
        timeout: 10_000,
      });
      await capture(`agent-registry-live-error-${tag}.png`);

      await page.getByRole("button", { name: "New agent" }).first().click();
      await capture(`agent-registry-create-panel-${tag}.png`);
      await page.getByRole("button", { name: "Close", exact: true }).click();

      await card.click();
      await capture(`agent-workspace-build-${tag}.png`);

      await page.getByRole("tab", { name: "Test" }).click();
      const studioResponsePromise = page.waitForResponse(
        (response) =>
          response.request().method() === "POST" &&
          response.url().includes("/api/studios/literature/run"),
      );
      await page.getByRole("button", { name: "Search & screen evidence" }).click();
      await studioResponsePromise;
      await expect(page.locator(".screening-record").first()).toBeVisible({
        timeout: 10_000,
      });
      await capture(`agent-workspace-test-${tag}.png`);

      await page.getByRole("tab", { name: "Evaluate" }).click();
      await expect(
        page
          .getByLabel("Advisory evaluation")
          .getByText("Not available yet", { exact: true }),
      ).toBeVisible({
        timeout: 10_000,
      });
      await capture(`agent-workspace-evaluate-error-${tag}.png`);

      await page.getByRole("tab", { name: "Deploy" }).click();
      await expect(
        page.getByLabel("Deployment").getByText("Not available yet", { exact: true }),
      ).toBeVisible({ timeout: 10_000 });
      await capture(`agent-workspace-deploy-error-${tag}.png`);

      await page.getByRole("tab", { name: "Monitor" }).click();
      await expect(
        page
          .getByLabel("Health and usage")
          .getByText("Not available yet", { exact: true }),
      ).toBeVisible({ timeout: 10_000 });
      await capture(`agent-workspace-monitor-${tag}.png`);

      await page.getByRole("tab", { name: "Versions" }).click();
      await expect(
        page.getByLabel("Versions").getByText("Not available yet", { exact: true }),
      ).toBeVisible({ timeout: 10_000 });
      await capture(`agent-workspace-versions-error-${tag}.png`);

      await page.getByRole("button", { name: "Registry", exact: true }).click();
      await ensureNavOpen(page);
      await page.getByRole("button", { name: "Connections" }).click();
      await capture(`connections-${tag}.png`);
    }
  });
});
