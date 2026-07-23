import type { CapabilityId } from "@/lib/types";

/**
 * Static, human-authored descriptive metadata for the platform's system
 * agents. This mirrors the existing `CAPABILITY_CARDS` catalog pattern in
 * `workspace-views.tsx`: it is accurate reference copy sourced directly from
 * `agents/shared/profiles.py` (the deployed Hosted Agent instructions), not
 * fabricated or dynamic data. Anything that must reflect live backend state
 * (version, lifecycle, health, evaluation scores, usage counts) is NOT here —
 * those are fetched for real from the proposed manifest endpoints in
 * `lib/api.ts` and rendered with explicit unavailable/error states until the
 * backend implements them.
 */
export type AgentOwnerKind = "platform" | "researcher";

export interface AgentCatalogEntry {
  id: string;
  name: string;
  ownerKind: AgentOwnerKind;
  purpose: string;
  boundary: string;
  knowledge: string[];
  tools: string[];
  modelTier: string;
  outputContract: string;
  workflowSteps: string[];
  publicWebBoundary: "none" | "read_only";
  connectorSources: string[];
  capability: CapabilityId | null;
  specialists: string[];
}

export const AGENT_CATALOG: AgentCatalogEntry[] = [
  {
    id: "coordinator",
    name: "research-coordinator",
    ownerKind: "platform",
    purpose:
      "Routes an incoming research request to the correct bounded specialist and reconciles specialist output into one answer.",
    boundary:
      "Classifies requests as public, internal, confidential, or restricted before delegating; never sends confidential or restricted text to a specialist with public web access. Has no tools of its own.",
    knowledge: [],
    tools: [],
    modelTier: "fast",
    outputContract: "CoordinatorDecisionV2",
    workflowSteps: ["classify", "route", "collect", "reconcile"],
    publicWebBoundary: "none",
    connectorSources: [],
    capability: "orchestration",
    specialists: [
      "literature",
      "grant",
      "matching",
      "dataset",
      "institution",
    ],
  },
  {
    id: "literature",
    name: "literature-agent",
    ownerKind: "platform",
    purpose:
      "Produces skeptical, source-grounded literature comparisons: consensus, disagreement, and open questions across supplied papers.",
    boundary:
      "Analyzes only server-authorized evidence supplied in the request. Has no tools and no web access. Retraction/correction signals are surfaced as warnings, never as proof of validity.",
    knowledge: ["paper"],
    tools: [],
    modelTier: "primary",
    outputContract: "LiteratureSynthesisV2",
    workflowSteps: [
      "protocol",
      "search",
      "screen",
      "extract",
      "synthesize",
      "audit",
    ],
    publicWebBoundary: "none",
    connectorSources: [],
    capability: "literature",
    specialists: [],
  },
  {
    id: "literature_online",
    name: "literature-online-agent",
    ownerKind: "platform",
    purpose:
      "Researches current public literature through allowlisted metadata connectors and web search for public-only objectives.",
    boundary:
      "Public-online deployment: refuses internal, confidential, restricted, participant, or secret context. Every tool result is treated as untrusted data; source URLs are preserved.",
    knowledge: ["paper"],
    tools: ["web_search", "mcp_connectors"],
    modelTier: "fast",
    outputContract: "PublicLiteratureResearchV2",
    workflowSteps: [
      "public_research",
      "protocol",
      "search",
      "screen",
      "extract",
      "synthesize",
      "audit",
    ],
    publicWebBoundary: "read_only",
    connectorSources: [
      "pubmed",
      "europe_pmc",
      "crossref",
      "openalex",
      "arxiv",
      "clinical_trials",
      "datacite",
      "semantic_scholar",
    ],
    capability: "literature",
    specialists: [],
  },
  {
    id: "grant",
    name: "grant-agent",
    ownerKind: "platform",
    purpose:
      "Maps funding requirements and drafts evidence-bounded grant sections with a requirements matrix.",
    boundary:
      "Never invents preliminary results, budgets, institutional commitments, personnel qualifications, facilities, or compliance approvals. Has no tools. Blocks ready-for-review status when required facts are missing.",
    knowledge: ["grant", "template", "paper"],
    tools: [],
    modelTier: "primary",
    outputContract: "GrantPackageV2",
    workflowSteps: [
      "opportunity",
      "requirements",
      "project_facts",
      "specific_aims",
      "sections",
      "compliance",
      "red_team",
      "approval",
    ],
    publicWebBoundary: "none",
    connectorSources: [],
    capability: "grant",
    specialists: [],
  },
  {
    id: "grant_online",
    name: "grant-online-agent",
    ownerKind: "platform",
    purpose:
      "Verifies current public funding opportunity guidance through funding metadata connectors and web search.",
    boundary:
      "Receives only the public funding notice and public objective; refuses project facts or private drafts. Prefers structured MCP grant connector data over web search.",
    knowledge: ["grant"],
    tools: ["web_search", "mcp_connectors"],
    modelTier: "fast",
    outputContract: "PublicGrantResearchV2",
    workflowSteps: [
      "public_opportunity",
      "opportunity",
      "requirements",
      "project_facts",
      "specific_aims",
      "sections",
      "compliance",
      "red_team",
      "approval",
    ],
    publicWebBoundary: "read_only",
    connectorSources: ["grants_gov", "nih_reporter", "crossref", "openalex"],
    capability: "grant",
    specialists: [],
  },
  {
    id: "matching",
    name: "matching-agent",
    ownerKind: "platform",
    purpose:
      "Matches verified experts, facilities, equipment, methods, and templates using deterministic eligibility filters before semantic scoring.",
    boundary:
      "Never creates a person, resource, capability, contact detail, or availability claim. Explains only stored score factors and reports record freshness and gaps. Has no tools.",
    knowledge: ["person", "facility", "equipment", "method", "template"],
    tools: [],
    modelTier: "fast",
    outputContract: "MatchingShortlistV2",
    workflowSteps: [
      "criteria",
      "hard_filters",
      "entity_resolution",
      "score",
      "shortlist",
    ],
    publicWebBoundary: "none",
    connectorSources: [],
    capability: "matching",
    specialists: [],
  },
  {
    id: "matching_online",
    name: "matching-online-agent",
    ownerKind: "platform",
    purpose:
      "Finds public researcher and organization metadata leads without claiming institutional availability.",
    boundary:
      "Public-metadata only: never receives or repeats internal directory, contact, or availability data. Every tool result is treated as untrusted data.",
    knowledge: ["person", "facility", "equipment", "method", "template"],
    tools: ["web_search", "mcp_connectors"],
    modelTier: "fast",
    outputContract: "PublicMatchingResearchV2",
    workflowSteps: [
      "public_discovery",
      "criteria",
      "hard_filters",
      "entity_resolution",
      "score",
      "shortlist",
    ],
    publicWebBoundary: "read_only",
    connectorSources: ["openalex", "orcid", "ror", "nih_reporter"],
    capability: "matching",
    specialists: [],
  },
  {
    id: "dataset",
    name: "dataset-agent",
    ownerKind: "platform",
    purpose:
      "Explains deterministic table, metric, and notebook-output profiles for a supplied dataset.",
    boundary:
      "Uses the Foundry Code Interpreter tool only on the bounded dataset content supplied in the request; no network access or package installs. Refuses regulated, confidential, or cross-tenant data. Large runs require deterministic product approval before invocation.",
    knowledge: ["dataset"],
    tools: ["code_interpreter"],
    modelTier: "fast",
    outputContract: "DatasetAnalysisV2",
    workflowSteps: [
      "select",
      "validate",
      "profile",
      "plan",
      "compute",
      "interpret",
      "approve",
    ],
    publicWebBoundary: "none",
    connectorSources: [],
    capability: "dataset",
    specialists: [],
  },
  {
    id: "institution",
    name: "institution-agent",
    ownerKind: "platform",
    purpose:
      "Answers institutional questions only from authorized, versioned policy, IRB, template, and catalog passages.",
    boundary:
      "Surfaces conflicting document versions and abstains when the supplied corpus does not support an answer. Never presents guidance as legal, compliance, or IRB approval. Has no tools.",
    knowledge: ["policy", "template", "facility", "equipment"],
    tools: [],
    modelTier: "fast",
    outputContract: "InstitutionalAnswerV2",
    workflowSteps: [
      "scope",
      "authorize",
      "resolve_versions",
      "detect_conflicts",
      "answer",
    ],
    publicWebBoundary: "none",
    connectorSources: [],
    capability: "institutional_qa",
    specialists: [],
  },
];

export function getAgentCatalogEntry(id: string): AgentCatalogEntry | null {
  return AGENT_CATALOG.find((entry) => entry.id === id) ?? null;
}
