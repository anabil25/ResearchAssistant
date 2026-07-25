import path from "node:path";
import type { Locator, Page } from "@playwright/test";

import AxeBuilder from "@axe-core/playwright";

import { completeWorkspaceRequests, expect, test } from "./fixtures";

type TriggerMode = "pointer" | "keyboard";

const desktopViewport = { width: 1536, height: 1000 };
const mobileViewport = { width: 390, height: 844 };
const libraryButtonPattern = /^Library \d+$/;
const runsButtonPattern = /Runs & approvals \d+/i;
const connectorsButtonPattern = /Connectors 12/i;
const accessibilityTags = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];
const studioViews = [
  "Literature Studio",
  "Grant Studio",
  "Matching Explorer",
  "Dataset Lab",
  "Institutional Q&A",
  "Workflow Automation",
] as const;
const artifactViews = [
  { studio: "Grant Studio", surface: "Requirement matrix" },
  { studio: "Matching Explorer", surface: "Match criteria" },
  { studio: "Dataset Lab", surface: "Schema & quality" },
  { studio: "Institutional Q&A", surface: "Authorized corpus" },
  { studio: "Workflow Automation", surface: "Evidence review graph" },
] as const;

function shellRoot(page: Page) {
  return page.locator(".workbench-shell");
}

function levelOneHeading(page: Page, name: string) {
  return page.getByRole("heading", { name, level: 1 });
}

function studioCard(page: Page, name: string) {
  return page.getByRole("button", { name, exact: true }).first();
}

async function loadWorkbench(page: Page, route = "/") {
  await completeWorkspaceRequests(page, () => page.goto(route));
  await expect(shellRoot(page)).toHaveAttribute(
    "data-workspace-ready",
    "true",
  );
}

async function switchToMobile(page: Page) {
  await page.setViewportSize(mobileViewport);
}

async function triggerControl(
  page: Page,
  target: Locator,
  mode: TriggerMode = "pointer",
  waitForWorkspace = false,
) {
  const performActivation =
    mode === "keyboard"
      ? async () => {
          await target.focus();
          await page.keyboard.press("Enter");
        }
      : async () => {
          await target.click();
        };

  if (waitForWorkspace) {
    await completeWorkspaceRequests(page, performActivation);
    return;
  }

  await performActivation();
}

async function openLibrary(page: Page, mode: TriggerMode = "pointer") {
  await triggerControl(
    page,
    page.getByRole("button", { name: libraryButtonPattern }),
    mode,
    true,
  );
  await expect(levelOneHeading(page, "Library")).toBeVisible();
}

async function openRuns(page: Page) {
  await triggerControl(
    page,
    page.getByRole("button", { name: runsButtonPattern }).first(),
  );
  await expect(levelOneHeading(page, "Runs & Approvals")).toBeVisible();
}

async function openMobileNavigation(page: Page, mode: TriggerMode = "pointer") {
  await triggerControl(page, page.getByLabel("Open navigation"), mode);
}

async function invokeStudioRun(
  page: Page,
  studio: string,
  commandLabel: string,
  mode: TriggerMode = "pointer",
) {
  const responseWaiter = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().includes(`/api/studios/${studio}/run`),
  );

  await triggerControl(
    page,
    page.getByRole("button", { name: commandLabel }),
    mode,
  );

  const response = await responseWaiter;
  const body = await response.text();
  expect(response.status(), body).toBe(200);
}

async function findSubMinimumText(page: Page) {
  return page.locator(".workbench-shell *").evaluateAll((nodes) =>
    nodes.flatMap((node) => {
      const ownsVisibleText = [...node.childNodes].some(
        (child) =>
          child.nodeType === Node.TEXT_NODE && Boolean(child.textContent?.trim()),
      );
      const computedStyle = window.getComputedStyle(node);
      const isVisible =
        computedStyle.display !== "none" &&
        computedStyle.visibility !== "hidden" &&
        node.getClientRects().length > 0;
      const fontSize = Number.parseFloat(computedStyle.fontSize);

      return ownsVisibleText && isVisible && fontSize < 12
        ? [
            {
              element: node.tagName.toLowerCase(),
              className: node.getAttribute("class"),
              size: fontSize,
              text: node.textContent?.trim().slice(0, 80),
            },
          ]
        : [];
    }),
  );
}

