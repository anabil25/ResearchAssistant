import { REQUIRED_PLAYWRIGHT_PROJECT_NAMES } from "./playwright-projects";

describe("REQUIRED_PLAYWRIGHT_PROJECT_NAMES", () => {
  it("declares exactly the three projects configured in playwright.config.ts", () => {
    // Kept in sync with `playwright.config.ts`, which imports this same
    // constant to name its `projects` entries -- see that file's
    // `chromiumProjectName`/`tabletProjectName`/`mobileProjectName`
    // destructuring. A drift here would mean the atomic release gate
    // (`scripts/run-e2e-coverage-gate.mjs`) validates against a project set
    // that no longer matches what actually runs.
    expect(REQUIRED_PLAYWRIGHT_PROJECT_NAMES).toEqual([
      "chromium",
      "tablet-chromium",
      "mobile-chromium",
    ]);
  });

  it("contains no duplicate names", () => {
    expect(new Set(REQUIRED_PLAYWRIGHT_PROJECT_NAMES).size).toBe(
      REQUIRED_PLAYWRIGHT_PROJECT_NAMES.length,
    );
  });
});
