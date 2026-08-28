import { expect, test, type APIRequestContext } from "@playwright/test";

const grantsGovId = "357744";
const canonicalUrl = `https://www.grants.gov/search-results-detail/${grantsGovId}`;

type JsonRecord = Record<string, unknown>;

const privateReplyPattern =
  /authorized_connector_ids|principal_id|project_id|selected_opportunities|sensitivity|session_files|session_id|tenant_id|your reply did not match/i;

type GrantOracle = {
  opportunityNumber: string;
  title: string;
  agency: string;
  status: string;
  postedDate: string | null;
  closeDate: string | null;
  archiveDate: string | null;
};

function record(value: unknown, label: string): JsonRecord {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`${label} was not a JSON object.`);
  }
  return value as JsonRecord;
}

function requiredText(value: unknown, label: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`${label} was missing.`);
  }
  return value.trim();
}

function grantDate(value: unknown): string | null {
  const match = String(value ?? "").trim().match(/^(\d{4}-\d{2}-\d{2})(?:-|$)/);
  return match?.[1] ?? null;
}

async function grantsGovOracle(request: APIRequestContext): Promise<GrantOracle> {
  const response = await request.post(
    "https://api.grants.gov/v1/api/fetchOpportunity",
    { data: { opportunityId: Number(grantsGovId) } },
  );
  const raw = await response.text();
  expect(response.ok(), raw).toBe(true);
  const payload = record(JSON.parse(raw), "Grants.gov response");
  expect(payload.errorcode).toBe(0);
  const data = record(payload.data, "Grants.gov opportunity");
  expect(String(data.id)).toBe(grantsGovId);
  const agencyDetails = record(data.agencyDetails, "Grants.gov agency");
  const synopsis = record(data.synopsis, "Grants.gov synopsis");
  return {
    opportunityNumber: requiredText(data.opportunityNumber, "Opportunity number"),
    title: requiredText(data.opportunityTitle, "Opportunity title"),
    agency: requiredText(agencyDetails.agencyName, "Opportunity agency"),
    status: requiredText(data.ost, "Opportunity status").toLocaleLowerCase("en-US"),
    postedDate: grantDate(synopsis.postingDateStr),
    closeDate: grantDate(synopsis.responseDateStr),
    archiveDate: grantDate(synopsis.archiveDateStr),
  };
}

async function readyProject(request: APIRequestContext): Promise<string> {
  const projectsResponse = await request.get("/api/backend/api/projects");
  expect(projectsResponse.ok(), await projectsResponse.text()).toBe(true);
  const projects = (await projectsResponse.json()) as JsonRecord[];
  let project = projects.find((item) => item.is_active === true);
  if (!project) {
    const created = await request.post("/api/backend/api/projects", {
      data: {
        name: "Deployment verification workspace",
        description: "Private workspace for deterministic release verification.",
      },
    });
    expect(created.ok(), await created.text()).toBe(true);
    project = record(await created.json(), "Created release project");
  }
  const projectId = requiredText(project.id, "Active project ID");
  const tested = await request.post(
    "/api/backend/api/connectors/grants_gov/test",
    { headers: { "X-Research-Project-ID": projectId } },
  );
  const testedRaw = await tested.text();
  expect(tested.ok(), testedRaw).toBe(true);
  const connector = record(JSON.parse(testedRaw), "Grants.gov connector");
  expect(connector.required).toBe(true);
  expect(connector.enabled).toBe(true);
  expect(connector.test_status).toMatch(/^ready(?:_with_key)?$/);
  expect(connector.assigned_agents).toContain("grant");
  return projectId;
}

