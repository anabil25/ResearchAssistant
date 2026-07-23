import type { ConnectorSetting } from "@/lib/types";

export const CONNECTOR_SPECIALISTS = [
  "literature",
  "grant",
  "matching",
  "dataset",
  "institution",
] as const;

export interface ConnectorStatusInfo {
  label: string;
  detail: string;
  tone:
    | "disabled"
    | "configuration-required"
    | "unavailable"
    | "warning"
    | "ready"
    | "untested";
}

export function connectorVersionStatusLabel(value: string): string {
  if (value === "configuration_required") return "setup required";
  if (value === "ready_with_key") return "ready, key recommended";
  return value.replaceAll("_", " ");
}

export function connectorStatusInfo(
  connector: ConnectorSetting,
): ConnectorStatusInfo {
  if (!connector.enabled) {
    return {
      label: "Disabled",
      detail:
        "This connector is intentionally disabled and will not be used by research runs.",
      tone: "disabled",
    };
  }
  if (connector.test_status === "configuration_required") {
    return {
      label: "Setup required",
      detail:
        "The provider is not down. An administrator must configure the connector gateway URL and managed identity before tests can reach it.",
      tone: "configuration-required",
    };
  }
  if (connector.test_status === "unavailable") {
    return {
      label: "Connection failed",
      detail:
        "The gateway is configured, but the latest bounded provider probe failed. Retry the test or inspect gateway logs before using this source.",
      tone: "unavailable",
    };
  }
  if (connector.test_status === "ready_with_key") {
    return {
      label: "Ready, key recommended",
      detail:
        "The connector is reachable with limited anonymous quota. Add the optional deployment-managed key for more reliable capacity.",
      tone: "warning",
    };
  }
  if (connector.test_status === "ready") {
    return {
      label: "Ready",
      detail:
        "The latest bounded probe succeeded and this connector can serve its assigned specialists.",
      tone: "ready",
    };
  }
  return {
    label: "Not tested",
    detail: "Run a bounded connection test before relying on this source.",
    tone: "untested",
  };
}

export function connectorResultTone(
  tone: ConnectorStatusInfo["tone"],
): "success" | "warning" | "error" {
  if (tone === "unavailable") return "error";
  if (
    tone === "configuration-required" ||
    tone === "warning" ||
    tone === "untested"
  ) {
    return "warning";
  }
  return "success";
}

export function filterConnectors(
  connectors: ConnectorSetting[],
  category: string,
  query: string,
): ConnectorSetting[] {
  const normalizedQuery = query.toLowerCase();
  return connectors.filter(
    (connector) =>
      (category === "All" || connector.category === category) &&
      `${connector.name} ${connector.description}`
        .toLowerCase()
        .includes(normalizedQuery),
  );
}

export function updateConnectorAssignment(
  assignedAgents: string[],
  agent: string,
  assigned: boolean,
): string[] {
  if (assigned) {
    return assignedAgents.includes(agent)
      ? assignedAgents
      : [...assignedAgents, agent];
  }
  return assignedAgents.filter((item) => item !== agent);
}
