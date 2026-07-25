import AxeBuilder from "@axe-core/playwright";
import type { Page, TestInfo } from "@playwright/test";

import { expect, test } from "./fixtures";

async function gotoView(page: Page, view: string) {
  await page.goto(`/?view=${view}`);
  await expect(page.locator(".workbench-shell")).toHaveAttribute(
    "data-workspace-ready",
    "true",
  );
}

async function runStudioAndCapturePayload(
  page: Page,
  capability: string,
  buttonName: string,
) {
  const requestPromise = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      request.url().includes(`/api/studios/${capability}/run`),
  );
  const responsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().includes(`/api/studios/${capability}/run`),
  );
  await page.getByRole("button", { name: buttonName }).click();
  const request = await requestPromise;
  const response = await responsePromise;
  expect(response.status()).toBe(200);
  return request.postDataJSON() as {
    objective: string;
    online_research: boolean;
    inputs: Record<string, unknown>;
  };
}

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

test.describe("[pw.matching-filters] matching record-type checkboxes send an empty selection", () => {
  test("[pw.matching-filters] unchecking every record-type checkbox sends an empty record_kinds array [pw.matching.need.entity-types:empty]", async ({
    page,
  }) => {
    await gotoView(page, "matching");
    // Only People/Facilities/Equipment are checked by default
    // (studio-components.tsx: RECORD_TYPE_OPTIONS.slice(0, 3)); Methods and
    // Templates already start unchecked.
    await page.getByRole("checkbox", { name: "People" }).uncheck();
    await page.getByRole("checkbox", { name: "Facilities" }).uncheck();
    await page.getByRole("checkbox", { name: "Equipment" }).uncheck();

    const payload = await runStudioAndCapturePayload(
      page,
      "matching",
      "Build verified shortlist",
    );
    expect(payload.inputs.record_kinds).toEqual([]);
  });
});

test.describe("[pw.matching-run] matching run failure surfaces across every submitting control", () => {
  test("[pw.matching-run] a failed run keeps the button disabled while loading and reports the error for every input feeding the request [pw.matching.run:loading][pw.matching.run:error]", async ({
    page,
    releaseDiagnostics,
  }, testInfo) => {
    await gotoView(page, "matching");

    let releaseFailure: (() => void) | undefined;
    const failureReleased = new Promise<void>((resolve) => {
      releaseFailure = resolve;
    });
    await page.route(
      "**/api/backend/api/studios/matching/run",
      async (route) => {
        await failureReleased;
        await route.fulfill({
          status: 503,
          contentType: "application/json",
          body: JSON.stringify({
            detail: "The bounded matching service is unavailable.",
          }),
        });
      },
    );
    releaseDiagnostics.expectConsoleError(
      /status of 503 \(Service Unavailable\)/,
    );

    const runButton = page.getByRole("button", {
      name: "Build verified shortlist",
    });
    await runButton.click();
    await expect(
      page.getByRole("button", { name: "Running workflow..." }),
    ).toBeDisabled();
    await capture(page, testInfo, "state-data-matching-run-loading");

    releaseFailure?.();
    await expect(page.locator(".error-banner[role='alert']")).toContainText(
      "The bounded matching service is unavailable.",
    );
    await expect(runButton).toBeEnabled();
    await expectAccessible(page);
    await capture(page, testInfo, "state-data-matching-run-error");
  });
});

test.describe("[pw.matching-sources] matching Work IQ source stays permanently unavailable", () => {
  test("[pw.matching-sources] the Work IQ collaboration source is visibly unchecked and disabled pending tenant consent [pw.matching.need.sources:consent-required]", async ({
    page,
  }, testInfo) => {
    await gotoView(page, "matching");

    const workIqToggle = page.getByRole("checkbox", {
      name: /work iq collaboration signals/i,
    });
    await expect(workIqToggle).toBeVisible();
    await expect(workIqToggle).toBeDisabled();
    await expect(workIqToggle).not.toBeChecked();
    await expect(
      page.getByText(
        "Disabled — requires tenant Microsoft Graph consent this workspace has not been granted.",
      ),
    ).toBeVisible();
    await capture(page, testInfo, "state-data-matching-sources-consent-required");
    await expectAccessible(page);
  });
});

