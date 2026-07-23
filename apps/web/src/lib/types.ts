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
 * Capability maturity: exactly these three values, no `experimental` alias.
 * `unknown` is always non-attachable — see `isCapabilityAttachable`. Distinct
 * from `CapabilityLifecycle`: maturity describes how proven an operation is
 * (ga/preview), lifecycle describes whether it's still in active service
 * (active/deprecated/retired). A `ga` instance can still be `deprecated` or
 * `retired` — do not collapse the two into one enum.
 */
export type CapabilityMaturity = "ga" | "preview" | "unknown";

/**
 * Capability lifecycle state, independent of maturity. `deprecated` and
 * `retired` instances remain visible in the UI (with `lifecycle_reason`
 * surfaced as an explicit warning) rather than being hidden — researchers
 * need to see what an agent is still bound to even after it's sunset — but
 * neither is attachable for new bindings; only `active` is.
 */
export type CapabilityLifecycle = "active" | "deprecated" | "retired";

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

/**
 * Approval is never a permanent grant. Every capability binding carries its
 * own time/record-bound approval state — `approved` still requires checking
 * `expires_at` (see `isCapabilityApprovalActive`) before treating a binding
 * as currently authorized.
 */
export type CapabilityApprovalStatus =
  | "not_required"
  | "pending"
  | "approved"
  | "rejected"
  | "expired"
  | "revoked";

/** Time/record-bound approval summary for one capability binding — never a bare boolean or permanent flag. */
export interface CapabilityApprovalSummary {
  status: CapabilityApprovalStatus;
  /** The backing approval record, if any — lets a researcher trace exactly which decision authorized this binding. */
  record_id: string | null;
  /** Content hash of the exact scope/behavior this approval covers — detects a binding drifting out from under a stale approval. */
  scope_hash: string | null;
  actor: string | null;
  expires_at: string | null;
}

/**
 * True only while an approval is currently in force: `not_required` needs no
 * approval to be usable, `approved` must not have passed its `expires_at`,
 * and every other status (`pending`/`rejected`/`expired`/`revoked`) is never
 * active. Fail-closed: an approval with an unparsable `expires_at` is never
 * treated as active.
 */
export function isCapabilityApprovalActive(
  approval: CapabilityApprovalSummary,
  now: Date = new Date(),
): boolean {
  if (approval.status === "not_required") return true;
  if (approval.status !== "approved") return false;
  if (!approval.expires_at) return true;
  const expiry = new Date(approval.expires_at).getTime();
  if (Number.isNaN(expiry)) return false;
  return expiry > now.getTime();
}

/**
 * Provider-driven catalog entry for a capability family + operation.
 * Immutable operation semantics/governance — fetched from
 * `GET /agent-studio/capabilities/descriptors`. `digest` content-addresses
 * this descriptor's semantics/governance so a binding's pinned reference can
 * be checked for drift.
 */
export interface CapabilityDescriptor {
  id: string;
  version: string;
  family: string;
  operation: string;
  risk_class: CapabilityRiskClass;
  description: string;
  digest: string;
}

/**
 * A concrete, tenant/workspace-scoped discovered deployment of a descriptor
 * — fetched from `GET /agent-studio/capabilities/instances`. Never
 * carries secrets/credentials. `fingerprint` is this instance's own live
 * configuration/version fingerprint, compared against a binding's pinned
 * `instance_fingerprint` to detect drift.
 */
