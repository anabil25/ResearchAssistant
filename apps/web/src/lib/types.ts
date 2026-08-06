import type { components } from "@/lib/generated-api";

export type CapabilityId = components["schemas"]["Capability"];

/**
 * A studio surface as the server declares it. Hand-written rather than
 * generated because `/api/agent-surfaces` post-dates the committed OpenAPI
 * snapshot; regenerate and switch to the generated shape when contracts are
 * next exported.
 */
export interface AgentSurfaceView {
  capability: CapabilityId;
  chat: boolean;
  icon?: string | null;
  eyebrow?: string | null;
  chat_title?: string | null;
  chat_description?: string | null;
  suggestions?: string[];
}

export interface Capability {
  id: CapabilityId;
  title: string;
  shortTitle: string;
  description: string;
  examplePrompt: string;
  accent: string;
}

export type Citation = components["schemas"]["Citation"];
export type AutomationStep = components["schemas"]["AutomationStep"];
export type StudioRun = components["schemas"]["StudioRun"];
export type WorkspaceSummary = components["schemas"]["WorkspaceSummary"];
export type LibraryItem = components["schemas"]["LibraryItem"];
export type RunSummary = components["schemas"]["RunSummary"];
export type ApprovalRecord = components["schemas"]["ApprovalRecord"];
export type ConnectorSetting = components["schemas"]["ConnectorSetting"];
export type PersonalProjectCreate = components["schemas"]["PersonalProjectCreate"];
export type PersonalProjectUpdate = components["schemas"]["PersonalProjectUpdate"];
export type ProjectSummary = components["schemas"]["ProjectSummary"];
export type ProjectSettings = components["schemas"]["ProjectSettings"];
export type AgentSetting = components["schemas"]["AgentSetting"];

/**
 * Agent chat contract (`/api/agent-chat`). Hand-written rather than generated
 * because `generated-api.ts` is regenerated from a committed
 * `packages/contracts/openapi.json` snapshot; these mirror the response models
 * in `services/api/src/research_assistant_api/agent_chat.py` and will collapse
 * into generated types on the next contract export.
 *
 * Note what is deliberately absent: the Foundry conversation id, session id,
 * isolation key, and owner principal never cross this boundary. The browser
 * holds only `id`, so it cannot bind itself to another user's sandbox.
 */
export interface ChatAgentChoice {
  name: string;
  label: string;
  description: string;
  online: boolean;
}

export interface ChatAttachment {
  path: string;
  size_bytes: number;
  content_type: string;
  uploaded_at: string;
}

export interface ChatActivity {
  kind: "approach" | "tool";
  label: string;
  status: string;
  detail: string | null;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  agent_name: string | null;
  attachments: ChatAttachment[];
  activity?: ChatActivity[];
  duration_ms?: number | null;
  source_count?: number;
}

export interface ChatThread {
  id: string;
  capability: CapabilityId;
  agent_name: string;
  created_at: string;
  updated_at: string;
  messages: ChatMessage[];
  attachments: ChatAttachment[];
}

export type ChatStreamEvent =
  | {
      type: "started";
      message_id: string;
      agent_name: string;
      created_at: string;
    }
  | {
      type: "activity";
      activity_id: string;
      activity: ChatActivity;
    }
  | { type: "text_delta"; delta: string }
  | { type: "completed"; message: ChatMessage }
  | { type: "error"; detail: string; status: number };

export type FoundryAgentType = "hosted" | "prompt" | "workflow" | "external" | "unknown";

export interface FoundryAgentInventoryItem {
  name: string;
  agent_type: FoundryAgentType;
  description: string | null;
  version: string | null;
  status: string | null;
  model_deployments: string[];
  model: string | null;
}

export interface FoundryProjectContext {
  project_id: string;
}

export interface FoundryModelDeployment {
  deployment_name: string;
  model_name: string;
  model_format: string;
  capacity: number | null;
}

export interface PromptAgentDraft {
  logical_agent_id: string;
  manifest: Record<string, unknown> & {
    instructions: string;
    capabilities: unknown[];
    model_deployment: FoundryModelDeployment | null;
  };
  etag: string;
}

export interface PromptCapabilityBinding {
  binding_id: string;
  descriptor_ref: { id: string };
  operation_ref: { id: string };
}
export type AutomationStudioResult =
  components["schemas"]["AutomationStudioResult"];
export type StudioResult = AutomationStudioResult;

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

