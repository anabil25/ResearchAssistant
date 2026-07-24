import {
  connectorAvailability,
  connectorAvailabilityCaption,
  isConnectorRunnable,
} from "./connector-availability";

describe("connectorAvailability", () => {
  it("maps enabled + ready test_status to ready", () => {
    expect(
      connectorAvailability({ enabled: true, test_status: "ready" }),
    ).toBe("ready");
  });

  it("maps enabled + ready_with_key test_status to ready", () => {
    expect(
      connectorAvailability({ enabled: true, test_status: "ready_with_key" }),
    ).toBe("ready");
  });

  it("maps enabled + configuration_required to needs-connection", () => {
    expect(
      connectorAvailability({
        enabled: true,
        test_status: "configuration_required",
      }),
    ).toBe("needs-connection");
  });

  it("maps enabled + unavailable to unavailable", () => {
    expect(
      connectorAvailability({ enabled: true, test_status: "unavailable" }),
    ).toBe("unavailable");
  });

  it("maps any other test_status while enabled to untested", () => {
    expect(
      connectorAvailability({
        enabled: true,
        test_status: "not_configured",
      }),
    ).toBe("untested");
  });

  it("maps disabled to disabled regardless of a ready test_status", () => {
    expect(
      connectorAvailability({ enabled: false, test_status: "ready" }),
    ).toBe("disabled");
  });

  it("maps disabled to disabled regardless of an unavailable test_status", () => {
    expect(
      connectorAvailability({ enabled: false, test_status: "unavailable" }),
    ).toBe("disabled");
  });
});

describe("isConnectorRunnable", () => {
  it("is true only for the ready availability category", () => {
    expect(
      isConnectorRunnable({ enabled: true, test_status: "ready" }),
    ).toBe(true);
    expect(
      isConnectorRunnable({ enabled: true, test_status: "ready_with_key" }),
    ).toBe(true);
  });

  it("is false for needs-connection, unavailable, disabled, and untested", () => {
    expect(
      isConnectorRunnable({
        enabled: true,
        test_status: "configuration_required",
      }),
    ).toBe(false);
    expect(
      isConnectorRunnable({ enabled: true, test_status: "unavailable" }),
    ).toBe(false);
    expect(
      isConnectorRunnable({ enabled: false, test_status: "ready" }),
    ).toBe(false);
    expect(
      isConnectorRunnable({ enabled: true, test_status: "not_configured" }),
    ).toBe(false);
  });
});

describe("connectorAvailabilityCaption", () => {
  it("returns null for ready (no caption needed)", () => {
    expect(connectorAvailabilityCaption("ready")).toBeNull();
  });

  it("returns a distinct human-readable caption for every non-ready category", () => {
    expect(connectorAvailabilityCaption("needs-connection")).toBe(
      "Needs connection setup",
    );
    expect(connectorAvailabilityCaption("unavailable")).toBe(
      "Currently unavailable",
    );
    expect(connectorAvailabilityCaption("disabled")).toBe(
      "Disabled in Settings",
    );
    expect(connectorAvailabilityCaption("untested")).toBe("Not yet tested");
  });
});
