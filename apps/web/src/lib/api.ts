import type {
  AgentSetting,
  ApprovalRecord,
  CapabilityId,
  ConnectorSetting,
  LibraryItem,
  ProjectSettings,
  RunSummary,
  StudioResult,
  WorkflowBlueprint,
  WorkspaceSummary,
} from "@/lib/types";

const API_BASE = "/api/backend/api";

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });

  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as {
      detail?: string;
      error?: string;
    } | null;
    throw new Error(
      payload?.detail ??
        payload?.error ??
        `Research API returned ${response.status}`,
    );
  }
  return (await response.json()) as T;
}

export interface WorkspaceData {
  summary: WorkspaceSummary;
  library: LibraryItem[];
  runs: RunSummary[];
  approvals: ApprovalRecord[];
  connectors: ConnectorSetting[];
  settings: ProjectSettings;
  agents: AgentSetting[];
  workflows: WorkflowBlueprint[];
}

export async function getWorkspaceData(): Promise<WorkspaceData> {
  const [
    summary,
    library,
    runs,
    approvals,
    connectors,
    settings,
    agents,
    workflows,
  ] = await Promise.all([
    apiFetch<WorkspaceSummary>("/workspace"),
    apiFetch<LibraryItem[]>("/library"),
    apiFetch<RunSummary[]>("/runs"),
    apiFetch<ApprovalRecord[]>("/approvals"),
    apiFetch<ConnectorSetting[]>("/connectors"),
    apiFetch<ProjectSettings>("/settings"),
    apiFetch<AgentSetting[]>("/agents"),
    apiFetch<WorkflowBlueprint[]>("/workflows"),
  ]);
  return {
    summary,
    library,
    runs,
    approvals,
    connectors,
    settings,
    agents,
    workflows,
  };
}

export async function runStudio(
  capability: CapabilityId,
  objective: string,
  options: {
    onlineResearch?: boolean;
    inputs?: Record<string, unknown>;
  } = {},
): Promise<StudioResult> {
  return apiFetch<StudioResult>(`/studios/${capability}/run`, {
    method: "POST",
    body: JSON.stringify({
      objective,
      online_research: options.onlineResearch ?? false,
      inputs: options.inputs ?? {},
    }),
  });
}

export async function decideApproval(
  approvalId: string,
  decision: "approved" | "rejected",
  rationale: string,
): Promise<ApprovalRecord> {
  return apiFetch<ApprovalRecord>(`/approvals/${approvalId}/decision`, {
    method: "POST",
    body: JSON.stringify({ decision, rationale }),
  });
}

export async function testConnector(
  connectorId: string,
): Promise<ConnectorSetting> {
  return apiFetch<ConnectorSetting>(`/connectors/${connectorId}/test`, {
    method: "POST",
  });
}

export async function updateConnector(
  connector: ConnectorSetting,
): Promise<ConnectorSetting> {
  return apiFetch<ConnectorSetting>(`/connectors/${connector.id}`, {
    method: "PUT",
    body: JSON.stringify({
      enabled: connector.enabled,
      assigned_agents: connector.assigned_agents,
    }),
  });
}

export async function updateSettings(
  settings: ProjectSettings,
): Promise<ProjectSettings> {
  return apiFetch<ProjectSettings>("/settings", {
    method: "PUT",
    body: JSON.stringify(settings),
  });
}

export async function ingestLibraryItem(payload: {
  title: string;
  kind: string;
  source: string;
  access: string;
  license: string;
  description: string;
}): Promise<{ item: LibraryItem; run: RunSummary }> {
  return apiFetch<{ item: LibraryItem; run: RunSummary }>("/library/ingest", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function uploadLibraryItem(
  formData: FormData,
): Promise<{ item: LibraryItem; run: RunSummary }> {
  const response = await fetch(`${API_BASE}/library/upload`, {
    method: "POST",
    body: formData,
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as {
      detail?: string;
      error?: string;
    } | null;
    throw new Error(
      payload?.detail ??
        payload?.error ??
        `Research API returned ${response.status}`,
    );
  }
  return (await response.json()) as { item: LibraryItem; run: RunSummary };
}
