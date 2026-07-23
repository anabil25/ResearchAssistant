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
import {
  connectorStatusInfo,
} from "@/components/connections-view";
import { formatTime } from "@/components/workspace-views";
import {
  AsyncStateBanner,
  EmptyBlock,
  LoadingBlock,
  classifyAsyncError,
} from "@/components/async-state";
import { getAgentCatalogEntry } from "@/lib/agent-catalog";
import {
  getAgentEvaluation,
  getAgentHealth,
  getAgentVersions,
  proposeManifestChange,
  runStudio,
  type WorkspaceData,
} from "@/lib/api";
import type {
  AgentEvaluationSummary,
  AgentHealthSummary,
  AgentVersionRecord,
  CapabilityId,
  ManifestChangeProposal,
  StudioResult,
} from "@/lib/types";

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

export function BuildTab({ agentId }: { agentId: string }) {
  const [messages, setMessages] = useState<
    { role: "user" | "system"; text: string; tone?: "success" | "unavailable" }[]
  >([
    {
      role: "system",
      text: "Describe the change you want in plain language. I'll propose a typed manifest diff for you to review — no code required.",
    },
  ]);
  const [draft, setDraft] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const submit = () => {
    const intent = draft.trim();
    if (!intent) return;
    setMessages((current) => [...current, { role: "user", text: intent }]);
    setDraft("");
    setSubmitting(true);
    void proposeManifestChange(agentId, intent, [
      { path: "builder_intent", before: null, after: intent },
    ])
      .then((proposal: ManifestChangeProposal) => {
        setMessages((current) => [
          ...current,
          {
            role: "system",
            text: `Proposed manifest change ${proposal.id}: ${proposal.summary}. Nothing is applied until you approve it.`,
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
                ? "Manifest change proposals aren't available yet — the backend doesn't expose this endpoint. Your intent is preserved above so you can resubmit once it ships."
                : classified.message,
            tone: "unavailable",
          },
        ]);
      })
      .finally(() => setSubmitting(false));
  };

  return (
    <section className="panel agent-build-tab" aria-label="Builder Agent chat">
      <div className="settings-section-heading">
        <div>
          <h2>Builder Agent</h2>
          <p>
            Every change you make here is a typed, reviewable manifest
            proposal — you never need to read or write code.
          </p>
        </div>
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
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="e.g. Only cite passages published in the last 5 years."
        />
        <button
          type="submit"
          className="primary-button"
          disabled={submitting || draft.trim().length === 0}
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
  status,
}: {
  agentId: string;
  ownerKind: "platform" | "researcher";
  status: string | undefined;
}) {
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<
    | { kind: "success"; message: string }
    | { kind: "unavailable"; message: string }
    | null
  >(null);

  const requestDeploy = () => {
    setSubmitting(true);
    setResult(null);
    void proposeManifestChange(agentId, "Request deployment", [
      { path: "lifecycle", before: status ?? null, after: "active" },
    ])
      .then((proposal) =>
        setResult({
          kind: "success",
          message: `Deployment request recorded as proposal ${proposal.id}.`,
        }),
      )
      .catch((error: unknown) => {
        const classified = classifyAsyncError(error);
        setResult({
          kind: "unavailable",
          message:
            classified.kind === "unavailable"
              ? "Direct deployment controls aren't available yet. This request routes through the same manifest proposal pipeline as Build."
              : classified.message,
        });
      })
      .finally(() => setSubmitting(false));
  };

  return (
    <section className="panel agent-deploy-tab" aria-label="Deployment">
      <div className="settings-section-heading">
        <div>
          <h2>Deployment</h2>
          <p>
            {ownerKind === "platform"
              ? "Only platform owners publish new versions of a system agent. Researchers can inspect, fork, and propose changes from Build."
              : "Researcher-created agents deploy through the same reviewed manifest proposal pipeline as system agents."}
          </p>
        </div>
      </div>
      <dl className="agent-registry-facts">
        <div>
          <dt>Current status</dt>
          <dd>{status ?? "Not discovered yet"}</dd>
        </div>
      </dl>
      <button
        type="button"
        className="primary-button"
        disabled={submitting}
        onClick={requestDeploy}
      >
        {submitting ? "Requesting…" : "Request deployment"}
      </button>
      {result ? (
        result.kind === "success" ? (
          <div className="save-status success" role="status">
            {result.message}
          </div>
        ) : (
          <AsyncStateBanner kind="unavailable" message={result.message} />
        )
      ) : null}
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
  const [versions, setVersions] = useState<AgentVersionRecord[] | null>(null);
  const [errorState, setErrorState] = useState<ReturnType<
    typeof classifyAsyncError
  > | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getAgentVersions(agentId)
      .then((next) => {
        if (!cancelled) setVersions(next);
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
    <section className="panel agent-versions-tab" aria-label="Versions">
      <div className="settings-section-heading">
        <div>
          <h2>Versions</h2>
          <p>
            {ownerKind === "platform"
              ? "This agent's baseline is immutable. Platform owners publish new versions; researchers inspect and fork."
              : "Forked and researcher-built agents keep their own version history once deployment support ships."}
          </p>
        </div>
      </div>
      {loading ? (
        <LoadingBlock label="Loading version history…" />
      ) : errorState ? (
        <AsyncStateBanner kind={errorState.kind} message={errorState.message} />
      ) : versions && versions.length > 0 ? (
        <ul className="agent-version-list">
          {versions.map((version) => (
            <li key={version.version}>
              <strong>{version.version}</strong>
              <span>{version.changelog}</span>
              <small>
                {version.created_by} · {formatTime(version.created_at)}
              </small>
            </li>
          ))}
        </ul>
      ) : (
        <EmptyBlock
          title="Immutable baseline only"
          description="No version history has been recorded for this agent yet."
        />
      )}
    </section>
  );
}

export function AgentWorkspaceView({
  agentId,
  data,
  onRefresh,
  onBack,
}: AgentWorkspaceProps) {
  const entry = getAgentCatalogEntry(agentId);
  const [tab, setTab] = useState<AgentWorkspaceTabId>("build");
  const [advanced, setAdvanced] = useState(false);

  if (!entry) {
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

  const live = data?.agents.find((agent) => agent.id === entry.id);
  const connections = (data?.connectors ?? []).filter((connector) =>
    connector.assigned_agents.includes(entry.id),
  );

  return (
    <div className="operational-page agent-workspace-page">
      <header className="operational-header agent-workspace-header">
        <button type="button" className="agent-workspace-back" onClick={onBack}>
          <ChevronLeft size={16} /> Registry
        </button>
        <div>
          <span className="eyebrow">Agent Workspace</span>
          <h1>
            <Bot size={20} /> {entry.name}
          </h1>
          <p>{entry.purpose}</p>
        </div>
        <span className="agent-registry-owner" data-owner={entry.ownerKind}>
          {entry.ownerKind === "platform" ? "Platform-owned" : "Researcher-owned"}
        </span>
      </header>

      <div className="agent-workspace-layout">
        <aside className="panel agent-workspace-contract" aria-label="Behavioral contract">
          <h2>Behavioral contract</h2>
          <dl>
            <div>
              <dt>Purpose</dt>
              <dd>{entry.purpose}</dd>
            </div>
            <div>
              <dt>Input & artifact</dt>
              <dd>{entry.outputContract}</dd>
            </div>
            <div>
              <dt>Instructions</dt>
              <dd>{entry.boundary}</dd>
            </div>
            <div>
              <dt>Evidence & citations</dt>
              <dd>
                Every claim must resolve to an authorized source ID or be
                marked unresolved — model text alone never counts as proof.
              </dd>
            </div>
            <div>
              <dt>
                <Cpu size={13} /> Model
              </dt>
              <dd>{live?.deployment ?? entry.modelTier} (discovered from project deployments)</dd>
            </div>
            <div>
              <dt>Knowledge</dt>
              <dd>
                {entry.knowledge.length > 0 ? entry.knowledge.join(", ") : "None"}
              </dd>
            </div>
            <div>
              <dt>Tools</dt>
              <dd>{entry.tools.length > 0 ? entry.tools.join(", ") : "None"}</dd>
            </div>
            <div>
              <dt>Memory</dt>
              <dd>
                Persistent memory is off by default. When enabled, scope,
                retention, inspect, correct, forget, and export controls are
                available under Advanced.
              </dd>
            </div>
            <div>
              <dt>
                <Link2 size={13} /> Connections
              </dt>
              <dd>
                {connections.length > 0 ? (
                  <ul className="agent-connections-list">
                    {connections.map((connector) => {
                      const info = connectorStatusInfo(connector);
                      return (
                        <li key={connector.id}>
                          {connector.name}
                          <span className="status-chip" data-tone={info.tone}>
                            {info.label}
                          </span>
                        </li>
                      );
                    })}
                  </ul>
                ) : (
                  "No workspace connections assigned yet."
                )}
              </dd>
            </div>
            <div>
              <dt>
                <Users size={13} /> Specialists
              </dt>
              <dd>
                {entry.specialists.length > 0
                  ? entry.specialists
                      .map((id) => getAgentCatalogEntry(id)?.name ?? id)
                      .join(", ")
                  : "None"}
              </dd>
            </div>
            <div>
              <dt>Safety</dt>
              <dd>
                <Globe2 size={13} />{" "}
                {entry.publicWebBoundary === "read_only"
                  ? "Public web access is read-only; every result is treated as untrusted data."
                  : "No public web access."}
              </dd>
            </div>
            <div>
              <dt>Tests</dt>
              <dd>Run real studio requests from the Test tab.</dd>
            </div>
            <div>
              <dt>Deployment</dt>
              <dd>{live?.status ?? "Not discovered yet"}</dd>
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
                <dd>{entry.outputContract}</dd>
              </div>
              <div>
                <dt>Runtime</dt>
                <dd>
                  Runtime selection is automatic and hidden by default; this
                  deployment is currently pinned to the{" "}
                  {live?.model_tier ?? entry.modelTier} tier discovered from
                  the project&apos;s model deployments.
                </dd>
              </div>
              <div>
                <dt>
                  <Database size={13} /> Identity
                </dt>
                <dd>
                  Agent ID <code>{entry.id}</code>
                  {live ? (
                    <>
                      {" "}
                      · Deployment <code>{live.deployment}</code>
                    </>
                  ) : null}
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
            {tab === "build" ? <BuildTab agentId={entry.id} /> : null}
            {tab === "test" ? (
              <TestTab capability={entry.capability} data={data} onRefresh={onRefresh} />
            ) : null}
            {tab === "evaluate" ? <EvaluateTab agentId={entry.id} /> : null}
            {tab === "deploy" ? (
              <DeployTab
                agentId={entry.id}
                ownerKind={entry.ownerKind}
                status={live?.status}
              />
            ) : null}
            {tab === "monitor" ? (
              <MonitorTab
                agentId={entry.id}
                data={data}
                capability={entry.capability}
              />
            ) : null}
            {tab === "versions" ? (
              <VersionsTab agentId={entry.id} ownerKind={entry.ownerKind} />
            ) : null}
          </div>
        </div>
      </div>
      <p className="agent-workspace-empty-note">
        <Lightbulb size={13} /> Non-developers never need to review code here
        — every change flows through the typed manifest proposal above.
      </p>
    </div>
  );
}
