import AxeBuilder from "@axe-core/playwright";
import type { Page, TestInfo } from "@playwright/test";

import { expect, test } from "./fixtures";

// This spec closes the remaining explicit Playwright state-token gaps for
// Library, Runs, and Approvals (plus two shared route/error behaviors) that
// are reachable in production code but had no truthful `[pw.<id>:<state>]`
// token anywhere in e2e/. Every state below is backed by a specific,
// cited code path -- see the manifest evidence comments in
// `src/testing/interaction-manifest.ts` for `library.item.open`,
// `approvals.rationale`, and `approvals.decide` for the states that were
// removed as impossible rather than added here.

async function waitForWorkspace(page: Page) {
  await page.goto("/");
  await expect(page.locator(".workbench-shell")).toHaveAttribute(
    "data-workspace-ready",
    "true",
  );
}

async function gotoView(page: Page, view: string) {
  await page.goto(`/?view=${view}`);
  await expect(page.locator(".workbench-shell")).toHaveAttribute(
    "data-workspace-ready",
    "true",
  );
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

function libraryItem(overrides: {
  id: string;
  status: string;
  title: string;
}) {
  return {
    id: overrides.id,
    title: overrides.title,
    description:
      "State-coverage fixture item used only to exercise a specific, real backend status.",
    kind: "Paper",
    source: "Fixture provider",
    provider: "Fixture provider",
    connector: "fixture-connector",
    access: "internal",
    version: "1.0",
    license: "Project supplied",
    checksum: "sha256-state-fixture",
    evidence_count: 4,
    added_at: new Date().toISOString(),
    tags: ["fixture"],
    status: overrides.status,
  };
}

async function mockLibrary(page: Page, items: unknown[]) {
  await page.route("**/api/backend/api/library", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(items),
    });
  });
}

// Fixture run/approval pair reused across the Runs/Approvals decision tests
// below. Named distinctly from `coverage-gap-closing.spec.ts`'s
// `FIXED_RUN_WAITING`/`FIXED_APPROVAL` (each e2e spec file defines its own
// local fixtures rather than sharing a module, matching the existing
// convention in this suite).
const STATE_FIXTURE_RUN_WAITING = {
  id: "state-fixture-run-waiting",
  durable_instance_id: "research-state-fixture-run-waiting",
  project_id: "proj-demo",
  capability: "grant",
  title: "State-coverage fixture run awaiting review",
  status: "waiting_for_approval",
  progress: 82,
  current_stage: "Reviewer approval",
  owner: "Dr. Reviewer",
  started_at: new Date().toISOString(),
  completed_at: null,
  artifact_count: 3,
  scheduler_managed: false,
  approval_id: "state-fixture-approval-1",
};
const STATE_FIXTURE_APPROVAL = {
  id: "state-fixture-approval-1",
  run_id: "state-fixture-run-waiting",
  title: "Release state-coverage fixture package",
  state: "pending",
  risk: "High",
  gated_action: "Export the fixture package to the reviewer destination.",
  destination: "Fixture destination",
  requested_by: "grant-agent",
  requested_at: new Date().toISOString(),
  evidence_summary: "Fixture evidence summary for state coverage.",
  idempotency_key: "state-fixture-approval-1-key",
};
const DECISION_RATIONALE =
  "Reviewed exact gated action and destination; recording a decision.";

async function mockRunsAndApprovals(
  page: Page,
  runs: unknown[],
  approvals: unknown[],
) {
  await page.route("**/api/backend/api/runs", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(runs),
    });
  });
  await page.route("**/api/backend/api/approvals", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(approvals),
    });
  });
}

async function openApprovalForDecision(page: Page) {
  await mockRunsAndApprovals(
    page,
    [STATE_FIXTURE_RUN_WAITING],
    [STATE_FIXTURE_APPROVAL],
  );
  await gotoView(page, "runs");
  await page.locator(".detailed-run-list button").first().click();
  const rationaleField = page.locator(".approval-card textarea");
  await rationaleField.fill(DECISION_RATIONALE);
  return rationaleField;
}