test("live grant conversation renders the exact linked record on two turns", async ({
  page,
  request,
}) => {
  test.setTimeout(600_000);
  const oracle = await grantsGovOracle(request);
  const projectId = await readyProject(request);
  const failedResponses: string[] = [];
  const consoleErrors: string[] = [];
  page.on("response", (response) => {
    if (response.status() >= 400) {
      failedResponses.push(`${response.status()} ${response.url()}`);
    }
  });
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });

  await page.goto("/?view=grant");
  await expect(page.getByRole("heading", { name: "Grant Studio" })).toBeVisible();
  const prompts = [
    `Look up Grants.gov opportunity ID ${grantsGovId}. Return exactly this one opportunity only after the exact Grants.gov lookup succeeds; do not substitute another opportunity.`,
    "In this same conversation, re-check the opportunity from my immediately preceding request against Grants.gov and return exactly that one verified opportunity again. Use the conversation context; do not substitute a different opportunity.",
  ];
  const composer = page.getByPlaceholder("Ask the agent anything, or drop a file here");
  const allResults = page.getByRole("region", { name: "Verified grant opportunities" });
  let threadPath: string | null = null;
  const clientMessageIds = new Set<string>();

  for (const [turnIndex, prompt] of prompts.entries()) {
    const streamResponse = page.waitForResponse((response) =>
      new URL(response.url()).pathname.endsWith("/messages/stream"),
    );
    await composer.fill(prompt);
    await page.getByRole("button", { name: "Send" }).click();

    await expect(allResults).toHaveCount(turnIndex + 1, { timeout: 480_000 });
    const results = allResults.nth(turnIndex);
    await expect(results.locator("tbody tr")).toHaveCount(1);
    const link = results.getByRole("link", {
      name: oracle.opportunityNumber,
      exact: false,
    });
    await expect(link).toHaveAttribute("href", canonicalUrl);
    await expect(link).toHaveAttribute("target", "_blank");
    await expect(results).toContainText(oracle.title);
    await expect(results).toContainText(oracle.agency);
    await expect(results).toContainText(oracle.status, { ignoreCase: true });

    const streamed = await streamResponse;
    expect(streamed.status()).toBe(200);
    const streamPath = new URL(streamed.url()).pathname;
    const currentThreadPath = streamPath.replace(/\/messages\/stream$/, "");
    if (threadPath === null) threadPath = currentThreadPath;
    expect(currentThreadPath).toBe(threadPath);
    const requestPayload = record(
      streamed.request().postDataJSON(),
      "Stream request payload",
    );
    const clientMessageId = requiredText(
      requestPayload.client_message_id,
      "Client message ID",
    );
    expect(clientMessageIds.has(clientMessageId)).toBe(false);
    clientMessageIds.add(clientMessageId);
    const streamBody = await streamed.text();
    expect(streamBody).toContain("event: started");
    expect(streamBody).toContain("event: completed");
    expect(streamBody).toContain(`"grants_gov_id":"${grantsGovId}"`);
    expect(streamBody).toContain(`"posted_date":${JSON.stringify(oracle.postedDate)}`);
    expect(streamBody).toContain(`"close_date":${JSON.stringify(oracle.closeDate)}`);
    expect(streamBody).toContain(`"archive_date":${JSON.stringify(oracle.archiveDate)}`);
    expect(streamBody).not.toContain("event: text_delta");
    expect(streamBody).not.toMatch(privateReplyPattern);
    const assistantText = await page.locator(".agent-chat-assistant").last().innerText();
    expect(assistantText).not.toMatch(privateReplyPattern);
    expect(assistantText.trimStart()).not.toMatch(/^[{[]/);

    const replay = await request.post(streamPath, {
      data: requestPayload,
      headers: { "X-Research-Project-ID": projectId },
    });
    const replayBody = await replay.text();
    expect(replay.ok(), replayBody).toBe(true);
    expect(replayBody).toContain("event: completed");
    expect(replayBody).not.toContain("event: started");
    expect(replayBody).toContain(`"id":"reply-${clientMessageId}"`);

    const persisted = await request.get(currentThreadPath, {
      headers: { "X-Research-Project-ID": projectId },
    });
    expect(persisted.ok(), await persisted.text()).toBe(true);
    const persistedThread = record(await persisted.json(), "Persisted chat thread");
    expect(Array.isArray(persistedThread.messages)).toBe(true);
    expect((persistedThread.messages as unknown[]).length).toBe((turnIndex + 1) * 2);
  }

  expect(clientMessageIds.size).toBe(2);

  const results = allResults.last();

  await page.screenshot({
    path: "test-results/live-grant-release-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await results.scrollIntoViewIfNeeded();
  const overflow = await page.evaluate(() => {
    const viewportWidth = document.documentElement.clientWidth;
    return Array.from(
      document.querySelectorAll<HTMLElement>(
        ".grant-results, .grant-results-table, .grant-results-table tr, .grant-results-table td",
      ),
    ).filter((element) => {
      const bounds = element.getBoundingClientRect();
      return (
        bounds.left < -1 ||
        bounds.right > viewportWidth + 1 ||
        element.scrollWidth > element.clientWidth + 1
      );
    }).length;
  });
  expect(overflow).toBe(0);
  await page.screenshot({
    path: "test-results/live-grant-release-mobile.png",
    fullPage: true,
  });

  expect(failedResponses).toEqual([]);
  expect(consoleErrors).toEqual([]);
});