test.describe("[pw.matching-run] matching run is keyboard-activatable", () => {
  test("[pw.matching-run] the run button submits via focus and Enter [pw.matching.run:keyboard]", async ({
    page,
  }) => {
    await gotoView(page, "matching");
    const responsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().includes("/api/studios/matching/run"),
    );
    const runButton = page.getByRole("button", {
      name: "Build verified shortlist",
    });
    await runButton.focus();
    await page.keyboard.press("Enter");
    const response = await responsePromise;
    expect(response.status()).toBe(200);
    await expect(page.locator(".match-card").first()).toBeVisible();
  });
});

test.describe("[pw.matching-results] matching candidate selection by pointer and keyboard", () => {
  test("[pw.matching-results] clicking a candidate card selects it and updates the score explainer [pw.matching.result.select:selected]", async ({
    page,
  }, testInfo) => {
    await gotoView(page, "matching");
    await runStudioAndCapturePayload(page, "matching", "Build verified shortlist");

    const cards = page.locator(".match-card");
    // The demo repository is seeded with exactly one person, facility,
    // equipment, and template chunk (fixtures.py), so the deterministic
    // ranked search (service.py._matching) always returns 4 candidates.
    await expect(cards).toHaveCount(4);

    const secondCard = cards.nth(1);
    const secondName = await secondCard.locator(".match-copy strong").textContent();
    await secondCard.locator(".match-select").click();

    await expect(secondCard).toHaveAttribute("data-active", "true");
    await expect(cards.nth(0)).toHaveAttribute("data-active", "false");
    await expect(secondCard.locator(".match-select")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await expect(
      page.locator(".score-explainer").getByRole("heading", { level: 2 }),
    ).toHaveText(secondName ?? "");
    await capture(page, testInfo, "state-data-matching-result-selected");
    await expectAccessible(page);
  });

  test("[pw.matching-results] a candidate card is selectable via focus and Enter [pw.matching.result.select:keyboard]", async ({
    page,
  }) => {
    await gotoView(page, "matching");
    await runStudioAndCapturePayload(page, "matching", "Build verified shortlist");

    const cards = page.locator(".match-card");
    await expect(cards).toHaveCount(4);

    const targetCard = cards.nth(2);
    const targetName = await targetCard.locator(".match-copy strong").textContent();
    await targetCard.locator(".match-select").focus();
    await page.keyboard.press("Enter");

    await expect(targetCard).toHaveAttribute("data-active", "true");
    await expect(targetCard.locator(".match-select")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await expect(
      page.locator(".score-explainer").getByRole("heading", { level: 2 }),
    ).toHaveText(targetName ?? "");
  });
});

test.describe("[pw.matching-shortlist] matching shortlist comparison is keyboard-activatable", () => {
  test("[pw.matching-shortlist] the compare-shortlisted toggle activates via focus and Enter [pw.matching.compare-shortlist:keyboard]", async ({
    page,
  }, testInfo) => {
    await gotoView(page, "matching");
    await runStudioAndCapturePayload(page, "matching", "Build verified shortlist");

    await page.locator(".shortlist-toggle").first().click();
    const compareButton = page.getByRole("button", {
      name: "Compare shortlisted",
    });
    await expect(compareButton).toBeVisible();
    await compareButton.focus();
    await page.keyboard.press("Enter");

    await expect(page.locator(".shortlist-compare[role='table']")).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Hide comparison" }),
    ).toBeVisible();
    await capture(page, testInfo, "state-data-matching-compare-keyboard");
    await expectAccessible(page);
  });
});

test.describe("[pw.dataset-upload] dataset upload starts empty", () => {
  test("[pw.dataset-upload] the upload tile is pristine before any file is chosen [pw.dataset.upload:empty]", async ({
    page,
  }, testInfo) => {
    await gotoView(page, "dataset");
    const tile = page.locator(".asset-upload-tile");
    await expect(tile).toHaveAttribute("data-read-status", "idle");
    await expect(tile).toHaveAttribute("data-active", "false");
    await expect(tile).toContainText("Upload a dataset");
    await expect(tile).toContainText("CSV or JSON · up to 5 MB");
    await capture(page, testInfo, "state-data-dataset-upload-empty");
    await expectAccessible(page);
  });
});

test.describe("[pw.dataset-plan] dataset analysis objective editing", () => {
  test("[pw.dataset-plan] the objective field shows its default, accepts keyboard typing, and is submitted verbatim [pw.dataset.objective:ready][pw.dataset.objective:keyboard]", async ({
    page,
  }) => {
    await gotoView(page, "dataset");
    const field = page.getByRole("textbox", { name: "Analysis objective" });
    await expect(field).toHaveValue(
      "Profile the pilot outcome dataset and plan a descriptive group comparison.",
    );

    await field.fill("");
    await field.focus();
    await page.keyboard.type("Keyboard-typed dataset objective.");
    await expect(field).toHaveValue("Keyboard-typed dataset objective.");

    await page
      .getByRole("checkbox", { name: /I approve sending this bounded dataset/ })
      .check();
    const payload = await runStudioAndCapturePayload(
      page,
      "dataset",
      "Analyze with Foundry Code Interpreter",
    );
    expect(payload.objective).toBe("Keyboard-typed dataset objective.");
  });
});

test.describe("[pw.dataset-profile] dataset run is keyboard-activatable", () => {
  test("[pw.dataset-profile] the run button submits via focus and Enter once approved [pw.dataset.profile:keyboard]", async ({
    page,
  }) => {
    await gotoView(page, "dataset");
    await page
      .getByRole("checkbox", { name: /I approve sending this bounded dataset/ })
      .check();

    const responsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().includes("/api/studios/dataset/run"),
    );
    const runButton = page.getByRole("button", {
      name: "Analyze with Foundry Code Interpreter",
    });
    await runButton.focus();
    await page.keyboard.press("Enter");
    const response = await responsePromise;
    expect(response.status()).toBe(200);
  });
});