test.describe(
  "[pw.library-detail] library item statuses beyond the ready baseline",
  () => {
    test("[pw.library-detail] a processing, needs-review, or blocked library item opens a truthful detail dialog via mouse and keyboard [pw.library.item.open:processing][pw.library.item.open:needs-review][pw.library.item.open:blocked]", async ({
      page,
    }, testInfo) => {
      const processingItem = libraryItem({
        id: "state-fixture-lib-processing",
        status: "processing",
        title: "Fixture dataset undergoing extraction",
      });
      const needsReviewItem = libraryItem({
        id: "state-fixture-lib-needs-review",
        status: "needs_review",
        title: "Fixture policy awaiting reviewer judgement",
      });
      const blockedItem = libraryItem({
        id: "state-fixture-lib-blocked",
        status: "blocked",
        title: "Fixture source blocked pending governance",
      });
      await mockLibrary(page, [processingItem, needsReviewItem, blockedItem]);
      await gotoView(page, "library");
      await expect(
        page.locator(".library-row:not(.library-head)"),
      ).toHaveCount(3);

      // `processing`: real, distinct amber styling (globals.css ~L3375-3379),
      // opened via mouse click.
      const processingRow = page.locator(".library-row", {
        hasText: processingItem.title,
      });
      await expect(processingRow.locator(".table-status")).toHaveClass(
        /processing/,
      );
      await expect(processingRow.locator(".table-status")).toHaveText(
        "processing",
      );
      const processingColor = await processingRow
        .locator(".table-status")
        .evaluate((el) => getComputedStyle(el).color);
      expect(processingColor).toBe("rgb(124, 89, 31)");
      await processingRow.click();
      let dialog = page.getByRole("dialog", { name: processingItem.title });
      await expect(dialog).toBeVisible();
      await expect(
        dialog.locator('dt:text-is("Status") + dd'),
      ).toHaveText("processing");
      await capture(page, testInfo, "library-item-open-processing");
      await expectAccessible(page);
      await dialog
        .getByRole("button", { name: "Close source detail" })
        .click();
      await expect(dialog).not.toBeVisible();

      // `needs_review`: no CSS color override exists for this status
      // (globals.css has no `.table-status.needs_review` rule), so only the
      // class name and the real, underscore-replaced label are asserted --
      // no fabricated visual distinction. Opened and closed entirely via
      // keyboard: focus the row button and activate with Enter, then focus
      // and activate the dialog's bottom "Close" button.
      const needsReviewRow = page.locator(".library-row", {
        hasText: needsReviewItem.title,
      });
      await expect(needsReviewRow.locator(".table-status")).toHaveClass(
        /needs_review/,
      );
      const needsReviewColor = await needsReviewRow
        .locator(".table-status")
        .evaluate((el) => getComputedStyle(el).color);
      expect(needsReviewColor).toBe("rgb(60, 101, 72)");
      await needsReviewRow.focus();
      await expect(needsReviewRow).toBeFocused();
      await page.keyboard.press("Enter");
      dialog = page.getByRole("dialog", { name: needsReviewItem.title });
      await expect(dialog).toBeVisible();
      await expect(
        dialog.locator('dt:text-is("Status") + dd'),
      ).toHaveText("needs review");
      await capture(page, testInfo, "library-item-open-needs-review");
      const closeButton = dialog.getByRole("button", {
        name: "Close",
        exact: true,
      });
      await closeButton.focus();
      await page.keyboard.press("Enter");
      await expect(dialog).not.toBeVisible();

      // `blocked`: real, distinct rose styling (globals.css ~L3381-3385),
      // opened via mouse click.
      const blockedRow = page.locator(".library-row", {
        hasText: blockedItem.title,
      });
      await expect(blockedRow.locator(".table-status")).toHaveClass(
        /blocked/,
      );
      const blockedColor = await blockedRow
        .locator(".table-status")
        .evaluate((el) => getComputedStyle(el).color);
      expect(blockedColor).toBe("rgb(121, 68, 63)");
      await blockedRow.click();
      dialog = page.getByRole("dialog", { name: blockedItem.title });
      await expect(
        dialog.locator('dt:text-is("Status") + dd'),
      ).toHaveText("blocked");
      await capture(page, testInfo, "library-item-open-blocked");
      await expectAccessible(page);
    });
  },
);