async function findUndersizedButtons(page: Page, minimum: number) {
  return page.locator("button").evaluateAll(
    (buttons, floor) =>
      buttons.flatMap((button) => {
        const computedStyle = window.getComputedStyle(button);
        const isVisible =
          computedStyle.display !== "none" &&
          computedStyle.visibility !== "hidden" &&
          button.getClientRects().length > 0;
        const bounds = button.getBoundingClientRect();

        return isVisible && (bounds.width + 0.01 < floor || bounds.height + 0.01 < floor)
          ? [
              {
                name:
                  button.getAttribute("aria-label") ??
                  button.textContent?.trim().slice(0, 80),
                width: bounds.width,
                height: bounds.height,
              },
            ]
          : [];
      }),
    minimum,
  );
}

async function runAccessibilityAudit(page: Page) {
  const report = await new AxeBuilder({ page })
    .withTags([...accessibilityTags])
    .analyze();
  expect(report.violations).toEqual([]);
}

async function readValidity(input: Locator) {
  return input.evaluate((element: HTMLInputElement) => element.validity.valid);
}

async function saveScreenshot(
  page: Page,
  outputDirectory: string,
  name: string,
  fullPage = true,
) {
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(100);
  await page.screenshot({
    path: path.join(outputDirectory, name),
    fullPage,
  });
}

