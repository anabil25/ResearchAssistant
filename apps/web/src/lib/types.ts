import type { components } from "@/lib/generated-api";

export type CapabilityId = components["schemas"]["Capability"];

export interface Capability {
  id: CapabilityId;
  title: string;
  shortTitle: string;
  description: string;
  examplePrompt: string;
  accent: string;
}

export type Citation = components["schemas"]["Citation"];
export type ArtifactSection = components["schemas"]["ArtifactSection"];
export type Metric = components["schemas"]["Metric"];
export type MatchItem = components["schemas"]["MatchItem"];
export type RankedEntity = components["schemas"]["RankedEntity"];
export type ScoreComponent = components["schemas"]["ScoreComponent"];
export type AutomationStep = components["schemas"]["AutomationStep"];
export type ScreeningDecision = components["schemas"]["ScreeningDecision"];
export type RunRecord = components["schemas"]["RunRecord"];
export type StudioRun = components["schemas"]["StudioRun"];
export type WorkspaceSummary = components["schemas"]["WorkspaceSummary"];
export type LibraryItem = components["schemas"]["LibraryItem"];
export type RunSummary = components["schemas"]["RunSummary"];
export type ApprovalRecord = components["schemas"]["ApprovalRecord"];
export type ConnectorSetting = components["schemas"]["ConnectorSetting"];
export type ProjectSettings = components["schemas"]["ProjectSettings"];
export type AgentSetting = components["schemas"]["AgentSetting"];
export type LiteratureStudioResult =
  components["schemas"]["LiteratureStudioResult"];
export type GrantStudioResult = components["schemas"]["GrantStudioResult"];
export type MatchingStudioResult =
  components["schemas"]["MatchingStudioResult"];
export type DatasetStudioResult = components["schemas"]["DatasetStudioResult"];
export type InstitutionalStudioResult =
  components["schemas"]["InstitutionalStudioResult"];
export type AutomationStudioResult =
  components["schemas"]["AutomationStudioResult"];
export type StudioResult =
  | LiteratureStudioResult
  | GrantStudioResult
  | MatchingStudioResult
  | DatasetStudioResult
  | InstitutionalStudioResult
  | AutomationStudioResult;

export interface WorkflowStage {
  id: string;
  label: string;
  description: string;
  owner: string;
  human_checkpoint: boolean;
}

export interface WorkflowBlueprint {
  capability: CapabilityId;
  title: string;
  purpose: string;
  primary_artifact: string;
  online_research_policy: string;
  stages: WorkflowStage[];
}

type GeneratedResearchResult = components["schemas"]["ResearchResult"];

export type ResearchResult = Omit<
  GeneratedResearchResult,
  "matches" | "metadata" | "metrics" | "warnings"
> & {
  matches: MatchItem[];
  metadata: Record<string, unknown>;
  metrics: Metric[];
  warnings: string[];
};

// ---------------------------------------------------------------------------
// Agent Studio contract — PENDING BACKEND, target namespace `/v1/agent-studio`.
//
// `packages/contracts/openapi.json` only defines `AgentSetting` today (id,
// name, deployment, model_tier, status, web_access, workflow_steps). The
// types below are UI-facing READ MODELS — not a reduced mirror of a backend
// `AgentManifest` row — reconciled with the coordinating "Workflow page
// redesign" session across two rounds of contract alignment. When the
// backend ships generated OpenAPI types for `/v1/agent-studio/**`,
// `lib/api.ts` should adapt those into these same read models rather than
// changing every consuming component; that boundary is the point of keeping
// this as a distinct layer instead of passing a raw manifest through.
//
// Until those endpoints exist, every function in `lib/api.ts` that returns
// these types performs a real fetch against the real proxy and will
// genuinely fail (404/502) — UI code MUST render an explicit
// unavailable/error state on failure and must never fabricate a successful
// response. The one exception is `AgentSummary.source === "legacy_agents_endpoint"`,
// which is populated only from the real `/api/agents` `AgentSetting` fields
// (id, name, deployment, model_tier, status, web_access) — never from
// hand-authored copy — with every field `/agents` doesn't return left
// visibly `null`/"Not available yet".
// ---------------------------------------------------------------------------

/** Draft lifecycle only. Never applied to an immutable release row (see AgentReleaseSummary). */
export type AgentDraftStatus =
  | "editing"
  | "validating"
  | "evaluating"
  | "ready_for_review"
  | "rejected";

/** Where a specific immutable release is actually running, if anywhere. */
export type AgentDeploymentStatus = "not_deployed" | "deployed" | "rolled_back";

/** Registry-level lifecycle, derived from draft/release state but distinct from either. */
export type AgentRegistryLifecycle =
  | "draft_only"
  | "released"
  | "deprecated"
  | "archived";

/**
 * Capability maturity: exactly these four values, no `experimental` alias.
 * `unknown` is always non-attachable — see `isCapabilityAttachable`.
 */
export type CapabilityMaturity = "ga" | "preview" | "retired" | "unknown";