test.describe("[pw.library-ingest] library ingestion in flight", () => {
  test("[pw.library-ingest] the ingest form disables submission but keeps Cancel live while the upload request is in flight, then closes on success [pw.library.ingest.form:submitting]", async ({
    page,
  }, testInfo) => {
    await waitForWorkspace(page);
    await page.getByRole("button", { name: /^Library \d+$/ }).click();
    await page.getByRole("button", { name: "Ingest source" }).click();
    const dialog = page.getByRole("dialog", { name: "Add source to Library" });
    const title = `State-coverage submitting fixture ${Date.now()}`;
    // The ingest form calls `onRefresh()` after a successful upload
    // (workspace-views.tsx ~L636-648), which re-fetches the real
    // `/api/backend/api/library` endpoint. Mock it up front so that refetch
    // resolves to a list containing the exact ingested item, instead of
    // hitting the unmocked backend and never finding the fixture title.
    const ingestedItem = libraryItem({
      id: "state-fixture-ingest-item",
      status: "processing",
      title,
    });
    await mockLibrary(page, [ingestedItem]);
    await dialog.getByLabel("Title").fill(title);
    await dialog.getByLabel("Source file").setInputFiles({
      name: "fixture.txt",
      mimeType: "text/plain",
      buffer: Buffer.from(
        "Fixture content used only to exercise the submitting state.",
      ),
    });
    await dialog
      .getByLabel("Description")
      .fill(
        "Fixture description used only to exercise the submitting state.",
      );

    let releaseUpload: (() => void) | undefined;
    const uploadReleased = new Promise<void>((resolve) => {
      releaseUpload = resolve;
    });
    let uploadRequestBody: string | null = null;
    await page.route("**/api/backend/api/library/upload", async (route) => {
      uploadRequestBody = route.request().postData();
      await uploadReleased;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          item: ingestedItem,
          run: { durable_instance_id: "research-run-ingest-state-fixture" },
        }),
      });
    });

    const submitButton = dialog.getByRole("button", {
      name: "Start ingestion",
    });
    const cancelButton = dialog.getByRole("button", { name: "Cancel" });
    await submitButton.click();
    const submittingButton = dialog.getByRole("button", { name: "Queuing…" });
    await expect(submittingButton).toBeVisible();
    await expect(submittingButton).toBeDisabled();
    // Only the submit control is gated -- `Cancel` has no `disabled` binding
    // (workspace-views.tsx ~L718-724) and must remain a live escape hatch.
    await expect(cancelButton).toBeEnabled();
    // Real payload evidence: the actual multipart request carries the title
    // the user typed, captured before the deferred gate is released.
    await expect.poll(() => uploadRequestBody).not.toBeNull();
    expect(uploadRequestBody).toContain(title);
    await capture(page, testInfo, "library-ingest-submitting");
    await expectAccessible(page);
    releaseUpload?.();
    await expect(dialog).not.toBeVisible();
    await expect(page.getByText(title, { exact: true })).toBeVisible();
  });
});