test.describe("workbench shell coverage", () => {
  test("[pw.route-state] workspace routes survive direct links and browser history [pw.shell.navigation.primary-routes:ready][pw.shell.navigation.primary-routes:selected]", async ({
    page,
  }) => {
    await loadWorkbench(page, "/?view=dataset");
    await expect(levelOneHeading(page, "Dataset Lab")).toBeVisible();

    await test.step("visit settings from a deep link", async () => {
      await page.getByLabel("Open project settings").click();
      await expect(page).toHaveURL(/view=settings/);
      await expect(levelOneHeading(page, "Project Settings")).toBeVisible();
    });

    await test.step("return through browser history", async () => {
      await page.goBack();
      await expect(page).toHaveURL(/view=dataset/);
      await expect(levelOneHeading(page, "Dataset Lab")).toBeVisible();
    });
  });

  test("[pw.route-state] keyboard activation and mobile viewport navigate to a URL-addressable route [pw.shell.navigation.primary-routes:keyboard][pw.shell.navigation.primary-routes:mobile]", async ({
    page,
  }) => {
    await loadWorkbench(page);

    await test.step("open settings with the keyboard", async () => {
      await triggerControl(
        page,
        page.getByLabel("Open project settings"),
        "keyboard",
        true,
      );
      await expect(page).toHaveURL(/view=settings/);
      await expect(levelOneHeading(page, "Project Settings")).toBeVisible();
      await page.waitForLoadState("networkidle");
    });

    await test.step("land on the dataset route in a phone viewport", async () => {
      await switchToMobile(page);
      await loadWorkbench(page, "/?view=dataset");
      await expect(levelOneHeading(page, "Dataset Lab")).toBeVisible();
    });
  });

  test("[pw.mobile-navigation] mobile navigation opens, closes, and preserves the selected view [pw.shell.navigation.open-mobile:ready][pw.shell.navigation.open-mobile:selected][pw.shell.navigation.close-mobile:selected]", async ({
    page,
  }) => {
    await switchToMobile(page);
    await loadWorkbench(page);

    const navigation = page.getByLabel("Project navigation");
    await test.step("expand the drawer", async () => {
      await openMobileNavigation(page);
      await expect(navigation).toHaveAttribute("data-open", "true");
    });

    await test.step("select Dataset Lab and confirm the drawer closes", async () => {
      await page.getByRole("button", { name: "Dataset Lab", exact: true }).click();
      await expect(levelOneHeading(page, "Dataset Lab")).toBeVisible();
      await expect(navigation).toHaveAttribute("data-open", "false");
    });
  });

  test("[pw.mobile-navigation] keyboard opens the mobile navigation drawer [pw.shell.navigation.open-mobile:keyboard]", async ({
    page,
  }) => {
    await switchToMobile(page);
    await loadWorkbench(page);
    await openMobileNavigation(page, "keyboard");
    await expect(page.getByLabel("Project navigation")).toHaveAttribute(
      "data-open",
      "true",
    );
  });

  test("[pw.distinct-studios] overview presents six purpose-built research studios [pw.overview.open-studio-card:ready]", async ({
    page,
  }) => {
    await loadWorkbench(page);
    await expect(
      page.getByRole("heading", { name: /move from question to/i }),
    ).toBeVisible();
    await expect(page.locator(".capability-card")).toHaveCount(6);
    await expect(page.getByText("Evidence control plane")).toBeVisible();
    await expect(page.getByText("Governance is product state")).toBeVisible();
  });

  test("[pw.literature-open] [pw.literature-protocol] keyboard opens the literature protocol workspace [pw.overview.start-literature:ready][pw.overview.start-literature:keyboard][pw.literature.protocol.question:ready][pw.literature.protocol.sources:ready]", async ({
    page,
  }) => {
    await loadWorkbench(page);
    await triggerControl(
      page,
      page.getByRole("button", { name: /literature review synthesis/i }),
      "keyboard",
    );

    await expect(levelOneHeading(page, "Literature Studio")).toBeVisible();
    await expect(page.getByLabel("Research question")).toHaveValue(
      /auditable retrieval/i,
    );
    await expect(page.getByText("Scholarly sources")).toBeVisible();
    await expect(page.getByText("No screening run yet")).toBeVisible();
  });

  test("[pw.literature-open] pointer click opens the literature protocol workspace at mobile viewport [pw.overview.start-literature:selected][pw.overview.start-literature:mobile]", async ({
    page,
  }) => {
    await switchToMobile(page);
    await loadWorkbench(page);
    await page
      .getByRole("button", { name: /start a literature review/i })
      .click();
    await expect(levelOneHeading(page, "Literature Studio")).toBeVisible();
  });

  test("visible workbench text never renders below twelve pixels", async ({
    page,
  }) => {
    await loadWorkbench(page);

    for (const studioName of studioViews) {
      await test.step(`inspect ${studioName}`, async () => {
        await studioCard(page, studioName).click();
        const undersizedText = await findSubMinimumText(page);
        expect(undersizedText, `${studioName} contains undersized text`).toEqual(
          [],
        );
      });
    }
  });

  test("[pw.mobile-navigation] interactive targets meet desktop and mobile size floors [pw.shell.navigation.open-mobile:mobile]", async ({
    page,
  }) => {
    await loadWorkbench(page);
    expect(await findUndersizedButtons(page, 32)).toEqual([]);

    await switchToMobile(page);
    await openMobileNavigation(page);
    expect(await findUndersizedButtons(page, 44)).toEqual([]);
  });

  test("all static workbench surfaces pass automated WCAG checks", async ({
    page,
  }) => {
    await loadWorkbench(page);
    await runAccessibilityAudit(page);

    for (const studioName of studioViews) {
      await test.step(`audit ${studioName}`, async () => {
        await studioCard(page, studioName).click();
        await runAccessibilityAudit(page);
      });
    }

    await openLibrary(page);
    await runAccessibilityAudit(page);

    await openRuns(page);
    await runAccessibilityAudit(page);

    await page.getByLabel("Open project settings").click();
    await runAccessibilityAudit(page);

    await switchToMobile(page);
    const navigationButton = page.getByLabel("Open navigation");
    await expect(navigationButton).toHaveAttribute("aria-expanded", "false");
    await navigationButton.click();
    await expect(navigationButton).toHaveAttribute("aria-expanded", "true");
    await runAccessibilityAudit(page);
  });
});

