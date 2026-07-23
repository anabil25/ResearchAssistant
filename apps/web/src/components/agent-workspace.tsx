"use client";

import {
  Bot,
  ChevronLeft,
  Cpu,
  Database,
  Globe2,
  Lightbulb,
  Link2,
  Send,
  Sliders,
  Users,
} from "lucide-react";
import { useEffect, useState } from "react";

import { StudioForCapability } from "@/components/studio-components";
import { formatTime } from "@/components/workspace-views";
import {
  AsyncStateBanner,
  EmptyBlock,
  LoadingBlock,
  classifyAsyncError,
} from "@/components/async-state";
import { buildLegacyAgentSummaries } from "@/lib/agent-catalog";
import {
  applyBuilderProposal,
  forgetAgentMemoryScope,
  getAgentDeployment,
  getAgentDraft,
  getAgentEvaluation,
  getAgentHealth,
  getAgentRelease,
  getAgentReleases,
  getAgentTraces,
  postBuilderMessage,
  runStudio,
  type WorkspaceData,
} from "@/lib/api";
import {
  isCapabilityApprovalActive,
  isCapabilityAttachable,
  defaultMemoryView,
  type AgentBuilderProposal,
  type AgentContractView,
  type AgentDraftView,
  type AgentEvaluationSummary,
  type AgentHealthSummary,
  type AgentReleaseSummary,
  type AgentTraceSummary,
  type CapabilityId,
  type MemoryScope,
  type MemoryView,
  type StudioResult,
} from "@/lib/types";

function defaultContractView(): AgentContractView {
  return {
    purpose: null,
    boundary: null,
    input_artifact: null,
    instructions: null,
    evidence_policy: null,
    model: { deployment: null, discovered: false },
    knowledge: null,
    tools: null,
    memory: defaultMemoryView(),
    connections: null,
    specialists: null,
    capabilities: null,
    safety: null,
    tests: null,
    deployment: null,
    public_boundary: null,
  };
}

type AgentWorkspaceTabId =
  | "build"
  | "test"
  | "evaluate"
  | "deploy"
  | "monitor"
  | "versions";

const TABS: { id: AgentWorkspaceTabId; label: string }[] = [
  { id: "build", label: "Build" },
  { id: "test", label: "Test" },
  { id: "evaluate", label: "Evaluate" },
  { id: "deploy", label: "Deploy" },
  { id: "monitor", label: "Monitor" },
  { id: "versions", label: "Versions" },
];

interface AgentWorkspaceProps {
  agentId: string;
  data: WorkspaceData | null;
  onRefresh: () => Promise<void>;
  onBack: () => void;
}

/**
 * Loads the behavioral contract for one agent: the latest immutable release
 * if one exists, otherwise the current draft. Both calls hit
 * `/agent-studio/...` and will genuinely 404 until the backend ships
 * them — callers see an explicit `AsyncStateBanner`, never a fabricated
 * contract.
 */
function useAgentContract(agentId: string) {
  const [status, setStatus] = useState<"loading" | "ready" | "error">(
    "loading",
  );
  const [contract, setContract] = useState<AgentContractView>(
    defaultContractView(),
  );
  const [releaseVersion, setReleaseVersion] = useState<string | null>(null);
  const [error, setError] = useState<ReturnType<
    typeof classifyAsyncError
  > | null>(null);
  // Reset to "loading" during render (React's blessed pattern for adjusting
  // state when a prop changes) rather than synchronously inside the effect
  // body below, which react-hooks/set-state-in-effect flags as a footgun.
  const [trackedAgentId, setTrackedAgentId] = useState(agentId);
  if (trackedAgentId !== agentId) {
    setTrackedAgentId(agentId);
    setStatus("loading");
    setError(null);
  }

  useEffect(() => {
    let cancelled = false;
    void getAgentReleases(agentId)
      .then((releases) => {
        const latest = releases[releases.length - 1];
        if (!latest) throw new Error("no releases yet");
        return getAgentRelease(agentId, latest.version).then((result) => {
          if (cancelled) return;
          setContract(result.contract);
          setReleaseVersion(latest.version);
          setStatus("ready");
        });
      })
      .catch(() =>
        getAgentDraft(agentId).then((draft) => {
          if (cancelled) return;
          setContract(draft.contract);
          setReleaseVersion(null);
          setStatus("ready");
        }),
      )
      .catch((finalError: unknown) => {
        if (cancelled) return;
        setError(classifyAsyncError(finalError));
        setStatus("error");
      });
    return () => {
      cancelled = true;
    };
  }, [agentId]);

  return { status, contract, releaseVersion, error };
}