test.describe("[pw.dataset-profile] dataset run failure surfaces across the submitting controls", () => {
  test("[pw.dataset-profile] a failed run keeps the button disabled while loading and reports the error [pw.dataset.profile:loading][pw.dataset.profile:error][pw.dataset.execution:running]", async ({
    page,
    releaseDiagnostics,
  }, testInfo) => {
    await gotoView(page, "dataset");
    await page
      .getByRole("checkbox", { name: /I approve sending this bounded dataset/ })
      .check();

    let releaseFailure: (() => void) | undefined;
    const failureReleased = new Promise<void>((resolve) => {
      releaseFailure = resolve;
    });
    await page.route(
      "**/api/backend/api/studios/dataset/run",
      async (route) => {
        await failureReleased;
        await route.fulfill({
          status: 503,
          contentType: "application/json",
          body: JSON.stringify({
            detail: "The bounded dataset service is unavailable.",
          }),
        });
      },
    );
    releaseDiagnostics.expectConsoleError(
      /status of 503 \(Service Unavailable\)/,
    );

    const runButton = page.getByRole("button", {
      name: "Analyze with Foundry Code Interpreter",
    });
    await runButton.click();
    await expect(
      page.getByRole("button", { name: "Running workflow..." }),
    ).toBeDisabled();
    await capture(page, testInfo, "state-data-dataset-run-loading");

    releaseFailure?.();
    await expect(page.locator(".error-banner[role='alert']")).toContainText(
      "The bounded dataset service is unavailable.",
    );
    await expect(runButton).toBeEnabled();
    await expectAccessible(page);
    await capture(page, testInfo, "state-data-dataset-run-error");
  });
});

test.describe("[pw.dataset-plan] dataset plan approval resets when the asset changes", () => {
  test("[pw.dataset-plan] approval starts pending and returns to pending after switching assets [pw.dataset.plan.approve:draft]", async ({
    page,
  }, testInfo) => {
    await gotoView(page, "dataset");
    const chip = page.locator(".analysis-notebook .subtle-chip");
    await expect(chip).toHaveText("Pending approval");
    await capture(page, testInfo, "state-data-dataset-plan-draft");
    await expectAccessible(page);

    const approvalCheckbox = page.getByRole("checkbox", {
      name: /I approve sending this bounded dataset/,
    });
    await approvalCheckbox.check();
    await expect(chip).toHaveText("Plan approved");

    await page
      .locator(".asset-picker button", {
        hasText: "clinical-events-archive.parquet",
      })
      .click();
    await expect(approvalCheckbox).not.toBeChecked();
    await expect(chip).toHaveText("Pending approval");
  });
});