test.describe("studio and operations coverage", () => {
  test("[pw.distinct-studios] every studio exposes a distinct workflow and artifact surface [pw.overview.open-studio-card:selected]", async ({
    page,
  }) => {
    await loadWorkbench(page);

    for (const { studio, surface } of artifactViews) {
      await test.step(`verify ${studio}`, async () => {
        await studioCard(page, studio).click();
        await expect(levelOneHeading(page, studio)).toBeVisible();
        await expect(page.getByText(surface).first()).toBeVisible();
      });
    }
  });

  test("[pw.distinct-studios] keyboard activation and mobile viewport open a studio card [pw.overview.open-studio-card:keyboard][pw.overview.open-studio-card:mobile]", async ({
    page,
  }) => {
    await loadWorkbench(page);

    await test.step("open Grant Studio from the keyboard", async () => {
      await triggerControl(page, studioCard(page, "Grant Studio"), "keyboard");
      await expect(levelOneHeading(page, "Grant Studio")).toBeVisible();
    });

    await test.step("open Matching Explorer in the mobile drawer", async () => {
      await switchToMobile(page);
      await loadWorkbench(page);
      await openMobileNavigation(page);
      await studioCard(page, "Matching Explorer").click();
      await expect(levelOneHeading(page, "Matching Explorer")).toBeVisible();
    });
  });

  test("[pw.literature-run] [pw.literature-screen] [pw.literature-extract] literature workflow returns screening, extraction, and resolved evidence [pw.literature.protocol.run:ready][pw.literature.protocol.run:keyboard][pw.literature.protocol.run:success][pw.literature.screen.tab:ready][pw.literature.extract.tab:ready]", async ({
    page,
  }) => {
    await loadWorkbench(page);
    await page
      .getByRole("button", { name: /literature review synthesis/i })
      .click();

    await test.step("run the literature protocol", async () => {
      await invokeStudioRun(
        page,
        "literature",
        "Search & screen evidence",
        "keyboard",
      );
      await expect(page.locator(".screening-record")).not.toHaveCount(0);
    });

    await test.step("inspect extraction and evidence", async () => {
      await page.getByRole("button", { name: "Extract", exact: true }).click();
      await expect(page.getByText("Extraction matrix").first()).toBeVisible();
      await expect(page.locator(".extraction-row")).not.toHaveCount(0);
      await expect(page.locator(".evidence-source-list article")).not.toHaveCount(
        0,
      );
      await expect(page.getByText(/research-run-/).first()).toBeVisible();
    });
  });

  test("[pw.operational-surfaces] [pw.run-detail] [pw.connector-test] Library, Runs, and connector settings contain operational data [pw.runs.select:ready][pw.runs.select:selected][pw.settings.connectors.test:ready][pw.overview.open-library:ready][pw.overview.open-library:selected]", async ({
    page,
  }) => {
    await loadWorkbench(page);

    await test.step("inspect the Library", async () => {
      await openLibrary(page);
      expect(
        await page.locator(".library-row:not(.library-head)").count(),
      ).toBeGreaterThanOrEqual(9);
      await expect(page.getByRole("button", { name: "Ingest source" })).toBeVisible();
    });

    await test.step("inspect runs and approvals", async () => {
      await openRuns(page);
      await page.getByText("Open infrastructure application").first().click();
      await expect(page.getByText("Exact gated action")).toBeVisible();
      await expect(
        page.getByText("research-run-grant-001", { exact: true }),
      ).toBeVisible();
    });

    await test.step("inspect connector settings", async () => {
      await page.getByLabel("Open project settings").click();
      await page.getByRole("button", { name: connectorsButtonPattern }).click();
      await expect(page.locator(".connector-card")).toHaveCount(12);
      await expect(page.getByText("Foundry Web Search")).toBeVisible();
      await expect(page.getByText("Assigned specialists").first()).toBeVisible();
    });
  });

  test("[pw.operational-surfaces] keyboard activation and mobile viewport open the Library [pw.overview.open-library:keyboard][pw.overview.open-library:mobile]", async ({
    page,
  }) => {
    await loadWorkbench(page);

    await test.step("open Library with the keyboard", async () => {
      await openLibrary(page, "keyboard");
    });

    await test.step("open Library from the mobile drawer", async () => {
      await switchToMobile(page);
      await loadWorkbench(page);
      await openMobileNavigation(page);
      await openLibrary(page);
    });
  });

  test("[pw.library-ingest] Library ingestion creates a governed item and durable run [pw.library.ingest.open-close:closed][pw.library.ingest.open-close:open][pw.library.ingest.form:empty][pw.library.ingest.form:invalid][pw.library.ingest.form:valid][pw.library.ingest.form:success]", async ({
    page,
  }) => {
    const itemTitle = `New reproducibility protocol ${Date.now()}`;

    await loadWorkbench(page);
    await openLibrary(page);
    await page.getByRole("button", { name: "Ingest source" }).click();

    const dialog = page.getByRole("dialog", { name: "Add source to Library" });
    const titleField = dialog.getByLabel("Title");

    await test.step("exercise dialog validation", async () => {
      await expect(titleField).toHaveValue("");
      await titleField.fill("ab");
      expect(await readValidity(titleField)).toBe(false);
      await titleField.fill(itemTitle);
      expect(await readValidity(titleField)).toBe(true);
    });

    await test.step("submit the upload", async () => {
      await dialog.getByLabel("Source file").setInputFiles({
        name: "protocol.txt",
        mimeType: "text/plain",
        buffer: Buffer.from(
          "Protocol version 1.0\n\nInclude primary studies with explicit methods and limitations.",
        ),
      });
      await dialog.getByLabel("Description").fill(
        "A project-supplied protocol queued for governed extraction and indexing.",
      );

      const responseWaiter = page.waitForResponse(
        (response) =>
          response.request().method() === "POST" &&
          response.url().includes("/api/library/upload"),
      );

      await dialog.getByRole("button", { name: "Start ingestion" }).click();
      const response = await responseWaiter;
      const payload = (await response.json()) as {
        item: { status: string; title: string };
        run: { durable_instance_id: string };
      };

      expect(response.status()).toBe(200);
      expect(payload.item).toMatchObject({
        title: itemTitle,
        status: "processing",
      });
      expect(payload.run.durable_instance_id).toMatch(/^research-run-ingest-/);
      await expect(page.getByText(itemTitle, { exact: true })).toBeVisible();
    });
  });

  test("[pw.library-ingest] keyboard opens and closes the ingest dialog [pw.library.ingest.open-close:keyboard]", async ({
    page,
  }) => {
    await loadWorkbench(page);
    await openLibrary(page);

    const openDialogButton = page.getByRole("button", { name: "Ingest source" });
    await triggerControl(page, openDialogButton, "keyboard");

    const dialog = page.getByRole("dialog", { name: "Add source to Library" });
    await expect(dialog).toBeVisible();

    await triggerControl(page, page.getByLabel("Close ingest dialog"), "keyboard");
    await expect(dialog).not.toBeVisible();
  });

  test("[pw.library-oversize] BFF rejects oversized uploads before API processing [pw.library.ingest.form:error]", async ({
    page,
    releaseDiagnostics,
  }) => {
    await loadWorkbench(page);
    await openLibrary(page);
    await page.getByRole("button", { name: "Ingest source" }).click();

    const dialog = page.getByRole("dialog", { name: "Add source to Library" });
    await dialog.getByLabel("Title").fill("Oversized protocol");
    await dialog.getByLabel("Description").fill(
      "This upload must be rejected at the BFF boundary.",
    );
    await dialog.getByLabel("Source file").setInputFiles({
      name: "oversized.txt",
      mimeType: "text/plain",
      buffer: Buffer.alloc(21_000_000, "A"),
    });

    releaseDiagnostics.expectConsoleError(/status of 413 \(Payload Too Large\)/);
    await dialog.getByRole("button", { name: "Start ingestion" }).click();

    await expect(dialog.getByRole("alert")).toContainText("Request body exceeds");
  });
});