// ---------------------------------------------------------------------------
// Agent Studio contract — PENDING BACKEND, canonical namespace
// `/api/agent-studio` (confirmed against the backend's actual committed
// router prefix — see the Round 4/8/11 history below for the full flip-flop;
// not yet final since generated OpenAPI hasn't shipped — see `lib/api.ts`
// for the single choke-point helper this is centralized behind).
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
 * backend's real `OperationMaturity` enum (`agent_studio/models.py`, commit
 * `5dab8b7`, superseding the `d6df0fe`-era single five-value enum this file
 * previously mirrored). `OperationMaturity` is now exactly three values —
 * `ga`/`preview`/`unknown` — with `retired` and a distinct `deprecated` state
 * moved to the independent `OperationLifecycle` axis (see
 * `CapabilityOperationLifecycle` below). `unknown` is the fail-closed default
 * when a discovery source didn't report a maturity tier and must never be
 * treated as safe-to-attach.
 *
 * An earlier round of this file attempted this same maturity/lifecycle split
 * speculatively, then reverted it after inspecting `d6df0fe`, which genuinely
 * had no `lifecycle` field. The backend has since (independently, and
 * verified again directly against `5dab8b7`'s committed Pydantic source
 * rather than taken on a paraphrase) added the split for real. `is_bindable`
 * on the backend's `CapabilityOperation` is a plain `@property`, not a
 * `@computed_field`, so it is never present on the wire — see
 * `isCapabilityAttachable` for this UI's own preliminary/display-only
 * derivation from `maturity`+`lifecycle`, not a mirror of that backend
 * property.
 */
export type CapabilityMaturity = "ga" | "preview" | "unknown";

/**
 * Capability *operation* lifecycle — the independent axis from `maturity`,
 * verified against the backend's real `OperationLifecycle` enum (commit
 * `5dab8b7`). A `ga`-maturity operation can still be `deprecated` (still
 * works, scheduled for removal) or `retired` (withdrawn); either makes it
 * permanently non-attachable regardless of maturity. Defaults to `active`
 * on the backend model.
 */
export type CapabilityOperationLifecycle = "active" | "deprecated" | "retired";

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
 * backend's real `CapabilityOperation` model. `maturity`/`lifecycle`/
 * `operation_class`/`requires_approval` are all operation-level, not
 * descriptor-level: two operations on the same descriptor can have entirely
 * different maturity, lifecycle, and risk profiles. `maturity` and
 * `lifecycle` are two independent fields on the real backend model (commit
 * `5dab8b7`) — a `ga` operation can still be `deprecated` or `retired`, and
 * neither axis implies the other. `source_url`/`source_version`/
 * `last_verified_at` are the provenance trail for the maturity claim.
 * `input_schema_digest`/`output_schema_digest` are operation-level (verified
 * against backend commit `a23b73e`, which added these directly to
 * `CapabilityOperation`) because a single descriptor's operations can have
 * distinct I/O shapes; a `CapabilityBinding` copies both digests at attach
 * time (see below).
 */
export interface CapabilityOperation {
  name: string;
  maturity: CapabilityMaturity;
  /** Independent of `maturity` — see `CapabilityOperationLifecycle`. Backend default is `active`. */
  lifecycle: CapabilityOperationLifecycle;
  operation_class: CapabilityOperationClass;
  side_effect_destinations: string[];
  requires_approval: boolean;
  /** Surfaced when maturity isn't `ga` or lifecycle isn't `active` (e.g. why an operation is `preview`/`deprecated`/`retired`). */
  reason: string | null;
  source_url: string | null;
  source_version: string | null;
  last_verified_at: string | null;
  input_schema_digest: string | null;
  output_schema_digest: string | null;
}

/**
 * Provider-declared capability *catalog/governance* entry — verified
 * field-for-field against the backend's real `CapabilityDescriptor` model.
 * Fetched from `GET /agent-studio/capabilities/descriptors`. `operations` is
 * the honest, per-operation maturity/lifecycle surface: `preview`/`unknown`
 * maturity and `deprecated`/`retired` lifecycle operations remain visible
 * (with `reason`) but are rejected at attach time. `version` is the descriptor's own catalog
 * version, pinned by any `CapabilityBinding` that attaches it, so a later
 * catalog update never silently changes an already-released agent's
 * behavior. There is deliberately no descriptor-level `digest` field on this
 * wire type — the backend computes a content digest of the descriptor only
 * at attach time (`compute_descriptor_digest`) and stores the result on the
 * attaching `CapabilityBinding.descriptor_digest`, never on the descriptor
 * itself; this UI therefore uses `version` comparison as its own drift
 * proxy (see `resolveCapabilityBindingView`). Per-operation I/O schema
 * digests live on `CapabilityOperation.input_schema_digest`/
 * `output_schema_digest`, copied onto the binding at attach time.
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
 *
 * `descriptor_version` (the exact `CapabilityDescriptor.version` consulted
 * at discovery) and `instance_fingerprint` (a canonical digest pinning
 * provider/descriptor/operation identity, operation definitions/versions,
 * side-effect destinations, tenant/data boundaries, and non-secret
 * discovered config — excluding health/timestamps/secrets) were verified
 * against backend commit `a23b73e`. A `CapabilityBinding` that attaches this
 * instance copies `instance_fingerprint` at attach time so later
 * reconfiguration (not just a health/readiness flap) is independently
 * detectable — see `resolveCapabilityBindingView`'s fingerprint-drift check.
 */