test.describe("[pw.dataset-plan] dataset large-asset submission requires human approval", () => {
  test("[pw.dataset-plan] a large asset run reports it is waiting for human approval [pw.dataset.execution:waiting-for-approval]", async ({
    page,
  }, testInfo) => {
    await gotoView(page, "dataset");
    await page
      .locator(".asset-picker button", {
        hasText: "clinical-events-archive.parquet",
      })
      .click();
    await page
      .getByRole("checkbox", { name: /I approve sending this bounded dataset/ })
      .check();

    await runStudioAndCapturePayload(
      page,
      "dataset",
      "Analyze with Foundry Code Interpreter",
    );

    await expect(page.locator(".approval-needed")).toContainText(
      "Human approval required before submit",
    );
    await expect(page.locator(".status-chip")).toHaveText("waiting for approval");
    await capture(page, testInfo, "state-data-dataset-plan-waiting-for-approval");
    await expectAccessible(page);
  });
});

test.describe("[pw.dataset-upload] dataset execution is blocked for un-profiled uploads", () => {
  test("[pw.dataset-upload] uploading a JSON file that skips profiling reports a blocked execution [pw.dataset.execution:blocked]", async ({
    page,
  }, testInfo) => {
    await gotoView(page, "dataset");
    await page.setInputFiles('input[aria-label="Upload a dataset file"]', {
      name: "small-preview.json",
      mimeType: "application/json",
      buffer: Buffer.from(JSON.stringify({ rows: [{ id: 1, outcome: "improved" }] })),
    });

    const tile = page.locator(".asset-upload-tile", {
      hasText: "small-preview.json",
    });
    await expect(tile).toHaveAttribute("data-read-status", "ready");
    await page
      .getByRole("checkbox", { name: /I approve sending this bounded dataset/ })
      .check();

    await runStudioAndCapturePayload(
      page,
      "dataset",
      "Analyze with Foundry Code Interpreter",
    );

    await expect(page.locator(".status-chip")).toHaveText("blocked");
    await expect(page.getByText("Estimate only · no profile")).toBeVisible();
    await expect(page.getByText("Asset not profiled")).toBeVisible();
    await expect(page.locator(".local-compute")).toContainText(
      "Safe for bounded local computation",
    );
    await capture(page, testInfo, "state-data-dataset-execution-blocked");
    await expectAccessible(page);
  });
});

