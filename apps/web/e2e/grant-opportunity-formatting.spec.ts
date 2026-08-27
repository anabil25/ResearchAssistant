import { expect, test, type Page, type Route } from "@playwright/test";

const now = "2026-08-27T12:00:00Z";
const canonicalUrl = "https://www.grants.gov/search-results-detail/357744";

const validOpportunity = {
  grants_gov_id: "357744",
  opportunity_number: "RFA-HG-25-009",
  title:
    "Supporting Talented Early Career Researchers in Genomics (R01 Clinical Trial Optional)",
  agency: "National Institutes of Health",
  status: "posted",
  posted_date: "2024-12-16",
  close_date: "2027-02-26",
  archive_date: "2027-04-03",
  canonical_url: canonicalUrl,
  relevance: "unassessed",
  relevance_rationale: "Verified on Grants.gov; review the full notice to confirm project fit.",
  verified_at: now,
};

const malformedOpportunity = {
  ...validOpportunity,
  grants_gov_id: "123",
  opportunity_number: "MALFORMED",
  title: "Record with a mismatched provider URL",
  canonical_url: canonicalUrl,
};

const assistantMessage = {
  id: "reply-browser-test-message",
  role: "assistant",
  content: "RFA-HG-25-009 is the strongest verified match. Grants.gov record 357744 is currently posted.",
  created_at: now,
  agent_name: "grant-agent",
  attachments: [],
  activity: [],
  duration_ms: 812,
  source_count: 1,
  opportunities: [validOpportunity, malformedOpportunity],
};

function json(route: Route, body: unknown): Promise<void> {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockGrantChat(page: Page): Promise<void> {
  let completed = false;
  await page.route("**/api/backend/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;

    if (path === "/api/backend/api/projects") {
      await json(route, [
        {
          id: "project-browser-test",
          name: "Browser test project",
          description: "Structured grant rendering",
          is_active: true,
          source_count: 0,
          active_runs: 0,
        },
      ]);
      return;
    }
    if (path === "/api/backend/api/workspace") {
      await json(route, {
        library_items: 0,
        active_runs: 0,
        pending_approvals: 0,
        connector_ready: 1,
        connector_total: 1,
        last_activity_at: now,
        persistence: "browser-test",
      });
      return;
    }
    if (["library", "runs", "approvals", "connectors", "agents", "workflows"].some(
      (resource) => path === `/api/backend/api/${resource}`,
    )) {
      await json(route, []);
      return;
    }
    if (path === "/api/backend/api/settings") {
      await json(route, {
        project_id: "project-browser-test",
        name: "Browser test project",
        description: "Structured grant rendering",
        default_classification: "internal",
        retention_days: 365,
        citation_coverage_threshold: 1,
        require_human_approval: true,
        allowed_export_destinations: [],
        model_profile: "balanced",
      });
      return;
    }
    if (path === "/api/backend/api/agent-chat/agents") {
      await json(route, [
        {
          name: "grant-agent",
          label: "Grant agent",
          description: "Finds and verifies funding opportunities.",
        },
      ]);
      return;
    }

    const thread = {
      id: "thread-browser-test",
      capability: "grant",
      agent_name: "grant-agent",
      created_at: now,
      updated_at: now,
      messages: completed
        ? [
            {
              id: "browser-test-message",
              role: "user",
              content: "Find a genomics grant.",
              created_at: now,
              agent_name: null,
              attachments: [],
              activity: [],
              duration_ms: null,
              source_count: 0,
              opportunities: [],
            },
            assistantMessage,
          ]
        : [],
      attachments: [],
    };

    if (path === "/api/backend/api/agent-chat/threads" && request.method() === "POST") {
      await json(route, thread);
      return;
    }
    if (path.endsWith("/messages/stream") && request.method() === "POST") {
      completed = true;
      const body = [
        `event: started\ndata: ${JSON.stringify({
          type: "started",
          message_id: assistantMessage.id,
          agent_name: "grant-agent",
          created_at: now,
        })}\n\n`,
        `event: completed\ndata: ${JSON.stringify({
          type: "completed",
          message: assistantMessage,
        })}\n\n`,
      ].join("");
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: { "Cache-Control": "no-cache" },
        body,
      });
      return;
    }
    if (path === "/api/backend/api/agent-chat/threads/thread-browser-test") {
      await json(route, thread);
      return;
    }

    await route.fulfill({ status: 404, contentType: "application/json", body: "{}" });
  });
}