test.describe(
  "[pw.approval-decision] approval decisions in flight, failing, and rejected",
  () => {
    test("[pw.approval-decision] disables both decision buttons and the rationale field while a decision request is in flight [pw.approvals.decide:submitting][pw.approvals.rationale:disabled]", async ({
      page,
    }, testInfo) => {
      const rationaleField = await openApprovalForDecision(page);

      let releaseDecision: (() => void) | undefined;
      const decisionReleased = new Promise<void>((resolve) => {
        releaseDecision = resolve;
      });
      let capturedBody: { decision: string; rationale: string } | null = null;
      await page.route(
        "**/api/backend/api/approvals/state-fixture-approval-1/decision",
        async (route) => {
          capturedBody = route.request().postDataJSON();
          await decisionReleased;
          await route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({
              ...STATE_FIXTURE_APPROVAL,
              state: "approved",
            }),
          });
        },
      );

      const approveButton = page.getByRole("button", {
        name: "Approve exact action",
      });
      const rejectButton = page.getByRole("button", { name: "Reject action" });
      await approveButton.click();
      await expect(approveButton).toBeDisabled();
      await expect(rejectButton).toBeDisabled();
      // Evidence for the `approvals.rationale` manifest entry: the textarea
      // is bound to `disabled={deciding}` (workspace-views.tsx ~L979), so it
      // becomes genuinely non-interactive for the duration of the in-flight
      // decision request, and re-enables once the request settles.
      await expect(rationaleField).toBeDisabled();
      await expect.poll(() => capturedBody).toEqual({
        decision: "approved",
        rationale: DECISION_RATIONALE,
      });
      await capture(page, testInfo, "approval-decide-submitting");
      await expectAccessible(page);
      releaseDecision?.();
      await expect(approveButton).toBeEnabled();
      await expect(rejectButton).toBeEnabled();
      await expect(rationaleField).toBeEnabled();
    });

    test("[pw.approval-decision] surfaces the exact backend failure message and re-enables the decision controls [pw.approvals.decide:error]", async ({
      page,
      releaseDiagnostics,
    }, testInfo) => {
      await openApprovalForDecision(page);
      releaseDiagnostics.expectConsoleError(
        /status of 500 \(Internal Server Error\)/,
      );
      let capturedBody: { decision: string; rationale: string } | null = null;
      await page.route(
        "**/api/backend/api/approvals/state-fixture-approval-1/decision",
        async (route) => {
          capturedBody = route.request().postDataJSON();
          await route.fulfill({
            status: 500,
            contentType: "application/json",
            body: JSON.stringify({
              detail: "The approval ledger is temporarily unavailable.",
            }),
          });
        },
      );

      const approveButton = page.getByRole("button", {
        name: "Approve exact action",
      });
      const rejectButton = page.getByRole("button", { name: "Reject action" });
      await approveButton.click();
      await expect(page.locator(".error-banner[role='alert']")).toHaveText(
        "The approval ledger is temporarily unavailable.",
      );
      await expect.poll(() => capturedBody).toEqual({
        decision: "approved",
        rationale: DECISION_RATIONALE,
      });
      await capture(page, testInfo, "approval-decide-error");
      await expectAccessible(page);
      await expect(approveButton).toBeEnabled();
      await expect(rejectButton).toBeEnabled();
    });

    test("[pw.approval-decision] records an exact rejected decision payload via keyboard activation [pw.approvals.decide:rejected]", async ({
      page,
    }, testInfo) => {
      await openApprovalForDecision(page);
      let capturedBody: { decision: string; rationale: string } | null = null;
      await page.route(
        "**/api/backend/api/approvals/state-fixture-approval-1/decision",
        async (route) => {
          capturedBody = route.request().postDataJSON();
          await route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify({
              ...STATE_FIXTURE_APPROVAL,
              state: "rejected",
            }),
          });
        },
      );

      const rejectButton = page.getByRole("button", { name: "Reject action" });
      await rejectButton.focus();
      await expect(rejectButton).toBeFocused();
      await page.keyboard.press("Enter");
      await expect.poll(() => capturedBody).toEqual({
        decision: "rejected",
        rationale: DECISION_RATIONALE,
      });
      await capture(page, testInfo, "approval-decide-rejected");
      await expectAccessible(page);
    });
  },
);