test.describe("[pw.dataset-upload] dataset upload readiness and race guards", () => {
  test("[pw.dataset.upload:reading] a stale reader from a superseded file selection never overwrites the newer file's status or content [pw.dataset.upload:validated]", async ({
    page,
  }, testInfo) => {
    // Gate FileReader.readAsText behind test-controlled promises (no fixed
    // timers/sleeps) so the test can deterministically resolve the *second*
    // file's read before the *first* (now-stale) file's read, reproducing
    // the exact out-of-order race a rapid reselection can trigger. Each
    // reader instance also tracks its own settle event so the test can wait
    // for the stale reader's real completion attempt before asserting it
    // had no effect.
    await page.addInitScript(() => {
      const OriginalFileReader = window.FileReader;
      const releaseGates: Array<() => void> = [];
      let callIndex = -1;
      (window as unknown as { __releaseFileReader: (index: number) => void }).__releaseFileReader = (
        index: number,
      ) => {
        releaseGates[index]?.();
      };
      class GatedFileReader extends OriginalFileReader {
        constructor() {
          super();
          const bump = () => {
            const win = window as unknown as { __fileReaderSettledCount?: number };
            win.__fileReaderSettledCount = (win.__fileReaderSettledCount ?? 0) + 1;
          };
          this.addEventListener("load", bump);
          this.addEventListener("error", bump);
          this.addEventListener("abort", bump);
        }
        override readAsText(...args: Parameters<FileReader["readAsText"]>) {
          const index = ++callIndex;
          const gate = new Promise<void>((resolve) => {
            releaseGates[index] = resolve;
          });
          void gate.then(() => {
            OriginalFileReader.prototype.readAsText.apply(this, args);
          });
        }
      }
      window.FileReader = GatedFileReader;
    });
    await gotoView(page, "dataset");

    const approvalCheckbox = page.getByRole("checkbox", {
      name: /I approve sending this bounded dataset/,
    });
    const runButton = page.getByRole("button", {
      name: "Analyze with Foundry Code Interpreter",
    });
    const tile = page.locator(".asset-upload-tile");

    await page.setInputFiles('input[aria-label="Upload a dataset file"]', {
      name: "first-selection.csv",
      mimeType: "text/csv",
      buffer: Buffer.from("id,outcome\n1,first-file-content\n"),
    });
    await expect(tile).toHaveAttribute("data-read-status", "reading");
    await expect(tile).toContainText("first-selection.csv");

    // Rapidly reselect a different file before the first read resolves.
    await page.setInputFiles('input[aria-label="Upload a dataset file"]', {
      name: "second-selection.csv",
      mimeType: "text/csv",
      buffer: Buffer.from("id,outcome\n2,second-file-content\n"),
    });
    await expect(tile).toHaveAttribute("data-read-status", "reading");
    await expect(tile).toContainText("second-selection.csv");
    await capture(page, testInfo, "state-data-dataset-upload-race-reading");

    // Resolve the newer (second) file's read first.
    await page.evaluate(() => (window as unknown as { __releaseFileReader: (i: number) => void }).__releaseFileReader(1));
    await expect(tile).toHaveAttribute("data-read-status", "ready");
    await expect(tile).toContainText("second-selection.csv");
    await expect(page.locator(".error-banner")).toHaveCount(0);

    // Now resolve the stale first reader out of order. Its onload still
    // fires for real; the guard must ignore it so the UI keeps showing the
    // newer file untouched.
    await page.evaluate(() => (window as unknown as { __releaseFileReader: (i: number) => void }).__releaseFileReader(0));
    await page.waitForFunction(
      () =>
        (window as unknown as { __fileReaderSettledCount?: number })
          .__fileReaderSettledCount === 2,
    );
    await expect(tile).toHaveAttribute("data-read-status", "ready");
    await expect(tile).toContainText("second-selection.csv");
    await expect(page.locator(".error-banner")).toHaveCount(0);
    await capture(page, testInfo, "state-data-dataset-upload-race-settled");
    await expectAccessible(page);

    await approvalCheckbox.check();
    await expect(runButton).toBeEnabled();
    const payload = await runStudioAndCapturePayload(
      page,
      "dataset",
      "Analyze with Foundry Code Interpreter",
    );
    expect(payload.inputs.filename).toBe("second-selection.csv");
    expect(payload.inputs.csv_text).toContain("second-file-content");
    expect(payload.inputs.csv_text).not.toContain("first-file-content");
  });

  test("[pw.dataset.upload:error] a failed CSV read keeps the run blocked even after approval, until a newly selected file becomes ready [pw.dataset.upload:validated]", async ({
    page,
  }, testInfo) => {
    // Gate only the *first* CSV read's error dispatch behind a
    // test-controlled promise (no fixed timers/sleeps), so "reading" ->
    // "error" is deterministically observable and controllable. Later reads
    // (the recovery file selected after the error) fall through to the
    // real FileReader so the "ready" transition proves recovery genuinely
    // works, not just that the mock allows it.
    await page.addInitScript(() => {
      const OriginalFileReader = window.FileReader;
      let callIndex = -1;
      class GatedErrorFileReader extends OriginalFileReader {
        override readAsText(...args: Parameters<FileReader["readAsText"]>) {
          const index = ++callIndex;
          if (index > 0) {
            OriginalFileReader.prototype.readAsText.apply(this, args);
            return;
          }
          const gate = new Promise<void>((resolve) => {
            (
              window as unknown as { __releaseErrorReader?: () => void }
            ).__releaseErrorReader = resolve;
          });
          void gate.then(() => {
            this.dispatchEvent(new ProgressEvent("error"));
          });
        }
      }
      window.FileReader = GatedErrorFileReader;
    });
    await gotoView(page, "dataset");

    const approvalCheckbox = page.getByRole("checkbox", {
      name: /I approve sending this bounded dataset/,
    });
    const runButton = page.getByRole("button", {
      name: "Analyze with Foundry Code Interpreter",
    });
    const tile = page.locator(".asset-upload-tile");

    await page.setInputFiles('input[aria-label="Upload a dataset file"]', {
      name: "broken.csv",
      mimeType: "text/csv",
      buffer: Buffer.from("id,outcome\n1,improved\n"),
    });
    await expect(tile).toHaveAttribute("data-read-status", "reading");

    await page.evaluate(() =>
      (
        window as unknown as { __releaseErrorReader: () => void }
      ).__releaseErrorReader(),
    );
    await expect(tile).toHaveAttribute("data-read-status", "error");
    await expect(
      page.getByText(/this csv file could not be read/i),
    ).toBeVisible();

    // Real defect fix: approving the plan must not unblock a run whose CSV
    // failed to read. Previously runDisabled only checked
    // csvReadStatus === "reading", so an "error" status plus an approved
    // plan silently enabled a submit that would carry no csv_text.
    await approvalCheckbox.check();
    await expect(runButton).toBeDisabled();
    await capture(page, testInfo, "state-data-dataset-upload-error-blocked");
    await expectAccessible(page);

    // Recovery: selecting a new, valid file must still be able to reach
    // "ready" and unblock the run once approved again.
    await page.setInputFiles('input[aria-label="Upload a dataset file"]', {
      name: "recovered.csv",
      mimeType: "text/csv",
      buffer: Buffer.from("id,outcome\n1,recovered-content\n"),
    });
    await expect(tile).toHaveAttribute("data-read-status", "ready");
    await expect(page.locator(".error-banner")).toHaveCount(0);
    await approvalCheckbox.check();
    await expect(runButton).toBeEnabled();

    const payload = await runStudioAndCapturePayload(
      page,
      "dataset",
      "Analyze with Foundry Code Interpreter",
    );
    expect(payload.inputs.filename).toBe("recovered.csv");
    expect(payload.inputs.csv_text).toContain("recovered-content");
  });
});