export function BuildTab({ agentId }: { agentId: string }) {
  const [messages, setMessages] = useState<
    { role: "user" | "system"; text: string; tone?: "success" | "unavailable" }[]
  >([
    {
      role: "system",
      text: "Describe the change you want in plain language. I'll propose a typed manifest diff for you to review — no code required.",
    },
  ]);
  const [intentDraft, setIntentDraft] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [applying, setApplying] = useState(false);
  const [agentDraft, setAgentDraft] = useState<AgentDraftView | null>(null);
  const [draftError, setDraftError] = useState<ReturnType<
    typeof classifyAsyncError
  > | null>(null);
  const [proposal, setProposal] = useState<AgentBuilderProposal | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getAgentDraft(agentId)
      .then((next) => {
        if (!cancelled) setAgentDraft(next);
      })
      .catch((error: unknown) => {
        if (!cancelled) setDraftError(classifyAsyncError(error));
      });
    return () => {
      cancelled = true;
    };
  }, [agentId]);

  const submit = () => {
    const intent = intentDraft.trim();
    if (!intent) return;
    setMessages((current) => [...current, { role: "user", text: intent }]);
    setIntentDraft("");
    setSubmitting(true);
    const draftId = agentDraft?.draft_id ?? agentId;
    const baseEtag = agentDraft?.etag ?? "";
    void postBuilderMessage(draftId, intent, baseEtag)
      .then((next: AgentBuilderProposal) => {
        setProposal(next);
        setMessages((current) => [
          ...current,
          {
            role: "system",
            text: `Proposed change ${next.proposal_id}: ${next.summary}. Review the before/after summary below, then Approve to apply — nothing changes until you do.`,
            tone: "success",
          },
        ]);
      })
      .catch((error: unknown) => {
        const classified = classifyAsyncError(error);
        setMessages((current) => [
          ...current,
          {
            role: "system",
            text:
              classified.kind === "unavailable"
                ? "Builder proposals aren't available yet — the backend doesn't expose this endpoint. Your intent is preserved above so you can resubmit once it ships."
                : classified.message,
            tone: "unavailable",
          },
        ]);
      })
      .finally(() => setSubmitting(false));
  };

  const approve = (activeProposal: AgentBuilderProposal) => {
    setApplying(true);
    void applyBuilderProposal(
      activeProposal.draft_id,
      activeProposal.proposal_id,
      activeProposal.base_etag,
    )
      .then((nextDraft) => {
        setAgentDraft(nextDraft);
        setProposal(null);
        setMessages((current) => [
          ...current,
          {
            role: "system",
            text: "Change applied to the draft.",
            tone: "success",
          },
        ]);
      })
      .catch((error: unknown) => {
        const classified = classifyAsyncError(error);
        setMessages((current) => [
          ...current,
          {
            role: "system",
            text:
              classified.kind === "unavailable"
                ? "Applying this proposal isn't available yet — the backend doesn't expose this endpoint."
                : classified.message,
            tone: "unavailable",
          },
        ]);
      })
      .finally(() => setApplying(false));
  };

  return (
    <section className="panel agent-build-tab" aria-label="Builder Agent chat">
      <div className="settings-section-heading">
        <div>
          <h2>Builder Agent</h2>
          <p>
            Every change you make here is a typed, reviewable proposal — you
            never need to read or write code. Nothing is applied until you
            approve it.
          </p>
        </div>
        <span className="subtle-chip" data-tone={draftError ? "unavailable" : undefined}>
          Draft status: {agentDraft?.status ?? "not loaded yet"}
        </span>
      </div>
      <div className="agent-build-chat" role="log" aria-live="polite">
        {messages.map((message, index) => (
          <div
            key={index}
            className="agent-build-message"
            data-role={message.role}
            data-tone={message.tone}
          >
            {message.text}
          </div>
        ))}
      </div>
      {proposal ? (
        <div className="agent-build-proposal" role="group" aria-label="Proposed change">
          <dl className="agent-registry-facts">
            <div>
              <dt>Before</dt>
              <dd>{proposal.before_summary}</dd>
            </div>
            <div>
              <dt>After</dt>
              <dd>{proposal.after_summary}</dd>
            </div>
            {proposal.capability_changes.length > 0 ? (
              <div>
                <dt>Capability changes</dt>
                <dd>{proposal.capability_changes.join(", ")}</dd>
              </div>
            ) : null}
            {proposal.permission_changes.length > 0 ? (
              <div>
                <dt>Permission changes</dt>
                <dd>{proposal.permission_changes.join(", ")}</dd>
              </div>
            ) : null}
            {proposal.data_boundary_changes.length > 0 ? (
              <div>
                <dt>Data-boundary changes</dt>
                <dd>{proposal.data_boundary_changes.join(", ")}</dd>
              </div>
            ) : null}
            {proposal.validation_warnings.length > 0 ? (
              <div>
                <dt>Validation warnings</dt>
                <dd>{proposal.validation_warnings.join(", ")}</dd>
              </div>
            ) : null}
          </dl>
          <button
            type="button"
            className="primary-button"
            disabled={applying}
            onClick={() => approve(proposal)}
          >
            {applying ? "Applying…" : "Approve & apply"}
          </button>
        </div>
      ) : null}
      <form
        className="agent-build-composer"
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <label className="sr-only" htmlFor="agent-build-input">
          Describe the change you want
        </label>
        <textarea
          id="agent-build-input"
          rows={2}
          value={intentDraft}
          onChange={(event) => setIntentDraft(event.target.value)}
          placeholder="e.g. Only cite passages published in the last 5 years."
        />
        <button
          type="submit"
          className="primary-button"
          disabled={submitting || intentDraft.trim().length === 0}
        >
          <Send size={14} /> {submitting ? "Proposing…" : "Propose change"}
        </button>
      </form>
    </section>
  );
}