test.describe("visual coverage capture", () => {
  test("capture the V3 UI foundation at desktop and mobile", async ({ page }) => {
    const outputDirectory = process.env.UX_SCREENSHOT_DIR;
    test.skip(!outputDirectory, "Screenshot directory not requested.");

    await page.setViewportSize(desktopViewport);
    await loadWorkbench(page);
    await saveScreenshot(page, outputDirectory!, "01-overview-v3-m1.png");

    const captures: ReadonlyArray<{
      name: string;
      prepare: () => Promise<void>;
      fullPage?: boolean;
    }> = [
      {
        name: "02-literature-protocol-v3-m1.png",
        prepare: async () => {
          await page
            .getByRole("button", { name: /literature review synthesis/i })
            .click();
        },
      },
      {
        name: "03-literature-results-v3-m1.png",
        prepare: async () => {
          await invokeStudioRun(page, "literature", "Search & screen evidence");
          await expect(page.locator(".screening-record")).not.toHaveCount(0);
          await expect(
            page.getByRole("button", { name: "Search & screen evidence" }),
          ).toBeEnabled();
        },
      },
      {
        name: "04-grant-studio-v3-m1.png",
        prepare: async () => {
          await studioCard(page, "Grant Studio").click();
          await page
            .getByRole("button", { name: "Parse notice & build package" })
            .click();
          await expect(page.locator(".requirement-done")).not.toHaveCount(0);
        },
      },
      {
        name: "05-matching-explorer-v3-m1.png",
        prepare: async () => {
          await studioCard(page, "Matching Explorer").click();
          await page
            .getByRole("button", { name: "Build verified shortlist" })
            .click();
          await expect(page.locator(".match-card")).not.toHaveCount(0);
          await expect(
            page.getByRole("button", { name: "Build verified shortlist" }),
          ).toBeEnabled();
        },
      },
      {
        name: "06-dataset-lab-v3-m1.png",
        prepare: async () => {
          await studioCard(page, "Dataset Lab").click();
          await page
            .getByLabel(
              /I approve sending this bounded dataset to the Foundry Dataset Agent/,
            )
            .check();
          await page
            .getByRole("button", {
              name: "Analyze with Foundry Code Interpreter",
            })
            .click();
          await expect(page.locator(".schema-row")).not.toHaveCount(0);
          await expect(
            page.getByRole("button", {
              name: "Analyze with Foundry Code Interpreter",
            }),
          ).toBeEnabled();
        },
      },
      {
        name: "07-institutional-qa-v3-m1.png",
        prepare: async () => {
          await studioCard(page, "Institutional Q&A").click();
          await page
            .getByRole("button", { name: "Resolve policy answer" })
            .click();
          await expect(page.locator(".answer-card")).toBeVisible();
          await expect(
            page.getByRole("button", { name: "Resolve policy answer" }),
          ).toBeEnabled();
        },
      },
      {
        name: "08-workflow-automation-v3-m1.png",
        prepare: async () => {
          await studioCard(page, "Workflow Automation").click();
          await page.getByRole("button", { name: "Validate & dry run" }).click();
          await expect(page.getByText("Dry run passed")).toBeVisible();
          await expect(
            page.getByRole("button", { name: "Validate & dry run" }),
          ).toBeEnabled();
        },
      },
      {
        name: "09-library-v3-m1.png",
        prepare: async () => {
          await openLibrary(page);
        },
      },
      {
        name: "10-runs-approvals-v3-m1.png",
        prepare: async () => {
          await openRuns(page);
          await page.getByText("Open infrastructure application").first().click();
          await expect(page.getByText("Exact gated action")).toBeVisible();
        },
      },
      {
        name: "11-connectors-settings-v3-m1.png",
        prepare: async () => {
          await page.getByLabel("Open project settings").click();
          await page.getByRole("button", { name: connectorsButtonPattern }).click();
        },
      },
      {
        name: "12-mobile-navigation-v3-m1.png",
        fullPage: false,
        prepare: async () => {
          await switchToMobile(page);
          await openMobileNavigation(page);
        },
      },
    ];

    for (const capture of captures) {
      await test.step(`capture ${capture.name}`, async () => {
        await capture.prepare();
        await saveScreenshot(
          page,
          outputDirectory!,
          capture.name,
          capture.fullPage ?? true,
        );
      });
    }
  });
});
