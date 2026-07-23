import {
  DECLARED_SCREENSHOT_IDS,
  INTERACTION_GAPS,
  INTERACTION_MANIFEST,
  UI_COVERAGE_MANIFEST,
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
    expect(UI_COVERAGE_MANIFEST).toHaveLength(INTERACTION_MANIFEST.length);
    for (const interaction of UI_COVERAGE_MANIFEST) {
      expect(interaction.route).toMatch(/^\/(?:\?|$)/);
      expect(interaction.viewports).toEqual(["desktop", "tablet", "mobile"]);
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
      expect(
        interaction.rtlTestIds.every((id) => id.startsWith("jest.")),
      ).toBe(true);
      expect(
        interaction.playwrightTestIds.every((id) => id.startsWith("pw.")),
      ).toBe(true);
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