export interface CapabilityInstance {
  id: string;
  tenant_id: string;
  project_id: string;
  descriptor_id: string;
  descriptor_version: string;
  discovered_provider_version: string | null;
  readiness: CapabilityInstanceReadiness;
  health_status: CapabilityHealthStatus;
  config_fingerprint: string | null;
  instance_fingerprint: string | null;
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
 * An operation is only attachable when BOTH independent axes clear: `ga`
 * maturity AND `active` lifecycle — verified against the backend's real
 * (non-serialized) `CapabilityOperation.is_bindable` property (commit
 * `5dab8b7`): `return self.maturity == OperationMaturity.GA and
 * self.lifecycle == OperationLifecycle.ACTIVE`. Because `is_bindable` is a
 * plain `@property`, not a `@computed_field`, it never appears on the wire —
 * this function is this UI's own preliminary/display-only derivation of that
 * same rule from the two raw fields, not a mirror of a backend-computed
 * value. `preview`/`unknown` maturity and `deprecated`/`retired` lifecycle
 * are always non-attachable — fail-closed rather than assuming availability;
 * a `ga`+`deprecated` or `ga`+`retired` operation must never read as
 * attachable just because its maturity is `ga`. When the operation requires
 * a discovered instance (`binding.instance_id` is set), that instance must
 * additionally be `ready` — this extra instance-readiness check is this UI's
 * own conservative display policy (not a backend mirror, since no direct
 * attach action exists yet); operations that need no instance pass
 * `instance = null` and are gated on maturity+lifecycle alone.
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
  if (operation.lifecycle !== "active") return false;
  if (!instance) return true;
  return instance.readiness === "ready";
}

/**
 * Persisted, immutable-manifest-embedded attachment of one capability
 * operation to a specific agent version/draft — verified field-for-field
 * against the backend's real `CapabilityBinding` Pydantic model, most
 * recently against commit `a23b73e` ("Phase 1 schema corrections: binding
 * digests, instance fingerprint, ToolRegistrationSpec"). This is a flat set
 * of pinned identity refs plus an attach audit trail: `descriptor_id`/
 * `descriptor_version` pin the catalog entry, `descriptor_digest` pins the
 * descriptor's *content* (not just its version string) so a catalog edit
 * that bumps content without bumping the version string can't silently
 * change an already-attached binding's behavior, `operation` names the
 * specific operation, `instance_id` optionally pins a discovered resource
 * and `instance_fingerprint` (copied from the resolved `CapabilityInstance`
 * at attach time) detects later reconfiguration independent of a health/
 * readiness flap, `pinned_provider_version`/`input_schema_digest`/
 * `output_schema_digest` pin the upstream contract/schema (two independent
 * digests, copied from the resolved `CapabilityOperation`, since a
 * descriptor's operations can have distinct I/O shapes), `config_hash` is a
 * canonical digest of this binding's own `config` computed at attach time so
 * config drift is independently detectable, and `connection_ref`/
 * `policy_ref` are flat resource references. There is deliberately no
 * `enabled` toggle and no approval status on this row — approval is a
 * declarative `requires_approval` flag on the resolved `CapabilityOperation`,
 * and any actual authorization decision lives in a separate, version-scoped
 * `StudioApprovalRecord` — never a field here. Never embeds the full
 * descriptor or any volatile instance health/readiness (see
 * `CapabilityBindingView` for the derived, resolved-for-display expansion of
 * this row, kept strictly separate from this persisted shape).
 *
 * The digest/fingerprint fields are the backend's own recomputed values, not
 * anything the UI derives — see `resolveCapabilityBindingView` for how the
 * `instance_fingerprint` comparison is used to detect drift client-side
 * (mirroring the backend's `check_binding_freshness`, which is a registry
 * primitive not yet wired into a formal release/invoke hard gate).
 */
export interface CapabilityBinding {
  descriptor_id: string;
  descriptor_version: string;
  descriptor_digest: string | null;
  operation: string;
  instance_id: string | null;
  instance_fingerprint: string | null;
  pinned_provider_version: string | null;
  input_schema_digest: string | null;
  output_schema_digest: string | null;
  config: Record<string, unknown>;
  config_hash: string | null;
  connection_ref: string | null;
  policy_ref: string | null;
  attached_by: string;
  attached_at: string;
}