test("verified grants render as exact responsive policy-approved links", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "chromium", "Desktop and mobile are checked in one run.");
  await mockGrantChat(page);

  await page.goto("/?view=grant");
  await expect(page.getByRole("heading", { name: "Grant Studio" })).toBeVisible();
  await page.getByPlaceholder("Ask the agent anything, or drop a file here").fill(
    "Find a genomics grant.",
  );
  await page.getByRole("button", { name: "Send" }).click();

  const results = page.getByRole("region", { name: "Verified grant opportunities" });
  await expect(results).toBeVisible();
  await expect(results.getByText("1 result")).toBeVisible();
  await expect(results.getByRole("columnheader", { name: "Opportunity" })).toBeVisible();
  await expect(results.getByRole("columnheader", { name: "Agency" })).toBeVisible();
  await expect(results.getByRole("columnheader", { name: "Availability" })).toBeVisible();
  await expect(results.getByRole("columnheader", { name: "Fit" })).toBeVisible();
  const link = results.getByRole("link", {
    name: /RFA-HG-25-009/,
  });
  await expect(link).toHaveAttribute("href", canonicalUrl);
  await expect(link).toHaveAttribute("target", "_blank");
  await expect(link).toHaveCSS("color", "rgb(40, 95, 134)");
  await link.focus();
  await expect(link).toBeFocused();
  await expect(results.getByText("MALFORMED", { exact: false })).toHaveCount(0);
  await expect(results).toContainText("National Institutes of Health");
  await expect(results).toContainText("Closes Feb 26, 2027");
  await expect(results).toContainText("Review fit");
  await expect(results).toContainText(validOpportunity.title);
  const analysis = page.getByText("Analysis and limitations");
  await expect(analysis).toBeVisible();
  await expect(page.locator(".agent-chat-analysis")).not.toHaveAttribute("open", "");
  await analysis.click();
  const answer = page.locator(".agent-chat-answer").last();
  await expect(answer.getByRole("link", { name: /RFA-HG-25-009/ })).toHaveAttribute(
    "href",
    canonicalUrl,
  );
  await expect(answer.getByRole("link", { name: /357744/ })).toHaveAttribute(
    "href",
    canonicalUrl,
  );
  await expect(page.locator(".agent-chat-assistant").last()).not.toContainText(canonicalUrl);

  const desktopOverflow = await results.evaluate(
    (element) => element.scrollWidth > element.clientWidth + 1,
  );
  expect(desktopOverflow).toBe(false);
  await page.screenshot({
    path: "test-results/grant-opportunities-desktop.png",
    fullPage: true,
  });

  await page.setViewportSize({ width: 390, height: 844 });
  await results.scrollIntoViewIfNeeded();
  const mobileOverflow = await page.evaluate(() => {
    const viewportWidth = document.documentElement.clientWidth;
    return Array.from(
      document.querySelectorAll<HTMLElement>(
        ".grant-results, .grant-results-table, .grant-results-table tr, .grant-results-table td",
      ),
    ).filter((element) => {
      const rect = element.getBoundingClientRect();
      return rect.left < -1 || rect.right > viewportWidth + 1 || element.scrollWidth > element.clientWidth + 1;
    }).length;
  });
  expect(mobileOverflow).toBe(0);
  await page.screenshot({
    path: "test-results/grant-opportunities-mobile.png",
    fullPage: true,
  });
});