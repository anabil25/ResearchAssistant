import type {
  AgentBuilderProposal,
  AgentContractView,
  AgentDraftIntent,
  AgentDraftView,
  AgentEvaluationSummary,
  AgentHealthSummary,
  AgentReleaseSummary,
  AgentSetting,
  AgentSummary,
  AgentTraceSummary,
  ApprovalRecord,
  CapabilityDescriptor,
  CapabilityId,
  CapabilityInstance,
  ConnectionView,
  ConnectorSetting,
  LibraryItem,
  MemoryScope,
  MemoryScopeControl,
  MemoryView,
  ProjectSettings,
  RunSummary,
  StudioResult,
  WorkflowBlueprint,
  WorkspaceSummary,
} from "@/lib/types";

const API_BASE = "/api/backend/api";

/**
 * Thrown by `apiFetch` for any non-2xx response. Carries the real HTTP
 * status so callers can render a precise, honest state (unauthorized,
 * unavailable/not-yet-implemented, needs-approval, etc.) instead of a single
 * generic error banner. Still an `Error`, so existing
 * `error instanceof Error ? error.message : ...` call sites keep working.
 */
export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

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
    throw new ApiError(
      payload?.detail ??
        payload?.error ??
        `Research API returned ${response.status}`,
      response.status,
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

// ---------------------------------------------------------------------------
// Agent Studio contract — PENDING BACKEND, target namespace `/v1/agent-studio`.
//
// See the "Agent Studio contract" section in lib/types.ts for the read-model
// rationale. This section was reconciled 2026-07-23/24 across two rounds
// with the coordinating "Workflow page redesign" session:
//   Round 1 — runtime-neutral manifest direction, capability
//     Descriptor/Instance/Binding split, risk classes, memory scopes.
//   Round 2 — replaced the reduced `AgentManifest` idea with distinct UI
//     read models (AgentSummary/AgentContractView/AgentDraftView/
//     AgentReleaseSummary/CapabilityView/ConnectionView); retargeted every
//     endpoint below to `/v1/agent-studio/...`; changed capability maturity
//     to ga|preview|retired|unknown; made connections/memory/specialists
//     rich objects; split immutable Release rows from mutable Draft status;
//     replaced the flat public boundary flag with a structured summary;
//     replaced free-form manifest-change proposals with a concurrency-safe
//     (etag) builder-message -> proposal -> apply flow.
//
// `agentStudioFetch` targets the backend's own `/v1/agent-studio/...` path
// (relative to its API root) through the same `/api/backend` proxy used
// everywhere else — the `/api` segment is a proxy-routing detail, not part
// of the backend's versioned namespace. Every function below issues a real
// request; until the backend ships these routes they will reject with a
// real error (404/502) that callers must surface as an explicit unavailable
// state, never a fabricated success. `getWorkspaceData`'s `/agents` read
// (AgentSetting[]) remains the one legacy exception, used only to build the
// `source: "legacy_agents_endpoint"` fallback in `lib/agent-catalog.ts`.
// ---------------------------------------------------------------------------

async function agentStudioFetch<T>(path: string, init?: RequestInit): Promise<T> {
  return apiFetch<T>(`/v1/agent-studio${path}`, init);
}

/** Released-agent catalog/summary — authoritative once this endpoint exists. */
export async function getAgentStudioCatalog(): Promise<AgentSummary[]> {
  return agentStudioFetch<AgentSummary[]>("/agents");
}

/** Exact, immutable version contract for one released version. */
export async function getAgentRelease(
  agentId: string,
  version: string,
): Promise<{ release: AgentReleaseSummary; contract: AgentContractView }> {
  return agentStudioFetch<{ release: AgentReleaseSummary; contract: AgentContractView }>(
    `/agents/${agentId}/releases/${version}`,
  );
}

/** Full immutable release history for one agent. */
export async function getAgentReleases(
  agentId: string,
): Promise<AgentReleaseSummary[]> {
  return agentStudioFetch<AgentReleaseSummary[]>(`/agents/${agentId}/releases`);
}

/** The mutable in-progress draft contract + concurrency etag for one agent. */
export async function getAgentDraft(agentId: string): Promise<AgentDraftView> {
  return agentStudioFetch<AgentDraftView>(`/agents/${agentId}/draft`);
}

/** Provider-driven capability descriptors and discovered instances. */
export async function getCapabilityCatalog(): Promise<{
  descriptors: CapabilityDescriptor[];
  instances: CapabilityInstance[];
}> {
  return agentStudioFetch<{
    descriptors: CapabilityDescriptor[];
    instances: CapabilityInstance[];
  }>("/capabilities");
}

/** Discovered project model deployments — the only source for model selection. */
export async function getProjectModels(): Promise<
  { id: string; deployment: string }[]
> {
  return agentStudioFetch<{ id: string; deployment: string }[]>("/models");
}

/** Workspace connections bound to this agent, as rich objects (not string ids). */
export async function getAgentConnections(
  agentId: string,
): Promise<ConnectionView[]> {
  return agentStudioFetch<ConnectionView[]>(`/agents/${agentId}/connections`);
}

export async function getAgentHealth(
  agentId: string,
): Promise<AgentHealthSummary> {
  return agentStudioFetch<AgentHealthSummary>(`/agents/${agentId}/health`);
}

/** Evaluation is always advisory signal; see `hard_gates` for the blocking release gates. */
export async function getAgentEvaluation(
  agentId: string,
): Promise<AgentEvaluationSummary> {
  return agentStudioFetch<AgentEvaluationSummary>(`/agents/${agentId}/evaluation`);
}

export async function getAgentDeployment(
  agentId: string,
): Promise<{ status: string; version: string | null }> {
  return agentStudioFetch<{ status: string; version: string | null }>(
    `/agents/${agentId}/deployment`,
  );
}

/** Recent execution traces for this agent. */
export async function getAgentTraces(
  agentId: string,
): Promise<AgentTraceSummary[]> {
  return agentStudioFetch<AgentTraceSummary[]>(`/agents/${agentId}/traces`);
}

/** Independent per-scope memory controls. Persistent scopes default off. */
export async function getAgentMemory(agentId: string): Promise<MemoryView> {
  return agentStudioFetch<MemoryView>(`/agents/${agentId}/memory`);
}

export async function updateAgentMemoryScope(
  agentId: string,
  scope: MemoryScope,
  patch: { enabled?: boolean; retention_days?: number | null },
): Promise<MemoryScopeControl> {
  return agentStudioFetch<MemoryScopeControl>(
    `/agents/${agentId}/memory/${scope}`,
    { method: "PUT", body: JSON.stringify(patch) },
  );
}

/** Erases stored memory for one scope — the "forget" control. */
export async function forgetAgentMemoryScope(
  agentId: string,
  scope: MemoryScope,
): Promise<MemoryScopeControl> {
  return agentStudioFetch<MemoryScopeControl>(
    `/agents/${agentId}/memory/${scope}/forget`,
    { method: "POST" },
  );
}

/**
 * Step 1 of the builder flow: send a natural-language message + the etag the
 * client last observed. The server returns a typed, reviewable proposal —
 * the client never authors a `{path,before,after}` patch itself.
 */
export async function postBuilderMessage(
  draftId: string,
  message: string,
  baseEtag: string,
): Promise<AgentBuilderProposal> {
  return agentStudioFetch<AgentBuilderProposal>(
    `/drafts/${draftId}/builder/messages`,
    { method: "POST", body: JSON.stringify({ message, base_etag: baseEtag }) },
  );
}

/**
 * Step 2 of the builder flow: apply a previously returned proposal, subject
 * to an etag check so a stale draft can never silently overwrite newer
 * changes.
 */
export async function applyBuilderProposal(
  draftId: string,
  proposalId: string,
  baseEtag: string,
): Promise<AgentDraftView> {
  return agentStudioFetch<AgentDraftView>(
    `/drafts/${draftId}/proposals/${proposalId}/apply`,
    { method: "POST", body: JSON.stringify({ base_etag: baseEtag }) },
  );
}

export async function createAgentDraft(
  intent: AgentDraftIntent,
): Promise<AgentDraftView> {
  return agentStudioFetch<AgentDraftView>("/drafts", {
    method: "POST",
    body: JSON.stringify(intent),
  });
}

/**
 * Forking creates a user-owned draft with lineage back to the source
 * release, but never inherits the source agent's privileged connection
 * authorization — the returned draft's connections must be re-granted.
 * System agents can only ever be modified by platform owners; forking a
 * system agent always produces a researcher-owned draft, never a mutation
 * of the system agent itself.
 */
export async function forkAgent(
  agentId: string,
  version?: string,
): Promise<AgentDraftView> {
  return agentStudioFetch<AgentDraftView>(
    `/agents/${agentId}/fork${version ? `?version=${encodeURIComponent(version)}` : ""}`,
    { method: "POST" },
  );
}
