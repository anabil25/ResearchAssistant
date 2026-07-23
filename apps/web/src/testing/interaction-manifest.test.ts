import {
  INTERACTION_GAPS,
  INTERACTION_MANIFEST,
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
});