/**
 * Operation risk classification shared with the Workflow page redesign
 * contract. Distinct from capability maturity: risk class governs approval
 * requirements, maturity governs availability/attachability.
 */
export type CapabilityRiskClass =
  | "pure"
  | "read"
  | "write_reversible"
  | "write_irreversible"
  | "privileged";

export type CapabilityApprovalState =
  | "not_required"
  | "required"
  | "pending"
  | "granted";

/** Provider-driven catalog entry for a capability family + operation. */
export interface CapabilityDescriptor {
  id: string;
  family: string;
  operation: string;
  risk_class: CapabilityRiskClass;
  description: string;
}

/** A concrete, discovered deployment of a descriptor in this project. */
export interface CapabilityInstance {
  id: string;
  descriptor_id: string;
  maturity: CapabilityMaturity;
  provider: string;
  destination: string | null;
  /** Readiness to attach. Always `false` when `maturity` is `"unknown"`. */
  available: boolean;
}

/** How a specific agent is bound to a discovered capability instance, with an exact version pin. */
export interface CapabilityBinding {
  instance_id: string;
  enabled: boolean;
  approval_state: CapabilityApprovalState;
  version_pin: string | null;
}

/** Combined descriptor + discovered instance + agent binding for one capability row in the UI. */
export interface CapabilityView {
  descriptor: CapabilityDescriptor;
  instance: CapabilityInstance | null;
  binding: CapabilityBinding | null;
}

/** Only GA operations attach; `unknown` maturity is always non-attachable regardless of `available`. */
export function isCapabilityAttachable(
  instance: CapabilityInstance | null | undefined,
): boolean {
  if (!instance) return false;
  if (instance.maturity === "unknown") return false;
  return instance.maturity === "ga" && instance.available;
}

/** @deprecated Use CapabilityDescriptor + CapabilityInstance + CapabilityBinding via CapabilityView. */
export interface AgentCapabilityRef {
  id: string;
  family: string;
  operation: string;
  maturity: CapabilityMaturity;
}

export interface AgentHealthSummary {
  state: "healthy" | "degraded" | "unavailable" | "unknown";
  last_checked_at: string | null;
  detail: string;
}

export interface AgentEvaluationGate {
  id: string;
  label: string;
  passing: boolean;
}

export interface AgentEvaluationSummary {
  /** Evaluations are always advisory signal, never a hard release gate. */
  advisory: true;
  citation_resolution: number | null;
  claim_entailment: number | null;
  retrieval_completeness: number | null;
  last_run_at: string | null;
  /** Objective, blocking release gates — evaluated separately from advisory metrics above. */
  hard_gates: AgentEvaluationGate[];
}

export interface AgentTraceSummary {
  id: string;
  started_at: string;
  status: "success" | "error";
  summary: string;
}

/** Memory scopes shared with the Workflow page redesign contract — four independent scopes, not one selector. */
export type MemoryScope = "conversation" | "user" | "project" | "private-agent";

export const MEMORY_SCOPES: MemoryScope[] = [
  "conversation",
  "user",
  "project",
  "private-agent",
];

/** Per-scope memory controls. Persistent memory (user/project/private-agent) defaults off. */
export interface MemoryScopeControl {
  scope: MemoryScope;
  enabled: boolean;
  default_enabled: boolean;
  retention_days: number | null;
  provider: string | null;
  access: string;
}

export interface MemoryView {
  scopes: MemoryScopeControl[];
}

/** A safe, everything-off placeholder used before any memory data has loaded. */
export function defaultMemoryView(): MemoryView {
  return {
    scopes: MEMORY_SCOPES.map((scope) => ({
      scope,
      enabled: false,
      default_enabled: false,
      retention_days: null,
      provider: null,
      access: "Not configured",
    })),
  };
}

/** Rich, workspace-level connection object — never a bare string id. */
export interface ConnectionView {
  id: string;
  name: string;
  readiness:
    | "ready"
    | "ready_with_key"
    | "configuration_required"
    | "unavailable"
    | "disabled";
  permissions: ("read" | "write")[];
  scope: "workspace";
  policy: string | null;
  version: string | null;
}

/** Rich specialist attachment — either a registry agent or a private specialist, never a bare string id. */
export interface SpecialistView {
  id: string;
  name: string | null;
  owner_kind: "platform" | "researcher" | null;
  purpose: string | null;
  attached: boolean;
}

/**
 * Behavioral/data-boundary summary — not a flat `read_only|read_write` flag.
 * `mode` is derived heuristically from the real `AgentSetting.web_access`
 * text until a real Agent Studio endpoint exists; `null` means the source
 * text was ambiguous and must be shown as "Not available yet" rather than
 * guessed.
 */
export interface PublicBoundaryView {
  mode: "none" | "public_online" | null;
  sources: string[] | null;
  outbound_data_boundary: string | null;
  write_destinations: string[] | null;
  approval_required: boolean | null;
}

