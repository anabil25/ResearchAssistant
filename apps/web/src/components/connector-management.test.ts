import type { ConnectorSetting } from "@/lib/types";

import {
  connectorResultTone,
  connectorStatusInfo,
  connectorVersionStatusLabel,
  filterConnectors,
  updateConnectorAssignment,
} from "./connector-management";

function connector(
  overrides: Partial<ConnectorSetting> = {},
): ConnectorSetting {
  return {
    id: "openalex",
    name: "OpenAlex",
    category: "Literature",
    description: "Public scholarly metadata.",
    auth_kind: "None",
    secret_status: "Not required",
    enabled: true,
    test_status: "untested",
    last_tested_at: null,
    assigned_agents: ["literature"],
    terms_url: "https://openalex.org/terms",
    data_boundary: "Public metadata only.",
    capabilities: ["Works"],
    ...overrides,
  };
}

describe("connector management policy", () => {
  it.each([
    ["configuration_required", "setup required"],
    ["ready_with_key", "ready, key recommended"],
    ["not_tested_yet", "not tested yet"],
  ])("formats version status %s", (value, expected) => {
    expect(connectorVersionStatusLabel(value)).toBe(expected);
  });

  it.each([
    [
      { enabled: false },
      "Disabled",
      "disabled",
      "intentionally disabled",
    ],
    [
      { test_status: "configuration_required" },
      "Setup required",
      "configuration-required",
      "provider is not down",
    ],
    [
      { test_status: "unavailable" },
      "Connection failed",
      "unavailable",
      "bounded provider probe failed",
    ],
    [
      { test_status: "ready_with_key" },
      "Ready, key recommended",
      "warning",
      "limited anonymous quota",
    ],
    [
      { test_status: "ready" },
      "Ready",
      "ready",
      "probe succeeded",
    ],
    [
      { test_status: "untested" },
      "Not tested",
      "untested",
      "Run a bounded connection test",
    ],
  ] as const)(
    "maps connector state to truthful diagnostics",
    (overrides, label, tone, detail) => {
      expect(connectorStatusInfo(connector(overrides))).toEqual(
        expect.objectContaining({
          label,
          tone,
          detail: expect.stringContaining(detail),
        }),
      );
    },
  );

  it.each([
    ["unavailable", "error"],
    ["configuration-required", "warning"],
    ["warning", "warning"],
    ["untested", "warning"],
    ["ready", "success"],
    ["disabled", "success"],
  ] as const)("maps %s to a user notification tone", (tone, expected) => {
    expect(connectorResultTone(tone)).toBe(expected);
  });

  it("filters by category and case-insensitive name or description", () => {
    const connectors = [
      connector(),
      connector({
        id: "grants",
        name: "Grants.gov",
        category: "Funding",
        description: "Federal opportunities",
      }),
    ];

    expect(filterConnectors(connectors, "All", "federal")).toEqual([
      connectors[1],
    ]);
    expect(filterConnectors(connectors, "Literature", "OPEN")).toEqual([
      connectors[0],
    ]);
    expect(filterConnectors(connectors, "Funding", "openalex")).toEqual([]);
  });

  it("adds assignments once and removes every matching assignment", () => {
    expect(updateConnectorAssignment(["grant"], "literature", true)).toEqual([
      "grant",
      "literature",
    ]);
    expect(updateConnectorAssignment(["grant"], "grant", true)).toEqual([
      "grant",
    ]);
    expect(
      updateConnectorAssignment(["grant", "literature", "grant"], "grant", false),
    ).toEqual(["literature"]);
  });
});
