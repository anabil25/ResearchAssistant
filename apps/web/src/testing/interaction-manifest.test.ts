import {
  CORE_SCREENSHOT_CONTRACTS,
  DECLARED_SCREENSHOT_IDS,
  INTERACTION_GAPS,
  INTERACTION_MANIFEST,
  STATE_SCREENSHOT_IDS,
  UI_COVERAGE_MANIFEST,
  viewportsForInteraction,
} from "./interaction-manifest";

describe("V3 interaction manifest", () => {
  it("has unique stable IDs and executable acceptance metadata", () => {
    const ids = INTERACTION_MANIFEST.map((item) => item.id);

    expect(new Set(ids).size).toBe(ids.length);
    for (const interaction of INTERACTION_MANIFEST) {
      expect(interaction.behavior.length).toBeGreaterThan(20);
      expect(interaction.states.length).toBeGreaterThan(0);
      expect(interaction.testIds.length).toBeGreaterThan(0);
      expect(interaction.testIds.every((id) => id.includes("."))).toBe(true);
    }
  });

  it("keeps zero unwired or missing visible interactions", () => {
    expect(INTERACTION_GAPS).toHaveLength(0);
    expect(
      INTERACTION_MANIFEST.every(
        (item) =>
          item.baseline === "functional-covered" ||
          item.baseline === "functional-uncovered",
      ),
    ).toBe(true);
  });

  it("no longer lists the obsolete project switcher now that the header is noninteractive", () => {
    expect(
      INTERACTION_MANIFEST.some(
        (item) => item.id === "shell.navigation.project-switcher",
      ),
    ).toBe(false);
  });

  it("links every control and state to routes, viewports, tests, and screenshots", () => {
    expect(CORE_SCREENSHOT_CONTRACTS.map((contract) => contract.id)).toEqual(
      expect.arrayContaining([
        "visual.core.overview",
        "visual.core.literature",
        "visual.core.workflow",
      ]),
    );
    expect(STATE_SCREENSHOT_IDS).toEqual([
      "visual.state.empty",
      "visual.state.loading",
      "visual.state.error",
      "visual.state.authorization",
    ]);
    expect(UI_COVERAGE_MANIFEST).toHaveLength(INTERACTION_MANIFEST.length);
    // The classification must be a real partition, not a constant wearing a
    // function's clothes: some interactions genuinely scope to all three
    // viewports and most genuinely scope to desktop only.
    const multiViewport = UI_COVERAGE_MANIFEST.filter(
      (interaction) => interaction.viewports.length > 1,
    );
    expect(multiViewport.length).toBeGreaterThan(0);
    expect(multiViewport.length).toBeLessThan(UI_COVERAGE_MANIFEST.length);
    // Everything classified viewport-sensitive is a shell surface -- the
    // navigation rail, command palette and approvals control are the
    // elements this app's `@media (max-width: 900px)` and `(max-width:
    // 680px)` rules actually restructure.
    for (const interaction of multiViewport) {
      expect(interaction.id.startsWith("shell.")).toBe(true);
      expect(interaction.viewports).toEqual(["desktop", "tablet", "mobile"]);
    }
    for (const interaction of UI_COVERAGE_MANIFEST) {
      expect(interaction.route).toMatch(/^\/(?:\?|$)/);
      // Previously asserted `["desktop","tablet","mobile"]` for *every*
      // interaction, which is what a blanket `viewports: ALL_VIEWPORTS`
      // produced. That assertion did not verify anything -- it restated a
      // constant -- while the manifest claimed all 77 interactions and all
      // 298 states were covered at three breakpoints, when runtime evidence
      // showed tablet and mobile proving three states each. Scope is now
      // classified per interaction and grounded in this app's actual media
      // queries, so this asserts the classification instead of the constant.
      expect(interaction.viewports).toEqual(
        viewportsForInteraction(interaction.id),
      );
      expect(
        interaction.viewports.length === 1 || interaction.viewports.length === 3,
      ).toBe(true);
      expect(interaction.classifiedStates).toHaveLength(
        interaction.states.length,
      );
      expect(interaction.screenshotIds.length).toBeGreaterThan(0);
      expect(
        interaction.screenshotIds.every((id) =>
          DECLARED_SCREENSHOT_IDS.has(id),
        ),
      ).toBe(true);
      expect(
        interaction.rtlTestIds.length + interaction.playwrightTestIds.length,
      ).toBe(interaction.testIds.length);
      expect(interaction.playwrightTestIds.length).toBeGreaterThan(0);
      expect(
        interaction.rtlTestIds.every((id) => id.startsWith("jest.")),
      ).toBe(true);
      expect(
        interaction.playwrightTestIds.every((id) => id.startsWith("pw.")),
      ).toBe(true);
    }
  });

  it("no longer derives a blanket per-state Playwright coverage claim", () => {
    // Regression guard for the bug this task fixes: every interaction must NOT carry a
    // pre-derived `playwrightStateTestIds`-style field that maps every state to every
    // test id. Truthful per-state coverage is machine-checked in
    // e2e/coverage-contract.spec.ts by scanning actual `[pw.<id>:<state>]` test title
    // tokens, not declared in this manifest.
    for (const interaction of UI_COVERAGE_MANIFEST) {
      expect(
        Object.prototype.hasOwnProperty.call(
          interaction,
          "playwrightStateTestIds",
        ),
      ).toBe(false);
    }
  });

  it("classifies async, empty, error, and authorization states explicitly", () => {
    const stateKinds = new Set(
      UI_COVERAGE_MANIFEST.flatMap((interaction) =>
        interaction.classifiedStates.map((state) => state.kind),
      ),
    );
    expect(stateKinds).toEqual(
      new Set(["behavior", "async", "empty", "error", "auth"]),
    );
  });
});
