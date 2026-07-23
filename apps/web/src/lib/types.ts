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
// Agent Studio contract — PENDING BACKEND, target namespace `/agent-studio`
// (updated per the coordinating session's report that the backend's actual
// routing convention is `/api/agent-studio/...`, not the earlier proposed
// `/v1/agent-studio` version prefix; not yet final — see `lib/api.ts` for
// the single choke-point helper this is centralized behind).
//
// `packages/contracts/openapi.json` only defines `AgentSetting` today (id,
// name, deployment, model_tier, status, web_access, workflow_steps). The
// types below are UI-facing READ MODELS — not a reduced mirror of a backend
// `AgentManifest` row — reconciled with the coordinating "Workflow page
// redesign" session across multiple rounds of contract alignment. When the
// backend ships generated OpenAPI types for `/agent-studio/**`,
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

/** Draft lifecycle only. Never applied to an immutable version row (see AgentVersionSummary). */
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
 * Capability *operation* maturity — verified field-for-field against the
 * backend's real `OperationMaturity` enum (commit `d6df0fe`,
 * `agent_studio/models.py`). This is a single five-value enum, not split
 * into a separate "lifecycle": `retired` and `unavailable` ARE maturity
 * states, not a distinct lifecycle dimension. Only `ga` is ever attachable —
 * see `isCapabilityAttachable`. `unknown` is the fail-closed default when a
 * discovery source didn't report a maturity tier and must never be treated
 * as safe-to-attach, identically to `unavailable`.
 *
 * A prior round of this file split this into `maturity`/`CapabilityLifecycle`
 * per an unverified cross-session correction; that split has been reverted
 * after directly inspecting the committed Pydantic source, which has no
 * `lifecycle` field on `CapabilityOperation` at all.
 */
export type CapabilityMaturity =
  | "ga"
  | "preview"
  | "unavailable"
  | "retired"
  | "unknown";

/**
 * Deterministic side-effect classification for a capability operation —
 * verified against the backend's real `OperationClass` enum. Independent of
 * `maturity` (availability/attachability) and of `requires_approval`
 * (declared alongside it on `CapabilityOperation`): this describes *what
 * kind* of effect invoking the operation can have, not whether it's safe to
 * attach or needs sign-off. Backend field name is `operation_class`.
 */
export type CapabilityOperationClass =
  | "pure"
  | "read"
  | "write_reversible"
  | "write_irreversible"
  | "privileged";

/**
 * A single operation on a capability descriptor — verified against the
 * backend's real `CapabilityOperation` model. `maturity`/`operation_class`/
 * `requires_approval` are all operation-level, not descriptor-level: two
 * operations on the same descriptor can have entirely different maturity
 * and risk profiles. `source_url`/`source_version`/`last_verified_at` are
 * the provenance trail for the maturity claim.
 */
export interface CapabilityOperation {
  name: string;
  maturity: CapabilityMaturity;
  operation_class: CapabilityOperationClass;
  side_effect_destinations: string[];
  requires_approval: boolean;
  /** Surfaced when maturity isn't `ga` (e.g. why an operation is `preview`/`retired`/`unavailable`). */
  reason: string | null;
  source_url: string | null;
  source_version: string | null;
  last_verified_at: string | null;
}

/**
 * Provider-declared capability *catalog/governance* entry — verified
 * field-for-field against the backend's real `CapabilityDescriptor` model.
 * Fetched from `GET /agent-studio/capabilities/descriptors`. `operations` is
 * the honest, per-operation maturity surface: `preview`/`retired`/
 * `unavailable`/`unknown` operations remain visible (with `reason`) but are
 * rejected at attach time. `version` is the descriptor's own catalog
 * version, pinned by any `CapabilityBinding` that attaches it, so a later
 * catalog update never silently changes an already-released agent's
 * behavior. There is deliberately no descriptor-level `digest` field in the
 * real backend model — drift is detected via `version` comparison, not a
 * content hash, at this level (schema-level drift is instead carried on the
 * binding's own `schema_digest`).
 */
export interface CapabilityDescriptor {
  id: string;
  version: string;
  provider: string;
  title: string;
  description: string;
  operations: CapabilityOperation[];
  auth_requirements: string[];
  risk_tier: string;
  data_boundary: string;
  managed_foundry_native: boolean;
}

/** Discovered-resource readiness — verified against the backend's real `InstanceReadiness` enum (three values only). */
export type CapabilityInstanceReadiness = "ready" | "degraded" | "unavailable";

/** Verified against the backend's real `HealthStatus` enum. */
export type CapabilityHealthStatus =
  | "healthy"
  | "degraded"
  | "unhealthy"
  | "unknown";

/**
 * A tenant/project-discovered *resource* for a capability descriptor —
 * verified field-for-field against the backend's real `CapabilityInstance`
 * model. Distinct from `CapabilityDescriptor` (immutable catalog semantics)
 * and from `CapabilityBinding` (an agent's attachment): this is the
 * concrete, discovered thing a binding points at via `instance_id`, with its
 * own readiness/health independent of the descriptor's static catalog entry.
 * Never persisted inside a manifest; resolved and validated at attach/gate
 * time. Scope is `tenant_id` + `project_id` (never a bare "workspace" id —
 * a workspace is a display alias over `project_id`, not a distinct scope
 * dimension). Never carries secrets/credentials.
 */