/**
 * How a bound capability operation is actually invoked at runtime —
 * verified against the backend's real `ToolRegistrationKind` enum
 * (commit `a23b73e`).
 */
export type ToolRegistrationKind = "managed_foundry_native" | "custom_handler";

/**
 * Persisted *spec* declaring how a `CapabilityBinding` is dispatched —
 * verified against the backend's real `ToolRegistrationSpec` model. This is
 * data, not a runtime handler: `handler_ref` is an opaque reference the
 * harness/provider compiler resolves into the actual non-serializable
 * callable at dispatch time; this backend (and this UI) never constructs or
 * serializes a callable handler, only this spec. Immutable once created.
 * The backend renamed the persisted-spec type from `ToolRegistration` to
 * `ToolRegistrationSpec` specifically to free the `ToolRegistration` name for
 * that future non-serializable runtime object — the UI must never introduce
 * a competing runtime-handler read model under either name. Surfaced only
 * behind Advanced (non-devs never need this).
 */
export interface ToolRegistrationSpec {
  id: string;
  tenant_id: string;
  logical_agent_id: string;
  descriptor_id: string;
  operation: string;
  kind: ToolRegistrationKind;
  handler_ref: string;
  registered_by: string;
  registered_at: string;
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
 *
 * The check order mirrors the backend's real `CapabilityRegistry
 * .check_binding_freshness(binding)` (verified against commit `a23b73e`):
 * descriptor resolvability, then instance resolvability/unavailability/
 * fingerprint drift. Two differences from the backend function, both
 * intentional: (1) this also flags a vanished *operation* name on an
 * otherwise-resolvable descriptor — a UI-only rendering concern the backend
 * gate doesn't need to check; (2) descriptor *content*-digest drift
 * (`descriptor_digest`) is checked by the backend against a live-recomputed
 * hash the wire `CapabilityDescriptor` type doesn't expose, so this uses the
 * descriptor's `version` string as the closest available proxy signal
 * instead of reimplementing the backend's canonical-JSON digest algorithm
 * client-side. `instance_fingerprint`, by contrast, IS already a plain
 * string on both the binding and the resolved instance, so that comparison
 * mirrors the backend exactly. `check_binding_freshness` is a registry
 * primitive, not yet wired into a formal backend release/invoke hard gate —
 * this client-side mirror is display-only reconciliation, not a substitute
 * for that gate.
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
  } else if (binding.instance_id && instance?.readiness === "unavailable") {
    staleReason = `This binding's discovered instance is unavailable: ${
      instance.unavailable_reason ?? "no reason supplied"
    }.`;
  } else if (
    binding.instance_id &&
    instance &&
    binding.instance_fingerprint &&
    instance.instance_fingerprint &&
    binding.instance_fingerprint !== instance.instance_fingerprint
  ) {
    staleReason =
      "This binding's discovered instance has been reconfigured since attach (fingerprint mismatch) — rebind and re-review before release.";
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

export interface AgentHealthSummary {
  state: "healthy" | "degraded" | "unavailable" | "unknown";
  last_checked_at: string | null;
  detail: string;
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
 * `mode` must only ever be set from an explicit, structured boundary
 * supplied by a real Agent Studio endpoint — never guessed from free text
 * or an agent's id. `null` means no such structured boundary exists yet and
 * must be shown as "Not available yet" rather than inferred.
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
 * Builds the legacy-fallback public-boundary display from the real, live
 * `AgentSetting.web_access` free-text field. This field is unstructured
 * internal-display text, not a governed public-boundary contract, so `mode`
 * (and every other structured field) is always `null` here regardless of
 * content — independent review found the previous substring heuristic
 * (`includes("public")` => `public_online`) genuinely misclassified real
 * agents: a canonical agent can support both authorized evidence and
 * request-scoped public discovery, so free text containing "public" cannot
 * identify the active mode or its data boundary. The raw `web_access` text
 * is still surfaced verbatim as
 * `outbound_data_boundary` purely for human context — it never asserts a
 * boundary. Only a real Agent Studio endpoint (not yet implemented) may
 * ever set `mode` to a non-null value.
 */
export function derivePublicBoundaryFromWebAccess(
  webAccess: string | undefined,
): PublicBoundaryView {
  if (!webAccess) return defaultPublicBoundary();
  return {
    ...defaultPublicBoundary(),
    outbound_data_boundary: webAccess,
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
  /** `"legacy_agents_endpoint"` until `/api/agent-studio/agents` exists; then `"agent_studio"` is authoritative. */
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