test.describe("[pw.run-detail] partial run status", () => {
  test("[pw.run-detail] selecting a partially completed run shows its true, unstyled status label via keyboard [pw.runs.select:partial]", async ({
    page,
  }, testInfo) => {
    const partialRun = {
      ...STATE_FIXTURE_RUN_WAITING,
      id: "state-fixture-run-partial",
      durable_instance_id: "research-state-fixture-run-partial",
      capability: "dataset",
      title: "State-coverage fixture run left partially complete",
      status: "partial",
      progress: 54,
      current_stage: "Partial completion boundary",
      approval_id: null,
    };
    await mockRunsAndApprovals(page, [partialRun], []);
    await gotoView(page, "runs");

    const partialRow = page.locator(".detailed-run-list button", {
      hasText: partialRun.title,
    });
    await expect(partialRow.locator(".table-status")).toHaveClass(/partial/);
    await expect(partialRow.locator(".table-status")).toHaveText("partial");
    // `partial` has no dedicated CSS color override (globals.css only
    // special-cases processing/running and waiting_for_approval/blocked), so
    // this only asserts the real, generic default color -- not a fabricated
    // distinction.
    const partialColor = await partialRow
      .locator(".table-status")
      .evaluate((el) => getComputedStyle(el).color);
    expect(partialColor).toBe("rgb(60, 101, 72)");

    // Selected via keyboard: native `<button>` elements activate on Enter
    // without any custom keydown handler needed.
    await partialRow.focus();
    await expect(partialRow).toBeFocused();
    await page.keyboard.press("Enter");
    const overviewStatus = page.locator(".run-overview .table-status");
    await expect(overviewStatus).toHaveClass(/partial/);
    await expect(overviewStatus).toHaveText("partial");
    await capture(page, testInfo, "runs-select-partial");
    await expectAccessible(page);
  });
});

test.describe(
  "[pw.operational-surfaces] the runs surface while /runs is loading or failing",
  () => {
    test("[pw.operational-surfaces] shows the same real empty-state markup while /runs is in flight, never a fabricated ready state, then updates once data resolves [pw.runs.surface.load:loading]", async ({
      page,
    }, testInfo) => {
      let releaseRuns: (() => void) | undefined;
      const runsReleased = new Promise<void>((resolve) => {
        releaseRuns = resolve;
      });
      await page.route("**/api/backend/api/runs", async (route) => {
        await runsReleased;
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([STATE_FIXTURE_RUN_WAITING]),
        });
      });

      await page.goto("/?view=runs");
      // `data-workspace-ready` must genuinely stay "false": `getWorkspaceData()`
      // uses `Promise.all` (api.ts ~L50-80), so the whole workspace load is
      // gated on this single held endpoint (research-workbench.tsx
      // ~L453 `data-workspace-ready={Boolean(data)}`).
      await expect(page.locator(".workbench-shell")).toHaveAttribute(
        "data-workspace-ready",
        "false",
      );
      await expect(
        page.getByRole("heading", { name: "Runs & Approvals" }),
      ).toBeVisible();
      // Documented-defect pattern: with `data === null`, `RunsView` (which
      // renders unconditionally regardless of workspace-ready) falls into
      // the identical "No durable runs available" placeholder used for a
      // genuinely empty, already-loaded run list -- same family as the
      // existing `[pw.overview.open-runs:empty]` documented-defect test.
      await expect(page.getByText("No durable runs available")).toBeVisible();
      await capture(page, testInfo, "runs-select-loading");
      await expectAccessible(page);

      releaseRuns?.();
      await expect(page.locator(".workbench-shell")).toHaveAttribute(
        "data-workspace-ready",
        "true",
      );
      await expect(page.locator(".detailed-run-list button")).toHaveCount(1);
      await expect(page.locator(".run-overview h2")).toHaveText(
        STATE_FIXTURE_RUN_WAITING.title,
      );
    });

    test("[pw.operational-surfaces] falls back to the same real empty-state markup and a truthful connection banner rather than crashing when /runs returns a server error [pw.runs.surface.load:error]", async ({
      page,
      releaseDiagnostics,
    }, testInfo) => {
      releaseDiagnostics.expectConsoleError(
        /status of 500 \(Internal Server Error\)/,
      );
      await page.route("**/api/backend/api/runs", async (route) => {
        await route.fulfill({
          status: 500,
          contentType: "application/json",
          body: JSON.stringify({
            detail: "Durable runs store is temporarily unavailable.",
          }),
        });
      });

      await page.goto("/?view=runs");
      await expect(page.locator(".workbench-shell")).toHaveAttribute(
        "data-workspace-ready",
        "false",
      );
      await expect(
        page.getByRole("heading", { name: "Runs & Approvals" }),
      ).toBeVisible();
      await expect(page.getByText("No durable runs available")).toBeVisible();
      await expect(page.locator(".connection-banner")).toContainText(
        "Durable runs store is temporarily unavailable.",
      );
      await capture(page, testInfo, "runs-select-error");
      await expectAccessible(page);
    });
  },
);

