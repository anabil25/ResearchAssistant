import { expect, test } from "@playwright/test";


test("live workbench loads a Cosmos-backed project without failed requests", async ({
  page,
}) => {
  const failedResponses: string[] = [];
  page.on("response", (response) => {
    if (response.status() >= 400) {
      failedResponses.push(`${response.status()} ${response.url()}`);
    }
  });

  await page.goto("/");
  await expect(
    page.getByRole("banner").getByText("Research command center", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("The research workbench could not load")).toHaveCount(0);
  await expect(page.getByText("Live workspace data is unavailable")).toHaveCount(0);
  expect(failedResponses).toEqual([]);
});


test("removed workbench surfaces stay unreachable", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("button", { name: /Runs & approvals/i })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /Workflow Automation/i })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Search workspace" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /pending approvals/i })).toHaveCount(0);

  await page.keyboard.press("Control+K");
  await expect(page.getByRole("dialog", { name: "Search workspace" })).toHaveCount(0);

  for (const removedView of ["runs", "orchestration"]) {
    await page.goto(`/?view=${removedView}`);
    await expect(
      page.getByRole("banner").getByText("Research command center", { exact: true }),
    ).toBeVisible();
  }
});


test("Project Settings exposes real connection and credential controls", async ({
  page,
}) => {
  await page.goto("/?view=settings");
  await expect(page.getByRole("heading", { name: "Project Settings" })).toBeVisible();

  await page.getByRole("button", { name: /^Connections/ }).click();
  await expect(page.getByRole("heading", { name: "Research connections" })).toBeVisible();
  await expect(
    page.getByText(/API keys are deployment-wide gateway secrets/i),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Test connection" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Save configuration" })).toBeVisible();

  await page
    .getByRole("combobox", { name: "Connection to manage" })
    .selectOption({ label: "Semantic Scholar" });
  await expect(
    page.getByRole("textbox", {
      name: "API key for Semantic Scholar",
    }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Save key" })).toBeDisabled();

  await expect(page.getByText("Gateway & tool versions")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Evaluation" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Readiness" })).toHaveCount(0);
});


test("live literature agent opens a real session and answers", async ({ page }) => {
  await page.goto("/?view=literature");
  await expect(page.getByRole("heading", { name: "Literature Studio" })).toBeVisible();
  await page.getByPlaceholder("Ask the agent anything, or drop a file here").fill(
    "Return one sentence confirming readiness. Do not invent evidence.",
  );
  await page.getByRole("button", { name: "Send" }).click();

  await expect(page.locator(".agent-chat-assistant").last()).toContainText(
    /ready|assist|evidence/i,
    { timeout: 240_000 },
  );
});


for (const binding of [
  { view: "literature", studio: "Literature Studio", agent: "literature-agent" },
  { view: "grant", studio: "Grant Studio", agent: "grant-agent" },
  { view: "matching", studio: "Matching Explorer", agent: "matching-agent" },
  { view: "dataset", studio: "Dataset Lab", agent: "dataset-agent" },
  { view: "screening", studio: "Screening Studio", agent: "screening-agent" },
]) {
  test(`${binding.studio} is bound to ${binding.agent}`, async ({ page }) => {
    await page.goto(`/?view=${binding.view}`);
    await expect(page.getByRole("heading", { name: binding.studio })).toBeVisible();
    await expect(page.getByLabel(`Deployed agent ${binding.agent}`)).toBeVisible();
    await expect(page.getByRole("combobox", { name: "Agent" })).toHaveCount(0);
    await expect(page.getByPlaceholder("Ask the agent anything, or drop a file here")).toBeVisible();
  });
}


test("Institutional Q&A is an intentional responsive preview", async ({ page }) => {
  await page.goto("/?view=institutional_qa");
  const preview = page.locator(".institutional-preview");
  await expect(preview).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Coming soon in preview" }),
  ).toBeVisible();
  await expect(page.getByText("Permission boundary preserved")).toBeVisible();
  await expect(page.getByText("No institutional content is queried or synthesized here yet.")).toBeVisible();
  await expect(page.getByRole("button", { name: /resolve policy answer/i })).toHaveCount(0);

  const overflow = await preview.evaluate(
    (element) => element.scrollWidth > element.clientWidth,
  );
  expect(overflow).toBe(false);
});