export interface CapabilityInstance {
  id: string;
  tenant_id: string;
  project_id: string;
  descriptor_id: string;
  discovered_provider_version: string | null;
  readiness: CapabilityInstanceReadiness;
  health_status: CapabilityHealthStatus;
  config_fingerprint: string | null;
  unavailable_reason: string | null;
  discovered_at: string;
  registered_by: string;
}

/**
 * Version/release-scoped approval record — verified against the backend's
 * real `StudioApprovalRecord` model. Approval is explicitly NOT a
 * per-capability-binding concept in the real backend: it gates release
 * promotion, fork promotion, or admin escalation for an entire agent
 * version, keyed by `version_id`. A capability operation's declarative
 * `requires_approval` (see `CapabilityOperation`) only states that approval
 * is *needed*; whether one is currently in force is this record, never a
 * field on `CapabilityBinding`.
 */
export type StudioApprovalKind =
  | "release_promotion"
  | "fork_promotion"
  | "admin_escalation";

/** Verified against the backend's real `ApprovalState` enum — three values only, no `not_required`/`expired`/`revoked` variants; expiry is tracked separately via `expires_at`. */
export type StudioApprovalState = "pending" | "approved" | "rejected";

export interface StudioApprovalRecord {
  id: string;
  version_id: string;
  kind: StudioApprovalKind;
  state: StudioApprovalState;
  gated_action: string;
  destination: string;
  requested_by: string;
  requested_at: string;
  evidence_summary: string;
  risk: string;
  idempotency_key: string;
  approver_id: string | null;
  decided_at: string | null;
  rationale: string | null;
  /** The exact version/manifest content hash this approval is bound to — never a blanket, open-ended grant. */
  content_hash: string | null;
  expires_at: string | null;
}

/**
 * True only while a version-scoped approval is currently in force:
 * `approved` must not have passed its `expires_at`; `pending`/`rejected` are
 * never active. Fail-closed: an unparsable `expires_at` is never active.
 */
export function isStudioApprovalActive(
  record: StudioApprovalRecord,
  now: Date = new Date(),
): boolean {
  if (record.state !== "approved") return false;
  if (!record.expires_at) return true;
  const expiry = new Date(record.expires_at).getTime();
  if (Number.isNaN(expiry)) return false;
  return expiry > now.getTime();
}

/**
 * Aggregate response shape for `GET /agent-studio/capabilities/discovery`
 * — a single-request convenience read combining the descriptor and instance
 * catalogs for one UI load. `getCapabilityDescriptors`/`getCapabilityInstances`
 * remain the canonical per-resource reads; this is a derived/expanded read,
 * not a distinct persisted resource.
 */
export interface CapabilityDiscovery {
  descriptors: CapabilityDescriptor[];
  instances: CapabilityInstance[];
  warnings: string[];
  refreshed_at: string | null;
}

/**
 * Only a `ga`-maturity operation is ever attachable — verified against the
 * backend's explicit docstring ("Only GA is ever attachable") on
 * `OperationMaturity`. `preview`/`unavailable`/`retired`/`unknown` are always
 * non-attachable — fail-closed rather than assuming availability. When the
 * operation requires a discovered instance (`binding.instance_id` is set),
 * that instance must additionally be `ready`; operations that need no
 * instance pass `instance = null` and are gated on maturity alone.
 * Non-attachable operations/instances still surface via
 * `CapabilityBindingView` for display — attachability only gates *new*
 * bindings, it doesn't hide existing ones.
 */
export function isCapabilityAttachable(
  operation: CapabilityOperation | null | undefined,
  instance: CapabilityInstance | null | undefined,
): boolean {
  if (!operation) return false;
  if (operation.maturity !== "ga") return false;
  if (!instance) return true;
  return instance.readiness === "ready";
}

/**
 * Persisted, immutable-manifest-embedded attachment of one capability
 * operation to a specific agent version/draft — verified field-for-field
 * against the backend's real `CapabilityBinding` Pydantic model (commit
 * `d6df0fe`). This is a flat set of pinned identity refs plus an attach
 * audit trail: `descriptor_id`/`descriptor_version` pin the catalog entry,
 * `operation` names the specific operation, `instance_id` optionally pins a
 * discovered resource, `pinned_provider_version`/`schema_digest` pin the
 * upstream contract/schema, `connection_ref`/`policy_ref` are flat resource
 * references, and `config` is this binding's own inline configuration data.
 * There is deliberately no `enabled` toggle and no approval status on this
 * row — approval is a declarative `requires_approval` flag on the resolved
 * `CapabilityOperation`, and any actual authorization decision lives in a
 * separate, version-scoped `StudioApprovalRecord` — never a field here.
 * Never embeds the full descriptor or any volatile instance health/readiness
 * (see `CapabilityBindingView` for the derived, resolved-for-display
 * expansion of this row, kept strictly separate from this persisted shape).
 */