test.describe("Shared route and error behavior", () => {
  test("an unknown route renders the real 404 not-found page with a keyboard-operable recovery link", async ({
    page,
    releaseDiagnostics,
  }, testInfo) => {
    // The top-level document navigation itself resolves with a real 404
    // status (asserted below), and the browser also logs a genuine resource
    // load failure for that same navigation -- expect it rather than
    // silently allowing it to fail the diagnostics gate.
    releaseDiagnostics.expectConsoleError(
      /status of 404 \(Not Found\)/,
    );
    const response = await page.goto(
      "/this-route-does-not-exist-in-the-app",
    );
    expect(response?.status()).toBe(404);
    await expect(
      page.getByRole("heading", {
        name: "Research workspace page not found",
        level: 1,
      }),
    ).toBeVisible();
    await expect(
      page.getByText("The requested page is not part of this accelerator."),
    ).toBeVisible();
    await capture(page, testInfo, "shared-not-found");
    await expectAccessible(page);

    const returnLink = page.getByRole("link", {
      name: "Return to the research workbench",
    });
    await returnLink.focus();
    await expect(returnLink).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page.locator(".workbench-shell")).toHaveAttribute(
      "data-workspace-ready",
      "true",
    );
  });

  test("a render-time exception is caught by the real error boundary and is fully recoverable via Try again", async ({
    page,
    releaseDiagnostics,
  }, testInfo) => {
    // Genuine, pre-existing gap (not an invented state): `apiFetch` performs
    // no runtime shape validation, and `RunsView` only optional-chains on
    // `data`, not `data.approvals` (workspace-views.tsx ~L765, ~L807) --
    // `data?.approvals.find(...)` / `.filter(...)`. A malformed-but-valid-JSON
    // upstream response (`null` instead of an array) is a real, plausible
    // failure mode that throws a real, uncaught TypeError during render,
    // caught by Next's real `error.tsx` route boundary (confirmed via a
    // production build in playwright.config.ts, so no dev overlay
    // intercepts it, and there is no competing `global-error.tsx`).
    let approvalsAreBroken = true;
    await page.route("**/api/backend/api/approvals", async (route) => {
      if (approvalsAreBroken) {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: "null",
        });
      } else {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: "[]",
        });
      }
    });
    // React 19's error-boundary machinery itself logs the raw thrown error
    // via console.error (its default `onCaughtError` reporter), in addition
    // to our own `error.tsx` handler's explicit
    // `console.error("Research workbench render failed", error)` call.
    // Verified directly against the freshly built production server: BOTH
    // fire for a single render-time throw, each carrying the real
    // TypeError's message and stack, so both must be allow-listed rather
    // than assuming only one console.error occurs per thrown error.
    releaseDiagnostics.expectConsoleError(
      /Cannot read properties of null \(reading 'filter'\)/,
    );
    releaseDiagnostics.expectConsoleError(
      /Cannot read properties of null \(reading 'filter'\)/,
    );

    await page.goto("/?view=runs");
    // Scoped to `.route-error[role="alert"]` rather than a bare
    // `getByRole("alert")`: Next's client runtime also renders its own
    // built-in `#__next-route-announcer__` element with `role="alert"` for
    // route-change announcements, which is present on every page regardless
    // of this error boundary and would otherwise make the locator ambiguous
    // (and would still match `not.toBeVisible()` after recovery, since it
    // never actually goes away).
    const alert = page.locator(".route-error[role='alert']");
    await expect(alert).toBeVisible();
    await expect(
      alert.getByRole("heading", {
        name: "The research workbench could not load",
        level: 1,
      }),
    ).toBeVisible();
    await capture(page, testInfo, "shared-error-boundary");
    await expectAccessible(page);

    approvalsAreBroken = false;
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(page.locator(".workbench-shell")).toHaveAttribute(
      "data-workspace-ready",
      "true",
    );
    await expect(alert).not.toBeVisible();
  });
});
