import type {
  AgentBuilderProposal,
  AgentContractView,
  AgentDraftIntent,
  AgentDraftView,
  AgentHealthSummary,
  AgentReleaseSummary,
  AgentSetting,
  AgentSummary,
  AgentTraceSummary,
  ApprovalRecord,
  CapabilityDescriptor,
  CapabilityDiscovery,
  CapabilityId,
  CapabilityInstance,
  ChatAgentChoice,
  ChatAttachment,
  ChatMessage,
  ChatStreamEvent,
  ChatThread,
  ConnectionView,
  ConnectorSetting,
  FoundryAgentInventoryItem,
  LibraryItem,
  MemoryScope,
  MemoryScopeControl,
  MemoryView,
  PersonalProjectCreate,
  PersonalProjectUpdate,
  ProjectSummary,
  ProjectSettings,
  RunSummary,
  StudioResult,
  WorkflowBlueprint,
  WorkspaceSummary,
} from "@/lib/types";

// The Next.js catch-all backend proxy (`src/app/api/backend/[...path]/route.ts`)
// forwards anything matching its `ALLOWED_PREFIXES` allowlist straight through
// to the FastAPI backend root. Every feature router, including Agent Studio,
// is mounted under the backend's own `/api` router prefix, hence a single
// `API_BASE` for both. Agent Studio previously had its own `AGENT_STUDIO_BASE`
// pointed at a standalone `/v1/agent-studio` mount — see the Round 8/9 history
// below for why that was believed to be the real mount point at the time.
// Verified directly against the current backend commit `5dab8b7`
// (`services/api/src/research_assistant_api/agent_studio/router.py`):
// `router = APIRouter(prefix="/api/agent-studio", ...)`, mounted via
// `app.include_router(agent_studio_router)` with no extra prefix — so
// Agent Studio's real, final mount point is `/api/agent-studio`, nested
// under the same `/api` prefix as every other feature router. There is no
// longer a reason for a separate base; `AGENT_STUDIO_BASE` now just adds
// the `/agent-studio` segment onto the shared `API_BASE`.
const PROXY_BASE = "/api/backend";
const API_BASE = `${PROXY_BASE}/api`;
const AGENT_STUDIO_BASE = `${API_BASE}/agent-studio`;

/**
 * Thrown by `apiFetch`/`backendFetch` for any non-2xx response. Carries the
 * real HTTP status so callers can render a precise, honest state
 * (unauthorized, unavailable/not-yet-implemented, needs-approval, etc.)
 * instead of a single generic error banner. Still an `Error`, so existing
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

function apiErrorMessage(payload: unknown, status: number): string {
  if (!payload || typeof payload !== "object") {
    return `Research API returned ${status}`;
  }
  const body = payload as { detail?: unknown; error?: unknown };
  for (const value of [body.detail, body.error]) {
    if (typeof value === "string" && value.trim()) return value;
    if (Array.isArray(value)) {
      const messages = value
        .map((item) =>
          item && typeof item === "object" && "msg" in item
            ? String(item.msg)
            : null,
        )
        .filter((item): item is string => Boolean(item));
      if (messages.length) return messages.join("; ");
    }
    if (value && typeof value === "object" && "message" in value) {
      const message = String(value.message).trim();
      if (message) return message;
    }
  }
  return `Research API returned ${status}`;
}

/**
 * Shared fetch/error-handling core for every backend call, regardless of
 * which base path it's rooted under. `apiFetch` and `agentStudioFetch` are
 * both thin wrappers over this that only differ in which base they prefix.
 */
function requestHeaders(
  headers: HeadersInit | undefined,
  projectId?: string,
): Record<string, string> {
  const providedHeaders = headers
    ? Object.fromEntries(new Headers(headers).entries())
    : {};
  return {
    "Content-Type": "application/json",
    ...providedHeaders,
    ...(projectId ? { "X-Research-Project-ID": projectId } : {}),
  };
}

