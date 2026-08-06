import { expect, test, type Page } from "@playwright/test";


const TURN_TIMEOUT = 300_000;

interface TurnResult {
  answer: string;
  facts: string;
  tools: string[];
}

async function sendTurn(page: Page, turn: number, prompt: string): Promise<TurnResult> {
  const usersBefore = await page.locator(".agent-chat-user").count();
  const assistantsBefore = await page.locator(".agent-chat-assistant").count();
  const composer = page.getByPlaceholder("Ask the agent anything, or drop a file here");

  await composer.fill(prompt);
  await page.getByRole("button", { name: "Send" }).click();

  const live = page.locator('.agent-chat-assistant[data-live="true"]');
  const failure = page.getByRole("alert").filter({ hasText: "Hosted Agent" });
  await expect(live.or(failure)).toBeVisible({ timeout: 30_000 });
  await expect(failure, `turn ${turn} failed before streaming`).toHaveCount(0);
  await expect(live.getByText("Live", { exact: true })).toBeVisible();

  await expect(live, `turn ${turn} never reached a terminal state`).toHaveCount(0, {
    timeout: TURN_TIMEOUT,
  });
  await expect(failure, `turn ${turn} ended with a Hosted Agent error`).toHaveCount(0);
  await expect(page.locator(".agent-chat-user")).toHaveCount(usersBefore + 1);
  await expect(page.locator(".agent-chat-assistant")).toHaveCount(assistantsBefore + 1);

  const assistant = page.locator(".agent-chat-assistant").last();
  const answer = assistant.getByRole("region", { name: /response$/i });
  await expect(answer).toBeVisible();
  const answerText = (await answer.innerText()).trim();
  expect(answerText.length, `turn ${turn} returned an empty answer`).toBeGreaterThan(40);

  const activity = assistant.locator("details.agent-chat-activity");
  await expect(activity).toBeVisible();
  const facts = (await activity.locator(".agent-chat-activity-facts").innerText()).trim();
  await activity.locator("summary").click();
  const activityText = await activity.locator(".agent-chat-activity-body").innerText();
  expect(activityText).not.toMatch(
    /call_id|encrypted_content|reasoning_content|"arguments"\s*:|"output"\s*:/i,
  );
  const tools = await activity.locator(".agent-chat-activity-copy strong").allInnerTexts();
  if (tools.length > 0) {
    await expect(activity.locator(".agent-chat-activity-status").last()).toContainText(
      /completed/i,
    );
  } else {
    expect(facts).toMatch(/^Direct response\b/i);
  }
  await activity.locator("summary").click();

  console.log(JSON.stringify({ turn, facts, tools, answerCharacters: answerText.length }));
  return { answer: answerText, facts, tools };
}

test("local Screening completes a five-turn governed research conversation", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "chromium", "One real five-turn run is sufficient.");
  test.setTimeout(1_500_000);

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

  await page.goto("/?view=screening");
  await expect(page.getByRole("heading", { name: "Screening Studio" })).toBeVisible();
  await expect(page.getByLabel("Deployed agent screening-agent")).toBeVisible();

  const results: TurnResult[] = [];
  results.push(
    await sendTurn(
      page,
      1,
      "Use Web Search to identify current authoritative research on adult obesity population trends in the United States. Give two stable source URLs and distinguish measured prevalence from projections. Do not invent evidence.",
    ),
  );
  results.push(
    await sendTurn(
      page,
      2,
      "Using the population trend from your previous answer, use PubMed through Tool Search and Call Tool to find one systematic review published from 2023 through 2026 about adult obesity prevalence. Return its PMID and DOI and explain how it relates to the earlier sources.",
    ),
  );
  results.push(
    await sendTurn(
      page,
      3,
      "Use Crossref through Tool Search and Call Tool to verify the Ward projection article from turn 1: title, publication year, journal, and DOI 10.1056/NEJMsa1909301. Compare the metadata with your earlier answer and report any mismatch rather than guessing.",
    ),
  );
  results.push(
    await sendTurn(
      page,
      4,
      "Use ClinicalTrials.gov through Tool Search and Call Tool to find one recruiting interventional study of a GLP-1 medicine for adults with obesity. Return the NCT identifier, recruitment status, key eligibility, and stable source URL.",
    ),
  );
  results.push(
    await sendTurn(
      page,
      5,
      "Using only the sources and findings already discussed, synthesize a concise evidence map. Separate measured prevalence evidence, projected prevalence evidence, bibliographic verification, and treatment-trial evidence. Do not use a new tool unless the existing conversation is insufficient. Preserve the unresolved systematic-review and recruiting-trial gaps, and list limitations.",
    ),
  );

  expect(results[0].tools).toContain("Web Search");
  await expect(page.getByRole("link", { name: /cdc\.gov/i }).first()).toBeVisible();
  await expect(page.getByText(/Blocked URL: https:\/\/(?:www\.)?cdc\.gov/i)).toHaveCount(0);
  expect(results[1].tools.join(" ")).toMatch(/Tool Search|Pubmed \/ Search|Research Connector/i);
  expect(results[2].tools.join(" ")).toMatch(/Tool Search|Crossref \/ Search|Research Connector/i);
  expect(results[3].tools.join(" ")).toMatch(/Tool Search|Clinical Trials \/ Search|Research Connector/i);
  expect(results[4].answer).toMatch(/population|prevalence/i);
  expect(results[4].answer).toMatch(/trial|treatment/i);
  expect(results[4].answer).toMatch(/uncertain|unresolved|limitation/i);

  await page.screenshot({ path: "test-results/screening-five-turn-local.png", fullPage: true });
  expect(failedResponses).toEqual([]);
  expect(consoleErrors).toEqual([]);
});