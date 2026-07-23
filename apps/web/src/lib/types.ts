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
// Proposed Agent Registry / Agent Workspace contract — PENDING BACKEND.
//
// `packages/contracts/openapi.json` only defines `AgentSetting` today (id,
// name, deployment, model_tier, status, web_access, workflow_steps). The
// types below describe the richer manifest surface the Agent Registry and
// Agent Workspace need and were proposed to the backend team (see the
// coordinating "Workflow page redesign" session). Until the backend ships
// matching endpoints, every function in `lib/api.ts` that returns these types
// performs a real fetch against the real proxy and will genuinely fail
// (404/502) — UI code MUST render an explicit unavailable/error state on
// failure and must never fabricate a successful response.
// ---------------------------------------------------------------------------

export type AgentLifecycleState =
  | "draft"
  | "validating"
  | "evaluating"
  | "deploying"
  | "active"
  | "deprecated"
  | "archived";

export interface AgentCapabilityRef {
  id: string;
  family: string;
  operation: string;
  maturity: "ga" | "preview" | "experimental";
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
  hard_gates: AgentEvaluationGate[];
}

export interface AgentVersionRecord {
  version: string;
  created_at: string;
  created_by: string;
  status: AgentLifecycleState;
  changelog: string;
}

export interface AgentUsageSummary {
  studio_runs: number;
  workflow_uses: number;
  last_used_at: string | null;
}

export interface AgentManifest {
  id: string;
  owner_kind: "platform" | "researcher";
  purpose: string;
  boundary: string;
  discovered_project_model: string | null;
  capabilities: AgentCapabilityRef[];
  lifecycle: AgentLifecycleState;
  public_web_boundary: "none" | "read_only" | "read_write";
  memory: {
    enabled: boolean;
    scope: string | null;
    retention_days: number | null;
  };
  connections: string[];
  specialists: string[];
}

export interface ManifestFieldChange {
  path: string;
  before: unknown;
  after: unknown;
}

export interface ManifestChangeProposal {
  id: string;
  agent_id: string;
  summary: string;
  changes: ManifestFieldChange[];
  created_at: string;
}

export interface AgentDraftIntent {
  source: "template" | "blank";
  template_capability?: CapabilityId;
  intent: string;
}

export interface AgentDraftResult {
  id: string;
}
