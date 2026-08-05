import { getAgentSurfaces } from "@/lib/api";
import type { AgentSurfaceView, CapabilityId } from "@/lib/types";

/**
 * Chat-surface metadata, seeded with the capabilities that shipped with the
 * browser bundle and then overlaid with whatever `/api/agent-surfaces` reports.
 *
 * The seed exists so routing and copy resolve on first paint, before the fetch
 * lands. A capability the bundle has never heard of resolves once priming
 * completes, which is what makes adding an agent a server-side change only.
 */
const SEED: Record<string, AgentSurfaceView> = {
  literature: {
    capability: "literature",
    chat: true,
    icon: "BookOpen",
    eyebrow: "Evidence review",
    chat_title: "Literature Studio",
    chat_description:
      "Ask for a synthesis, a screening decision, or an extraction. Attach the papers you want it to work from.",
    suggestions: [
      "Compare the methods used across the papers I attached and flag where they disagree.",
      "Screen these abstracts against an inclusion criterion of randomised trials since 2020.",
      "Build an extraction matrix of population, method, outcome, and limitation.",
    ],
  },
  grant: {
    capability: "grant",
    chat: true,
    icon: "FileText",
    eyebrow: "Application lifecycle",
    chat_title: "Grant Studio",
    chat_description:
      "Attach the funding notice and your project facts, then ask for a requirement matrix, a draft, or a red-team review.",
    suggestions: [
      "Turn the attached notice into a requirement matrix with owners and evidence gaps.",
      "Draft the specific aims section from the attached project facts.",
      "Red-team this draft against the sponsor's review criteria.",
    ],
  },
  matching: {
    capability: "matching",
    chat: true,
    icon: "Users",
    eyebrow: "Discovery",
    chat_title: "Matching Explorer",
    chat_description:
      "Describe the eligibility bar and what you need. Attach a roster or facility list to search within it.",
    suggestions: [
      "Shortlist investigators with wet-lab capacity and prior NIH funding.",
      "Resolve duplicate entries in the attached roster before ranking.",
      "Explain which stored factors drove the top three matches.",
    ],
  },
  dataset: {
    capability: "dataset",
    chat: true,
    icon: "FlaskConical",
    eyebrow: "Data analysis",
    chat_title: "Dataset Lab",
    chat_description:
      "Attach a CSV or notebook output and ask what you want computed. Compute stays inside the approved sandbox.",
    suggestions: [
      "Profile the attached CSV: schema, missingness, and obvious quality problems.",
      "Propose an analysis plan for the outcome column and say what it cannot support.",
      "Compute descriptive statistics per group and show the code you ran.",
    ],
  },
};

const registry = new Map<string, AgentSurfaceView>(Object.entries(SEED));

/** Overlay server-declared surfaces. Safe to call more than once. */
export function primeAgentSurfaces(surfaces: AgentSurfaceView[]): void {
  for (const surface of surfaces) {
    registry.set(surface.capability, surface);
  }
}

let priming: Promise<void> | null = null;

/** Load the server registry once per page. Any failure leaves the seed in place. */
export function ensureAgentSurfaces(): Promise<void> {
  // The call is wrapped rather than chained: a synchronous throw would escape a
  // trailing .catch() and take the render down with it.
  priming ??= (async () => {
    try {
      primeAgentSurfaces(await getAgentSurfaces());
    } catch {
      // An older API without this route still renders from the seed.
    }
  })();
  return priming;
}

export function agentSurface(
  capability: CapabilityId,
): AgentSurfaceView | null {
  return registry.get(capability) ?? null;
}

export function isChatCapability(capability: CapabilityId): boolean {
  return agentSurface(capability)?.chat === true;
}

export function chatCapabilities(): CapabilityId[] {
  return [...registry.values()]
    .filter((surface) => surface.chat)
    .map((surface) => surface.capability);
}