export function defaultPublicBoundary(): PublicBoundaryView {
  return {
    mode: null,
    sources: null,
    outbound_data_boundary: null,
    write_destinations: null,
    approval_required: null,
  };
}

/**
 * Derives a coarse `none | public_online` mode from the real, live
 * `AgentSetting.web_access` free-text field (e.g. "Never direct",
 * "Public-only deployment"). This is a lightweight heuristic over real data,
 * not fabricated copy: ambiguous text returns `null` rather than guessing,
 * and the raw `web_access` string is always shown verbatim as
 * `outbound_data_boundary` alongside it. Current public-online agents are
 * treated as read-only (no write destinations, approval required) unless a
 * future Agent Studio endpoint says otherwise.
 */
export function derivePublicBoundaryFromWebAccess(
  webAccess: string | undefined,
): PublicBoundaryView {
  if (!webAccess) return defaultPublicBoundary();
  const normalized = webAccess.toLowerCase();
  const isNone =
    normalized.includes("never") ||
    normalized.includes("forbidden") ||
    normalized.includes("no raw data") ||
    normalized === "none";
  const isPublic = normalized.includes("public");
  const mode: PublicBoundaryView["mode"] = isNone
    ? "none"
    : isPublic
      ? "public_online"
      : null;
  return {
    mode,
    sources: null,
    outbound_data_boundary: webAccess,
    write_destinations: mode === "public_online" ? [] : null,
    approval_required: mode === "public_online" ? true : mode === "none" ? false : null,
  };
}

/** Immutable release row. Never carries draft/evaluating/deploying mutable status — see AgentDraftView. */
export interface AgentReleaseSummary {
  version: string;
  created_at: string;
  created_by: string;
  changelog: string;
  /** Parent version this release was cut from, if any. */
  derived_from: string | null;
  /** Content-addressed hash of the frozen contract for this release. */
  content_hash: string;
  /** Exact discovered model deployment this release was pinned to. */
  model_version: string;
  /** Exact capability instance versions bound at release time, by capability id. */
  capability_versions: Record<string, string>;
  /** Where this specific immutable release currently sits — distinct from draft status. */
  deployment_status: AgentDeploymentStatus;
}

export interface AgentUsageSummary {
  studio_runs: number;
  workflow_uses: number;
  last_used_at: string | null;
}

/**
 * Full behavioral contract for one agent (release or draft), consumed by
 * both Registry and Workspace. Every field is nullable: `null` means "not
 * available from the current data source", rendered as an explicit
 * unavailable state rather than fabricated copy.
 */
export interface AgentContractView {
  purpose: string | null;
  boundary: string | null;
  input_artifact: string | null;
  instructions: string | null;
  evidence_policy: string | null;
  model: { deployment: string | null; discovered: boolean };
  knowledge: string[] | null;
  tools: string[] | null;
  memory: MemoryView;
  connections: ConnectionView[] | null;
  specialists: SpecialistView[] | null;
  capabilities: CapabilityView[] | null;
  safety: string | null;
  tests: string | null;
  deployment: string | null;
  public_boundary: PublicBoundaryView | null;
}

/**
 * Registry-list read model. Distinct from `AgentContractView`: this is the
 * lightweight summary shown on cards, before opening the full workspace.
 */
export interface AgentSummary {
  id: string;
  name: string;
  owner_kind: "platform" | "researcher";
  lifecycle: AgentRegistryLifecycle;
  latest_release: string | null;
  purpose: string | null;
  boundary: string | null;
  discovered_project_model: string | null;
  public_boundary: PublicBoundaryView;
  capability: CapabilityId | null;
  /** `"legacy_agents_endpoint"` until `/v1/agent-studio/agents` exists; then `"agent_studio"` is authoritative. */
  source: "agent_studio" | "legacy_agents_endpoint";
}

export interface AgentDraftView {
  draft_id: string;
  agent_id: string | null;
  /** The release this draft was branched from — null for a from-scratch draft. */
  base_version: string | null;
  status: AgentDraftStatus;
  /** Optimistic-concurrency token; required on every mutating call against this draft. */
  etag: string;
  contract: AgentContractView;
  created_by: string;
  created_at: string;
}

/** @deprecated replaced by the {message, base_etag} -> proposal -> apply flow. Kept only as a display-shape reference. */
export interface ManifestFieldChange {
  path: string;
  before: unknown;
  after: unknown;
}

/**
 * Server-generated proposal returned from a builder message. The client
 * never authors `{path,before,after}` changes directly — it only sends
 * natural-language `message` + `base_etag`, and separately applies the
 * returned `proposal_id` once reviewed.
 */
export interface AgentBuilderProposal {
  proposal_id: string;
  draft_id: string;
  summary: string;
  patch: ManifestFieldChange[];
  before_summary: string;
  after_summary: string;
  capability_changes: string[];
  permission_changes: string[];
  data_boundary_changes: string[];
  validation_warnings: string[];
  base_etag: string;
}

export interface AgentDraftIntent {
  source: "template" | "blank";
  template_capability?: CapabilityId;
  intent: string;
}