export function TestTab({
  capability,
  data,
  onRefresh,
}: {
  capability: CapabilityId | null;
  data: WorkspaceData | null;
  onRefresh: () => Promise<void>;
}) {
  const [result, setResult] = useState<StudioResult | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!capability) {
    return (
      <EmptyBlock
        title="No attached studio yet"
        description="This agent isn't wired to a runnable studio capability yet, so there is nothing to test directly. Use Build to propose behavior changes instead."
      />
    );
  }

  const cap: CapabilityId = capability;
  const workflow = data?.workflows.find((item) => item.capability === cap);

  const onRun = async (
    runCapability: CapabilityId,
    objective: string,
    options?: { onlineResearch?: boolean; inputs?: Record<string, unknown> },
  ) => {
    setRunning(true);
    setError(null);
    try {
      const next = await runStudio(runCapability, objective, options);
      setResult(next);
      void onRefresh();
    } catch (runError) {
      setError(
        runError instanceof Error ? runError.message : "The test run failed.",
      );
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="agent-test-tab">
      <StudioForCapability
        capability={cap}
        result={result}
        running={running}
        error={error}
        workflow={workflow}
        data={data}
        onRefresh={onRefresh}
        onRun={onRun}
      />
    </div>
  );
}

export function EvaluateTab({ agentId }: { agentId: string }) {
  const [loading, setLoading] = useState(true);
  const [evaluation, setEvaluation] = useState<AgentEvaluationSummary | null>(
    null,
  );
  const [errorState, setErrorState] = useState<ReturnType<
    typeof classifyAsyncError
  > | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getAgentEvaluation(agentId)
      .then((next) => {
        if (!cancelled) setEvaluation(next);
      })
      .catch((error: unknown) => {
        if (!cancelled) setErrorState(classifyAsyncError(error));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [agentId]);

  return (
    <section className="panel agent-evaluate-tab" aria-label="Advisory evaluation">
      <div className="settings-section-heading">
        <div>
          <h2>Advisory evaluation</h2>
          <p>
            Evaluation scores are advisory signal only — they never block or
            approve a deployment by themselves. The hard gates below are the
            only pass/fail requirement.
          </p>
        </div>
      </div>
      {loading ? (
        <LoadingBlock label="Loading evaluation results…" />
      ) : errorState ? (
        <AsyncStateBanner kind={errorState.kind} message={errorState.message} />
      ) : evaluation ? (
        <>
          <dl className="agent-registry-facts">
            <div>
              <dt>Citation resolution</dt>
              <dd>{evaluation.citation_resolution ?? "—"}%</dd>
            </div>
            <div>
              <dt>Claim entailment</dt>
              <dd>{evaluation.claim_entailment ?? "—"}%</dd>
            </div>
            <div>
              <dt>Retrieval completeness</dt>
              <dd>{evaluation.retrieval_completeness ?? "—"}%</dd>
            </div>
            <div>
              <dt>Last run</dt>
              <dd>{formatTime(evaluation.last_run_at)}</dd>
            </div>
          </dl>
          <h3>Objective hard gates</h3>
          <ul className="agent-hard-gates">
            {evaluation.hard_gates.map((gate) => (
              <li key={gate.id} data-passing={gate.passing}>
                {gate.label}
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </section>
  );
}

export function DeployTab({
  agentId,
  ownerKind,
}: {
  agentId: string;
  ownerKind: "platform" | "researcher";
}) {
  const [loading, setLoading] = useState(true);
  const [deployment, setDeployment] = useState<{
    status: string;
    version: string | null;
  } | null>(null);
  const [errorState, setErrorState] = useState<ReturnType<
    typeof classifyAsyncError
  > | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getAgentDeployment(agentId)
      .then((next) => {
        if (!cancelled) setDeployment(next);
      })
      .catch((error: unknown) => {
        if (!cancelled) setErrorState(classifyAsyncError(error));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [agentId]);

  return (
    <section className="panel agent-deploy-tab" aria-label="Deployment">
      <div className="settings-section-heading">
        <div>
          <h2>Deployment</h2>
          <p>
            {ownerKind === "platform"
              ? "Only platform owners publish new versions of a system agent. Researchers can inspect, fork, and propose changes from Build."
              : "Researcher-created agents deploy through the same reviewed builder proposal pipeline as system agents."}{" "}
            A release only ships once its draft passes the objective hard
            gates in Evaluate — request changes from Build, not here.
          </p>
        </div>
      </div>
      {loading ? (
        <LoadingBlock label="Loading deployment status…" />
      ) : errorState ? (
        <AsyncStateBanner kind={errorState.kind} message={errorState.message} />
      ) : (
        <dl className="agent-registry-facts">
          <div>
            <dt>Current deployment status</dt>
            <dd>{deployment!.status}</dd>
          </div>
          <div>
            <dt>Deployed version</dt>
            <dd>{deployment!.version ?? "Not deployed yet"}</dd>
          </div>
        </dl>
      )}
    </section>
  );
}

export function MonitorTab({
  agentId,
  data,
  capability,
}: {
  agentId: string;
  data: WorkspaceData | null;
  capability: CapabilityId | null;
}) {
  const [loading, setLoading] = useState(true);
  const [health, setHealth] = useState<AgentHealthSummary | null>(null);
  const [errorState, setErrorState] = useState<ReturnType<
    typeof classifyAsyncError
  > | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getAgentHealth(agentId)
      .then((next) => {
        if (!cancelled) setHealth(next);
      })
      .catch((error: unknown) => {
        if (!cancelled) setErrorState(classifyAsyncError(error));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [agentId]);

  const studioRuns = capability
    ? (data?.runs ?? []).filter((run) => run.capability === capability)
    : [];
  const workflowUses = capability
    ? (data?.workflows ?? []).filter((wf) => wf.capability === capability).length
    : 0;
  const lastUsed = studioRuns
    .map((run) => run.completed_at ?? run.started_at)
    .filter((value): value is string => Boolean(value))
    .sort()
    .pop();

  const [traces, setTraces] = useState<AgentTraceSummary[] | null>(null);
  useEffect(() => {
    let cancelled = false;
    void getAgentTraces(agentId)
      .then((next) => {
        if (!cancelled) setTraces(next);
      })
      .catch(() => {
        if (!cancelled) setTraces(null);
      });
    return () => {
      cancelled = true;
    };
  }, [agentId]);

  return (
    <section className="panel agent-monitor-tab" aria-label="Health and usage">
      <div className="settings-section-heading">
        <div>
          <h2>Health & usage</h2>
          <p>Live health checks and real studio/workflow usage counts.</p>
        </div>
      </div>
      {loading ? (
        <LoadingBlock label="Checking live health…" />
      ) : errorState ? (
        <AsyncStateBanner kind={errorState.kind} message={errorState.message} />
      ) : health ? (
        <div className="agent-registry-live-grid">
          <div>
            <strong>State</strong>
            <span>{health.state}</span>
          </div>
          <div>
            <strong>Last checked</strong>
            <span>{formatTime(health.last_checked_at)}</span>
          </div>
          <div>
            <strong>Detail</strong>
            <span>{health.detail}</span>
          </div>
        </div>
      ) : null}
      <dl className="agent-registry-facts">
        <div>
          <dt>Studio usage</dt>
          <dd>
            {studioRuns.length} run{studioRuns.length === 1 ? "" : "s"}
          </dd>
        </div>
        <div>
          <dt>Workflow usage</dt>
          <dd>
            {workflowUses} workflow{workflowUses === 1 ? "" : "s"}
          </dd>
        </div>
        <div>
          <dt>Last used</dt>
          <dd>{formatTime(lastUsed ?? null)}</dd>
        </div>
      </dl>
      <h3>Recent traces</h3>
      {traces && traces.length > 0 ? (
        <ul className="agent-trace-list">
          {traces.map((trace) => (
            <li key={trace.id} data-status={trace.status}>
              <span>{formatTime(trace.started_at)}</span>
              <span>{trace.status}</span>
              <span>{trace.summary}</span>
            </li>
          ))}
        </ul>
      ) : (
        <EmptyBlock
          title="No traces available yet"
          description="Execution traces will appear here once the Agent Studio traces endpoint ships."
        />
      )}
    </section>
  );
}

export function VersionsTab({
  agentId,
  ownerKind,
}: {
  agentId: string;
  ownerKind: "platform" | "researcher";
}) {
  const [loading, setLoading] = useState(true);
  const [releases, setReleases] = useState<AgentReleaseSummary[] | null>(null);
  const [errorState, setErrorState] = useState<ReturnType<
    typeof classifyAsyncError
  > | null>(null);
  const [draftStatus, setDraftStatus] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getAgentReleases(agentId)
      .then((next) => {
        if (!cancelled) setReleases(next);
      })
      .catch((error: unknown) => {
        if (!cancelled) setErrorState(classifyAsyncError(error));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    void getAgentDraft(agentId)
      .then((next) => {
        if (!cancelled) setDraftStatus(next.status);
      })
      .catch(() => {
        if (!cancelled) setDraftStatus(null);
      });
    return () => {
      cancelled = true;
    };
  }, [agentId]);

  return (
    <section className="panel agent-versions-tab" aria-label="Versions">
      <div className="settings-section-heading">
        <div>
          <h2>Versions</h2>
          <p>
            {ownerKind === "platform"
              ? "Every release below is immutable. Platform owners publish new versions; researchers inspect and fork."
              : "Forked and researcher-built agents keep their own immutable release history once deployment support ships."}
          </p>
        </div>
      </div>
      <p className="agent-draft-status-note">
        Current draft status (mutable, separate from the immutable releases
        below): <strong>{draftStatus ?? "not available yet"}</strong>
      </p>
      {loading ? (
        <LoadingBlock label="Loading version history…" />
      ) : errorState ? (
        <AsyncStateBanner kind={errorState.kind} message={errorState.message} />
      ) : releases && releases.length > 0 ? (
        <ul className="agent-version-list">
          {releases.map((release) => (
            <li key={release.version}>
              <strong>{release.version}</strong>
              <span className="status-chip" data-tone={release.deployment_status}>
                {release.deployment_status.replace("_", " ")}
              </span>
              <span>{release.changelog}</span>
              <small>
                {release.created_by} · {formatTime(release.created_at)}
              </small>
              <small className="agent-version-lineage">
                {release.derived_from
                  ? `Forked from ${release.derived_from}`
                  : "Original release"}{" "}
                · model {release.model_version} · hash{" "}
                {release.content_hash.slice(0, 12)}
              </small>
            </li>
          ))}
        </ul>
      ) : (
        <EmptyBlock
          title="Immutable baseline only"
          description="No release history has been recorded for this agent yet."
        />
      )}
    </section>
  );
}

function MemoryScopePanel({
  agentId,
  memory,
}: {
  agentId: string;
  memory: MemoryView;
}) {
  const [forgetting, setForgetting] = useState<MemoryScope | null>(null);
  const [forgetResult, setForgetResult] = useState<string | null>(null);

  const forget = (scope: MemoryScope) => {
    setForgetting(scope);
    setForgetResult(null);
    void forgetAgentMemoryScope(agentId, scope)
      .then(() => setForgetResult(`Forget requested for ${scope} memory.`))
      .catch((error: unknown) => {
        setForgetResult(classifyAsyncError(error).message);
      })
      .finally(() => setForgetting(null));
  };

  return (
    <div className="agent-memory-panel">
      <ul className="agent-memory-scopes">
        {memory.scopes.map((scope) => (
          <li key={scope.scope} data-enabled={scope.enabled}>
            <strong>{scope.scope}</strong>
            <span className="status-chip" data-tone={scope.enabled ? "ready" : "disabled"}>
              {scope.enabled ? "Enabled" : "Disabled (default off)"}
            </span>
            <small>
              {scope.retention_days
                ? `${scope.retention_days}-day retention`
                : "No retention set"}{" "}
              · {scope.provider ?? "no provider"} · {scope.access}
            </small>
            <button
              type="button"
              className="ghost-button"
              disabled={forgetting === scope.scope}
              onClick={() => forget(scope.scope)}
            >
              {forgetting === scope.scope ? "Forgetting…" : "Forget"}
            </button>
          </li>
        ))}
      </ul>
      <p className="agent-memory-hint">
        Persistent memory defaults to off in every scope. Inspect, correct,
        and export controls will appear here once the memory endpoints ship;
        Forget above already calls the real (pending) endpoint.
      </p>
      {forgetResult ? (
        <div className="save-status" role="status">
          {forgetResult}
        </div>
      ) : null}
    </div>
  );
}

export function AgentWorkspaceView({
  agentId,
  data,
  onRefresh,
  onBack,
}: AgentWorkspaceProps) {
  const summary = data
    ? (buildLegacyAgentSummaries(data.agents).find((item) => item.id === agentId) ?? null)
    : null;
  const {
    status: contractStatus,
    contract,
    releaseVersion,
    error: contractError,
  } = useAgentContract(agentId);
  const [tab, setTab] = useState<AgentWorkspaceTabId>("build");
  const [advanced, setAdvanced] = useState(false);

  if (data && !summary) {
    return (
      <div className="operational-page">
        <EmptyBlock
          title="Agent not found"
          description="This agent isn't in the registry. It may have been removed or the link is out of date."
          action={
            <button type="button" className="primary-button" onClick={onBack}>
              <ChevronLeft size={14} /> Back to registry
            </button>
          }
        />
      </div>
    );
  }

  if (!summary) {
    return (
      <div className="operational-page">
        <LoadingBlock label="Loading agent workspace…" />
      </div>
    );
  }

  const live = data!.agents.find((agent) => agent.id === summary.id)!;
  const boundary = contract.public_boundary ?? summary.public_boundary;

  return (
    <div className="operational-page agent-workspace-page">
      <header className="operational-header agent-workspace-header">
        <button type="button" className="agent-workspace-back" onClick={onBack}>
          <ChevronLeft size={16} /> Registry
        </button>
        <div>
          <span className="eyebrow">Agent Workspace</span>
          <h1>
            <Bot size={20} /> {summary.name}
          </h1>
          <p>{contract.purpose ?? summary.purpose ?? "Purpose not available yet."}</p>
        </div>
        <span className="agent-registry-owner" data-owner={summary.owner_kind}>
          {summary.owner_kind === "platform" ? "Platform-owned" : "Researcher-owned"}
        </span>
      </header>

      {releaseVersion ? (
        <p className="agent-workspace-release-note">
          Showing the immutable release <strong>{releaseVersion}</strong>. Draft
          status lives on the Versions tab, separate from this release.
        </p>
      ) : contractStatus === "ready" ? (
        <p className="agent-workspace-release-note">
          No published release yet — showing the current mutable draft.
        </p>
      ) : null}

      <div className="agent-workspace-layout">
        <aside className="panel agent-workspace-contract" aria-label="Behavioral contract">
          <h2>Behavioral contract</h2>
          {contractStatus === "loading" ? (
            <LoadingBlock label="Loading behavioral contract…" />
          ) : contractStatus === "error" && contractError ? (
            <AsyncStateBanner kind={contractError.kind} message={contractError.message} />
          ) : null}
          <dl>
            <div>
              <dt>Purpose</dt>
              <dd>{contract.purpose ?? summary.purpose ?? "Not available yet."}</dd>
            </div>
            <div>
              <dt>Input & artifact</dt>
              <dd>{contract.input_artifact ?? "Not available yet."}</dd>
            </div>
            <div>
              <dt>Instructions</dt>
              <dd>
                {contract.instructions ??
                  contract.boundary ??
                  summary.boundary ??
                  "Not available yet."}
              </dd>
            </div>
            <div>
              <dt>Evidence & citations</dt>
              <dd>
                {contract.evidence_policy ??
                  "Every claim must resolve to an authorized source ID or be marked unresolved — model text alone never counts as proof."}
              </dd>
            </div>
            <div>
              <dt>
                <Cpu size={13} /> Model
              </dt>
              <dd>
                {live.deployment ||
                  contract.model.deployment ||
                  summary.discovered_project_model ||
                  "Not discovered yet"}{" "}
                (discovered from project deployments)
              </dd>
            </div>
            <div>
              <dt>Knowledge</dt>
              <dd>
                {contract.knowledge && contract.knowledge.length > 0
                  ? contract.knowledge.join(", ")
                  : "Not available yet."}
              </dd>
            </div>
            <div>
              <dt>Tools</dt>
              <dd>
                {contract.tools && contract.tools.length > 0
                  ? contract.tools.join(", ")
                  : "Not available yet."}
              </dd>
            </div>
            <div>
              <dt>Memory</dt>
              <dd>
                <MemoryScopePanel agentId={summary.id} memory={contract.memory} />
              </dd>
            </div>
            <div>
              <dt>
                <Link2 size={13} /> Connections
              </dt>
              <dd>
                {contract.connections && contract.connections.length > 0 ? (
                  <ul className="agent-connections-list">
                    {contract.connections.map((connection) => (
                      <li key={connection.id}>
                        {connection.name}
                        <span className="status-chip" data-tone={connection.readiness}>
                          {connection.readiness.replace(/_/g, " ")}
                        </span>
                        <small>
                          {connection.permissions.length > 0
                            ? connection.permissions.join("/")
                            : "no permissions"}{" "}
                          · {connection.scope}
                          {connection.version ? ` · v${connection.version}` : ""}
                        </small>
                      </li>
                    ))}
                  </ul>
                ) : (
                  "Not available yet."
                )}
              </dd>
            </div>
            <div>
              <dt>
                <Users size={13} /> Specialists
              </dt>
              <dd>
                {contract.specialists && contract.specialists.length > 0 ? (
                  <ul className="agent-specialists-list">
                    {contract.specialists.map((specialist) => (
                      <li key={specialist.id} data-attached={specialist.attached}>
                        {specialist.name ?? specialist.id}
                        {specialist.owner_kind ? ` (${specialist.owner_kind})` : ""}
                        {specialist.attached ? " — attached" : " — not attached"}
                      </li>
                    ))}
                  </ul>
                ) : (
                  "None attached yet."
                )}
              </dd>
            </div>
            <div>
              <dt>Capabilities</dt>
              <dd>
                {contract.capabilities && contract.capabilities.length > 0 ? (
                  <ul className="agent-capabilities-list">
                    {contract.capabilities.map((capability) => {
                      const descriptor = capability.resolved_descriptor;
                      const instance = capability.resolved_instance;
                      const approval = capability.binding.approval;
                      const lifecycle = instance?.lifecycle ?? null;
                      return (
                        <li
                          key={`${capability.binding.descriptor.id}-${capability.binding.instance.id}`}
                          data-attachable={isCapabilityAttachable(instance)}
                          data-stale={Boolean(capability.stale_reason)}
                        >
                          <strong>
                            {descriptor?.family ?? capability.binding.descriptor.id}
                          </strong>{" "}
                          · {descriptor?.operation ?? capability.binding.operation}
                          <span
                            className="status-chip"
                            data-tone={descriptor?.risk_class ?? "unknown"}
                          >
                            {(descriptor?.risk_class ?? "unknown").replace(/_/g, " ")}
                          </span>
                          <span
                            className="status-chip"
                            data-tone={instance?.maturity ?? "unknown"}
                          >
                            {instance ? instance.maturity : "unknown"}
                          </span>
                          <span
                            className="status-chip"
                            data-tone={lifecycle ?? "unknown"}
                          >
                            {lifecycle ?? "unknown"}
                          </span>
                          <small>
                            {capability.binding.enabled ? "Enabled" : "Disabled"} ·{" "}
                            {approval.status.replace(/_/g, " ")}
                            {approval.status !== "not_required" &&
                            !isCapabilityApprovalActive(approval)
                              ? " (not currently active)"
                              : ""}
                            {capability.binding.instance.version
                              ? ` · pinned to ${capability.binding.instance.version}`
                              : ""}
                            {capability.binding.provider_contract_version
                              ? ` · provider contract v${capability.binding.provider_contract_version}`
                              : ""}
                          </small>
                          {capability.binding.destination_constraints &&
                          capability.binding.destination_constraints.length > 0 ? (
                            <small className="agent-capability-destinations">
                              Destinations: {capability.binding.destination_constraints.join(", ")}
                            </small>
                          ) : null}
                          {lifecycle && lifecycle !== "active" ? (
                            <small
                              className="agent-capability-lifecycle-warning"
                              data-tone={lifecycle}
                            >
                              {lifecycle === "retired" ? "Retired" : "Deprecated"}
                              {instance?.lifecycle_reason
                                ? `: ${instance.lifecycle_reason}`
                                : " — no reason provided."}
                            </small>
                          ) : null}
                          {capability.stale_reason ? (
                            <small className="agent-capability-stale" data-tone="stale">
                              Stale: {capability.stale_reason}
                            </small>
                          ) : null}
                        </li>
                      );
                    })}
                  </ul>
                ) : (
                  "Not available yet."
                )}
              </dd>
            </div>
            <div>
              <dt>Safety & public web boundary</dt>
              <dd>
                <Globe2 size={13} />{" "}
                {boundary.outbound_data_boundary ?? "Not available yet."}
                {boundary.mode === "public_online"
                  ? " Every result is treated as untrusted data; writes require approval."
                  : ""}
                {boundary.approval_required ? " Approval required for writes." : ""}
              </dd>
            </div>
            <div>
              <dt>Tests</dt>
              <dd>{contract.tests ?? "Run real studio requests from the Test tab."}</dd>
            </div>
            <div>
              <dt>Deployment</dt>
              <dd>{contract.deployment || live.status || "Not discovered yet"}</dd>
            </div>
          </dl>

          <button
            type="button"
            className="agent-workspace-advanced-toggle"
            aria-expanded={advanced}
            onClick={() => setAdvanced((current) => !current)}
          >
            <Sliders size={14} /> Advanced
          </button>
          {advanced ? (
            <dl className="agent-workspace-advanced">
              <div>
                <dt>Output schema</dt>
                <dd>{contract.input_artifact ?? "Not available yet."}</dd>
              </div>
              <div>
                <dt>Runtime</dt>
                <dd>
                  Runtime selection is automatic and hidden by default; this
                  deployment is currently pinned to the{" "}
                  {live.model_tier || "undiscovered"} tier discovered from
                  the project&apos;s model deployments.
                </dd>
              </div>
              <div>
                <dt>
                  <Database size={13} /> Identity
                </dt>
                <dd>
                  Agent ID <code>{summary.id}</code> · Deployment{" "}
                  <code>{live.deployment}</code>
                </dd>
              </div>
              <div>
                <dt>Attach a specialist</dt>
                <dd>
                  To attach a registry agent or private specialist, describe
                  it in Build (e.g. &quot;Add the Grant agent as a
                  specialist&quot;) — every attachment flows through the same
                  reviewable builder proposal pipeline as any other manifest
                  change.
                </dd>
              </div>
            </dl>
          ) : null}
        </aside>

        <div className="agent-workspace-main">
          <div className="segmented-control agent-workspace-tabs" role="tablist">
            {TABS.map((item) => (
              <button
                key={item.id}
                type="button"
                role="tab"
                aria-selected={tab === item.id}
                data-active={tab === item.id}
                onClick={() => setTab(item.id)}
              >
                {item.label}
              </button>
            ))}
          </div>

          <div role="tabpanel">
            {tab === "build" ? <BuildTab agentId={summary.id} /> : null}
            {tab === "test" ? (
              <TestTab capability={summary.capability} data={data} onRefresh={onRefresh} />
            ) : null}
            {tab === "evaluate" ? <EvaluateTab agentId={summary.id} /> : null}
            {tab === "deploy" ? (
              <DeployTab agentId={summary.id} ownerKind={summary.owner_kind} />
            ) : null}
            {tab === "monitor" ? (
              <MonitorTab
                agentId={summary.id}
                data={data}
                capability={summary.capability}
              />
            ) : null}
            {tab === "versions" ? (
              <VersionsTab agentId={summary.id} ownerKind={summary.owner_kind} />
            ) : null}
          </div>
        </div>
      </div>
      <p className="agent-workspace-empty-note">
        <Lightbulb size={13} /> Non-developers never need to review code here
        — every change flows through the typed builder proposal above.
      </p>
    </div>
  );
}