export interface CapabilityInstance {
  id: string;
  descriptor_id: string;
  /** Digest of the descriptor this instance was discovered against. */
  descriptor_digest: string;
  version: string;
  fingerprint: string;
  tenant_id: string | null;
  workspace_id: string | null;
  maturity: CapabilityMaturity;
  /** Independent of `maturity` — see `CapabilityLifecycle`. */
  lifecycle: CapabilityLifecycle;
  /** Human-readable reason surfaced when `lifecycle !== "active"` (e.g. sunset date, replacement pointer). `null` only when `lifecycle === "active"` or the provider gave no reason. */
  lifecycle_reason: string | null;
  provider: string;
  destination: string | null;
  readiness: "ready" | "degraded" | "unavailable" | "unknown";
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
 * Only GA, `active`-lifecycle, `ready` instances attach. `unknown` maturity,
 * a non-`active` lifecycle (`deprecated`/`retired`), or any readiness other
 * than `ready` is always non-attachable — fail-closed rather than assuming
 * availability. Non-attachable instances (deprecated/retired especially)
 * still surface via `CapabilityBindingView` for display — attachability
 * only gates *new* bindings, it doesn't hide existing ones.
 */
export function isCapabilityAttachable(
  instance: CapabilityInstance | null | undefined,
): boolean {
  if (!instance) return false;
  if (instance.maturity === "unknown") return false;
  if (instance.readiness !== "ready") return false;
  if (instance.lifecycle !== "active") return false;
  return instance.maturity === "ga";
}

/**
 * Typed reference to the pinned capability descriptor + version this binding
 * was created against. Never a bare string id — always carries the version
 * that was pinned so drift can be detected against the live catalog.
 */
export interface CapabilityDescriptorRef {
  id: string;
  version: string;
}

/**
 * Typed reference to the pinned discovered instance this binding was created
 * against. `version` is nullable — a binding may pin to "whatever version is
 * live" rather than a specific release — but `fingerprint` is always the
 * concrete configuration/version fingerprint pinned at bind time.
 */
export interface CapabilityInstanceRef {
  id: string;
  version: string | null;
  fingerprint: string;
}

/** Typed reference to a workspace configuration resource this binding depends on. */
export interface CapabilityConfigurationRef {
  ref: string;
}

/** Typed reference to a workspace connection resource this binding depends on. */
export interface CapabilityConnectionRef {
  ref: string;
}

/** Typed reference to a governing policy resource this binding depends on. */
export interface CapabilityPolicyRef {
  ref: string;
}

/**
 * Persisted, immutable-manifest-embedded binding of one capability to a
 * specific agent version. Embeds only pinned typed references, schema
 * digests, provider contract version, frozen destination constraints, and
 * the authorizing approval summary — never the full descriptor or any
 * volatile instance health/readiness (see `CapabilityBindingView` for the
 * derived, resolved-for-display expansion of this row, kept strictly
 * separate from this persisted shape).
 */
export interface CapabilityBinding {
  descriptor: CapabilityDescriptorRef;
  operation: string;
  instance: CapabilityInstanceRef;
  /** `null` when this binding needs no workspace configuration. */
  configuration: CapabilityConfigurationRef | null;
  /** `null` when this binding needs no workspace connection (e.g. a pure/local operation). */
  connection: CapabilityConnectionRef | null;
  /** `null` when no policy beyond the descriptor's own risk class governs this binding. */
  policy: CapabilityPolicyRef | null;
  /**
   * The actual upstream provider's contract/API version this binding was
   * authorized against — distinct from `descriptor.version`/`instance.version`
   * (Agent Studio's own catalog versions). Never exposed as an ambiguous
   * bare `provider_version` alias; `null` only when the provider doesn't
   * version its contract.
   */
  provider_contract_version: string | null;
  /**
   * Frozen at bind time: the destinations this binding is constrained to
   * send data to. Distinct from the live, volatile `CapabilityInstance.destination`
   * — this is what was actually authorized, not what the instance currently reports.
   */
  destination_constraints: string[] | null;
  input_schema_digest: string | null;
  output_schema_digest: string | null;
  enabled: boolean;
  approval: CapabilityApprovalSummary;
}

/**
 * Derived, read-only expansion of a `CapabilityBinding` for rendering in the
 * Workspace/detail view. Never the persisted shape: `resolved_descriptor`
 * and `resolved_instance` are looked up live at read time and may be `null`
 * (unresolvable) or drifted from what the binding pinned — see
 * `resolveCapabilityBindingView` and `stale_reason`.
 */
export interface CapabilityBindingView {
  binding: CapabilityBinding;
  resolved_descriptor: CapabilityDescriptor | null;
  resolved_instance: CapabilityInstance | null;
  /** Non-null when the pinned descriptor/instance can't be resolved, or the resolved instance has drifted from what the binding pinned. */
  stale_reason: string | null;
}

/**
 * Reconciles a persisted binding against live descriptor/instance reads,
 * producing the derived `CapabilityBindingView` shown in the Workspace. This
 * is the one place staleness is computed — never store `stale_reason` on
 * the persisted binding itself.
 */
export function resolveCapabilityBindingView(
  binding: CapabilityBinding,
  descriptor: CapabilityDescriptor | null,
  instance: CapabilityInstance | null,
): CapabilityBindingView {
  let staleReason: string | null = null;
  if (!descriptor) {
    staleReason =
      "This binding's capability descriptor is no longer resolvable from the provider catalog.";
  } else if (!instance) {
    staleReason =
      "This binding's discovered instance is no longer resolvable — it may have been removed or is unavailable.";
  } else if (instance.fingerprint !== binding.instance.fingerprint) {
    staleReason =
      "The discovered instance's live fingerprint no longer matches what this binding pinned at bind time.";
  } else if (instance.descriptor_digest !== descriptor.digest) {
    staleReason =
      "The descriptor's governance/semantics digest has changed since this instance was discovered.";
  }
  return {
    binding,
    resolved_descriptor: descriptor,
    resolved_instance: instance,
    stale_reason: staleReason,
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