test.describe("[pw.institutional-corpora] institutional corpus checkboxes send an empty selection", () => {
  test("[pw.institutional-corpora] unchecking every unlocked corpus sends an empty corpus_scopes array [pw.institutional.corpora:empty]", async ({
    page,
  }, testInfo) => {
    await gotoView(page, "institutional_qa");
    await page.getByRole("checkbox", { name: /IRB & human subjects/ }).uncheck();
    await page.getByRole("checkbox", { name: /^Research records/ }).uncheck();
    await page.getByRole("checkbox", { name: /Data governance/ }).uncheck();
    await capture(page, testInfo, "state-data-institutional-corpora-empty");
    await expectAccessible(page);

    const payload = await runStudioAndCapturePayload(
      page,
      "institutional_qa",
      "Resolve policy answer",
    );
    expect(payload.inputs.corpus_scopes).toEqual([]);
  });
});

test.describe("[pw.institutional-answer] institutional run failure surfaces across the submitting controls", () => {
  test("[pw.institutional-answer] a failed run keeps the button disabled while loading and reports the error [pw.institutional.question:loading][pw.institutional.question:error]", async ({
    page,
    releaseDiagnostics,
  }, testInfo) => {
    await gotoView(page, "institutional_qa");

    let releaseFailure: (() => void) | undefined;
    const failureReleased = new Promise<void>((resolve) => {
      releaseFailure = resolve;
    });
    await page.route(
      "**/api/backend/api/studios/institutional_qa/run",
      async (route) => {
        await failureReleased;
        await route.fulfill({
          status: 503,
          contentType: "application/json",
          body: JSON.stringify({
            detail: "The bounded institutional service is unavailable.",
          }),
        });
      },
    );
    releaseDiagnostics.expectConsoleError(
      /status of 503 \(Service Unavailable\)/,
    );

    const runButton = page.getByRole("button", {
      name: "Resolve policy answer",
    });
    await runButton.click();
    await expect(
      page.getByRole("button", { name: "Running workflow..." }),
    ).toBeDisabled();
    await capture(page, testInfo, "state-data-institutional-run-loading");

    releaseFailure?.();
    await expect(page.locator(".error-banner[role='alert']")).toContainText(
      "The bounded institutional service is unavailable.",
    );
    await expect(runButton).toBeEnabled();
    await expectAccessible(page);
    await capture(page, testInfo, "state-data-institutional-run-error");
  });
});

test.describe("[pw.institutional-answer] institutional run is keyboard-activatable", () => {
  test("[pw.institutional-answer] the run button submits via focus and Enter, not a newline in the question [pw.institutional.question:keyboard]", async ({
    page,
  }) => {
    await gotoView(page, "institutional_qa");
    const responsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().includes("/api/studios/institutional_qa/run"),
    );
    const runButton = page.getByRole("button", {
      name: "Resolve policy answer",
    });
    await runButton.focus();
    await page.keyboard.press("Enter");
    const response = await responsePromise;
    expect(response.status()).toBe(200);
  });
});