async function backendFetch<T>(
  fullPath: string,
  init?: RequestInit,
  projectId?: string,
): Promise<T> {
  const response = await fetch(fullPath, {
    ...init,
    headers: requestHeaders(init?.headers, projectId),
  });

  if (!response.ok) {
    const payload: unknown = await response.json().catch(() => null);
    throw new ApiError(apiErrorMessage(payload, response.status), response.status);
  }
  return (await response.json()) as T;
}

async function apiFetch<T>(
  path: string,
  init?: RequestInit,
  projectId?: string,
): Promise<T> {
  return backendFetch<T>(`${API_BASE}${path}`, init, projectId);
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

export async function getWorkspaceData(projectId?: string): Promise<WorkspaceData> {
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
    apiFetch<WorkspaceSummary>("/workspace", undefined, projectId),
    apiFetch<LibraryItem[]>("/library", undefined, projectId),
    apiFetch<RunSummary[]>("/runs", undefined, projectId),
    apiFetch<ApprovalRecord[]>("/approvals", undefined, projectId),
    apiFetch<ConnectorSetting[]>("/connectors", undefined, projectId),
    apiFetch<ProjectSettings>("/settings", undefined, projectId),
    apiFetch<AgentSetting[]>("/agents", undefined, projectId),
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

export async function listProjects(): Promise<ProjectSummary[]> {
  return apiFetch<ProjectSummary[]>("/projects");
}

export async function createProject(
  payload: PersonalProjectCreate,
): Promise<ProjectSummary> {
  return apiFetch<ProjectSummary>("/projects", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function activateProject(projectId: string): Promise<ProjectSummary> {
  return apiFetch<ProjectSummary>(`/projects/${projectId}/activate`, {
    method: "POST",
  });
}

export async function updateProject(
  projectId: string,
  payload: PersonalProjectUpdate,
): Promise<ProjectSummary> {
  return apiFetch<ProjectSummary>(`/projects/${projectId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function runStudio(
  capability: CapabilityId,
  objective: string,
  options: {
    inputs?: Record<string, unknown>;
  } = {},
  projectId?: string,
): Promise<StudioResult> {
  return apiFetch<StudioResult>(`/studios/${capability}/run`, {
    method: "POST",
    body: JSON.stringify({
      objective,
      inputs: options.inputs ?? {},
    }),
  }, projectId);
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
  projectId?: string,
): Promise<ConnectorSetting> {
  return apiFetch<ConnectorSetting>(`/connectors/${connectorId}/test`, {
    method: "POST",
  }, projectId);
}

export async function updateConnector(
  connector: ConnectorSetting,
  projectId?: string,
): Promise<ConnectorSetting> {
  return apiFetch<ConnectorSetting>(`/connectors/${connector.id}`, {
    method: "PUT",
    body: JSON.stringify({
      enabled: connector.enabled,
      assigned_agents: connector.assigned_agents,
    }),
  }, projectId);
}

export async function updateConnectorCredential(
  connectorId: string,
  apiKey: string | null,
  projectId?: string,
): Promise<ConnectorSetting> {
  return apiFetch<ConnectorSetting>(`/connectors/${connectorId}/credential`, {
    method: "PUT",
    body: JSON.stringify({ api_key: apiKey }),
  }, projectId);
}

export async function updateSettings(
  settings: ProjectSettings,
): Promise<ProjectSettings> {
  return apiFetch<ProjectSettings>("/settings", {
    method: "PUT",
    body: JSON.stringify(settings),
  }, settings.project_id);
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
// Agent chat — the conversational surface the four capability studios render.
// Mounted under the same `/api` prefix as every other feature router; see
// `services/api/src/research_assistant_api/agent_chat.py`.
// ---------------------------------------------------------------------------

const AGENT_CHAT_BASE = `${API_BASE}/agent-chat`;

export async function listChatAgents(
  capability: CapabilityId,
  projectId?: string,
): Promise<ChatAgentChoice[]> {
  return backendFetch<ChatAgentChoice[]>(
    `${AGENT_CHAT_BASE}/agents?capability=${encodeURIComponent(capability)}`,
    undefined,
    projectId,
  );
}

export async function openChatThread(
  capability: CapabilityId,
  agentName: string,
  projectId?: string,
): Promise<ChatThread> {
  return backendFetch<ChatThread>(
    `${AGENT_CHAT_BASE}/threads`,
    {
      method: "POST",
      body: JSON.stringify({ capability, agent_name: agentName }),
    },
    projectId,
  );
}

export async function getChatThread(
  threadId: string,
  projectId?: string,
): Promise<ChatThread> {
  return backendFetch<ChatThread>(
    `${AGENT_CHAT_BASE}/threads/${encodeURIComponent(threadId)}`,
    undefined,
    projectId,
  );
}

export async function sendChatMessage(
  threadId: string,
  text: string,
  clientMessageId: string,
  projectId?: string,
): Promise<ChatMessage> {
  return backendFetch<ChatMessage>(
    `${AGENT_CHAT_BASE}/threads/${encodeURIComponent(threadId)}/messages`,
    {
      method: "POST",
      body: JSON.stringify({ text, client_message_id: clientMessageId }),
    },
    projectId,
  );
}

function streamFrameData(frame: string): string {
  return frame
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
}

export async function streamChatMessage(
  threadId: string,
  text: string,
  clientMessageId: string,
  onEvent: (event: ChatStreamEvent) => void,
  projectId?: string,
): Promise<ChatMessage> {
  const response = await fetch(
    `${AGENT_CHAT_BASE}/threads/${encodeURIComponent(threadId)}/messages/stream`,
    {
      method: "POST",
      headers: requestHeaders(undefined, projectId),
      body: JSON.stringify({ text, client_message_id: clientMessageId }),
    },
  );
  if (!response.ok) {
    const payload: unknown = await response.json().catch(() => null);
    throw new ApiError(apiErrorMessage(payload, response.status), response.status);
  }
  if (!response.body) {
    throw new ApiError("The agent stream returned no response body.", 502);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let completed: ChatMessage | null = null;

  const processFrame = (frame: string) => {
    const data = streamFrameData(frame);
    if (!data) return;
    const event = JSON.parse(data) as ChatStreamEvent;
    onEvent(event);
    if (event.type === "error") {
      throw new ApiError(event.detail, event.status);
    }
    if (event.type === "completed") completed = event.message;
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let boundary = /\r?\n\r?\n/.exec(buffer);
      while (boundary?.index != null) {
        processFrame(buffer.slice(0, boundary.index));
        buffer = buffer.slice(boundary.index + boundary[0].length);
        boundary = /\r?\n\r?\n/.exec(buffer);
      }
      if (done) break;
    }
    if (buffer.trim()) processFrame(buffer);
  } finally {
    reader.releaseLock();
  }
  if (!completed) {
    throw new ApiError("The agent stream ended before completing the response.", 502);
  }
  return completed;
}

export async function uploadChatFile(
  threadId: string,
  file: File,
  projectId?: string,
): Promise<ChatAttachment> {
  const body = new FormData();
  body.append("file", file);
  // `requestHeaders` would force `Content-Type: application/json` and strip the
  // multipart boundary the browser generates, so this call sets its headers
  // itself instead of going through `backendFetch`.
  const response = await fetch(
    `${AGENT_CHAT_BASE}/threads/${encodeURIComponent(threadId)}/files`,
    {
      method: "POST",
      body,
      headers: projectId ? { "X-Research-Project-ID": projectId } : undefined,
    },
  );
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
  return (await response.json()) as ChatAttachment;
}

// ---------------------------------------------------------------------------
// Agent Studio contract — PENDING BACKEND, canonical namespace
// `/v1/agent-studio` (see Round 4/8 history below for the flip-flop; the
// backend's real committed router prefix, confirmed by direct source
// inspection, is `/v1/agent-studio` mounted at the app root — not nested
// under `/api`).
//
// See the "Agent Studio contract" section in lib/types.ts for the read-model
// rationale. This section was reconciled 2026-07-23/24 across rounds with
// the coordinating "Workflow page redesign" session:
//   Round 1 — runtime-neutral manifest direction, capability
//     Descriptor/Instance/Binding split, risk classes, memory scopes.
//   Round 2 — replaced the reduced `AgentManifest` idea with distinct UI
//     read models (AgentSummary/AgentContractView/AgentDraftView/
//     AgentReleaseSummary/CapabilityView/ConnectionView); changed capability
//     maturity to ga|preview|retired|unknown; made connections/memory/
//     specialists rich objects; split immutable Release rows from mutable
//     Draft status; added a structured source-policy summary; replaced
//     free-form manifest-change proposals with
//     a concurrency-safe (etag) builder-message -> proposal -> apply flow.
//   Round 3 — split the single `/capabilities` read into three canonical
//     resource shapes: `/capabilities/descriptors` (immutable operation
//     semantics/governance), `/capabilities/instances` (tenant/workspace-
//     scoped discovered readiness/health), and `/capabilities/discovery`
//     (an optional combined `{descriptors,instances,warnings,refreshed_at}`
//     aggregate for one UI load). Replaced the old `CapabilityBinding`
//     (enabled + approval_state + version_pin) with a richer persisted
//     `CapabilityBinding` (pinned descriptor/instance ids+versions,
//     instance_fingerprint, schema digests, config/connection/policy refs,
//     a full `CapabilityApprovalSummary`) plus a derived
//     `CapabilityBindingView {binding,resolved_descriptor,resolved_instance,
//     stale_reason}` for rendering — the view is never the persisted shape.
//   Round 4 — the sibling session reported the backend's actual routing
//     convention is `/api/agent-studio/...` (matching every other real
//     endpoint below `API_BASE`), not the earlier proposed `/v1/agent-studio`
//     version prefix. Retargeted `agentStudioFetch` accordingly. This path
//     is still NOT final — the backend hasn't shipped an OpenAPI contract
//     for this namespace yet, and project-scoping/contract corrections are
//     still in flight upstream — so every caller must keep treating these
//     as real, possibly-404ing requests, never a fabricated success.
//   Round 5 — split capability `maturity` (ga|preview|unknown) from a new,
//     independent `lifecycle` (active|deprecated|retired) on
//     `CapabilityInstance`. New attachment requires ga+active+ready;
//     deprecated/retired instances stay visible (with a surfaced reason)
//     but are never attachable — `retired` is a lifecycle state, not a
//     maturity value.
//   Round 6 — restructured the persisted `CapabilityBinding` from flat
//     scalar ids/versions to typed ref objects (`descriptor`/`instance` as
//     `{id, version[, fingerprint]}`, `configuration`/`connection`/`policy`
//     as nullable `{ref}` objects) and added `provider_contract_version`
//     (the upstream provider's own contract version — never an ambiguous
//     `provider_version` alias) and `destination_constraints` (frozen at
//     bind time, distinct from the live/volatile `CapabilityInstance.destination`).
//   Round 7 — independent review of the Round 2 checkpoint (`7caafc0`)
//     flagged that `AgentReleaseSummary` conflated immutable version
//     identity (version/hash/lineage/model/capability pins) with a mutable
//     `deployment_status` field on the same "immutable" row. Split into a
//     purely immutable `AgentVersionSummary` and a separate, explicitly
//     mutable/derived `DeploymentSummary`; `AgentReleaseSummary` is now
//     `{version_summary, deployment}` — never a single flattened row. Note:
//     most other Round-7 review findings (maturity/lifecycle split, the
//     `/api/agent-studio` routing convention, typed-ref bindings,
//     `AgentContractView`-only) were already addressed in Rounds 4-6 above;
//     the review appears to have been run against the older `7caafc0`
//     checkpoint rather than the current HEAD.
//   Round 8 — the backend genuinely moved its router prefix again, this time
//     verified directly against the real commit (`d6df0fe`, on the sibling
//     backend session's branch) rather than taken on the sibling's word
//     alone: `agent_studio_router = APIRouter(prefix="/v1/agent-studio")`,
//     mounted at the FastAPI app root with `app.include_router(...)` and no
//     additional `/api` wrapper — unlike every other feature router, which
//     bakes its own `/api/...` prefix in. So Round 4's flip to `/api/agent-
//     studio` is itself now superseded; `agentStudioFetch` targets
//     `/v1/agent-studio` again, through a *separate* `AGENT_STUDIO_BASE`
//     (not `API_BASE`, since `/v1/...` isn't nested under `/api/`). The
//     backend proxy's `ALLOWED_PREFIXES` allowlist gained a matching `"v1/"`
//     entry. Also verified against `d6df0fe`'s actual Pydantic models: the
//     persisted `AgentManifest.capabilities` field is `tuple[CapabilityBinding,
//     ...]` — raw bindings, never the expanded `CapabilityBindingView` — so
//     `AgentContractView.capabilities` was corrected to match (see
//     `lib/types.ts`); the UI now resolves each raw binding to a view for
//     rendering client-side via `resolveCapabilityBindingView`, joined
//     against a separately-fetched `getCapabilityDiscovery()` read, instead
//     of assuming the contract embeds pre-resolved views. This round's
//     backend inspection did *not* find a `/versions/{id}/capability-views`
//     sidecar endpoint in `d6df0fe`'s router — that claim isn't corroborated
//     against committed backend source yet, so no client call was added for
//     it; the existing client-side resolve path covers the same need without
//     depending on an unconfirmed endpoint. The approval status enum already
//     matched (`not_required|pending|approved|rejected|expired|revoked`) —
//     no change needed there.
//
//   Round 9 — a further cross-session correction round claimed capability
//     bindings must drop `enabled`/approval, use richer nested pin objects
//     (descriptor id/version/digest, versioned operation, rich instance
//     ref, connection id/auth/authorization digest, destination
//     constraints/digest), a 6-value instance readiness, and a maturity/
//     lifecycle split. Rather than implementing that paraphrase, the real
//     committed Pydantic models in `d6df0fe` (`agent_studio/models.py`)
//     were read in full. Findings: `CapabilityBinding` genuinely has no
//     `enabled`/approval (confirming that part) but is otherwise FLAT —
//     `descriptor_id`/`descriptor_version` (strings, no digest),
//     `operation` (a bare name, no version), `instance_id` (a bare nullable
//     string), `pinned_provider_version`, a single `schema_digest`, inline
//     `config`, flat `connection_ref`/`policy_ref`, and `attached_by`/
//     `attached_at` audit fields — no destination_constraints/digest field
//     exists at all. `CapabilityInstance.readiness` (`InstanceReadiness`) is
//     three values (`ready|degraded|unavailable`), not six, and carries no
//     maturity/lifecycle — maturity lives on `CapabilityOperation`
//     (`OperationMaturity`: `ga|preview|unavailable|retired|unknown`, ONE
//     enum, no separate lifecycle field at all — the earlier maturity/
//     lifecycle split instruction does not match real backend source and
//     has been reverted). Approval (`StudioApprovalRecord`/`ApprovalState:
//     pending|approved|rejected`) is scoped to `version_id` for release/
//     fork/admin-escalation promotion — never a per-capability-binding
//     concept; a capability operation only declares `requires_approval`
//     (boolean). `types.ts` and its tests were corrected to this verified
//     shape; `isCapabilityAttachable` now takes `(operation, instance)` and
//     checks `maturity === "ga"` plus instance readiness only when an
//     instance is actually pinned. (This maturity/lifecycle finding was
//     itself later superseded — see Round 12 below: the backend genuinely
//     added the split afterward, and this round's "no lifecycle field at
//     all" claim no longer describes current backend source.)
//
//   Round 10 — the sibling asked to "rename or strengthen isCapabilityAttachable"
//     to require a `BindabilityDecision.bindable === true` from a
//     backend/provider decision object covering scope/consent/config/schema/
//     connection/policy/destination, with structured reason codes for
//     needs_consent/unauthorized/misconfigured/stale/policy/lifecycle states,
//     failing closed when that decision is missing. Before implementing,
//     grepped the entire `agent_studio` backend module tree (through the
//     latest commit on the backend branch, `a23b73e`) for "bindab" in any
//     casing: zero matches. `OperationMaturity` is still the same single
//     five-value enum (`ga|preview|unavailable|retired|unknown`, docstring:
//     "Only GA is ever attachable") with no lifecycle split, and
//     `InstanceReadiness` is still exactly three values
//     (`ready|degraded|unavailable`) with no `unauthorized`/`needs_consent`/
//     `misconfigured` variants. No `BindabilityDecision` type, field, or
//     reason-code taxonomy exists anywhere in the real backend. Declined to
//     implement it as invented/speculative.
//
//     `a23b73e` DID land real, independently-discovered (not sibling-
//     reported) schema corrections directly relevant to binding robustness:
//     `CapabilityBinding` gained `descriptor_digest` (content digest of the
//     attached descriptor, pinned at attach time), `instance_fingerprint`
//     (copied from the resolved `CapabilityInstance` at attach time),
//     `input_schema_digest`/`output_schema_digest` (replacing the old
//     singular `schema_digest`, now operation-level, copied at attach time),
//     and `config_hash`; `CapabilityInstance` gained `descriptor_version` and
//     its own `instance_fingerprint`; `CapabilityOperation` gained
//     `input_schema_digest`/`output_schema_digest`; a persisted
//     `ToolRegistrationSpec` type was added (backend renamed the old
//     `ToolRegistration` model to free that name for the future
//     non-serializable runtime handler object — this UI must never build a
//     runtime-handler read model under either name). Adopted all of these
//     field-for-field in `lib/types.ts`.
//
//     Also strengthened `resolveCapabilityBindingView` to mirror the
//     backend's real (registry-level, not-yet-a-hard-gate)
//     `CapabilityRegistry.check_binding_freshness(binding)`: a resolved
//     instance whose `readiness` is `unavailable` is now itself a stale
//     reason (previously only a missing/unresolvable instance was staleness;
//     an unavailable-but-still-resolvable one was silently just
//     non-attachable-but-"fresh"), and a mismatched `instance_fingerprint`
//     between the binding and the live instance now surfaces a
//     "reconfigured since attach" stale reason. Descriptor *content*-digest
//     drift (`descriptor_digest`) is NOT reproduced client-side — the wire
//     `CapabilityDescriptor` type carries no live-recomputed digest to
//     compare against, and reimplementing the backend's canonical-JSON
//     digest algorithm in TS would be an invented, unverifiable duplicate;
//     `version` comparison remains the UI's proxy signal for that case.
//
//   Round 11 — the sibling reported the earlier `/v1/agent-studio` mount
//     point from Round 8 (`d6df0fe`) was itself a temporary Phase1
//     checkpoint, and the current backend HEAD reverts to `/api/agent-studio`.
//     Verified directly against the real commit (`5dab8b7`, on the sibling
//     backend session's branch), not taken on the sibling's word alone:
//     `services/api/src/research_assistant_api/agent_studio/router.py`
//     defines `router = APIRouter(prefix="/api/agent-studio", ...)`, and
//     `app.py` does `app.include_router(agent_studio_router)` with no
//     additional prefix — so the final mount point is `/api/agent-studio`,
//     nested under the same `/api` prefix as every other feature router.
//     Retargeted `AGENT_STUDIO_BASE` to `${API_BASE}/agent-studio`, removing
//     the separate `/v1/agent-studio` base entirely. The backend proxy's
//     dedicated `v1/agent-studio` allowlist entry (added in the Round 8
//     follow-up) is likewise removed — the namespace is now reachable via
//     the existing generic `"api/"` prefix, and leaving the old entry in
//     place would keep open a route nothing serves.
//
//
//   Round 12 — the sibling asked to implement a capability two-axis
//     maturity/lifecycle model, citing the contract as "stable in backend
//     since `5dab8b7`". Rather than trusting that at face value (per this
//     round's own Round-9 experience of a paraphrase not matching real
//     source), the committed Pydantic models were read directly again at
//     `5dab8b7` (`agent_studio/models.py`). Finding: the claim is TRUE and
//     supersedes Round 9 — the backend genuinely evolved between `d6df0fe`
//     and `5dab8b7` to add the split. `OperationMaturity` is now exactly
//     three values (`ga|preview|unknown`, not five — `retired`/`unavailable`
//     are gone from this enum). A new, fully independent `OperationLifecycle`
//     enum (`active|deprecated|retired`) was added as a separate field on
//     `CapabilityOperation` (defaulting to `active`), alongside `maturity`.
//     `CapabilityOperation.is_bindable` (`maturity == GA and lifecycle ==
//     ACTIVE`) is a plain `@property`, not a `@computed_field`, so it is
//     never serialized on the wire — `isCapabilityAttachable` in
//     `lib/types.ts` was updated to independently derive the same rule
//     client-side (an explicitly-documented preliminary/display-only
//     derivation, not a backend mirror), now requiring BOTH `maturity ===
//     "ga"` AND `lifecycle === "active"` before falling through to the
//     existing conservative instance-readiness check. Tests cover the full
//     maturity×lifecycle matrix and explicitly
//     asserting no false-positive "attachable" outside `ga`+`active`. Per
//     explicit instruction, the route/OpenAPI integration itself was NOT
//     touched in this round — that remains frozen pending the backend's
//     final generated contract.
//
// `agentStudioFetch` is the single choke point for this namespace: every
// Agent Studio read/write goes through it, through the same `/api/backend`
// proxy used everywhere else (just a different sub-path than `apiFetch`).
// That's deliberate — when the backend's final OpenAPI lands, only this one
// function's base path (and the generated types layered underneath
// `lib/types.ts`) should need to change; no consuming component should ever
// hard-code an Agent Studio path itself. Every function below issues a real
// request; until the backend ships these routes they will reject with a
// real error (404/502) that callers must surface as an explicit unavailable
// state, never a fabricated success.
// ---------------------------------------------------------------------------

async function agentStudioFetch<T>(path: string, init?: RequestInit): Promise<T> {
  return backendFetch<T>(`${AGENT_STUDIO_BASE}${path}`, init);
}

/** Released-agent catalog/summary — authoritative once this endpoint exists. */
export async function getAgentStudioCatalog(): Promise<AgentSummary[]> {
  return agentStudioFetch<AgentSummary[]>("/agents");
}

/** Live inventory of the single Foundry project configured for this Studio deployment. */
export async function getFoundryAgentInventory(
): Promise<FoundryAgentInventoryItem[]> {
  return agentStudioFetch<FoundryAgentInventoryItem[]>("/foundry/agents");
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

/**
 * Provider-driven capability descriptors — immutable operation
 * semantics/governance, independent of any tenant/workspace discovery state.
 */
export async function getCapabilityDescriptors(): Promise<CapabilityDescriptor[]> {
  return agentStudioFetch<CapabilityDescriptor[]>("/capabilities/descriptors");
}

/** Tenant/workspace-scoped discovered capability instances (readiness/health, never secrets). */
export async function getCapabilityInstances(): Promise<CapabilityInstance[]> {
  return agentStudioFetch<CapabilityInstance[]>("/capabilities/instances");
}

/**
 * Convenience aggregate for one UI load — combines the descriptor and
 * instance catalogs plus discovery warnings/freshness. A derived/expanded
 * read, not a distinct persisted resource; `getCapabilityDescriptors`/
 * `getCapabilityInstances` remain the canonical per-resource reads.
 */
export async function getCapabilityDiscovery(projectId?: string): Promise<CapabilityDiscovery> {
  const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
  return agentStudioFetch<CapabilityDiscovery>(`/capabilities/discovery${query}`);
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
