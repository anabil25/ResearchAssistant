import {
  derivePublicBoundaryFromWebAccess,
  type AgentSetting,
  type AgentSummary,
  type CapabilityId,
} from "@/lib/types";

export type AgentOwnerKind = "platform" | "researcher";

/**
 * Minimal structural mapping — NOT descriptive copy. `/api/agents` only
 * returns `{id, name, deployment, model_tier, status, web_access,
 * workflow_steps}`; it has no notion of which Studio capability an agent is
 * wired to, or whether it is platform- vs. researcher-owned. This index
 * supplies only that plumbing — needed to compute real studio/workflow
 * usage counts and to separate "System agents" from "Your agents" in the
 * Registry — and carries no purpose/boundary/tools/knowledge/output-contract
 * text. All narrative behavioral-contract content must come from a real
 * Agent Studio endpoint (`AgentContractView`); see lib/types.ts and
 * lib/api.ts. Nothing here is sourced from `agents/shared/profiles.py`.
 */
const STRUCTURAL_AGENT_INDEX: Record<
  string,
  { ownerKind: AgentOwnerKind; capability: CapabilityId | null }
> = {
  coordinator: { ownerKind: "platform", capability: "orchestration" },
  literature: { ownerKind: "platform", capability: "literature" },
  literature_online: { ownerKind: "platform", capability: "literature" },
  grant: { ownerKind: "platform", capability: "grant" },
  grant_online: { ownerKind: "platform", capability: "grant" },
  matching: { ownerKind: "platform", capability: "matching" },
  matching_online: { ownerKind: "platform", capability: "matching" },
  dataset: { ownerKind: "platform", capability: "dataset" },
  institution: { ownerKind: "platform", capability: "institutional_qa" },
};

/** System agents are platform-owned; anything outside the structural index is a researcher-owned agent. */
export function getAgentOwnerKind(agentId: string): AgentOwnerKind {
  return STRUCTURAL_AGENT_INDEX[agentId]?.ownerKind ?? "researcher";
}

/** The Studio capability this agent is wired to, if any — used for usage-count attribution and Test-tab wiring. */
export function getAgentCapability(agentId: string): CapabilityId | null {
  return STRUCTURAL_AGENT_INDEX[agentId]?.capability ?? null;
}

/**
 * Builds the Registry's "legacy summary" fallback view directly from the
 * real `/api/agents` response (`AgentSetting[]`) — the only source used
 * until `/v1/agent-studio/agents` exists and becomes authoritative. Every
 * field `AgentSetting` doesn't carry (purpose, boundary narrative,
 * capability descriptors, releases, etc.) is left `null`/absent here so the
 * UI renders an explicit "Not available yet" rather than fabricated copy.
 * `public_boundary` is the one derived field, computed from the real
 * `web_access` text via `derivePublicBoundaryFromWebAccess` — a heuristic
 * over live data, not invented narrative.
 */
export function buildLegacyAgentSummaries(
  agents: AgentSetting[],
): AgentSummary[] {
  return agents.map((agent) => ({
    id: agent.id,
    name: agent.name,
    owner_kind: getAgentOwnerKind(agent.id),
    lifecycle:
      agent.status?.toLowerCase() === "active" ? "released" : "draft_only",
    latest_release: null,
    purpose: null,
    boundary: null,
    discovered_project_model: agent.deployment || null,
    public_boundary: derivePublicBoundaryFromWebAccess(agent.web_access),
    capability: getAgentCapability(agent.id),
    source: "legacy_agents_endpoint",
  }));
}
