"use client";

import {
  Clock3,
  Lock,
  ShieldCheck,
  Sparkles,
} from "lucide-react";

import type { WorkspaceData } from "@/lib/api";
import { AgentChat, isChatCapability } from "@/components/agent-chat";
import type {
  CapabilityId,
  StudioResult,
  WorkflowBlueprint,
} from "@/lib/types";

export interface StudioRunOptions {
  onlineResearch?: boolean;
  inputs?: Record<string, unknown>;
}

interface StudioCompatibilityProps {
  result?: StudioResult | null;
  running?: boolean;
  error?: string | null;
  workflow?: WorkflowBlueprint;
  data?: WorkspaceData | null;
  onRefresh?: () => Promise<void>;
  onRun?: (
    capability: CapabilityId,
    objective: string,
    options?: StudioRunOptions,
  ) => Promise<void>;
}

export function InstitutionalStudio() {
  return (
  <div className="studio-page institutional-studio institutional-coming-soon">
    <section
      className="institutional-preview"
      aria-labelledby="institutional-preview-title"
    >
      <div className="institutional-preview-rail" aria-hidden="true">
        <span><Lock size={20} /></span>
        <i />
        <span><ShieldCheck size={20} /></span>
        <i />
        <span><Sparkles size={20} /></span>
      </div>
      <div className="institutional-preview-copy">
        <span className="institutional-preview-status">
          <Clock3 size={15} />
          Preview planned
        </span>
        <span className="eyebrow">Institutional Q&amp;A · Work IQ</span>
        <h1 id="institutional-preview-title">Coming soon in preview</h1>
        <p className="institutional-preview-lede">
          The governed Microsoft 365 connection is not enabled in this release.
          This workspace will open when preview access is ready.
        </p>
        <div className="institutional-preview-boundary">
          <ShieldCheck size={19} />
          <span>
            <strong>Permission boundary preserved</strong>
            <small>No institutional content is queried or synthesized here yet.</small>
          </span>
        </div>
      </div>
      <dl className="institutional-preview-facts">
        <div>
          <dt>Availability</dt>
          <dd>Not enabled</dd>
        </div>
        <div>
          <dt>Release channel</dt>
          <dd>Preview</dd>
        </div>
        <div>
          <dt>Data access</dt>
          <dd>Permission-aware</dd>
        </div>
      </dl>
    </section>
  </div>
  );
}

export function StudioForCapability({
  capability,
  projectId,
}: {
  capability: CapabilityId;
  projectId?: string | null;
} & StudioCompatibilityProps) {
  if (isChatCapability(capability)) {
    return <AgentChat capability={capability} projectId={projectId} />;
  }
  switch (capability) {
    case "institutional_qa":
      return <InstitutionalStudio />;
    default:
      return null;
  }
}