export interface CapabilityBinding {
  descriptor_id: string;
  descriptor_version: string;
  operation: string;
  instance_id: string | null;
  pinned_provider_version: string | null;
  schema_digest: string | null;
  config: Record<string, unknown>;
  connection_ref: string | null;
  policy_ref: string | null;
  attached_by: string;
  attached_at: string;
}

/**
 * Derived, read-only expansion of a `CapabilityBinding` for rendering in the
 * Workspace/detail view. Never the persisted shape: `resolved_descriptor`,
 * `resolved_operation`, and `resolved_instance` are looked up live at read
 * time and may be `null` (unresolvable) or drifted from what the binding
 * pinned — see `resolveCapabilityBindingView` and `stale_reason`.
 * `attachable` is the derived attach decision for this exact resolved
 * operation/instance pair — never inferred by callers from maturity alone.
 */
export interface CapabilityBindingView {
  binding: CapabilityBinding;
  resolved_descriptor: CapabilityDescriptor | null;
  resolved_operation: CapabilityOperation | null;
  resolved_instance: CapabilityInstance | null;
  /** Non-null when the pinned descriptor/operation/instance can't be resolved, or has drifted from what the binding pinned. */
  stale_reason: string | null;
  attachable: boolean;
}

/**
 * Reconciles a persisted binding against live descriptor/instance reads,
 * producing the derived `CapabilityBindingView` shown in the Workspace. This
 * is the one place staleness/attachability is computed — never store
 * `stale_reason`/`attachable` on the persisted binding itself.
 */
export function resolveCapabilityBindingView(
  binding: CapabilityBinding,
  descriptor: CapabilityDescriptor | null,
  instance: CapabilityInstance | null,
): CapabilityBindingView {
  const operation =
    descriptor?.operations.find((op) => op.name === binding.operation) ??
    null;
  let staleReason: string | null = null;
  if (!descriptor) {
    staleReason =
      "This binding's capability descriptor is no longer resolvable from the provider catalog.";
  } else if (descriptor.version !== binding.descriptor_version) {
    staleReason =
      "The descriptor's catalog version has changed since this binding pinned its version.";
  } else if (!operation) {
    staleReason =
      "This binding's operation is no longer present on the resolved descriptor.";
  } else if (binding.instance_id && !instance) {
    staleReason =
      "This binding's discovered instance is no longer resolvable — it may have been removed or is unavailable.";
  }
  return {
    binding,
    resolved_descriptor: descriptor,
    resolved_operation: operation,
    resolved_instance: instance,
    stale_reason: staleReason,
    // A pinned-but-unresolvable instance must gate attachment closed — it is
    // NOT the same as "no instance required" (which is what a bare `null`
    // means to `isCapabilityAttachable`). Only fold in `instance` when the
    // binding actually pinned one; otherwise evaluate maturity alone.
    attachable: binding.instance_id
      ? isCapabilityAttachable(operation, instance) && instance !== null
      : isCapabilityAttachable(operation, null),
  };
}

/**
 * @deprecated Legacy flat capability reference shape. New Agent Studio
 * surfaces must be built against `CapabilityBindingView`
 * (descriptor + instance + binding) instead. This type must only be consumed
 * by the explicit adapter in `lib/legacy-capability-adapter.ts` —
 * `legacy-capability-adapter.test.ts` fails the suite if any other source
 * file references it.
 */
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

/**
 * Purely immutable identity of one agent version — content-addressed hash,
 * exact pinned model/capability versions, and creation lineage. Never
 * carries deployment/environment/health state — see `DeploymentSummary`
 * for that mutable, derived concern. A version row must never expose a
 * field that can change after the fact.
 */
export interface AgentVersionSummary {
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
}

/**
 * Mutable, derived environment binding for one immutable version — where it
 * currently sits (active/deprecated/rolled back). Never persisted on the
 * immutable version row itself (see `AgentVersionSummary`), and distinct
 * from `AgentHealthSummary`: deployment status is a placement fact, not a
 * health signal.
 */
export interface DeploymentSummary {
  deployment_status: AgentDeploymentStatus;
}

/**
 * One row in the agent's version history: the immutable version identity
 * plus its current (mutable) deployment binding, kept as clearly separate
 * nested objects — never flattened into a single ambiguous "immutable" row.
 */
export interface AgentReleaseSummary {
  version_summary: AgentVersionSummary;
  deployment: DeploymentSummary;
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
  /**
   * Raw, persisted `CapabilityBinding` rows — never the expanded
   * `CapabilityBindingView`. Verified against the backend's real
   * `AgentManifest.capabilities: tuple[CapabilityBinding, ...]` field
   * (commit `d6df0fe`): the canonical version/resolve/catalog contract
   * embeds only pinned typed refs, not resolved descriptor/instance data.
   * Callers that need to render these must resolve each binding against a
   * separately-fetched descriptor/instance discovery read via
   * `resolveCapabilityBindingView` — never assume this array arrives
   * pre-resolved.
   */
  capabilities: CapabilityBinding[] | null;
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
  /** `"legacy_agents_endpoint"` until `/agent-studio/agents` exists; then `"agent_studio"` is authoritative. */
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
